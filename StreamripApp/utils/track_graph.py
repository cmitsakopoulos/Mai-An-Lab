"""
Track graph: sparse k-NN adjacency over the music library.

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
something similar' to a seed-anchored trajectory walk over the acoustic graph
(see `walk`), and 'more by this artist' to artist neighbours. Provides the
continuous proximity the assistant needs instead of discrete buckets.

The walk (`walk`, "Seed-Anchored Smooth Flow"):
  • deterministic greedy trajectory — at each step pick the unvisited acoustic
    neighbour maximising 0.7·Sim(current) + 0.3·Sim(seed), so the queue flows
    forward while staying anchored to the seed (no random teleports);
  • a multiplicative metadata factor (genre-NPMI + shared-country, `_meta_score`)
    and a soft cross-community penalty, so the trajectory stays inside the seed's
    genre/community instead of riding a timbre bridge into a foreign one;
  • graceful degradation — with no enrichment / cluster labels the factors
    collapse to 1.0 and it's the pure acoustic dual-similarity flow.

(A stochastic Personalised-PageRank walker previously lived here; it was removed
in favour of this single, metadata-aware walker.)

All builders are async (DB-bound) but the numpy work runs synchronously —
no off-thread call is necessary for libraries up to ~20K tracks; Euclidean kNN
over the PCA-reduced vectors is bandwidth-limited and finishes in under a second.
For very large libraries the caller should wrap build_acoustic_edges in
asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import logging
import random
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
    persisted global Zr geometry is left untouched so project_to_zr is unaffected.
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


def _knn_edges(
    Zr: np.ndarray,
    paths: list[str],
    k: int,
    csls_beta: float,
) -> tuple[list[tuple[str, str, float]], list[tuple[int, int, float]], int, int, int]:
    """Self-tuning-affinity kNN + strict mutual-kNN pruning over the persisted
    Zr geometry.

    Pure numpy/Python (no DB) so `build_acoustic_edges` can run it via
    asyncio.to_thread off the event loop — the big `@` products release the GIL
    and the chunked passes never freeze the UI's tiny neighbour queries.
    Returns (edges, mutual_pairs, N, k_eff, mutual_total).
    """
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

    return edges, mutual_pairs, N, k_eff, mutual_total


def _render_pca_report(
    rows: list[dict],
    paths: list[str],
    Zr: np.ndarray,
    cluster_labels: np.ndarray,
) -> None:
    """Regenerate the on-device PCA / genre "mathematical truth" report.

    matplotlib figure rendering plus disk I/O — strictly CPU/IO-bound, so the
    async builder runs it via asyncio.to_thread (matplotlib uses the headless
    'Agg' backend, safe off the main thread). Caller swallows failures.
    """
    from utils.pca_engine import plot_pca_report, plot_genre_report
    from utils.streamrip_api import load_config

    cfg = load_config()
    library_folder = (cfg.get("downloads") or {}).get("folder") or ""
    library_folder = str(library_folder).strip()

    if library_folder and os.path.isdir(library_folder):
        report_dir = os.path.join(library_folder, "pca_report")
    else:
        report_dir = os.path.join(APP_DIR, "pca_report")

    saved = plot_pca_report(rows, report_dir, cluster_labels=cluster_labels)
    # Genre-coloured view of the REAL Zr geometry (aligned to paths).
    path_to_genre = {r["path"]: r.get("genre") for r in rows}
    genres_aligned = [path_to_genre.get(p) for p in paths]
    saved += plot_genre_report(
        paths, Zr, genres_aligned, report_dir, cluster_labels=cluster_labels,
    )
    if saved:
        logger.info(
            "track_graph: PCA cluster report updated in %s  (%d figures)",
            report_dir, len(saved),
        )


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

    # ── Pre-Network Metadata Enrichment & Genre Normalization Phase ──────────
    # Enrich artist country provenance & canonical genres BEFORE unsupervised
    # PCA graph embedding generation & metadata fusion gates.
    try:
        from utils.metadata_enrich import enrich_library
        await enrich_library(db_manager, with_genres=True)
    except Exception as exc:
        logger.debug("Pre-network metadata enrichment skipped: %s", exc)

    # ── Feature selection: drop covariance-redundant scalars ──────────────
    # The graph — and the Louvain communities + similarity walk built on it —
    # uses every feature that survives the unsupervised PCA / Pearson-covariance
    # analysis. Collinear scalars (e.g. rolloff ↔ brightness) are cleaved so
    # they don't double-count toward distance. The 68-D graph embedding timbre block and the
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
    # the PCA entirely: the SVD denoises only the ~75-D timbre+dynamics, and
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
        # SVD is the single heaviest op in the build; run it off the loop
        # (LAPACK releases the GIL) so it can't freeze the UI.
        _U, _S, _Vt = await asyncio.to_thread(
            np.linalg.svd, Z_cont, full_matrices=False,
        )
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
    # Single source of the graph's Zr space: any on-demand
    # projection of new tracks reads it back via load_pca_space() /
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
            logger.info(
                "track_graph: persisted Zr geometry (%d tracks × %d dims)",
                Zr.shape[0], Zr.shape[1],
            )
        except Exception as exc:
            logger.warning("track_graph: persisting Zr geometry failed: %s", exc)

    # Self-tuning-affinity kNN + strict mutual-kNN pruning. Offloaded: the
    # chunked N×N distance passes are the second-heaviest part of the build.
    edges, mutual_pairs, N, k_eff, mutual_total = await asyncio.to_thread(
        _knn_edges, Zr, paths, k, csls_beta,
    )

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
            cluster_labels = await asyncio.to_thread(
                _louvain, N, mutual_pairs, cluster_resolution,
            )
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
                        refine_labels = await asyncio.to_thread(
                            _louvain, N, mutual_pairs, refine_resolution,
                        )
                        logger.info("track_graph: ran second Louvain (resolution=%.2f) to seed refine partition", refine_resolution)
                    else:
                        refine_labels = cluster_labels
                    intra, n_ref = await asyncio.to_thread(
                        _local_refine_edges,
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
            # matplotlib + disk I/O — offloaded so figure rendering never
            # blocks the event loop.
            try:
                await asyncio.to_thread(
                    _render_pca_report, rows, paths, Zr, cluster_labels,
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

    # ── Genre-similarity model (NPMI 'genre-BLOSUM') ───────────────────────
    # Precompute + persist from the artist enrichment cache so the walk's
    # metadata gate loads it instead of rebuilding it each session. Non-fatal:
    # no enrichment → empty model → the walk's genre term degrades to Dice.
    try:
        await build_genre_affinity(db_manager)
    except Exception as gerr:
        logger.warning("track_graph: genre affinity build skipped (%s)", gerr)

    return len(edges)


async def build_genre_affinity(db_manager) -> int:
    """(Re)build + persist the NPMI genre-similarity model from the artist
    enrichment cache, so the walk's metadata gate loads a precomputed model
    rather than recomputing it per session. Returns the number of genre pairs
    stored — a graceful 0 when the backend lacks the enrichment accessors (test
    fakes) or there's no enrichment yet."""
    if not (
        hasattr(db_manager, "get_all_artist_genre_sets")
        and hasattr(db_manager, "save_genre_affinity")
    ):
        return 0
    from utils.genre_similarity import build_npmi_model
    token_sets = await db_manager.get_all_artist_genre_sets()
    model = build_npmi_model(token_sets)
    await db_manager.save_genre_affinity(model)
    logger.info(
        "track_graph: genre affinity model built (%d pairs from %d tagged artists)",
        len(model), len(token_sets),
    )
    return len(model)


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
    """Unpack a timbre BLOB to an L2-normalised float32 vector suitable for
    cosine on the graph timbre sub-space (mfcc mean/std + chroma + rhythm; delta
    excluded, matching the geometry). Returns None when the blob is absent or
    malformed. Cosine helper for embedding-space scoring (e.g. a negative-taste
    centroid); callers fetch BLOBs via db_manager.get_embeddings_for_paths."""
    v = unpack_graph_embedding(blob)
    if v is None:
        return None
    n = float(np.linalg.norm(v))
    if n < 1e-9:
        return None
    return (v / n).astype(np.float32)


# ── Country-bonus γ tiers (see `_resolve_gamma`) ─────────────────────────────
# γ scales how much a shared artist-country reinforces genre similarity in
# `_meta_score`. Two opposing intuitions decide the tier of a genre bucket:
#   • REGIONAL scenes travel with a country/language, so co-nationality is a
#     STRONG extra cue → boost γ. Folk/laiko was the sole original member; Latin,
#     Reggae and Asian-Pop have the same shape (salsa↔Latin America, dancehall↔
#     Jamaica, K/J/C-pop↔their nation).
#   • BORDERLESS genres are international scenes where nationality says little
#     → damp γ so provenance doesn't over-count.
# Borderless is contagious and wins ties: if either track carries a borderless
# genre we damp regardless of co-tags (preserves the original precedence).
# Everything else (Hip-Hop, Rock, Pop, Metal, Soul, Jazz, Gospel, Disco,
# Soundtrack, Other) stays neutral — candidates for future tiering.
_GAMMA_REGIONAL = frozenset({"Folk/Cntry", "Latin", "Reggae", "Asian-Pop"})
_GAMMA_BORDERLESS = frozenset({"Electronic", "Classical"})
_GAMMA_DAMP, _GAMMA_NEUTRAL, _GAMMA_BOOST = 0.05, 0.15, 0.30


def _resolve_gamma(genres_a, genres_b) -> float:
    """Country-bonus scale γ for `_meta_score`. Damped where nationality is a
    weak similarity cue (borderless genres) and boosted where it's strong
    (regional scenes that travel with a country/language)."""
    from utils.pca_engine import genre_bucket
    all_genres = (genres_a or set()) | (genres_b or set())
    if not all_genres:
        return _GAMMA_NEUTRAL
    buckets = {genre_bucket(g) for g in all_genres}
    if buckets & _GAMMA_BORDERLESS:
        return _GAMMA_DAMP
    if buckets & _GAMMA_REGIONAL:
        return _GAMMA_BOOST
    return _GAMMA_NEUTRAL


def _meta_score(a_path: str, b_path: str, meta_map: dict, genre_model: dict) -> float:
    """Metadata affinity between two tracks: soft genre-set similarity (NPMI
    'genre-BLOSUM') plus an ADDITIVE shared-country term.

    Two independent provenance signals, summed:  gx + β·same_country.
      • gx  — soft genre-set similarity (NPMI), in [0, ~1].
      • β   — the tiered country weight from `_resolve_gamma` (0.30 for regional
              scenes that travel with a nation, 0.05 for borderless genres,
              0.15 otherwise).
    Additive (not the old gx·(1+β·same_cty)) so shared provenance contributes
    EVEN WHEN genre overlap is thin — the multiplicative form zeroed the country
    signal whenever gx≈0, which is exactly the same-country-but-different-subgenre
    case where nationality is the only cue left (e.g. two GB drill/grime artists
    whose NPMI genre sets barely overlap).

    Returns 0 when either track lacks enrichment, or when they're the same
    artist (that coherence is already carried by the artist edge tier). Used by
    `walk` to fold provenance/genre proximity into the acoustic score.
    """
    from utils.genre_similarity import soft_set_sim
    ma = meta_map.get(a_path)
    mb = meta_map.get(b_path)
    if not ma or not mb:
        return 0.0
    aa, ab = ma.get("artist"), mb.get("artist")
    if aa and aa == ab:
        return 0.0  # same-artist coherence already carried by the artist edge tier

    ca, cb = ma.get("country"), mb.get("country")
    same_cty = 1.0 if (ca and ca == cb) else 0.0
    ga = ma.get("genres") or frozenset()
    gb = mb.get("genres") or frozenset()
    gx = soft_set_sim(ga, gb, genre_model)

    beta = _resolve_gamma(ga, gb)
    return gx + beta * same_cty


async def walk(
    db_manager,
    seed_path: str,
    length: int = 10,
    avoid: Optional[set[str]] = None,
    meta_lambda: float = 0.35,
    cluster_lambda: float = 0.5,
) -> list[str]:
    """Seed-Anchored Smooth Flow trajectory walk over the track graph.

    This is *the* walk: a deterministic, seed-anchored trajectory over the
    acoustic graph, shaped by the metadata (genre/country) and community terms.
    The stochastic Personalised-PageRank walker was removed — smooth+meta is the
    single queue builder, so the enrichment we compute is actually used.

    At each step i -> C, candidates are unvisited acoustic neighbours scored by

        Score(C) = (0.7·Sim(T_i, C) + 0.3·Sim(Seed, C))         # acoustic
                   · (1 + meta_lambda·meta(T_i, C))             # genre/country
                   · (1 - cluster_lambda   if C leaves T_i's community)

    The dual acoustic term keeps step-to-step coherence anchored to the seed;
    the metadata factor (see `_meta_score`) and the soft cross-community penalty
    fold in the provenance/genre signal, so the queue is genuinely acoustic +
    metadata rather than acoustic-only. Selection is deterministic greedy — no
    teleport, no MMR.

    Both metadata terms degrade gracefully: a backend without the enrichment /
    cluster accessors (e.g. the test fakes) or a library without enrichment
    collapses the factors to 1.0 and the walk is the original dual-similarity
    flow.
    """
    visited: set[str] = set(avoid or set())
    visited.add(seed_path)
    path_seq: list[str] = []

    # Prefetch seed's acoustic neighbors for seed-anchor scoring & fallback
    if hasattr(db_manager, "get_neighbors_multi"):
        seed_nbrs = await db_manager.get_neighbors_multi(
            seed_path, (KIND_ACOUSTIC,), k=50
        )
    elif hasattr(db_manager, "get_neighbors"):
        seed_nbrs = await db_manager.get_neighbors(
            seed_path, k=50, edge_kind=KIND_ACOUSTIC
        )
    else:
        seed_nbrs = []

    seed_sim_map: dict[str, float] = {
        n["path"]: float(n.get("weight", 0.5)) for n in seed_nbrs if n.get("path")
    }

    # ── Metadata context (genre-NPMI + country) ──────────────────────────
    # meta_map is filled lazily as candidates are seen; the genre model loads
    # once. If the backend can't serve enrichment, meta_active latches off and
    # the metadata factor is 1.0 for the rest of the walk.
    meta_active = meta_lambda > 0.0 and hasattr(db_manager, "get_artist_meta_for_paths")
    meta_map: dict[str, dict] = {}
    genre_model: dict = {}
    if meta_active and hasattr(db_manager, "get_genre_affinity"):
        try:
            genre_model = await db_manager.get_genre_affinity()
        except Exception:
            genre_model = {}

    async def _ensure_meta(paths: list[str]) -> None:
        nonlocal meta_active
        if not meta_active:
            return
        missing = [p for p in paths if p not in meta_map]
        if not missing:
            return
        try:
            meta_map.update(await db_manager.get_artist_meta_for_paths(missing))
        except Exception:
            meta_active = False

    # ── Cluster context: soft penalty for hopping to a different community ──
    cluster_active = cluster_lambda > 0.0 and hasattr(db_manager, "get_track_cluster")
    cluster_map: dict[str, int | None] = {}

    async def _cluster_of(path: str) -> int | None:
        if path not in cluster_map:
            try:
                cid = await db_manager.get_track_cluster(path)
            except Exception:
                cid = None
            cluster_map[path] = int(cid) if cid is not None else None
        return cluster_map[path]

    await _ensure_meta([seed_path, *seed_sim_map.keys()])

    current = seed_path
    for _step in range(length):
        if hasattr(db_manager, "get_neighbors_multi"):
            raw_nbrs = await db_manager.get_neighbors_multi(
                current, (KIND_ACOUSTIC,), k=40
            )
        elif hasattr(db_manager, "get_neighbors"):
            raw_nbrs = await db_manager.get_neighbors(
                current, k=40, edge_kind=KIND_ACOUSTIC
            )
        else:
            raw_nbrs = []

        candidates = [n for n in raw_nbrs if n.get("path") and n["path"] not in visited]

        # Fallback to seed's acoustic neighbors if current node hits a dead end
        if not candidates and current != seed_path:
            candidates = [n for n in seed_nbrs if n.get("path") and n["path"] not in visited]

        if not candidates:
            break

        # Cache candidate cluster ids carried on the neighbour rows (no extra
        # query) and resolve the current node's cluster once per step.
        cur_cid: int | None = None
        if cluster_active:
            for c in candidates:
                if c["path"] not in cluster_map:
                    cid = c.get("cluster_id")
                    cluster_map[c["path"]] = int(cid) if cid is not None else None
            cur_cid = await _cluster_of(current)

        await _ensure_meta([c["path"] for c in candidates])

        # Score candidates: acoustic dual-similarity, then metadata + cluster.
        best_cand = None
        best_score = -1.0
        for c in candidates:
            w_curr = max(0.0, float(c.get("weight", 0.5)))
            w_seed = seed_sim_map.get(c["path"], 0.0)
            score = 0.7 * w_curr + 0.3 * w_seed
            if meta_active:
                score *= 1.0 + meta_lambda * _meta_score(
                    current, c["path"], meta_map, genre_model,
                )
            if cluster_active and cur_cid is not None:
                cand_cid = cluster_map.get(c["path"])
                if cand_cid is not None and cand_cid != cur_cid:
                    score *= (1.0 - cluster_lambda)
            if score > best_score:
                best_score = score
                best_cand = c

        if not best_cand:
            break

        next_path = best_cand["path"]
        path_seq.append(next_path)
        visited.add(next_path)
        current = next_path

    return path_seq


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
