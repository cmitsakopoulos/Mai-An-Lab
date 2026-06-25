"""
Track graph: sparse k-NN adjacency over the music library + mood vocabulary.

Two tiers of edges:

  • acoustic  — Euclidean k-NN over the z-scored, PCA-reduced DSP feature
                vectors persisted by dsp.analyze_track(), reweighted by a
                Zelnik-Manor self-tuning Gaussian kernel. Top-K-per-source
                candidates are pruned by strict mutual-kNN intersection (keep
                edge i→j iff j ∈ topK(i) AND i ∈ topK(j)) so cluster-centroid
                "hub" tracks don't dominate every walk. The same affinity
                graph is the substrate for Louvain community detection
                (cluster_id), so walk and clustering share one geometry.
  • metadata  — same-artist and same-album co-occurrence (edge_kind 'artist'
                / 'album'). Weight is fixed at 1.0; ordering inside a tier
                falls back to library order.

The graph is the navigation backbone for the assistant: it routes 'play
something similar' to a personalised-PageRank-flavoured random walk over
acoustic + artist edges (see `walk`), 'more by this artist' to artist
neighbours, and 'play X mood' to `tracks_by_mood`. Provides the continuous
proximity the assistant needs instead of discrete buckets.

Walk improvements (versus a textbook random walk):
  • multi-tier pooling per step so the walker doesn't dead-end on
    un-analysed tracks mid-walk;
  • personalised-PageRank restart probability (α≈0.15) so it stays
    semantically anchored to the seed across longer sequences;
  • softmax-temperature selection (one tunable knob) instead of an
    implicit cosine² shaping;
  • MMR-style diversity term so re-asking from the same seed doesn't
    deterministically replay the same cluster;
  • batched 2-hop prefetch — one DB round-trip per walk regardless of
    length.

Mood vocabulary lives in `MOODS` (a `MoodSpec` per canonical name +
aliases + camelot_pref + bpm_smooth_weight). `MOOD_PROFILES` and
`MOOD_KEYWORDS` are derived views kept for backwards compatibility; do not
edit them directly. The assistant's intent regex imports `MOOD_KEYWORDS`
lazily so adding a new mood word here automatically extends the parser.

All builders are async (DB-bound) but the numpy work runs synchronously —
no off-thread call is necessary for libraries up to ~20K tracks; Euclidean kNN
over the PCA-reduced vectors is bandwidth-limited and finishes in under a second.
For very large libraries the caller should wrap build_acoustic_edges in
asyncio.to_thread.
"""

from __future__ import annotations

import logging
import random
import json
import os
from typing import Optional

import numpy as np

from utils.dsp import (
    GRAPH_EMBED_DIMS,
    FEATURES_VERSION,
    analyze_track,
    unpack_graph_embedding,
)
from utils.harmonic import key_index_to_camelot
from utils.config import APP_DIR

logger = logging.getLogger(__name__)

CUSTOM_MOODS_PATH = os.path.join(APP_DIR, "custom_moods.json")

# Islet membership defaults. Membership is a self-tuning Gaussian affinity to
# the exemplar in the unified graph Zr space (1.0 = the exemplar itself):
# tracks at or above the threshold are members, ranked by affinity descending,
# capped at ISLET_MAX. ISLET_MIN guards against islets too sparse to feel
# meaningful (an outlier exemplar with no neighbours returns an empty result
# rather than a one-track "group"). NOTE: the threshold is now an affinity in
# [0,1], not a raw-timbre cosine — legacy islets saved at ~0.93 may need
# re-tuning. Because σ is the exemplar's 7th-NN distance, affinity(7th-NN) ≈
# e⁻¹ ≈ 0.37 for every exemplar, so the threshold acts as a density-independent
# rank cutoff: ~0.37 ≈ the 7 nearest, lower ⇒ a wider neighbourhood. The 0.25
# default targets a ~10–15-track islet.
ISLET_THRESHOLD = 0.25
ISLET_MAX = 50
ISLET_MIN = 3

def load_custom_moods() -> dict:
    if not os.path.exists(CUSTOM_MOODS_PATH):
        return {}
    try:
        with open(CUSTOM_MOODS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load custom moods: %s", e)
        return {}

def save_custom_mood(
    name: str,
    centroid: list[float],
    exemplar_path: str,
    threshold: float = ISLET_THRESHOLD,
):
    """Save a user-named islet seeded by one exemplar track.

    The exemplar's timbre vector becomes the centroid; membership is computed
    on demand by `tracks_in_islet` against `threshold`. Per-islet threshold
    lets us tune sparse-library cases later without touching saved data.
    """
    moods = load_custom_moods()
    moods[name.lower().strip()] = {
        "centroid": centroid,
        "exemplar_path": exemplar_path,
        "threshold": float(threshold),
    }
    try:
        with open(CUSTOM_MOODS_PATH, "w", encoding="utf-8") as f:
            json.dump(moods, f, indent=4)
    except Exception as e:
        logger.error("Failed to save custom mood %s: %s", name, e)


def update_custom_mood(old_name: str, new_name: str, threshold: float) -> bool:
    """Rename an islet and/or change its threshold. Centroid and exemplar_path
    are preserved. Returns True if the islet existed and was updated. If
    `new_name` is already used by a different islet, the rename is rejected
    and the function returns False without writing anything.
    """
    old = old_name.lower().strip()
    new = new_name.lower().strip()
    if not new:
        return False
    moods = load_custom_moods()
    if old not in moods:
        return False
    if old != new and new in moods:
        return False
    entry = dict(moods[old])
    entry["threshold"] = float(threshold)
    if old != new:
        del moods[old]
    moods[new] = entry
    try:
        with open(CUSTOM_MOODS_PATH, "w", encoding="utf-8") as f:
            json.dump(moods, f, indent=4)
    except Exception as e:
        logger.error("Failed to update custom mood %s -> %s: %s", old_name, new_name, e)
        return False
    return True


def delete_custom_mood(name: str) -> bool:
    """Remove an islet by name. Returns True if it existed and was removed."""
    cleaned = name.lower().strip()
    moods = load_custom_moods()
    if cleaned not in moods:
        return False
    del moods[cleaned]
    try:
        with open(CUSTOM_MOODS_PATH, "w", encoding="utf-8") as f:
            json.dump(moods, f, indent=4)
    except Exception as e:
        logger.error("Failed to delete custom mood %s: %s", name, e)
        return False
    return True


def blacklist_track_from_islet(islet_name: str, track_path: str) -> bool:
    """Add a track path to the islet's blacklist array in custom_moods.json."""
    cleaned = islet_name.lower().strip()
    moods = load_custom_moods()
    if cleaned not in moods:
        return False
    if "blacklist" not in moods[cleaned]:
        moods[cleaned]["blacklist"] = []
    if track_path not in moods[cleaned]["blacklist"]:
        moods[cleaned]["blacklist"].append(track_path)
        try:
            with open(CUSTOM_MOODS_PATH, "w", encoding="utf-8") as f:
                json.dump(moods, f, indent=4)
            return True
        except Exception as e:
            logger.error("Failed to save blacklist for islet %s: %s", islet_name, e)
            return False
    return True


def clear_islet_blacklist(islet_name: str) -> bool:
    """Clear all track paths from the islet's blacklist array in custom_moods.json."""
    cleaned = islet_name.lower().strip()
    moods = load_custom_moods()
    if cleaned not in moods:
        return False
    if "blacklist" in moods[cleaned]:
        moods[cleaned]["blacklist"] = []
        try:
            with open(CUSTOM_MOODS_PATH, "w", encoding="utf-8") as f:
                json.dump(moods, f, indent=4)
            return True
        except Exception as e:
            logger.error("Failed to clear blacklist for islet %s: %s", islet_name, e)
            return False
    return True


def list_islets() -> list[str]:
    """Names of all user-saved islets, alphabetised."""
    return sorted(load_custom_moods().keys())


# Top-K acoustic neighbours stored per track. 20 is enough for both 'most
# similar' lookups and short random walks; bigger K eats DB rows without
# adding signal.
DEFAULT_K_ACOUSTIC = 20
# Top-K metadata neighbours stored per track per kind. Albums rarely have
# more than ~15 tracks; artists can have hundreds, but the assistant only
# samples a handful at a time.
DEFAULT_K_METADATA = 30

# Edge-kind tags written into track_neighbors.edge_kind.
KIND_ACOUSTIC = "acoustic"
KIND_ARTIST = "artist"
KIND_ALBUM = "album"


# ── Builders ─────────────────────────────────────────────────────────────────


def _kmeans(
    X: np.ndarray,
    k: int,
    max_iter: int = 50,
    n_restarts: int = 5,
) -> np.ndarray:
    """k-means clustering with k-means++ init. Returns labels (N,)."""
    N, D = X.shape
    best_labels = np.zeros(N, dtype=np.int32)
    best_inertia = np.inf
    rng = np.random.RandomState(42)

    # Pre-calculate squared norms of X for BLAS-accelerated Lloyd iterations
    X_sq = np.sum(X ** 2, axis=1, keepdims=True)

    for _ in range(n_restarts):
        # Optimized k-means++ initialisation
        centres = np.empty((k, D), dtype=np.float64)
        centres[0] = X[rng.choice(N)]
        min_dists = np.sum((X - centres[0]) ** 2, axis=1)
        for c in range(1, k):
            probs = min_dists / (min_dists.sum() + 1e-12)
            centres[c] = X[rng.choice(N, p=probs)]
            new_dists = np.sum((X - centres[c]) ** 2, axis=1)
            min_dists = np.minimum(min_dists, new_dists)

        # Lloyd iterations
        labels = np.zeros(N, dtype=np.int32)
        for _ in range(max_iter):
            # Assignment using BLAS dot-product distance calculation:
            # ||X - centres||^2 = X_sq - 2*X*centres^T + centres_sq
            centres_sq = np.sum(centres ** 2, axis=1, keepdims=True).T
            dists = X_sq - 2.0 * np.dot(X, centres.T) + centres_sq
            new_labels = np.argmin(dists, axis=1).astype(np.int32)
            if np.array_equal(new_labels, labels):
                labels = new_labels
                break
            labels = new_labels
            # Update
            for c in range(k):
                members = X[labels == c]
                if len(members) > 0:
                    centres[c] = members.mean(axis=0)

        inertia = 0.0
        for c in range(k):
            members = X[labels == c]
            if len(members) > 0:
                inertia += float(np.sum((members - centres[c]) ** 2))
        if inertia < best_inertia:
            best_inertia = inertia
            best_labels = labels.copy()

    return best_labels


def _louvain_local_move(
    n: int,
    edges: list[tuple[int, int, float]],
    self_w: np.ndarray,
    resolution: float,
    rng: random.Random,
    max_passes: int,
) -> tuple[np.ndarray, int]:
    """One Louvain level: greedily move nodes to maximise modularity.

    Returns (community-per-node, total moves made). `self_w[i]` is i's
    self-loop weight (intra-community weight folded in from a prior
    aggregation level; zero at the first level).
    """
    adj: list[dict[int, float]] = [dict() for _ in range(n)]
    for (u, v, w) in edges:
        adj[u][v] = adj[u].get(v, 0.0) + w
        adj[v][u] = adj[v].get(u, 0.0) + w

    # degree k_i = incident edge weight + 2× self-loop weight
    k = np.array(
        [sum(adj[i].values()) + 2.0 * float(self_w[i]) for i in range(n)],
        dtype=np.float64,
    )
    m2 = float(k.sum())  # = 2m
    if m2 <= 0.0:
        return np.arange(n, dtype=np.int64), 0

    comm = np.arange(n, dtype=np.int64)
    comm_tot = k.copy()  # Σ_tot: total degree per community
    order = list(range(n))
    total_moves = 0

    for _pass in range(max_passes):
        rng.shuffle(order)
        moved_this_pass = 0
        for i in order:
            ci = int(comm[i])
            ki = k[i]
            # tentatively remove i from its community
            comm_tot[ci] -= ki
            # summed weight from i into each neighbouring community
            nbr_w: dict[int, float] = {}
            for j, w in adj[i].items():
                cj = int(comm[j])
                nbr_w[cj] = nbr_w.get(cj, 0.0) + w
            # gain of joining C ≈ w(i,C) - γ·Σ_tot[C]·k_i/2m. Constant terms are
            # identical across C, so this comparison is exact for ranking.
            best_c = ci
            best_gain = nbr_w.get(ci, 0.0) - resolution * comm_tot[ci] * ki / m2
            for cj, wic in nbr_w.items():
                if cj == ci:
                    continue
                gain = wic - resolution * comm_tot[cj] * ki / m2
                if gain > best_gain:
                    best_gain = gain
                    best_c = cj
            comm_tot[best_c] += ki
            comm[i] = best_c
            if best_c != ci:
                moved_this_pass += 1
        total_moves += moved_this_pass
        if moved_this_pass == 0:
            break

    return comm, total_moves


def _louvain(
    n: int,
    edges: list[tuple[int, int, float]],
    resolution: float = 1.0,
    seed: int = 42,
    max_levels: int = 20,
    max_passes: int = 100,
) -> np.ndarray:
    """Louvain modularity community detection on a weighted undirected graph.

    Parameters
    ----------
    n : number of nodes (0..n-1).
    edges : undirected weighted edges (u, v, w) with u != v; duplicate /
        reversed pairs are summed. The graph need not be connected.
    resolution : γ in the modularity. >1 → more, smaller communities; <1 →
        fewer, larger. 1.0 is standard modularity.

    Returns
    -------
    np.ndarray (n,) int32 : contiguous community label (0..C-1) per node.
        Isolated nodes (no edges) come back as their own singleton community.

    Pure NumPy/Python (no SciPy/sklearn/igraph) so it runs on-device. Blondel
    et al. (2008): (1) greedily move nodes to the neighbouring community that
    most increases modularity until no move helps; (2) contract each community
    into a super-node (intra-community weight becomes a self-loop) and recurse
    on the smaller graph. Converges in a handful of levels at kNN scale.
    """
    rng = random.Random(seed)

    cur_n = n
    cur_self = np.zeros(cur_n, dtype=np.float64)
    cur_edges = [(int(u), int(v), float(w)) for (u, v, w) in edges if u != v]
    labels = np.arange(n, dtype=np.int64)  # original node -> current super-node

    for _level in range(max_levels):
        comm, moved = _louvain_local_move(
            cur_n, cur_edges, cur_self, resolution, rng, max_passes,
        )
        # Relabel communities to contiguous 0..c-1.
        uniq = sorted(set(int(c) for c in comm))
        remap = {c: i for i, c in enumerate(uniq)}
        comm = np.array([remap[int(c)] for c in comm], dtype=np.int64)
        c = len(uniq)

        labels = comm[labels]  # map original nodes through this partition

        if c == cur_n or moved == 0:
            break  # no contraction possible / nothing moved → converged

        # Contract communities into super-nodes for the next level.
        new_self = np.zeros(c, dtype=np.float64)
        for i in range(cur_n):
            new_self[comm[i]] += cur_self[i]
        edge_acc: dict[tuple[int, int], float] = {}
        for (u, v, w) in cur_edges:
            cu, cv = int(comm[u]), int(comm[v])
            if cu == cv:
                new_self[cu] += w
            else:
                key = (cu, cv) if cu < cv else (cv, cu)
                edge_acc[key] = edge_acc.get(key, 0.0) + w
        cur_edges = [(a, b, w) for (a, b), w in edge_acc.items()]
        cur_self = new_self
        cur_n = c

    uniq = sorted(set(int(x) for x in labels))
    remap = {c: i for i, c in enumerate(uniq)}
    return np.array([remap[int(x)] for x in labels], dtype=np.int32)


# ── Builders ─────────────────────────────────────────────────────────────────


# Canonical order of the scalar descriptors appended to the timbre block
# (EMBED_DIMS floats: mfcc mean/std/delta + chroma + rhythm, see dsp.py).
# `bpm` denotes the log2(bpm) column; cos_h/sin_h are the Camelot unit-circle
# coords (structural — never cleaved by the covariance analysis).
_SCALAR_ORDER = (
    "bpm", "brightness", "energy", "rolloff", "beat_strength",
    "spectral_flatness", "spectral_contrast", "cos_h", "sin_h", "key_mode",
)
_STRUCTURAL_SCALARS = frozenset({"cos_h", "sin_h", "key_mode"})
# Harmonic columns excluded from SVD and late-fused after PCA projection.
# Their rigid Camelot-wheel geometry must not be rotated by PCA.
_HARMONIC_SCALARS = frozenset({"cos_h", "sin_h", "key_mode"})


def _all_scalars(row: dict) -> dict[str, float]:
    """Every scalar descriptor for one track row, keyed by `_SCALAR_ORDER`.
    `bpm` is returned as log2(bpm); the Camelot key is encoded as
    (cos_h, sin_h, key_mode)."""
    bpm_raw = float(row.get("bpm", 0) or 0)
    log_bpm = float(np.log2(max(bpm_raw, 1.0)))
    ki = row.get("key_index", 0) or 0
    cam = key_index_to_camelot(ki)
    if cam is None:
        cos_h, sin_h, key_mode = 0.0, 0.0, 0.0
    else:
        hour, ring = cam
        theta = 2.0 * np.pi * (hour - 1) / 12.0
        cos_h = float(np.cos(theta))
        sin_h = float(np.sin(theta))
        key_mode = 1.0 if ring == "B" else 0.0
    return {
        "bpm": log_bpm,
        "brightness": float(row.get("brightness", 0) or 0),
        "energy": float(row.get("energy", 0) or 0),
        "rolloff": float(row.get("rolloff", 0) or 0),
        "beat_strength": float(row.get("beat_strength", 0) or 0),
        "spectral_flatness": float(row.get("spectral_flatness", 0) or 0),
        "spectral_contrast": float(row.get("spectral_contrast", 0) or 0),
        "cos_h": cos_h,
        "sin_h": sin_h,
        "key_mode": key_mode,
    }


def _surviving_scalars(redundant: set[str]) -> list[str]:
    """Scalar names that survive covariance cleaving, in canonical order.
    Structural/harmonic coords (cos_h/sin_h/key_mode) are always kept."""
    return [
        s for s in _SCALAR_ORDER
        if s in _STRUCTURAL_SCALARS or s not in redundant
    ]


def _feature_vector(row: dict, timbre: np.ndarray, surviving: list[str]) -> np.ndarray:
    """Full graph feature vector for one track: the EMBED_DIMS timbre block
    followed by the surviving scalar descriptors in `surviving` order."""
    sc = _all_scalars(row)
    scalars = np.array([sc[s] for s in surviving], dtype=np.float32)
    return np.concatenate([timbre.astype(np.float32), scalars])


def project_to_zr(row: dict, proj: dict) -> Optional[np.ndarray]:
    """Project one track row into the persisted graph Zr space.

    `proj` is the dict from `db_manager.load_pca_space()`: means/stds (D_cont,),
    projection (D_cont, k), and the feature spec (surviving scalars,
    scalar_weight, embed_dims, harmonic metadata). Returns None when the track
    lacks a usable timbre BLOB or the projection is absent/mismatched.

    Late Fusion: the harmonic columns (cos_h, sin_h, key_mode) are excluded
    from the PCA projection and concatenated back onto Zr using their own
    persisted z-score stats + harmonic_weight. This preserves the rigid
    Camelot wheel geometry that SVD would otherwise rotate.
    """
    if not proj or proj.get("projection") is None or proj.get("surviving") is None:
        return None
    v = unpack_graph_embedding(row.get("timbre"))
    if v is None or v.shape[0] != GRAPH_EMBED_DIMS:
        return None

    surviving = proj["surviving"]
    embed_dims = int(proj.get("embed_dims", GRAPH_EMBED_DIMS))
    scalar_weight = float(proj.get("scalar_weight", 1.0))
    harmonic_names = set(proj.get("harmonic_names") or [])
    harmonic_weight = float(proj.get("harmonic_weight", 1.5))

    x = _feature_vector(row, v, surviving)

    # ── Late-fusion split ────────────────────────────────────────────────
    if harmonic_names:
        # Determine which columns in x are harmonic (offset by embed_dims).
        harm_cols = []
        cont_cols = []
        for i, s in enumerate(surviving):
            col = embed_dims + i
            if s in harmonic_names:
                harm_cols.append(col)
            else:
                cont_cols.append(col)
        # Timbre columns are always continuous.
        cont_cols = list(range(embed_dims)) + cont_cols

        x_cont = x[cont_cols]
        x_harm = x[harm_cols]

        # z-score continuous part with persisted stats.
        means = np.asarray(proj["means"], dtype=np.float32)
        stds = np.asarray(proj["stds"], dtype=np.float32)
        if x_cont.shape[0] != means.shape[0]:
            return None
        z_cont = (x_cont - means) / stds
        # Boost non-timbre continuous scalars.
        if scalar_weight != 1.0:
            z_cont[embed_dims:] *= scalar_weight

        # z-score harmonic part with its own persisted stats.
        h_means = np.asarray(proj.get("harmonic_means", np.zeros(len(harm_cols))), dtype=np.float32)
        h_stds = np.asarray(proj.get("harmonic_stds", np.ones(len(harm_cols))), dtype=np.float32)
        z_harm = ((x_harm - h_means) / h_stds) * harmonic_weight

        # Project continuous part, concatenate harmonics.
        zr_cont = z_cont @ np.asarray(proj["projection"], dtype=np.float32)
        return np.concatenate([zr_cont, z_harm]).astype(np.float32)
    else:
        # Legacy path (pre-late-fusion projection): project the full vector.
        means = np.asarray(proj["means"], dtype=np.float32)
        stds = np.asarray(proj["stds"], dtype=np.float32)
        if x.shape[0] != means.shape[0]:
            return None
        z = (x - means) / stds
        z[embed_dims:] *= scalar_weight
        return (z @ np.asarray(proj["projection"], dtype=np.float32)).astype(np.float32)


def _local_refine_edges(
    paths: list[str],
    Z_cont: np.ndarray,
    Z_harm: np.ndarray,
    cluster_labels: np.ndarray,
    k: int,
    harmonic_weight: float,
    min_size: int = 12,
) -> tuple[list[tuple[str, str, float]], int]:
    """Re-embed each Louvain community in its OWN local PCA and recompute the
    intra-community acoustic edges with locally-informative axes.

    Why (suggestion 3): the global SVD's principal axes are owned by whatever
    genre dominates the library, so a minority-genre community is embedded on
    axes fit to a *different* genre's variance and its internal structure
    collapses (the majority-class / batch effect the genre diagnostic
    surfaced). Re-z-scoring and re-PCA'ing *within* the community gives that
    neighbourhood a metric fit to its own variance — the bioinformatics
    "subcluster with cluster-specific HVGs" move.

    Returns (intra_edges, n_refined_communities). The caller keeps the global
    cross-community edges and replaces only the intra-community ones. The
    persisted global Zr geometry is left untouched, so moods / islets /
    project_to_zr are unaffected.
    """
    intra_edges: list[tuple[str, str, float]] = []
    refined = 0
    for cid in np.unique(cluster_labels):
        members = np.where(cluster_labels == cid)[0]
        m = int(members.size)
        if m < min_size:
            continue  # too small for a meaningful local geometry

        # Local z-score of the continuous block (re-centre/scale within community).
        sub = Z_cont[members]
        mu = sub.mean(axis=0)
        sd = sub.std(axis=0)
        sd = np.where(sd < 1e-8, 1.0, sd)
        subz = (sub - mu) / sd

        # Local PCA (Kaiser λ>1, ≥3 comps) — community-specific principal axes.
        if subz.shape[1] >= 4 and m > 4:
            _u, _s, _vt = np.linalg.svd(subz, full_matrices=False)
            ev = (_s ** 2) / float(m - 1)
            kk = max(3, min(int((ev > 1.0).sum()), _vt.shape[0]))
            subr = (subz @ _vt[:kk].T).astype(np.float32)
        else:
            subr = subz.astype(np.float32)

        # Local harmonic late-fusion (re-z-scored within the community).
        h = Z_harm[members]
        hmu = h.mean(axis=0)
        hsd = h.std(axis=0)
        hsd = np.where(hsd < 1e-8, 1.0, hsd)
        subr = np.concatenate(
            [subr, ((h - hmu) / hsd).astype(np.float32) * harmonic_weight], axis=1,
        )

        # Local kNN + self-tuning σ + strict mutual-kNN — same recipe as the
        # global build, restricted to the community subgraph.
        kk = min(k, m - 1)
        if kk < 1:
            continue
        sq = np.sum(subr ** 2, axis=1)
        d2 = sq[:, None] - 2.0 * (subr @ subr.T) + sq[None, :]
        np.fill_diagonal(d2, np.inf)
        cand: list[list[tuple[int, float]]] = []
        for i in range(m):
            idx = np.argpartition(d2[i], kk)[:kk]
            idx = idx[np.argsort(d2[i, idx])]
            cand.append([(int(j), max(0.0, float(d2[i, j]))) for j in idx])
        LOCAL_K = 7
        sig = np.ones(m, dtype=np.float32)
        for i in range(m):
            if cand[i]:
                piv = cand[i][min(LOCAL_K - 1, len(cand[i]) - 1)][1]
                sig[i] = float(np.sqrt(max(0.0, piv)))
        sig = np.maximum(sig, 1e-3)
        nbr = [{j for j, _ in cand[i]} for i in range(m)]
        for i in range(m):
            for j, d2v in cand[i]:
                if i not in nbr[j]:
                    continue
                aff = float(np.exp(-d2v / (sig[i] * float(sig[j]))))
                intra_edges.append((paths[int(members[i])], paths[int(members[j])], aff))
        refined += 1
    return intra_edges, refined


async def build_acoustic_edges(
    db_manager,
    k: int = DEFAULT_K_ACOUSTIC,
    features_version: int = FEATURES_VERSION,
    z_score: bool = True,
    scalar_weight: float = 1.5,
    harmonic_weight: float = 1.5,
    cluster_resolution: float = 1.0,
    local_refine: bool = True,
    csls_beta: float = 0.0,
    refine_resolution: float | None = None,
) -> int:
    """Recompute the acoustic tier of the graph from scratch.

    Loads every track that has a current-version feature BLOB, optionally
    z-scores the vectors (or centers them if z_score=False), and writes the
    top-K neighbours per track back to `track_neighbors`. Returns the edge
    count written.

    Coverage degrades gracefully: tracks without features are simply absent
    from the acoustic graph. The assistant falls back to metadata edges for
    those.
    """
    rows = await db_manager.get_tracks_with_features(features_version)
    if len(rows) < 2:
        await db_manager.replace_neighbors_bulk([], KIND_ACOUSTIC)
        logger.info("track_graph: acoustic edges skipped (only %d tracks with features)", len(rows))
        return 0

    # ── Feature selection: drop covariance-redundant scalars ──────────────
    # The graph — and the Louvain communities + similarity walk built on it —
    # uses every feature that survives the unsupervised PCA / Pearson-covariance
    # analysis. Collinear scalars (e.g. rolloff ↔ brightness) are cleaved so
    # they don't double-count toward distance. The 52-D timbre block and the
    # harmonic unit-circle coords (cos_h/sin_h) are structural and always kept;
    # only the raw scalar descriptors are subject to cleaving.
    from utils.pca_engine import redundant_raw_features
    redundant = redundant_raw_features(rows)
    if redundant:
        logger.info(
            "track_graph: covariance analysis cleaved redundant scalars %s "
            "from the graph feature space", sorted(redundant),
        )

    surviving = _surviving_scalars(redundant)
    paths: list[str] = []
    vectors: list[np.ndarray] = []
    for r in rows:
        # The graph embedding is the v4 BLOB with mfcc_delta removed
        # (GRAPH_EMBED_DIMS): the ablation showed delta is dead weight for
        # similarity. Old/short BLOBs unpack to None and are skipped.
        v = unpack_graph_embedding(r.get("timbre"))
        if v is None or v.shape[0] != GRAPH_EMBED_DIMS:
            continue
        # timbre block + the surviving scalar descriptors (tempo as
        # log2(bpm), key as cos_h/sin_h/mode) so dynamics and harmony shape
        # similarity alongside timbre. See `_all_scalars` / `_feature_vector`.
        paths.append(r["path"])
        vectors.append(_feature_vector(r, v, surviving))

    if len(vectors) < 2:
        await db_manager.replace_neighbors_bulk([], KIND_ACOUSTIC)
        return 0

    X = np.stack(vectors, axis=0)  # (N, EMBED_DIMS + len(surviving))

    # ── Late Fusion split: separate harmonic columns from continuous ──────
    # The harmonic unit-circle coords (cos_h, sin_h, key_mode) encode the
    # rigid Camelot wheel geometry. SVD rotates *all* axes into mixed PCs,
    # which destroys that geometric integrity. Late Fusion keeps them out of
    # the PCA entirely: the SVD denoises only the ~58-D timbre+dynamics, and
    # the raw harmonic coordinates are concatenated back after projection.
    harmonic_names_in_surviving = [s for s in surviving if s in _HARMONIC_SCALARS]
    harm_col_indices = []   # column indices in X that are harmonic
    cont_col_indices = list(range(GRAPH_EMBED_DIMS))  # timbre block is always continuous
    for i, s in enumerate(surviving):
        col = GRAPH_EMBED_DIMS + i
        if s in _HARMONIC_SCALARS:
            harm_col_indices.append(col)
        else:
            cont_col_indices.append(col)

    X_cont = X[:, cont_col_indices]   # (N, D_cont)
    X_harm = X[:, harm_col_indices]   # (N, n_harm)  — typically 3

    # z-score the continuous block.
    mu_cont = X_cont.mean(axis=0)
    if z_score:
        sd_cont = X_cont.std(axis=0)
        sd_cont = np.where(sd_cont < 1e-8, 1.0, sd_cont)
    else:
        sd_cont = np.ones(X_cont.shape[1], dtype=X_cont.dtype)
    Z_cont = (X_cont - mu_cont) / sd_cont

    # z-score the harmonic block separately (preserves circle geometry).
    mu_harm = X_harm.mean(axis=0)
    if z_score:
        sd_harm = X_harm.std(axis=0)
        sd_harm = np.where(sd_harm < 1e-8, 1.0, sd_harm)
    else:
        sd_harm = np.ones(X_harm.shape[1], dtype=X_harm.dtype)
    Z_harm = (X_harm - mu_harm) / sd_harm

    # Boost the non-timbre continuous scalars AFTER z-scoring so tempo/dynamics
    # carry weight comparable to the individual timbre axes.
    n_cont_scalars = Z_cont.shape[1] - GRAPH_EMBED_DIMS
    if scalar_weight != 1.0 and n_cont_scalars > 0:
        Z_cont[:, GRAPH_EMBED_DIMS:] *= scalar_weight

    # ── PCA reduction (Kaiser-truncated SVD on the *continuous* matrix) ─────
    # Only the timbre + continuous dynamics enter the SVD; the harmonic columns
    # are fused back afterwards. This ensures the Camelot wheel's cos/sin
    # geometry is 100% preserved in the final affinity calculation.
    Zr_cont = Z_cont.astype(np.float32)
    N = Z_cont.shape[0]
    V_keep = np.eye(Z_cont.shape[1], dtype=np.float32)
    eigenvalues = np.zeros(Z_cont.shape[1], dtype=np.float32)
    if Z_cont.shape[1] >= 4 and N > 1:
        _U, _S, _Vt = np.linalg.svd(Z_cont, full_matrices=False)
        eigenvalues = (_S ** 2) / float(N - 1)
        kaiser_k = int((eigenvalues > 1.0).sum())
        kaiser_k = max(3, min(kaiser_k, _Vt.shape[0]))
        V_keep = _Vt[:kaiser_k].T.astype(np.float32)     # (D_cont, kaiser_k)
        Zr_cont = (Z_cont @ V_keep).astype(np.float32)   # (N, kaiser_k)
        cum_var = float(eigenvalues[:kaiser_k].sum() / eigenvalues.sum()) if eigenvalues.sum() > 0 else 0.0
        logger.info(
            "track_graph: PCA-reduced continuous dims from %d to %d "
            "(Kaiser λ>1; %.1f%% variance retained)",
            int(_Vt.shape[1]), V_keep.shape[1], cum_var * 100.0,
        )

    # ── Late Fusion: concatenate the raw harmonic coords onto Zr ───────────
    H_fused = (Z_harm * harmonic_weight).astype(np.float32)  # (N, n_harm)
    Zr = np.concatenate([Zr_cont, H_fused], axis=1)          # (N, kaiser_k + n_harm)
    logger.info(
        "track_graph: late-fused %d harmonic dims (weight=%.2f) → "
        "final Zr %d-D",
        H_fused.shape[1], harmonic_weight, Zr.shape[1],
    )

    # ── Persist the unified geometry (projection + per-track Zr coords) ────
    # Single source of the graph's Zr space: moods, islets and any on-demand
    # projection of new/exemplar tracks read it back via load_pca_space() /
    # project_to_zr(). The stored means/stds correspond to the *continuous*
    # columns only; harmonic stats are stored separately in feature_spec so
    # project_to_zr can replicate the same late-fusion split.
    if hasattr(db_manager, "save_pca_space"):
        try:
            feature_spec = {
                "surviving": surviving,
                "scalar_weight": float(scalar_weight),
                "embed_dims": int(GRAPH_EMBED_DIMS),
                "z_score": bool(z_score),
                "harmonic_names": harmonic_names_in_surviving,
                "harmonic_weight": float(harmonic_weight),
                "harmonic_means": mu_harm.astype(np.float32).tolist(),
                "harmonic_stds": sd_harm.astype(np.float32).tolist(),
            }
            await db_manager.save_pca_space(
                mu_cont.astype(np.float32), sd_cont.astype(np.float32),
                V_keep, eigenvalues.astype(np.float32), feature_spec,
            )
            await db_manager.update_tracks_pca_coords_batch(
                [(paths[i], Zr[i]) for i in range(Zr.shape[0])]
            )
            _PERCENTILE_CACHE.clear()
            logger.info(
                "track_graph: persisted Zr geometry (%d tracks × %d dims)",
                Zr.shape[0], Zr.shape[1],
            )
        except Exception as exc:
            logger.warning("track_graph: persisting Zr geometry failed: %s", exc)

    N = Zr.shape[0]
    k_eff = min(k, N - 1)
    Zr_sq = np.sum(Zr ** 2, axis=1)  # row squared-norms for distance expansion

    # ── Pass 1: self-tuning bandwidth σ_i (Zelnik-Manor) ──────────────────
    # σ_i = distance to i's LOCAL_K-th nearest neighbour. σ is a *distance*
    # scale, independent of how candidates are later ranked, so it's computed
    # up front over full rows. Chunked so we never materialise the full N×N
    # matrix. ||a-b||² = ||a||² - 2a·b + ||b||².
    LOCAL_K = 7
    chunk = 256
    sigmas = np.ones(N, dtype=np.float32)
    for i in range(0, N, chunk):
        block = Zr[i:i + chunk]              # (C, D)
        c = block.shape[0]
        d2 = Zr_sq[i:i + c, None] - 2.0 * (block @ Zr.T) + Zr_sq[None, :]
        for j in range(c):
            d2[j, i + j] = np.inf            # mask self
        # LOCAL_K-th smallest squared distance per row → σ = its sqrt.
        piv = np.partition(d2, LOCAL_K - 1, axis=1)[:, LOCAL_K - 1]
        sigmas[i:i + c] = np.sqrt(np.maximum(piv, 0.0))
    # Floor σ so a near-duplicate pivot (d ≈ 0) doesn't blow up the kernel.
    sigmas = np.maximum(sigmas, 1e-3)

    # After σ (Pass 1): per-node hubness r(x) = mean of x's top-LOCAL_K affinities.
    r = np.zeros(N, dtype=np.float32)
    if csls_beta > 0.0:
        for i in range(0, N, chunk):
            block = Zr[i:i + chunk]
            c = block.shape[0]
            d2 = Zr_sq[i:i + c, None] - 2.0 * (block @ Zr.T) + Zr_sq[None, :]
            for j in range(c):
                d2[j, i + j] = np.inf
            A = np.exp(-d2 / (sigmas[i:i + c, None] * sigmas[None, :]))
            r[i:i + c] = np.sort(A, axis=1)[:, -LOCAL_K:].mean(axis=1)

    # ── Pass 2: candidate neighbourhoods by self-tuning AFFINITY ──────────
    # affinity(i,j) = exp(-d(i,j)² / (σ_i·σ_j)). We select each track's top-K by
    # *affinity*, not by raw Euclidean distance. Gating on distance and only
    # then applying the kernel as an edge weight (the previous behaviour) threw
    # the kernel's hub mitigation away at the membership stage: the σ_j term
    # divides a candidate's distance by *its own* neighbourhood scale, so a
    # sparse-region neighbour — typically a minority-genre track — outranks an
    # equidistant majority "hub". A per-class retrieval audit
    # (tools/projection_diagnostic.py) showed affinity selection recovers
    # minority structure the distance gate discarded (e.g. Classical kNN purity
    # 0.21 → 0.33; global purity 0.631 → 0.640) at no majority cost. The
    # affinity computed here is reused directly as the edge weight, so there is
    # no separate rescale pass.
    candidates: list[list[tuple[int, float]]] = [[] for _ in range(N)]
    for i in range(0, N, chunk):
        block = Zr[i:i + chunk]              # (C, D)
        c = block.shape[0]
        d2 = Zr_sq[i:i + c, None] - 2.0 * (block @ Zr.T) + Zr_sq[None, :]
        for j in range(c):
            d2[j, i + j] = np.inf            # self → affinity 0, never selected
        A = np.exp(-d2 / (sigmas[i:i + c, None] * sigmas[None, :]))
        if csls_beta > 0.0:
            sel = A - csls_beta * 0.5 * (r[i:i + c, None] + r[None, :])
        else:
            sel = A
        # Largest-K affinities per row. argpartition on -sel is O(N); order slice.
        topk_unsorted = np.argpartition(-sel, k_eff, axis=1)[:, :k_eff]
        for j in range(c):
            idx = topk_unsorted[j]
            ordered = idx[np.argsort(-sel[j, idx])]
            candidates[i + j] = [(int(nb), float(A[j, nb])) for nb in ordered]

    # Mutual-kNN pruning: keep edge (i → j) iff j ∈ topK(i) AND i ∈ topK(j).
    # Strict mutual-kNN flattens hub over-representation. The surviving edges
    # are symmetric (the self-tuning affinity is symmetric in i,j), so we also
    # collect them as undirected (i<j) pairs to feed Louvain below.
    neighbour_set = [{nb for nb, _ in cands} for cands in candidates]
    edges: list[tuple[str, str, float]] = []
    mutual_pairs: list[tuple[int, int, float]] = []
    mutual_total = 0
    for src_idx, cands in enumerate(candidates):
        src = paths[src_idx]
        for nb_idx, weight in cands:
            if src_idx not in neighbour_set[nb_idx]:
                continue
            mutual_total += 1
            edges.append((src, paths[nb_idx], weight))
            if src_idx < nb_idx:
                mutual_pairs.append((src_idx, nb_idx, weight))

    await db_manager.replace_neighbors_bulk(edges, KIND_ACOUSTIC)
    logger.info(
        "track_graph: wrote %d acoustic edges across %d tracks (k=%d, mutual=%d)",
        len(edges), N, k_eff, mutual_total,
    )

    # ── Community detection (Louvain) on the affinity graph ───────────────
    # Replaces K-Means: the mutual-kNN affinity graph is the natural substrate
    # for clustering, and modularity optimisation discovers the number of
    # communities from the topology instead of guessing k. Labels persist in
    # play_counts.cluster_id and are consumed by walk() as a soft cross-cluster
    # penalty and by the PCA cluster report.
    if N >= 6:
        try:
            cluster_labels = _louvain(N, mutual_pairs, resolution=cluster_resolution)
            n_comm = int(len(np.unique(cluster_labels)))

            pairs = [(paths[i], int(cluster_labels[i])) for i in range(N)]
            await db_manager.save_track_clusters(pairs)

            # Log the largest communities for diagnostics (singletons elided).
            unique, counts = np.unique(cluster_labels, return_counts=True)
            order = np.argsort(-counts)
            size_str = ", ".join(
                f"C{int(unique[o])}={int(counts[o])}" for o in order[:15]
            )
            logger.info(
                "track_graph: Louvain (resolution=%.2f) found %d communities "
                "across %d tracks; top sizes: %s",
                cluster_resolution, n_comm, N, size_str,
            )

            # ── Local per-community re-embedding (suggestion 3) ──────────────
            # Replace intra-community acoustic edges with ones computed under a
            # community-local PCA/metric; keep the global cross-community edges.
            # cluster_id stays from the global Louvain above (no re-clustering)
            # so the refinement basis is fixed and non-circular.
            if local_refine:
                try:
                    if refine_resolution is not None:
                        refine_labels = _louvain(N, mutual_pairs, resolution=refine_resolution)
                        logger.info("track_graph: ran second Louvain (resolution=%.2f) to seed refine partition", refine_resolution)
                    else:
                        refine_labels = cluster_labels
                    intra, n_ref = _local_refine_edges(
                        paths, Z_cont, Z_harm, refine_labels, k_eff, harmonic_weight,
                    )
                    cl = {paths[i]: int(cluster_labels[i]) for i in range(N)}
                    cross = [e for e in edges if cl.get(e[0]) != cl.get(e[1])]
                    refined_edges = cross + intra
                    await db_manager.replace_neighbors_bulk(refined_edges, KIND_ACOUSTIC)
                    edges = refined_edges
                    logger.info(
                        "track_graph: local re-embedding refined %d communities → "
                        "%d intra + %d cross = %d acoustic edges",
                        n_ref, len(intra), len(cross), len(edges),
                    )
                except Exception as ref_err:
                    logger.warning(
                        "track_graph: local re-embedding failed (%s); "
                        "keeping global acoustic edges", ref_err,
                    )

            # ── Generate/update on-device mathematical truth report ──────────
            try:
                from utils.pca_engine import plot_pca_report, plot_genre_report
                from utils.streamrip_api import load_config

                cfg = load_config()
                library_folder = (cfg.get("downloads") or {}).get("folder") or ""
                library_folder = str(library_folder).strip()

                if library_folder and os.path.isdir(library_folder):
                    report_dir = os.path.join(library_folder, "pca_report")
                else:
                    report_dir = os.path.join(APP_DIR, "pca_report")

                saved = plot_pca_report(
                    rows, report_dir, cluster_labels=cluster_labels,
                )
                # Genre-coloured view of the REAL Zr geometry (aligned to paths).
                path_to_genre = {r["path"]: r.get("genre") for r in rows}
                genres_aligned = [path_to_genre.get(p) for p in paths]
                saved += plot_genre_report(
                    paths, Zr, genres_aligned, report_dir,
                    cluster_labels=cluster_labels,
                )
                if saved:
                    logger.info(
                        "track_graph: PCA cluster report updated in %s  (%d figures)",
                        report_dir, len(saved),
                    )
            except Exception as plot_err:
                logger.warning(
                    "track_graph: PCA visual report after edge build skipped: %s",
                    plot_err,
                )

        except Exception as exc:
            logger.warning(
                "track_graph: Louvain clustering failed (%s); "
                "walk will proceed without cluster constraint.",
                exc,
            )
    else:
        logger.info(
            "track_graph: skipping community detection (N=%d < 6)", N,
        )

    return len(edges)


async def build_metadata_edges(
    db_manager,
    k: int = DEFAULT_K_METADATA,
) -> tuple[int, int]:
    """Recompute the metadata tier. Two passes: same-artist and same-album.

    Returns (artist_edge_count, album_edge_count).
    """
    conn = await db_manager.get_connection()

    # Same-album: every other track in the album becomes a neighbour. Order by
    # track_num so the natural album sequence wins as the tie-breaker.
    album_edges: list[tuple[str, str, float]] = []
    sql_alb = '''
        SELECT al.id AS album_id, t.path, t.track_num
        FROM tracks t
        JOIN albums al ON al.id = t.album_id
        ORDER BY al.id, t.track_num NULLS LAST, t.path
    '''
    async with conn.execute(sql_alb) as cursor:
        rows = await cursor.fetchall()

    by_album: dict[int, list[str]] = {}
    for r in rows:
        by_album.setdefault(r["album_id"], []).append(r["path"])
    for paths in by_album.values():
        if len(paths) < 2:
            continue
        for i, src in enumerate(paths):
            for j, dst in enumerate(paths):
                if src == dst:
                    continue
                # Closer in the album → higher weight; clamp so weight stays in (0, 1].
                dist = abs(i - j)
                w = 1.0 / (1.0 + 0.1 * (dist - 1))
                album_edges.append((src, dst, float(w)))

    await db_manager.replace_neighbors_bulk(album_edges, KIND_ALBUM)

    # Same-artist: any other track by the same artist. Cap per-source to k
    # so a prolific artist doesn't write thousands of rows for one source.
    artist_edges: list[tuple[str, str, float]] = []
    sql_art = '''
        SELECT ar.id AS artist_id, t.path, t.added_date
        FROM tracks t
        JOIN albums  al ON al.id = t.album_id
        JOIN artists ar ON ar.id = al.artist_id
        ORDER BY ar.id, t.added_date DESC, t.path
    '''
    async with conn.execute(sql_art) as cursor:
        rows = await cursor.fetchall()

    by_artist: dict[int, list[str]] = {}
    for r in rows:
        by_artist.setdefault(r["artist_id"], []).append(r["path"])
    for paths in by_artist.values():
        if len(paths) < 2:
            continue
        # For each source, write up to k other tracks. Sampling is biased
        # towards the most-recently-added tracks (the SQL is already ordered
        # that way) — the assistant phrasings 'more by this artist' almost
        # always mean 'newer stuff first'.
        for src in paths:
            others = [p for p in paths if p != src]
            for dst in others[:k]:
                artist_edges.append((src, dst, 1.0))

    await db_manager.replace_neighbors_bulk(artist_edges, KIND_ARTIST)

    logger.info(
        "track_graph: wrote %d artist + %d album metadata edges",
        len(artist_edges), len(album_edges),
    )
    return len(artist_edges), len(album_edges)


# ── Traversal primitives ─────────────────────────────────────────────────────


async def neighbors(
    db_manager,
    track_path: str,
    k: int = 10,
    edge_kind: Optional[str] = None,
) -> list[dict]:
    """Top-k neighbours of `track_path`, joined with track metadata.

    edge_kind=None returns the highest-weighted edges across all tiers
    (acoustic neighbours generally rank above metadata ones because cosine
    weights tend to sit in [0.7, 0.99] while metadata weights are ≤ 1.0).
    """
    return await db_manager.get_neighbors(track_path, k=k, edge_kind=edge_kind)


# Default per-tier weights applied on top of the raw edge weight before the
# softmax. Acoustic affinity is the self-tuning kernel value ∈ (0, 1];
# artist/album edges store a flat 1.0 indicator. With acoustic at 10× the
# metadata tiers (post-batch-D z-scoring, the ratio is what matters, not the
# absolute scale), even a low-affinity acoustic neighbour (≈ 0.18) outranks
# any artist/album candidate. That demotes metadata to a tiebreaker used
# when acoustic neighbours are exhausted, instead of letting prolific
# artists trap the walker on a same-artist orbit. Album is lower still
# because its edges fan out O(N²) within an album and would otherwise
# saturate the candidate pool.
_DEFAULT_EDGE_KIND_WEIGHTS: dict[str, float] = {
    KIND_ACOUSTIC: 10.0,
    KIND_ARTIST:   0.4,
    KIND_ALBUM:    0.2,
}


def _unpack_embedding(blob: bytes | None) -> Optional[np.ndarray]:
    """Local helper: unpack a timbre BLOB to an L2-normalised float32 vector
    suitable for cosine on the graph timbre sub-space (mfcc mean/std + chroma +
    rhythm; delta excluded, matching the geometry). Returns None when the blob
    is absent or malformed. Used by the MMR diversity term."""
    v = unpack_graph_embedding(blob)
    if v is None:
        return None
    n = float(np.linalg.norm(v))
    if n < 1e-9:
        return None
    return (v / n).astype(np.float32)


async def walk(
    db_manager,
    seed_path: str,
    length: int = 10,
    edge_kind: Optional[str] = None,
    edge_kinds: Optional[tuple[str, ...]] = None,
    edge_kind_weights: Optional[dict[str, float]] = None,
    avoid: Optional[set[str]] = None,
    restart_prob: float = 0.15,
    diversity_lambda: float = 0.15,
    temperature: float = 0.04,
    taste_weight: float = 0.0,
    taste_explore: float = 0.05,
    negative_embs: Optional[list[np.ndarray]] = None,
    negative_lambda: float = 0.6,
    seed_rng: Optional[random.Random] = None,
    teleport_path: Optional[str] = None,
    prefetch_k: int = 40,
    cluster_lambda: float = 0.5,
) -> list[str]:
    """Personalised-PageRank-flavoured random walk over the track graph.

    Pipeline differences vs. the old version:
      • multi-tier: pools acoustic + artist (and optionally album) neighbours
        per step so the walk doesn't dead-end on un-analysed tracks mid-walk.
      • restart_prob: at each step, with probability α jump back to the seed
        instead of stepping forward. Anchors the walk semantically; without
        this the walker drifts away from the seed in 4–8 steps.
      • softmax-temperature selection (replaces cosine² shaping). One knob
        (`temperature`): small → near-greedy, large → near-uniform.
      • MMR-style diversity term: candidates close to already-visited tracks
        in the timbre sub-space get a penalty proportional to
        `diversity_lambda * max_cos_to_visited`. Prevents the walk grinding
        inside one tight cluster.
      • Batched 2-hop prefetch: one round-trip to materialise the local
        neighbourhood graph instead of `length` sequential DB calls.

    Parameters
    ----------
    edge_kind / edge_kinds : the legacy `edge_kind: str` is still accepted
        and shimmed onto `edge_kinds=(edge_kind,)`. New callers should pass
        `edge_kinds=(KIND_ACOUSTIC, KIND_ARTIST)` or similar.
    edge_kind_weights : per-tier multiplier applied before the softmax.
        Defaults: acoustic=1.0, artist=0.4, album=0.2.
    avoid : externally-managed exclusion set (e.g. the assistant's persistent
        recent-playback history). The seed itself is always avoided.
    restart_prob : 0 disables restart (pure random walk); 0.15 is the
        classic personalised PageRank α.
    diversity_lambda : 0 disables MMR; 0.3 trades a little similarity for
        much more diversity across re-asks from the same seed.
    temperature : softmax temperature on the effective weights. Tuning
        guidance — similarity (≈ greedy): 0.05–0.10; discovery: 0.15–0.25.
    taste_weight : γ on the taste-model contribution to per-candidate
        logits. 0 disables the taste re-rank entirely (e.g. for tests).
        Cold taste model (no feedback events yet) is also a no-op
        regardless of `taste_weight`.
    taste_explore : ε ∈ [0, 1]. At each step, with probability ε the taste
        term is skipped for that step. Counters the filter-bubble effect
        where the walk only surfaces tracks the user already likes.
    negative_embs : session-scoped embeddings of tracks the user just
        rejected (skips, dislikes). Candidates close to any of these in the
        timbre sub-space get a penalty proportional to
        `negative_lambda * max_cos_to_negative`. Symmetric to the MMR
        diversity term, but the centroid is "things actively disliked in
        this session" instead of "things already played in this walk."
        Pass `None`/empty to disable.
    negative_lambda : weight on the per-step negative-centroid penalty.
        Larger than `diversity_lambda` by default because a skip is a
        stronger signal than "we've already seen something similar."

    prefetch_k : number of neighbours to fetch per node in the 2-hop horizon
        prefetch. Default 40 (full quality). Pass 20 for the cheap Play
        Similar path; the graph is still well-connected at that depth.
    cluster_lambda : penalty for cross-cluster transitions. At each step,
        candidates in a different K-Means cluster from the current node
        have their effective weight multiplied by (1 - cluster_lambda).
        0 disables the constraint; 0.5 (default) halves the logit;
        1.0 would hard-block (not recommended).

    Returns
    -------
    list[str] : paths in walk order (the seed itself is not included),
        length ≤ `length`. Stops early if the local neighbourhood is
        exhausted.
    """
    rng = seed_rng or random.Random()
    visited: set[str] = set(avoid or set())
    visited.add(seed_path)
    path_seq: list[str] = []

    # Normalise the kind parameters: legacy single-kind kwarg → tuple.
    if edge_kinds is None:
        if edge_kind is not None:
            kinds: tuple[str, ...] = (edge_kind,)
        else:
            kinds = (KIND_ACOUSTIC, KIND_ARTIST)
    else:
        kinds = edge_kinds
    weights_per_kind = dict(_DEFAULT_EDGE_KIND_WEIGHTS)
    if edge_kind_weights:
        weights_per_kind.update(edge_kind_weights)

    # ── Batched neighbourhood prefetch ────────────────────────────────────
    # 1-hop from the seed, then 2-hop on the union of seed + 1-hop. Walking
    # in memory avoids `length` round-trips and dominates the DB cost.
    seed_neighbours = await db_manager.get_neighbors_multi(
        seed_path, kinds, k=prefetch_k,
    )
    horizon: dict[str, list[dict]] = {seed_path: seed_neighbours}
    second_hop_paths = [
        n["path"] for n in seed_neighbours if n["path"] not in horizon
    ]
    # De-dup the 1-hop fan-out before issuing 2-hop queries.
    second_hop_paths = list(dict.fromkeys(second_hop_paths))
    for p in second_hop_paths:
        horizon[p] = await db_manager.get_neighbors_multi(p, kinds, k=prefetch_k)

    # ── Cluster map (for cross-cluster penalty) ───────────────────────────
    # Build a path→cluster_id lookup from the prefetched neighbour rows.
    # The get_neighbors_multi query now JOINs play_counts and includes
    # cluster_id, so this is zero extra cost.
    cluster_active = cluster_lambda > 0.0
    cluster_map: dict[str, int | None] = {}
    if cluster_active:
        # Seed cluster: look up from the first-hop results or DB fallback.
        if hasattr(db_manager, "get_track_cluster"):
            seed_cluster = await db_manager.get_track_cluster(seed_path)
        else:
            seed_cluster = None
        cluster_map[seed_path] = seed_cluster
        for nbrs in horizon.values():
            for n in nbrs:
                cid = n.get("cluster_id")
                cluster_map[n["path"]] = int(cid) if cid is not None else None

    # ── MMR setup ─────────────────────────────────────────────────────────
    # Pull embeddings for the seed + any node we might reach in two hops.
    # The visited-embedding set is what the diversity term scores against;
    # the negative-centroid term reuses the same per-candidate vectors.
    neg_active = bool(negative_embs) and negative_lambda > 0
    diversity_active = diversity_lambda > 0
    need_candidate_embs = diversity_active or neg_active
    seed_emb: Optional[np.ndarray] = None
    candidate_embs: dict[str, np.ndarray] = {}
    if need_candidate_embs:
        emb_paths = {seed_path}
        for nbrs in horizon.values():
            for n in nbrs:
                emb_paths.add(n["path"])
        blobs = await db_manager.get_embeddings_for_paths(list(emb_paths))
        for p, blob in blobs.items():
            v = _unpack_embedding(blob)
            if v is not None:
                candidate_embs[p] = v
        seed_emb = candidate_embs.get(seed_path)

    visited_embs: list[np.ndarray] = []
    if seed_emb is not None:
        visited_embs.append(seed_emb)

    neg_emb_arr: Optional[np.ndarray] = None
    if neg_active:
        cleaned = [v for v in (negative_embs or []) if v is not None]
        if cleaned:
            neg_emb_arr = np.asarray(cleaned, dtype=np.float32)
        else:
            neg_active = False

    # ── Taste-model setup ─────────────────────────────────────────────────
    # One DB read for the model, one (cached) library load for path→PC. The
    # walk step loop then just does a dot product per candidate.
    taste_active = taste_weight > 0.0
    taste_w: Optional[np.ndarray] = None
    taste_b: float = 0.0
    taste_pcs: dict[str, np.ndarray] = {}
    if taste_active:
        try:
            from utils import taste_model as _tm
            w_arr, b_val, ne, ni = await _load_taste_model(db_manager)
            if ne + ni > 0:
                taste_w = w_arr
                taste_b = b_val
                rows_all, percentile_matrix = await _load_percentile_matrix(
                    db_manager, FEATURES_VERSION,
                )
                for i, r in enumerate(rows_all):
                    taste_pcs[r["path"]] = percentile_matrix[i]
                logger.warning(
                    "walk: Taste model is ACTIVE. Personalized re-ranking enabled (weight=%.2f, ne=%d, ni=%d). w=%s, b=%.4f",
                    taste_weight, ne, ni, np.round(taste_w, 4).tolist(), taste_b
                )
            else:
                taste_active = False  # cold model, skip the per-step work
                logger.warning("walk: Taste model is COLD (0 feedback events). Personalized re-ranking skipped.")
        except Exception as exc:
            logger.warning("walk: Taste model loading failed (%s); skipping re-rank", exc)
            taste_active = False
    else:
        logger.warning("walk: Taste model is disabled (taste_weight=0). Purely acoustic/artist walk.")

    def _merge_candidates(raw: list[dict]) -> list[dict]:
        """De-duplicate on `path`, taking the maximum effective weight across
        tiers. A track that's both an acoustic and an artist neighbour should
        appear once with the stronger signal."""
        merged: dict[str, dict] = {}
        for c in raw:
            if c["path"] in visited:
                continue
            kind_w = weights_per_kind.get(c["edge_kind"], 0.0)
            eff = max(0.0, float(c["weight"])) * kind_w
            existing = merged.get(c["path"])
            if existing is None or eff > existing["_eff"]:
                merged[c["path"]] = {**c, "_eff": eff}
        return list(merged.values())

    current = seed_path
    teleport_target = teleport_path or seed_path
    for step in range(length):
        # MMR penalty decays by step index so the diversity term doesn't
        # snowball into a flat -diversity_lambda on every reasonable
        # candidate by step ~5 (visited_embs grows monotonically and the
        # max-cos converges to ~1). 1/(1+step) keeps step 0 at full
        # weight, halves it by step 1, and is ~λ/6 by step 5 — enough
        # to keep the walk anchored to the seed cluster late in a chain.
        diversity_lambda_eff = float(diversity_lambda) / (1.0 + step)
        # Restart roll. We always step from `current` afterwards, so toggling
        # current back to the teleport target implements the personalised-PageRank
        # teleport without special-casing the selection.
        if restart_prob > 0 and rng.random() < restart_prob:
            current = teleport_target

        raw = horizon.get(current)
        if raw is None:
            # The walker stepped to a node we didn't prefetch (rare with the
            # 2-hop horizon, but possible if restart sent us back to a seed
            # whose own neighbours we already exhausted). Fall back to a
            # single round-trip and cache the result.
            raw = await db_manager.get_neighbors_multi(current, kinds, k=40)
            horizon[current] = raw
            if cluster_active:
                if current not in cluster_map:
                    if hasattr(db_manager, "get_track_cluster"):
                        current_cid = await db_manager.get_track_cluster(current)
                    else:
                        current_cid = None
                    cluster_map[current] = current_cid
                for n in raw:
                    cid = n.get("cluster_id")
                    cluster_map[n["path"]] = int(cid) if cid is not None else None
            if need_candidate_embs:
                new_paths = [n["path"] for n in raw if n["path"] not in candidate_embs]
                if new_paths:
                    blobs = await db_manager.get_embeddings_for_paths(new_paths)
                    for p, blob in blobs.items():
                        v = _unpack_embedding(blob)
                        if v is not None:
                            candidate_embs[p] = v

        cands = _merge_candidates(raw)
        if not cands:
            # If we're stuck mid-walk but the teleport target still has fresh options,
            # one-shot restart and try again. If both are dry, we're done.
            if current != teleport_target:
                current = teleport_target
                cands = _merge_candidates(horizon.get(teleport_target, []))
            if not cands:
                break

        # Per-step exploration toggle: roll once for the whole candidate
        # pool. If we're "exploring," the taste term is dropped for this
        # step so the walk can surface tracks outside the user's known
        # preferences.
        step_taste_active = (
            taste_active
            and taste_w is not None
            and (taste_explore <= 0.0 or rng.random() >= taste_explore)
        )

        # Compute logits: effective weight, optionally penalised by the
        # max cosine to any already-visited node in the timbre sub-space,
        # then nudged by the global taste model when it has signal.
        logits = np.empty(len(cands), dtype=np.float32)
        for i, c in enumerate(cands):
            eff = float(c["_eff"])
            cand_emb: Optional[np.ndarray] = None
            if diversity_active or neg_active:
                cand_emb = candidate_embs.get(c["path"])
            if diversity_active and visited_embs and cand_emb is not None:
                sims = np.array(
                    [float(np.dot(cand_emb, v)) for v in visited_embs],
                    dtype=np.float32,
                )
                eff -= diversity_lambda_eff * float(sims.max())
            if neg_active and neg_emb_arr is not None and cand_emb is not None:
                # Max cosine to any session-rejected track. The candidate
                # embedding is already L2-normalised by `_unpack_embedding`,
                # so a plain dot product is the cosine.
                neg_sims = neg_emb_arr @ cand_emb
                eff -= float(negative_lambda) * float(neg_sims.max())
            if step_taste_active:
                pc = taste_pcs.get(c["path"])
                if pc is not None:
                    z = float(np.dot(taste_w, pc)) + taste_b
                    z = max(-30.0, min(30.0, z))
                    p_like = 1.0 / (1.0 + float(np.exp(-z)))
                    eff += float(taste_weight) * (p_like - 0.5)
            # Cluster constraint: penalise cross-cluster transitions.
            if cluster_active:
                cur_cid = cluster_map.get(current)
                cand_cid = cluster_map.get(c["path"])
                if (
                    cur_cid is not None
                    and cand_cid is not None
                    and cur_cid != cand_cid
                ):
                    eff *= (1.0 - cluster_lambda)
            logits[i] = eff

        # Softmax with temperature. Subtract max for numerical stability.
        if temperature <= 0:
            chosen_idx = int(np.argmax(logits))
        else:
            # Per-node logit standardisation. Raw effective weights are not
            # calibrated across source nodes — a hub track has neighbours
            # crowded near the top of its (0, 1] affinity range, an outlier
            # spreads thinly. Without rescaling, `temperature = 0.05` reads
            # as near-greedy at the hub but near-uniform at the outlier.
            # Z-scoring before the softmax normalises the spread so the
            # temperature has consistent semantics everywhere in the graph.
            sd = float(logits.std())
            if sd > 1e-9:
                z_logits = (logits - float(logits.mean())) / sd
            else:
                # Degenerate: all candidates have equal effective weight
                # (rare; happens when avoid set strips everything but
                # near-duplicates). Keep raw logits so argmax is well-defined.
                z_logits = logits - float(logits.mean())
            # Flat temperature: every step has the same low transition cost
            # so the walk stays acoustically close to the seed throughout.
            # The previous "Long-Flow Gentle-Reset" modulation (0.75× normal,
            # 1.5× every 6th step) introduced deliberate hot jumps that broke
            # similarity chains — removed per user intent.
            scaled = (z_logits - z_logits.max()) / float(temperature)
            probs = np.exp(scaled)
            total = float(probs.sum())
            if not np.isfinite(total) or total <= 0:
                chosen_idx = int(np.argmax(logits))
            else:
                probs = probs / total
                # Manual sampler so we keep the caller-supplied RNG.
                r = rng.random()
                acc = 0.0
                chosen_idx = len(cands) - 1
                for i, p in enumerate(probs):
                    acc += float(p)
                    if r <= acc:
                        chosen_idx = i
                        break

        chosen = cands[chosen_idx]
        next_path = chosen["path"]
        
        # Calculate the taste model prediction P(like) if active
        p_like = 0.5
        if taste_active and taste_w is not None:
            pc = taste_pcs.get(next_path)
            if pc is not None:
                z = float(np.dot(taste_w, pc)) + taste_b
                z = max(-30.0, min(30.0, z))
                p_like = 1.0 / (1.0 + float(np.exp(-z)))
        
        logger.debug(
            "walk step: Selected candidate '%s' (P(like)=%.2f%%, final logit=%.4f)",
            os.path.basename(next_path), p_like * 100.0, logits[chosen_idx]
        )
        
        path_seq.append(next_path)
        visited.add(next_path)
        if diversity_active:
            emb = candidate_embs.get(next_path)
            if emb is not None:
                visited_embs.append(emb)
        current = next_path

    return path_seq


# ── Mood (DSP-driven) ───────────────────────────────────────────────────────
#
# MOODS is the single source of truth for the assistant's mood vocabulary,
# scoring profiles, and harmonic preferences. Every alias maps to a single
# canonical MoodSpec; MOOD_PROFILES is a derived dict (kept for backwards
# compatibility with `mood in MOOD_PROFILES` callers) and MOOD_KEYWORDS in
# assistant_intent imports its alias list from here lazily at module load
# time so the regex stays in sync.
#
# To add a mood: drop one entry into MOODS. To add a synonym: append it to
# the matching spec's aliases. Nothing else needs to change.

from dataclasses import dataclass


_PROFILE_SCHEMA_VERSION = 2


def _normalize_profile(
    profile: dict[str, float | tuple[float, float]] | None,
) -> dict[str, tuple[float, float]]:
    """Coerce a mood profile dict to the v2 (target, weight) shape.

    v1 callers (and existing `mood_profiles` rows) stored only a target
    float; we promote those to weight=1.0 so the weighted-Euclidean scorer
    keeps its old behaviour for un-tuned features. Tuples pass through
    unchanged. Returns {} for None/empty input.
    """
    if not profile:
        return {}
    out: dict[str, tuple[float, float]] = {}
    for feat, val in profile.items():
        if isinstance(val, tuple) and len(val) == 2:
            out[feat] = (float(val[0]), float(val[1]))
        else:
            out[feat] = (float(val), 1.0)
    return out


@dataclass(frozen=True)
class MoodSpec:
    """Per-mood configuration.

    `camelot_pref` biases the *starting anchor* of the playlist sequencer
    toward a track in that mode ("major" / "minor"). None means no preference.

    `bpm_smooth_weight` boosts the BPM-continuity penalty during sequencing
    for moods where listeners notice tempo jumps more (slow / chill / ambient).
    """
    canonical: str
    aliases: tuple[str, ...] = ()
    camelot_pref: str | None = None
    bpm_smooth_weight: float = 1.0
    # Optional dense centroid in the timbre sub-space (mfcc_mean + mfcc_delta
    # + chroma). Currently always empty for built-ins; populated for custom
    # moods so both code paths share `score_tracks_by_mood`.
    centroid: tuple[float, ...] = ()
    profile: dict[str, tuple[float, float]] | None = None


# Per-mood profiles. Built-in defaults are completely disabled.
# This dictionary now defines vocabulary, aliases, and sequencer preferences.
MOODS: dict[str, MoodSpec] = {
    "chill": MoodSpec(
        "chill",
        aliases=(
            "chilled", "relaxed", "relaxing", "calm", "mellow", "ambient", "soft",
            "dreamy", "dream", "ethereal", "washed", "acoustic", "organic", "clean"
        ),
        camelot_pref="minor",
        bpm_smooth_weight=1.5,
    ),
    "dark": MoodSpec(
        "dark",
        aliases=("sad", "gloomy", "melancholy", "moody", "gothic", "shadow"),
        camelot_pref="minor",
        bpm_smooth_weight=1.0,
    ),
    "upbeat": MoodSpec(
        "upbeat",
        aliases=("happy", "bright", "cheerful", "party", "dance", "pop"),
        camelot_pref="major",
        bpm_smooth_weight=1.0,
    ),
    "rock": MoodSpec(
        "rock",
        aliases=("groovy", "altrock", "alt-rock", "softrock", "soft-rock", "classicrock", "indierock", "classic", "indie"),
        camelot_pref=None,
        bpm_smooth_weight=1.0,
    ),
    "beats": MoodSpec(
        "beats",
        aliases=("hip-hop", "trap", "rap", "urban", "beats", "lofi", "lo-fi"),
        camelot_pref="minor",
        bpm_smooth_weight=1.0,
    ),
    "intense": MoodSpec(
        "intense",
        aliases=("hard", "heavy", "powerful", "noisy", "aggressive", "metal", "rock", "fast", "quick", "driving", "hype", "pumped", "energetic"),
        camelot_pref="minor",
        bpm_smooth_weight=1.0,
    ),
}


# ── Built-in mood targets (community → mood mapping) ──────────────────────────
# Per-mood target profile in *percentile* space over the 8 raw scalar features.
# Used once at graph-build time to label each Louvain community with the mood(s)
# whose acoustic profile it best matches (many-to-many). NOT used per query —
# `tracks_by_mood` ranks members in the unified Zr geometry. Values lifted from
# the validated tests/test_cluster_mood_mapping.py prototype.
_MAP_FEATURES = (
    "bpm", "brightness", "energy", "rolloff",
    "beat_strength", "spectral_flatness", "spectral_contrast", "key_mode",
)

MOOD_TARGETS: dict[str, dict[str, float]] = {
    "chill":   {"bpm":0.25,"energy":0.20,"beat_strength":0.30,"brightness":0.30,"spectral_flatness":0.50,"spectral_contrast":0.30,"rolloff":0.30,"key_mode":0.20},
    "dark":    {"bpm":0.35,"energy":0.25,"beat_strength":0.40,"brightness":0.20,"spectral_flatness":0.40,"spectral_contrast":0.50,"rolloff":0.25,"key_mode":0.10},
    "upbeat":  {"bpm":0.75,"energy":0.80,"beat_strength":0.75,"brightness":0.80,"spectral_flatness":0.60,"spectral_contrast":0.60,"rolloff":0.75,"key_mode":0.85},
    "beats":   {"bpm":0.40,"energy":0.60,"beat_strength":0.85,"brightness":0.50,"spectral_flatness":0.50,"spectral_contrast":0.70,"rolloff":0.55,"key_mode":0.30},
    "intense": {"bpm":0.85,"energy":0.90,"beat_strength":0.80,"brightness":0.70,"spectral_flatness":0.40,"spectral_contrast":0.80,"rolloff":0.80,"key_mode":0.20},
    "rock":    {"bpm":0.60,"energy":0.70,"beat_strength":0.65,"brightness":0.60,"spectral_flatness":0.50,"spectral_contrast":0.60,"rolloff":0.60,"key_mode":0.50},
}

def _mood_percentile_ranks(rows: list[dict]) -> dict[str, dict[str, float]]:
    """Percentile rank (0..1) of every track per `_MAP_FEATURES`. key_mode is
    ranked on the binary major/minor flag derived from the Camelot ring."""
    N = len(rows)
    out: dict[str, dict[str, float]] = {}
    for f in _MAP_FEATURES:
        if f == "key_mode":
            vals_list = []
            for r in rows:
                cam = key_index_to_camelot(r.get("key_index", 0) or 0)
                vals_list.append(1.0 if (cam is not None and cam[1] == "B") else 0.0)
            vals = np.array(vals_list, dtype=np.float32)
        else:
            vals = np.array([float(r.get(f) or 0.0) for r in rows], dtype=np.float32)
        ranks = np.argsort(np.argsort(vals)) / max(1, N - 1)
        out[f] = {rows[i]["path"]: float(ranks[i]) for i in range(N)}
    return out


def mood_canonical(name: str) -> str | None:
    """Resolve any alias to its canonical mood name, or None if unknown.
    Custom moods (loaded from custom_moods.json) are also resolved here so
    callers can use one lookup regardless of mood origin."""
    if not name:
        return None
    cleaned = name.lower().strip()
    spec = MOODS.get(cleaned)
    if spec is not None:
        return spec.canonical
    for sp in MOODS.values():
        if cleaned in sp.aliases:
            return sp.canonical
    # Custom moods (dynamic vocabulary loaded from custom_moods.json) keep
    # their own name as the canonical form.
    if cleaned in load_custom_moods():
        return cleaned
    return None


def _mood_spec(name: str) -> MoodSpec | None:
    """Resolve to MoodSpec (built-in only). Custom moods are handled
    separately by `_custom_mood_spec`. Returns None for unknown names."""
    canonical = mood_canonical(name)
    if canonical is None:
        return None
    spec = MOODS.get(canonical)
    return spec


def _custom_mood_spec(name: str) -> MoodSpec | None:
    """Build an ephemeral MoodSpec for a custom mood. Custom moods may carry
    only a timbre centroid (legacy v1) or also a percentile profile (v2);
    both shapes are accepted so an upgrade path is unnecessary. v1 profile
    floats are promoted to weight=1.0 by `_normalize_profile`."""
    cleaned = name.lower().strip()
    cm = load_custom_moods().get(cleaned)
    if cm is None:
        return None
    centroid = tuple(cm.get("centroid") or ())
    profile = _normalize_profile(cm.get("profile") or {})
    return MoodSpec(
        canonical=cleaned,
        profile=profile,
        centroid=centroid,
    )


# Derived view: every alias and every canonical name maps to its profile
# dict. Preserves the `mood in MOOD_PROFILES` and `MOOD_PROFILES.keys()`
# API for the existing call sites; do not write to this directly.
MOOD_PROFILES: dict[str, dict[str, tuple[float, float]]] = {}
for _spec in MOODS.values():
    _prof = _spec.profile if _spec.profile is not None else {"PC1": (0.0, 1.0), "PC2": (0.0, 1.0), "PC3": (0.0, 1.0)}
    MOOD_PROFILES[_spec.canonical] = _prof
    for _alias in _spec.aliases:
        MOOD_PROFILES[_alias] = _prof
del _spec


# Public alias list, importable by assistant_intent without pulling MoodSpec.
MOOD_KEYWORDS: tuple[str, ...] = tuple(MOOD_PROFILES.keys())


# Cache for the projected Zr coordinate matrix (avoids re-reading/projecting on
# every mood/islet query). Invalidated by build_acoustic_edges / invalidate_mood_cache.
_PERCENTILE_CACHE: dict[tuple, tuple] = {}


async def _load_percentile_matrix(
    db_manager,
    features_version: int,
) -> tuple[list[dict], np.ndarray]:
    """Returns (rows, Zr_matrix) in the unified graph geometry.

    Reads the per-track Zr coords persisted by `build_acoustic_edges`; any track
    missing cached coords is projected on demand via the stored projection
    (`project_to_zr`). Returns an empty-width matrix when no projection exists
    yet (e.g. before the first build). Cached on a cheap library-change key.
    """
    rows = await db_manager.get_tracks_with_features(features_version)
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)

    sentinel = max(r["path"] for r in rows)
    key = (features_version, len(rows), sentinel)
    cached = _PERCENTILE_CACHE.get(key)
    if cached is not None:
        return cached

    proj = await db_manager.load_pca_space()
    coords_rows = await db_manager.get_tracks_pca_coords()
    coords_map = {
        r["path"]: r["pca_coords"]
        for r in coords_rows if r.get("pca_coords")
    }

    dim = None
    if proj and proj.get("projection") is not None:
        dim = int(proj["projection"].shape[1])
    elif coords_map:
        dim = len(next(iter(coords_map.values())))
    if not dim:
        # No projection yet (pre-first-build / feature-less rows). Cache the
        # empty result too so repeated queries are cheap; build_acoustic_edges
        # clears this cache once the geometry exists.
        result = (rows, np.zeros((len(rows), 0), dtype=np.float32))
        _PERCENTILE_CACHE.clear()
        _PERCENTILE_CACHE[key] = result
        return result

    matrix = np.zeros((len(rows), dim), dtype=np.float32)
    to_cache: list[tuple[str, np.ndarray]] = []
    for idx, r in enumerate(rows):
        c = coords_map.get(r["path"])
        if c is not None and len(c) == dim:
            matrix[idx, :] = np.asarray(c, dtype=np.float32)
            continue
        z = project_to_zr(r, proj) if proj else None
        if z is not None and z.shape[0] == dim:
            matrix[idx, :] = z
            to_cache.append((r["path"], z))

    if to_cache and hasattr(db_manager, "update_tracks_pca_coords_batch"):
        await db_manager.update_tracks_pca_coords_batch(to_cache)

    _PERCENTILE_CACHE.clear()
    _PERCENTILE_CACHE[key] = (rows, matrix)
    logger.info("track_graph: Zr coord matrix cache rebuilt (N=%d, dim=%d)", len(rows), dim)
    return rows, matrix


# ── Mood partition + EQ (replaces the per-mood logistic regressor path) ─────
#
# A mood is now defined by:
#   * a *centroid* in PC space — the mean of the tracks the user has
#     assigned to this mood (or the hand-tuned seed in MOODS for empty
#     partitions);
#   * a per-feature *EQ weight* — multiplies the Euclidean distance along
#     that axis. The user adjusts these from the UI; the regressor that
#     used to nudge them is gone.
#
# Storage reuses `mood_profiles`: one row per (mood, PC) with
# `target = centroid_coord` and `weight = eq_slider`. Three rows per mood.


async def assign_track_to_mood(db_manager, track_path: str, mood: str) -> None:
    """Pin a track to a mood. Many-to-many: a track may be pinned to several
    moods, and a pin adds the track to that mood's subset on top of the
    community-derived members."""
    canonical = mood_canonical(mood) or mood.lower().strip()
    await db_manager.assign_track_to_mood(track_path, canonical)


async def unassign_track_from_mood(db_manager, track_path: str, mood: str | None = None) -> None:
    """Remove a track's pin. With `mood` given, drop only that mood's pin
    (leaving any other moods); without it, drop the track from all moods."""
    if mood:
        canonical = mood_canonical(mood) or mood.lower().strip()
        await db_manager.unassign_track_from_mood(track_path, canonical)
    else:
        await db_manager.unassign_track(track_path)


async def tracks_in_partition(db_manager, mood: str) -> list[str]:
    """Track paths currently assigned to `mood`. Resolves aliases first."""
    canonical = mood_canonical(mood) or mood.lower().strip()
    return await db_manager.get_tracks_in_mood(canonical)


def _get_default_quartiles(mood: str) -> dict[str, tuple[float, float]]:
    defaults = {
        "chill": {
            "bpm": (1.0, 1.0),      # Very Low
            "energy": (1.0, 1.0),   # Very Low
            "spectral_flatness": (4.0, 1.0), # Very High
        },
        "dark": {
            "energy": (1.0, 1.0),   # Very Low
            "spectral_flatness": (1.0, 1.0), # Very Low
        },
        "upbeat": {
            "bpm": (4.0, 1.0),      # Very High
            "energy": (4.0, 1.0),   # Very High
            "beat_strength": (4.0, 1.0), # Very High
        },
        "rock": {
            "bpm": (3.0, 1.0),      # High
            "brightness": (4.0, 1.0), # Very High
        },
        "beats": {
            "beat_strength": (4.0, 1.0), # Very High
        },
        "intense": {
            "energy": (4.0, 1.0),   # Very High
            "beat_strength": (4.0, 1.0), # Very High
        }
    }
    res = {}
    raw_features = ["bpm", "brightness", "energy", "rolloff", "beat_strength", "spectral_flatness", "spectral_contrast", "key_mode"]
    mood_def = defaults.get(mood, {})
    for f in raw_features:
        res[f] = mood_def.get(f, (0.0, 0.0))
    return res


# EQ <-> percentile conversion. The EQ exposes a 1–4 "band" per feature
# (1=Very Low … 4=Very High; 0=Any/Neutral → feature ignored). MOOD_TARGETS is
# in continuous percentile space, so the two map back and forth here.
def _quartile_to_pct(q: float) -> float:
    """1–4 EQ band → its centre percentile in [0,1] (0.125/0.375/0.625/0.875)."""
    return (float(q) - 0.5) / 4.0


def _pct_to_quartile(p: float) -> int:
    """[0,1] percentile target → nearest 1–4 EQ band for the slider's initial
    position (so an un-tuned mood shows its out-of-box MOOD_TARGETS profile)."""
    return int(min(4, max(1, int(float(p) * 4) + 1)))


async def set_mood_eq(
    db_manager,
    mood: str,
    eq_weights: dict[str, float],
) -> None:
    """Persist a user EQ adjustment for `mood`. Each feature carries a 1–4 band
    (0 ⇒ ignore that feature). Stored as (band, weight); `tracks_by_mood` then
    uses this adjusted profile in place of the out-of-box MOOD_TARGETS."""
    canonical = mood_canonical(mood) or mood.lower().strip()
    new_profile: dict[str, tuple[float, float]] = {}
    for f, val in eq_weights.items():
        quartile = float(val)
        weight = 1.0 if quartile > 0.0 else 0.0
        new_profile[f] = (quartile, weight)
    await db_manager.save_adjusted_mood_profile(canonical, new_profile)


async def get_mood_definition(
    db_manager,
    mood: str,
) -> dict[str, tuple[float, float]] | None:
    """The EQ profile to render for `mood`, as {feature: (band 1–4, weight)}.

    Precedence:
      1. A saved adjusted profile (the user has tuned this mood) → return it.
      2. Otherwise the out-of-box MOOD_TARGETS, converted to 1–4 bands, so the
         dialog opens on the optimised defaults the user can then nudge.
    """
    canonical = mood_canonical(mood)
    if canonical is None:
        return None
    stored = await db_manager.get_adjusted_mood_profile(canonical)
    if stored is not None:
        return stored
    base = MOOD_TARGETS.get(canonical)
    if not base:
        return {}
    return {f: (float(_pct_to_quartile(p)), 1.0) for f, p in base.items()}


# ── User-taste model wiring ─────────────────────────────────────────────────
#
# DEPRECATED / DISCONNECTED. The taste model lived in a 3-D PCA basis that no
# longer matches the unified 20-D graph Zr geometry, so its live training and
# re-rank hooks are inert. `taste_model.py` and this wiring are retained for
# revival — flip `_TASTE_ENABLED` to True (and re-fit the model in the current
# geometry) to re-enable. While disabled, the feedback hooks no-op and
# `taste_scores_for_matrix` reports "no signal" so callers fall back cleanly.

_TASTE_ENABLED = False

_TASTE_CACHE: tuple[np.ndarray, float, int, int] | None = None


def invalidate_taste_cache() -> None:
    """Drop the in-memory taste model. Call after every persist."""
    global _TASTE_CACHE
    _TASTE_CACHE = None


async def _load_taste_model(
    db_manager,
    features_version: int = FEATURES_VERSION,
) -> tuple[np.ndarray, float, int, int]:
    """Return (weights, bias, n_explicit, n_implicit). Fresh zeros on a cold
    start so callers can always score without a branch."""
    global _TASTE_CACHE
    if _TASTE_CACHE is not None:
        return _TASTE_CACHE
    from utils import taste_model as _tm
    row = await db_manager.get_taste_model(features_version)
    if row is None:
        w, b = _tm.fresh()
        _TASTE_CACHE = (w, b, 0, 0)
        logger.warning("taste_model: Loaded COLD model (0 samples, default w=zeros, b=0.0)")
    else:
        try:
            w = _tm.unpack_weights(row[0])
            b = float(row[1])
            _TASTE_CACHE = (w, b, int(row[2]), int(row[3]))
            logger.warning(
                "taste_model: Loaded trained model. Samples (explicit=%d, implicit=%d) -> w=%s, b=%.4f",
                int(row[2]), int(row[3]), np.round(w, 4).tolist(), b
            )
        except ValueError as exc:
            logger.warning("taste_model: weights blob malformed (%s); resetting", exc)
            w, b = _tm.fresh()
            _TASTE_CACHE = (w, b, 0, 0)
    return _TASTE_CACHE


async def record_explicit_feedback(
    db_manager,
    track_path: str,
    like: bool,
    features_version: int = FEATURES_VERSION,
) -> None:
    """Train the taste model on one like (`True`) or dislike (`False`) event.
    Looks up the track's cached PC coords; no-op if the track hasn't been
    analysed yet (a label we can't ground in features is signal we can't use)."""
    if not _TASTE_ENABLED:
        return  # taste model deprecated/disconnected — see _TASTE_ENABLED
    from utils import taste_model as _tm
    pcs = await _get_track_pcs(db_manager, track_path, features_version)
    if pcs is None:
        logger.warning("taste_model: feedback ignored; track '%s' lacks analyzed features.", os.path.basename(track_path))
        return
    w, b, ne, ni = await _load_taste_model(db_manager, features_version)
    y = 1 if like else 0
    w, b = _tm.online_update(
        pcs, y, w, b, sample_weight=_tm.WEIGHT_EXPLICIT, n_samples=ne + ni,
    )
    await db_manager.save_taste_model(
        _tm.pack_weights(w), b, ne + 1, ni, features_version,
    )
    logger.warning(
        "taste_model: SGD EXPLICIT feedback trained. track='%s' like=%s. Samples (explicit=%d, implicit=%d) -> w=%s, b=%.4f",
        os.path.basename(track_path), like, ne + 1, ni, np.round(w, 4).tolist(), b
    )
    invalidate_taste_cache()


async def record_play_event(
    db_manager,
    track_path: str,
    played_seconds: float,
    duration_seconds: float,
    features_version: int = FEATURES_VERSION,
) -> None:
    """Classify a playback event via `taste_model.classify_play_event` and
    feed it into the taste model with implicit-sample weight. Returns silently
    when the event is too short to be informative."""
    if not _TASTE_ENABLED:
        return  # taste model deprecated/disconnected — see _TASTE_ENABLED
    from utils import taste_model as _tm
    y = _tm.classify_play_event(float(played_seconds), float(duration_seconds))
    if y is None:
        logger.warning(
            "taste_model: Play event too short to interpret (played %.1fs / dur %.1fs). Discarding sample.",
            played_seconds, duration_seconds
        )
        return
    pcs = await _get_track_pcs(db_manager, track_path, features_version)
    if pcs is None:
        logger.warning("taste_model: play event ignored; track '%s' lacks analyzed features.", os.path.basename(track_path))
        return
    w, b, ne, ni = await _load_taste_model(db_manager, features_version)
    w, b = _tm.online_update(
        pcs, y, w, b, sample_weight=_tm.WEIGHT_IMPLICIT, n_samples=ne + ni,
    )
    await db_manager.save_taste_model(
        _tm.pack_weights(w), b, ne, ni + 1, features_version,
    )
    logger.warning(
        "taste_model: SGD IMPLICIT play event trained (y=%d). track='%s' (played %.1fs / dur %.1fs). Samples (explicit=%d, implicit=%d) -> w=%s, b=%.4f",
        y, os.path.basename(track_path), played_seconds, duration_seconds, ne, ni + 1, np.round(w, 4).tolist(), b
    )
    invalidate_taste_cache()


async def _get_track_pcs(
    db_manager,
    track_path: str,
    features_version: int,
) -> np.ndarray | None:
    """Pull a single track's cached PC vector. Returns None when the track
    has no analysed features yet — callers skip rather than zero-fill so the
    taste model doesn't learn off the origin."""
    rows, percentiles = await _load_percentile_matrix(db_manager, features_version)
    for i, r in enumerate(rows):
        if r.get("path") == track_path:
            return percentiles[i].astype(np.float32, copy=True)
    return None


async def taste_scores_for_matrix(
    db_manager,
    percentiles: np.ndarray,
    features_version: int = FEATURES_VERSION,
) -> tuple[np.ndarray, bool]:
    """Score every row of `percentiles` under the current taste model.
    Returns (scores, has_signal): `has_signal=False` means the model is
    cold (zero events seen) and callers should ignore the scores entirely
    rather than re-ranking by σ ≈ 0.5 noise."""
    if not _TASTE_ENABLED:
        # Deprecated/disconnected — report no signal so callers skip the re-rank.
        return np.full(percentiles.shape[0], 0.5, dtype=np.float32), False
    from utils import taste_model as _tm
    w, b, ne, ni = await _load_taste_model(db_manager, features_version)
    if ne + ni == 0:
        return np.full(percentiles.shape[0], 0.5, dtype=np.float32), False
    return _tm.score(percentiles, w, b).astype(np.float32), True


def score_tracks_for_repartition(
    rows: list[dict],
    adjusted_profiles: dict[str, dict[str, tuple[float, float]]],
) -> dict[str, np.ndarray]:
    """Helper used during partition recalculation. Returns a dict mapping
    mood -> np.ndarray of quartile-distance scores for all tracks in rows.
    """
    raw_features = ["bpm", "brightness", "energy", "rolloff", "beat_strength", "spectral_flatness", "spectral_contrast", "key_mode"]
    N = len(rows)
    if N == 0:
        return {m: np.zeros(0, dtype=np.float32) for m in adjusted_profiles}

    # Compute raw feature percentile ranks in memory
    track_ranks = {}
    for f in raw_features:
        vals = np.array([float(r.get(f) or 0.0) for r in rows], dtype=np.float32)
        ranks = np.argsort(np.argsort(vals)) / max(1, N - 1)
        track_ranks[f] = {rows[idx]["path"]: float(ranks[idx]) for idx in range(N)}

    out = {}
    for mood, profile in adjusted_profiles.items():
        scores = np.zeros(N, dtype=np.float32)
        for idx, r in enumerate(rows):
            path = r["path"]
            dist_sq_sum = 0.0
            active_count = 0
            for f in raw_features:
                target, weight = profile.get(f, (0.0, 0.0))
                if weight > 0.0 and target > 0.0:
                    pct = track_ranks[f].get(path, 0.5)
                    low_bound = (target - 1) * 0.25
                    high_bound = target * 0.25
                    if pct < low_bound:
                        d = low_bound - pct
                    elif pct > high_bound:
                        d = pct - high_bound
                    else:
                        d = 0.0
                    dist_sq_sum += d * d
                    active_count += 1
            if active_count > 0:
                scores[idx] = -np.sqrt(dist_sq_sum / active_count)
            else:
                scores[idx] = 0.0
        out[mood] = scores
    return out


async def tracks_by_mood(
    db_manager,
    mood: str,
    limit: int = 12,
    features_version: int = FEATURES_VERSION,
) -> list[dict]:
    """Tracks for a built-in mood, scored by how well each track's scalar-feature
    percentiles match the mood's target profile.

    Energy/tempo-defined moods (chill/dark/intense/…) live in the *scalar*
    percentile space, not the timbre-dominated graph Zr — ranking moods in Zr
    collapses unlike moods together (chill≡dark). Targets come from the out-of-box
    `MOOD_TARGETS` (restricted to the features that survived the graph's
    covariance cleaving, one shared feature set), unless the user has tuned the
    mood via the EQ — a saved adjusted profile then overrides per feature (see
    `set_mood_eq`/`get_mood_definition`). Custom moods (islets) are acoustic
    neighbourhoods and stay in Zr via `tracks_in_islet`.

    Many-to-many: a track can rank into several moods (no exclusivity). User
    pins (`assign_track_to_mood`) are floated to the top; tracks disliked in the
    mood (`mood_feedback < 0`) are excluded. Returns the top-`limit` rows.
    """
    canonical = mood_canonical(mood)
    if canonical is None:
        return []
    if canonical not in MOODS:
        return await tracks_in_islet(db_manager, canonical, features_version)

    rows = await db_manager.get_tracks_with_features(features_version)
    if not rows:
        return []
    base = MOOD_TARGETS.get(canonical)
    if not base:
        return []

    # Effective per-feature percentile targets:
    #   • a saved EQ adjustment (the user tuned this mood) overrides per feature
    #     — a 1–4 band → band-centre percentile; band/weight 0 drops the feature;
    #   • otherwise the out-of-box MOOD_TARGETS, restricted to the features that
    #     survived the graph's covariance cleaving (one shared feature set).
    adjusted = None
    if hasattr(db_manager, "get_adjusted_mood_profile"):
        try:
            adjusted = await db_manager.get_adjusted_mood_profile(canonical)
        except Exception:
            adjusted = None

    eff: dict[str, float] = {}
    if adjusted:
        for f, tw in adjusted.items():
            if f not in _MAP_FEATURES:
                continue  # only rankable scalar features (skip stale PC* rows)
            try:
                qval, weight = tw
            except (TypeError, ValueError):
                continue
            if weight > 0 and qval > 0:
                eff[f] = _quartile_to_pct(qval)
    if not eff:
        proj = await db_manager.load_pca_space() if hasattr(db_manager, "load_pca_space") else None
        surviving = set(proj.get("surviving") or []) if proj else None
        eff = {f: base[f] for f in base if (surviving is None or f in surviving)}
        if not eff:
            eff = dict(base)

    ranks = _mood_percentile_ranks(rows)  # {feat: {path: percentile}}

    try:
        fb = await db_manager.get_mood_feedback()
    except Exception:
        fb = {}
    excluded = {p for p, mds in (fb or {}).items() if mds.get(canonical, 0) < 0}
    pins = set(await db_manager.get_tracks_in_mood(canonical))

    feats = list(eff.keys())
    scored: list[tuple[float, dict]] = []
    inv_n = 1.0 / len(feats)
    for r in rows:
        p = r["path"]
        if p in excluded:
            continue
        d2 = 0.0
        for f in feats:
            diff = ranks[f][p] - eff[f]
            d2 += diff * diff
        score = -float(np.sqrt(d2 * inv_n))
        if p in pins:
            score += 10.0  # a pin always belongs, regardless of profile
        scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


async def adjust_mood_profile(
    db_manager,
    mood: str,
    track_path: str,
    feedback: int,
):
    """Like/dislike a track within a built-in mood (many-to-many).

      * `feedback == +1` (like in mood X) → pin the track to X and clear any
        prior dislike exclusion for X.
      * `feedback == -1` (dislike in mood X) → remove X's pin and record a
        per-mood exclusion so the track is dropped from X's subset.
      * any other value → no-op.

    The taste model is deprecated/disconnected, so no taste event is recorded.
    `tracks_by_mood` reflects the change on its next call.
    """
    canonical = mood_canonical(mood)
    if canonical is None:
        logger.warning("adjust_mood_profile: Unknown mood %s", mood)
        return
    if feedback == 1:
        await assign_track_to_mood(db_manager, track_path, canonical)
        if hasattr(db_manager, "save_mood_feedback"):
            await db_manager.save_mood_feedback(track_path, canonical, 1)
    elif feedback == -1:
        await unassign_track_from_mood(db_manager, track_path, canonical)
        if hasattr(db_manager, "save_mood_feedback"):
            await db_manager.save_mood_feedback(track_path, canonical, -1)
    else:
        return
    logger.info(
        "adjust_mood_profile: mood='%s' track='%s' feedback=%d",
        canonical, track_path, feedback,
    )


# Upper bound on per-feature weight so a long like-streak can't push one
# feature's contribution to infinity. 5× the default weight ≈ "this feature
# completely dominates the metric"; beyond that you've overfit one track.
_MAX_FEATURE_WEIGHT = 5.0


async def tracks_in_islet(
    db_manager,
    name: str,
    features_version: int = FEATURES_VERSION,
    min_count: int = ISLET_MIN,
) -> list[dict]:
    """Members of a named islet, ranked by similarity to its exemplar in the
    unified graph Zr space.

    Loads the islet's `custom_moods.json` entry → reads `exemplar_path`,
    `threshold`, `blacklist` → projects every analysed track into Zr (the same
    geometry as the walk and Louvain communities) → scores each by a
    self-tuning Gaussian affinity to the exemplar's Zr position → returns rows
    with affinity ≥ threshold, ranked descending, capped at ISLET_MAX, minus
    the blacklist.

    Replaces the legacy raw-timbre cosine path so "play similar" and an islet
    seeded on the same track now agree (one geometry). The threshold is an
    affinity in [0,1] (1 = the exemplar itself), not a raw-timbre cosine.

    Returns [] if fewer than `min_count` tracks pass — a non-generalising
    exemplar produces no playable queue. Pass `min_count=0` when the caller
    wants honest below-floor membership (e.g. the Library view, which still
    needs to render the accordion so the user can loosen the threshold).
    """
    cleaned = name.lower().strip()
    cm = load_custom_moods().get(cleaned)
    if cm is None:
        return []
    threshold = float(cm.get("threshold", ISLET_THRESHOLD))
    blacklist = set(cm.get("blacklist") or [])
    exemplar_path = cm.get("exemplar_path")
    if not exemplar_path:
        return []

    rows, Zr = await _load_percentile_matrix(db_manager, features_version)
    if not rows or Zr.shape[1] == 0:
        return []

    # The islet centroid is the exemplar's position in the unified Zr space.
    centroid = None
    for i, r in enumerate(rows):
        if r.get("path") == exemplar_path:
            centroid = Zr[i]
            break
    if centroid is None:
        logger.info(
            "tracks_in_islet: exemplar '%s' for islet '%s' has no analysed "
            "features; cannot score.", exemplar_path, cleaned,
        )
        return []

    # Squared Euclidean distance from every track to the exemplar in Zr.
    diff = Zr - centroid[None, :]
    d2 = np.einsum("ij,ij->i", diff, diff)
    # Self-tuning bandwidth σ² = exemplar's distance² to its LOCAL_K-th nearest
    # track (index 0 is the exemplar itself), so the threshold adapts to the
    # exemplar's local density — the same principle as the graph affinity.
    ISLET_LOCAL_K = 7
    order_d = np.sort(d2)
    pivot = order_d[min(ISLET_LOCAL_K, len(order_d) - 1)] if len(order_d) > 1 else 0.0
    sigma2 = max(float(pivot), 1e-6)
    affinity = np.exp(-d2 / sigma2)

    # Prune blacklisted tracks from membership (kept in the σ density estimate).
    if blacklist:
        for i, r in enumerate(rows):
            if r.get("path") in blacklist:
                affinity[i] = -1.0

    member_idx = np.where(affinity >= threshold)[0]
    if len(member_idx) < min_count:
        return []
    ordered = member_idx[np.argsort(-affinity[member_idx])][:ISLET_MAX]
    return [rows[int(i)] for i in ordered]


async def record_islet_negative(
    db_manager,
    islet_name: str,
    track_path: str,
    features_version: int = FEATURES_VERSION,
) -> bool:
    """Mark a track as unwanted in an islet by adding it to the islet's
    blacklist in `custom_moods.json`. Thin wrapper around
    `blacklist_track_from_islet` — kept as a named entrypoint because
    callers refer to the action as "record a negative" even though there's
    no longer any model being updated.

    `db_manager` and `features_version` are accepted for backwards
    compatibility with existing call sites but are unused.

    Returns True if the path was added (or was already present), False if
    the islet doesn't exist or the JSON write failed.
    """
    del db_manager, features_version  # unused; signature kept for compat
    return blacklist_track_from_islet(islet_name, track_path)


def invalidate_mood_cache() -> None:
    """Clear the percentile cache. Call after `bulk_analyze_library` so the
    next mood query sees the freshly-analysed rows. Cheap; only used after
    library-wide mutations."""
    _PERCENTILE_CACHE.clear()



async def bulk_analyze_library(
    db_manager,
    audio_service,
    progress_cb=None,
    cancel_check=None,
    features_version: int = FEATURES_VERSION,
) -> dict:
    """Analyse every track lacking current-version features and persist the
    extracted descriptors.

    Per-track cost on a modern phone is ~3–6 s (90 s decode @ hardware codec
    + numpy DSP). For a 5K-track fresh library this is ~5 h of CPU — caller
    is responsible for surfacing that to the user. `progress_cb(done, total,
    current_path, failures)` is invoked after every track, sync or async.
    `cancel_check()` is polled before each track so the caller can abort
    early without leaving an orphaned analyser running.

    Returns {analysed, failed, total} counts.
    """
    missing = await db_manager.get_tracks_missing_features(features_version)
    total = len(missing)
    if total == 0:
        return {"analysed": 0, "failed": 0, "total": 0}

    analysed = 0
    failures = 0

    async def _emit(done: int, current: str) -> None:
        if not progress_cb:
            return
        try:
            res = progress_cb(done, total, current, failures)
            if hasattr(res, "__await__"):
                await res
        except Exception as ex:
            logger.warning("track_graph: progress_cb raised %s", ex)

    for i, path in enumerate(missing, 1):
        if cancel_check is not None:
            try:
                if cancel_check():
                    break
            except Exception:
                pass

        try:
            features = await analyze_track(audio_service, path)
        except Exception as ex:
            failures += 1
            logger.warning("track_graph: analyse failed for %s: %s", path, ex)
            await _emit(i, path)
            continue

        try:
            await db_manager.update_track_features(
                path,
                features.bpm, features.energy, features.brightness,
                features.rolloff, features.beat_strength,
                features.spectral_flatness, features.spectral_contrast,
                features.key_index,
                features.timbre_blob(), features_version,
            )
            analysed += 1
        except Exception as ex:
            failures += 1
            logger.warning("track_graph: persist failed for %s: %s", path, ex)

        await _emit(i, path)

    # New features means the percentile distribution shifted; flush the
    # mood-scoring cache so the next query rebuilds against the full set.
    if analysed > 0:
        invalidate_mood_cache()

    return {"analysed": analysed, "failed": failures, "total": total}


async def graph_status(db_manager) -> dict:
    """Compact summary used by the assistant's first-open initialisation flow
    and the 'graph health' Settings panel (future). Returns counts per kind
    and a coverage estimate."""
    total_tracks = await db_manager.get_total_tracks()
    return {
        "total_tracks": total_tracks,
        "acoustic_edges": await db_manager.count_neighbors(KIND_ACOUSTIC),
        "artist_edges":   await db_manager.count_neighbors(KIND_ARTIST),
        "album_edges":    await db_manager.count_neighbors(KIND_ALBUM),
    }
