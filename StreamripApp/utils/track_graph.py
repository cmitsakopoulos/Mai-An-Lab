"""
Track graph: sparse k-NN adjacency over the music library + mood vocabulary.

Two tiers of edges:

  • acoustic  — cosine similarity over the DSP feature vectors persisted by
                dsp.analyze_track(). Top-K-per-source candidates are pruned
                by mutual-kNN intersection (keep edge i→j iff j is in i's
                top-K AND i is in j's top-K) so cluster-centroid "hub"
                tracks don't dominate every walk. Tracks with fewer than 5
                mutual partners fall back to their original top-K to keep
                the graph connected.
  • metadata  — same-artist and same-album co-occurrence (edge_kind 'artist'
                / 'album'). Weight is fixed at 1.0; ordering inside a tier
                falls back to library order.

The graph is the navigation backbone for the assistant: it routes 'play
something similar' to a personalised-PageRank-flavoured random walk over
acoustic + artist edges (see `walk`), 'more by this artist' to artist
neighbours, and 'play X mood' to `tracks_by_mood`. Replaces the MCL
clustering pipeline, which produced discrete buckets instead of the
continuous proximity the assistant needs.

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
no off-thread call is necessary for libraries up to ~20K tracks; cosine over
56-dim vectors is bandwidth-limited and finishes in well under a second.
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
    EMBED_DIMS,
    FEATURES_VERSION,
    analyze_track,
    unpack_timbre,
)
from utils.config import APP_DIR

logger = logging.getLogger(__name__)

CUSTOM_MOODS_PATH = os.path.join(APP_DIR, "custom_moods.json")

# Islet membership defaults. Cosine similarity on raw timbre vectors against
# the islet's centroid: tracks at or above the threshold are members, ranked
# by similarity descending, capped at ISLET_MAX. ISLET_MIN guards against
# islets too sparse to feel meaningful (an outlier exemplar with no neighbours
# returns an empty result rather than a one-track "group").
ISLET_THRESHOLD = 0.93
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


async def build_acoustic_edges(
    db_manager,
    k: int = DEFAULT_K_ACOUSTIC,
    features_version: int = FEATURES_VERSION,
) -> int:
    """Recompute the acoustic tier of the graph from scratch.

    Loads every track that has a current-version feature BLOB, z-scores the
    vectors so cosine on the normalised matrix is equivalent to scaled
    Euclidean, and writes the top-K neighbours per track back to
    `track_neighbors`. Returns the edge count written.

    Coverage degrades gracefully: tracks without features are simply absent
    from the acoustic graph. The assistant falls back to metadata edges for
    those.
    """
    rows = await db_manager.get_tracks_with_features(features_version)
    if len(rows) < 2:
        await db_manager.replace_neighbors_bulk([], KIND_ACOUSTIC)
        logger.info("track_graph: acoustic edges skipped (only %d tracks with features)", len(rows))
        return 0

    paths: list[str] = []
    vectors: list[np.ndarray] = []
    for r in rows:
        v = unpack_timbre(r.get("timbre"))
        if v is None or v.shape[0] != EMBED_DIMS:
            continue
        # Append all 8 scalar descriptors so BPM / brightness / energy / rolloff
        # / beat_strength / flatness / contrast / key_mode contribute to the
        # similarity ranking. Without them, two tracks with matching MFCC
        # profile but very different tempos or dynamic texture/harmonic profiles
        # would be neighbours, which is rarely what a listener means by 'similar'.
        ki = r.get("key_index", 0) or 0
        key_mode = 1.0 if ki < 12 else 0.0
        scalars = np.array([
            r.get("bpm", 0) or 0,
            r.get("brightness", 0) or 0,
            r.get("energy", 0) or 0,
            r.get("rolloff", 0) or 0,
            r.get("beat_strength", 0) or 0,
            r.get("spectral_flatness", 0) or 0,
            r.get("spectral_contrast", 0) or 0,
            key_mode,
        ], dtype=np.float32)
        paths.append(r["path"])
        vectors.append(np.concatenate([v.astype(np.float32), scalars]))

    if len(vectors) < 2:
        await db_manager.replace_neighbors_bulk([], KIND_ACOUSTIC)
        return 0

    X = np.stack(vectors, axis=0)  # (N, EMBED_DIMS + 8) -> (N, 60)
    # z-score per dimension so the disparate scales (MFCC vs BPM) don't let
    # one axis dominate the cosine. Replace zero-variance columns with 1 to
    # avoid NaNs.
    mu = X.mean(axis=0, keepdims=True)
    sd = X.std(axis=0, keepdims=True)
    sd = np.where(sd < 1e-8, 1.0, sd)
    Z = (X - mu) / sd
    # Cosine over z-scored vectors. L2-normalise so the dot product gives
    # cosine directly.
    norms = np.linalg.norm(Z, axis=1, keepdims=True)
    norms = np.where(norms < 1e-8, 1.0, norms)
    Zn = Z / norms

    N = Zn.shape[0]
    k_eff = min(k, N - 1)

    # First pass: collect each track's top-K candidates with their cosine
    # weight, keyed by source-index. Stored as parallel arrays per source so
    # the mutual-kNN intersection below can rebuild edges without recomputing.
    candidates: list[list[tuple[int, float]]] = [[] for _ in range(N)]
    chunk = 256
    for i in range(0, N, chunk):
        block = Zn[i:i + chunk]              # (C, D)
        sims = block @ Zn.T                  # (C, N)
        # Mask self-similarity.
        for j, row_idx in enumerate(range(i, i + block.shape[0])):
            sims[j, row_idx] = -np.inf
        # Top-K indices per row, unordered. argpartition is O(N); the final
        # ordering is recovered with argsort over the K slice.
        topk_unsorted = np.argpartition(-sims, k_eff, axis=1)[:, :k_eff]
        for j, row_idx in enumerate(range(i, i + block.shape[0])):
            idx = topk_unsorted[j]
            order = np.argsort(-sims[j, idx])
            ordered = idx[order]
            candidates[row_idx] = [
                (int(nb), max(-1.0, min(1.0, float(sims[j, nb]))))
                for nb in ordered
            ]

    # Mutual-kNN pruning: keep edge (i → j) iff j is in i's top-K AND i is in
    # j's top-K. This fights popularity / hub bias — a cluster centroid is
    # everybody's neighbour but its outgoing top-K all point into the cluster,
    # so the walker piles up there. Mutual-kNN flattens the over-representation
    # without disconnecting the graph.
    neighbour_set = [
        {nb for nb, _ in cands} for cands in candidates
    ]
    # Tracks with too few mutual edges fall back to their original top-K so
    # the walk still has somewhere to go. Threshold is small (5) — the goal
    # is connectivity, not strict mutuality.
    MUTUAL_FALLBACK = 5
    edges: list[tuple[str, str, float]] = []
    mutual_total = 0
    fallback_total = 0
    for src_idx, cands in enumerate(candidates):
        mutual = [
            (nb, w) for nb, w in cands if src_idx in neighbour_set[nb]
        ]
        if len(mutual) >= MUTUAL_FALLBACK:
            mutual_total += len(mutual)
            chosen = mutual
        else:
            fallback_total += 1
            chosen = cands
        src = paths[src_idx]
        for nb_idx, weight in chosen:
            edges.append((src, paths[nb_idx], weight))

    await db_manager.replace_neighbors_bulk(edges, KIND_ACOUSTIC)
    logger.info(
        "track_graph: wrote %d acoustic edges across %d tracks (k=%d, "
        "mutual=%d, fallback=%d)",
        len(edges), N, k_eff, mutual_total, fallback_total,
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
# softmax. Acoustic edges are already cosine ∈ [-1, 1]; metadata edges are
# fixed at 1.0 so the multipliers double as their effective preference.
_DEFAULT_EDGE_KIND_WEIGHTS: dict[str, float] = {
    KIND_ACOUSTIC: 5.0,
    KIND_ARTIST:   2.0,
    KIND_ALBUM:    1.5,
}


def _unpack_embedding(blob: bytes | None) -> Optional[np.ndarray]:
    """Local helper: unpack a timbre BLOB to an L2-normalised float32 vector
    suitable for cosine on the MFCC+delta+chroma sub-space. Returns None when
    the blob is absent or malformed. Used by the MMR diversity term."""
    v = unpack_timbre(blob)
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
    diversity_lambda: float = 0.3,
    temperature: float = 0.04,
    taste_weight: float = 0.0,
    taste_explore: float = 0.05,
    negative_embs: Optional[list[np.ndarray]] = None,
    negative_lambda: float = 0.6,
    seed_rng: Optional[random.Random] = None,
    teleport_path: Optional[str] = None,
    prefetch_k: int = 40,
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
    for _ in range(length):
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
                eff -= float(diversity_lambda) * float(sims.max())
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
            logits[i] = eff

        # Softmax with temperature. Subtract max for numerical stability.
        if temperature <= 0:
            chosen_idx = int(np.argmax(logits))
        else:
            # Flat temperature: every step has the same low transition cost
            # so the walk stays acoustically close to the seed throughout.
            # The previous "Long-Flow Gentle-Reset" modulation (0.75× normal,
            # 1.5× every 6th step) introduced deliberate hot jumps that broke
            # similarity chains — removed per user intent.
            scaled = (logits - logits.max()) / float(temperature)
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


# Feature columns participating in mood scoring. Order matters: weights and
# the z-scored matrix are aligned to this list. Adding a column here means
# every profile may optionally include it. NOTE: key_mode is a projected
# continuous binary mode (1.0 for major, 0.0 for minor) from key_index.
_MOOD_FEATURES = ("PC1", "PC2", "PC3")

# Module-level dynamically populated set of redundant features.
# Populated automatically by correlation coefficient analysis on-the-fly.
REDUNDANT_FEATURES: set[str] = set()

def get_redundant_features(rows, projection, eigenvalues, threshold=0.85) -> set[str]:
    if len(rows) < 50 or projection is None or eigenvalues is None:
        return set()
        
    raw_features = ["bpm", "brightness", "energy", "rolloff", "beat_strength", "spectral_flatness", "spectral_contrast", "key_mode"]
    N = len(rows)
    D = len(raw_features)
    
    # 1. Build the data matrix X
    X = np.zeros((N, D), dtype=np.float32)
    for idx, r in enumerate(rows):
        feat_vec = []
        for f in raw_features:
            if f == "key_mode":
                ki = r.get("key_index", 0) or 0
                val = 1.0 if ki < 12 else 0.0
            else:
                val = float(r.get(f, 0) or 0)
            feat_vec.append(val)
        X[idx, :] = feat_vec
        
    # 2. Compute Pearson correlation coefficient matrix
    stds = np.std(X, axis=0)
    stds[stds == 0] = 1.0
    X_norm = (X - np.mean(X, axis=0)) / stds
    corr_matrix = np.dot(X_norm.T, X_norm) / (N - 1)
    
    # 3. Calculate explained variance weights
    overall_weights = []
    for feat_idx, feat in enumerate(raw_features):
        weight = float(
            (projection[feat_idx, 0] ** 2) * eigenvalues[0] +
            (projection[feat_idx, 1] ** 2) * eigenvalues[1] +
            (projection[feat_idx, 2] ** 2) * eigenvalues[2]
        )
        overall_weights.append((feat, feat_idx, weight))
        
    # Sort descending by explained variance weight
    sorted_features = sorted(overall_weights, key=lambda x: x[2], reverse=True)
    
    redundant = set()
    # 4. Check correlations sequentially
    for i in range(len(sorted_features)):
        feat_i, idx_i, weight_i = sorted_features[i]
        # Check if it correlates with any feature that has higher explained variance and is not redundant
        for j in range(i):
            feat_j, idx_j, weight_j = sorted_features[j]
            if feat_j in redundant:
                continue
            r = abs(corr_matrix[idx_i, idx_j])
            if r >= threshold:
                redundant.add(feat_i)
                logger.warning(
                    "Dynamic Redundancy: Feature '%s' is highly correlated with '%s' (r=%.2f). Pruning '%s' (variance weight=%.4f < %.4f).",
                    feat_i, feat_j, r, feat_i, weight_i, weight_j
                )
                break
                
    return redundant

# Cache for the projected PCA matrix to avoid database calls and SVD projections on every query.
_PERCENTILE_CACHE: dict[tuple, tuple] = {}


async def optimize_pca_spacing(db_manager, features_version: int):
    """Mutation: Retrieves all analyzed tracks, computes PCA (Z-score standardized),
    saves the loadings/eigenvectors to the DB, projects all tracks into the 3D space,
    and caches their coordinates in the play_counts table."""
    rows = await db_manager.get_tracks_with_features(features_version)
    if len(rows) < 2:
        logger.warning("Fewer than 2 tracks analyzed. Cannot optimize PCA spacing.")
        return
        
    from utils.pca_engine import calculate_pca_projection, project_track
    means, stds, V_keep, eigenvalues, kaiser_k = calculate_pca_projection(rows)
    await db_manager.save_pca_space(means, stds, V_keep, eigenvalues)
    
    # Dynamically discover redundant features and cache them
    global REDUNDANT_FEATURES
    REDUNDANT_FEATURES = get_redundant_features(rows, V_keep, eigenvalues)
    
    batch_data = []
    for r in rows:
        z = project_track(r, means, stds, V_keep)
        batch_data.append((r["path"], z))
        
    await db_manager.update_tracks_pca_coords_batch(batch_data)
    _PERCENTILE_CACHE.clear()
    logger.info(f"PCA library spacing optimized successfully (Kaiser count={kaiser_k}).")

    # ── Generate on-device mathematical truth report ──────────────────────────
    try:
        from utils.pca_engine import plot_pca_report
        from utils.streamrip_api import load_config

        cfg = load_config()
        library_folder = (cfg.get("downloads") or {}).get("folder") or ""
        library_folder = str(library_folder).strip()

        if library_folder and os.path.isdir(library_folder):
            report_dir = os.path.join(library_folder, "pca_report")
        else:
            # User hasn't set a library path yet — shouldn't normally happen
            # since PCA requires scanned tracks, but guard gracefully.
            logger.warning(
                "optimize_pca_spacing: downloads.folder not set or missing; "
                "writing PCA report to APP_DIR fallback."
            )
            report_dir = os.path.join(APP_DIR, "pca_report")

        saved = plot_pca_report(rows, report_dir)
        if saved:
            logger.info(
                "PCA visual report written to: %s  (%d figures: %s)",
                report_dir,
                len(saved),
                ", ".join(os.path.basename(p) for p in saved),
            )
    except Exception as _plot_err:
        logger.warning("PCA visual report skipped: %s", _plot_err)


async def _load_percentile_matrix(
    db_manager,
    features_version: int,
) -> tuple[list[dict], np.ndarray]:
    """Returns (rows, pca_coordinate_matrix). Recovers/computes PCA projection
    on-the-fly and handles cached DB values transparently. Replaces legacy percentile matrix."""
    rows = await db_manager.get_tracks_with_features(features_version)
    if not rows:
        return [], np.zeros((0, len(_MOOD_FEATURES)), dtype=np.float32)
        
    sentinel = max(r["path"] for r in rows)
    key = (features_version, len(rows), sentinel)
    cached = _PERCENTILE_CACHE.get(key)
    if cached is not None:
        return cached
        
    pca_space = await db_manager.load_pca_space()
    if pca_space is None:
        logger.info("PCA projection space not found. Running auto-initialization...")
        await optimize_pca_spacing(db_manager, features_version)
        pca_space = await db_manager.load_pca_space()
        
    eigenvalues = None
    if pca_space is not None:
        if len(pca_space) == 4:
            means, stds, V_keep, eigenvalues = pca_space
        else:
            means, stds, V_keep = pca_space
    else:
        means = np.zeros(8, dtype=np.float32)
        stds = np.ones(8, dtype=np.float32)
        V_keep = np.eye(8, 3, dtype=np.float32)
        
    # Dynamically discover redundant features and cache them
    global REDUNDANT_FEATURES
    if eigenvalues is not None:
        REDUNDANT_FEATURES = get_redundant_features(rows, V_keep, eigenvalues)
    else:
        # Fallback if eigenvalues aren't saved yet: retrieve them by running projection calculation on the fly
        from utils.pca_engine import calculate_pca_projection
        _, _, _, calc_eigenvalues, _ = calculate_pca_projection(rows)
        REDUNDANT_FEATURES = get_redundant_features(rows, V_keep, calc_eigenvalues)
        
    matrix = np.zeros((len(rows), 3), dtype=np.float32)
    coords_rows = await db_manager.get_tracks_pca_coords()
    coords_map = {r["path"]: r["pca_coords"] for r in coords_rows}
    
    tracks_to_cache = []
    from utils.pca_engine import project_track
    for idx, r in enumerate(rows):
        path = r["path"]
        if path in coords_map:
            matrix[idx, :] = coords_map[path]
        else:
            z = project_track(r, means, stds, V_keep)
            matrix[idx, :] = z
            tracks_to_cache.append((path, z))
            
    if tracks_to_cache:
        await db_manager.update_tracks_pca_coords_batch(tracks_to_cache)
        
    _PERCENTILE_CACHE.clear()
    _PERCENTILE_CACHE[key] = (rows, matrix)
    logger.info(f"track_graph: PCA 3D matrix cache rebuilt (N={len(rows)})")
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
    """Assign one track to a mood, overwriting any prior assignment. Invalidates
    the centroid for the destination mood (and the source mood if it changed)
    so the next `tracks_by_mood` call recomputes."""
    canonical = mood_canonical(mood) or mood.lower().strip()
    await db_manager.assign_track_to_mood(track_path, canonical)
    # Recompute lazily on next read — calling `recompute_mood_centroid` here
    # would double the cost of bulk-assign flows. The mood_profiles row stays
    # stale until something reads it, at which point the partition count
    # signals "recompute me".


async def unassign_track_from_mood(db_manager, track_path: str) -> None:
    """Remove a track from its current mood partition."""
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


async def recompute_mood_centroid(
    db_manager,
    mood: str,
) -> dict[str, tuple[float, float]] | None:
    """Re-derive the target quartiles for `mood` from its current partition.
    Pulls the assigned tracks' percentiles and maps their column-wise mean to quartiles.
    """
    canonical = mood_canonical(mood) or mood.lower().strip()
    assigned_paths = await db_manager.get_tracks_in_mood(canonical)
    if not assigned_paths:
        return None
        
    rows = await db_manager.get_tracks_with_features(FEATURES_VERSION)
    if not rows:
        return None
        
    N = len(rows)
    raw_features = ["bpm", "brightness", "energy", "rolloff", "beat_strength", "spectral_flatness", "spectral_contrast", "key_mode"]
    
    track_ranks = {}
    for f in raw_features:
        vals = np.array([float(r.get(f) or 0.0) for r in rows], dtype=np.float32)
        ranks = np.argsort(np.argsort(vals)) / max(1, N - 1)
        track_ranks[f] = {rows[idx]["path"]: float(ranks[idx]) for idx in range(N)}
        
    new_profile = {}
    for f in raw_features:
        if f in REDUNDANT_FEATURES:
            new_profile[f] = (0.0, 0.0)
            continue
        active_pcts = [track_ranks[f].get(p, 0.5) for p in assigned_paths if p in track_ranks[f]]
        if active_pcts:
            avg_pct = np.mean(active_pcts)
            quartile = int(np.clip(np.floor(avg_pct * 4) + 1, 1, 4))
            new_profile[f] = (float(quartile), 1.0)
        else:
            new_profile[f] = (0.0, 0.0)
            
    await db_manager.save_adjusted_mood_profile(canonical, new_profile)
    return new_profile


async def set_mood_eq(
    db_manager,
    mood: str,
    eq_weights: dict[str, float],
) -> None:
    """Update target quartiles for `mood`."""
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
    """Resolve the target quartiles actually used to score `mood`.

    Precedence:
      1. If `mood_profiles` has rows → use them (user has partitioned or
         tuned the mood at least once).
      2. Else → Raise ValueError to force crash and remove default fallback.
    """
    canonical = mood_canonical(mood)
    if canonical is None:
        return None
    stored = await db_manager.get_adjusted_mood_profile(canonical)
    if stored is not None:
        return stored
    raise ValueError(f"No adjusted mood profile found in database for '{canonical}'. Built-in factory-default profiles are disabled.")


# ── User-taste model wiring ─────────────────────────────────────────────────
#
# Global model loaded once per process and re-cached after each update.
# Reads are hot (every walk step, every mood query); writes are rare
# (per playback event). Module-level cache keeps the steady-state cost at
# zero DB hits per scoring call.

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
                if f in REDUNDANT_FEATURES:
                    continue
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


def _score_against_centroid(
    centroid: np.ndarray,
    timbres: np.ndarray,
) -> np.ndarray:
    """Cosine similarity between each row's timbre BLOB and the centroid.
    Higher is better. Returns shape (N,). Used for custom-mood centroids."""
    if timbres.size == 0 or centroid.size == 0:
        return np.zeros(timbres.shape[0], dtype=np.float32)
    norm_c = float(np.linalg.norm(centroid)) + 1e-8
    norm_t = np.linalg.norm(timbres, axis=1) + 1e-8
    return (timbres @ centroid) / (norm_t * norm_c)


# Re-rank weight for the taste model on top of the mood score. Small enough
# that taste shapes ordering at the margin but doesn't let a global
# preference drag an off-mood track into the result.
_MOOD_TASTE_BETA = 0.20


async def tracks_by_mood(
    db_manager,
    mood: str,
    limit: int = 12,
    features_version: int = FEATURES_VERSION,
) -> list[dict]:
    """Rank tracks by how well their physical feature percentiles match the target quartiles
    of the mood, re-ranked at the top by the global user-taste model.

    Returns [] for unknown moods or empty libraries.
    """
    canonical = mood_canonical(mood)
    if canonical is None:
        return []

    # Built-in moods follow the partition + EQ path. Custom moods (islets)
    # keep the legacy cosine/regressor pipeline — see `tracks_in_islet`.
    if canonical not in MOODS:
        return await tracks_in_islet(db_manager, canonical, features_version)

    rows, percentiles = await _load_percentile_matrix(db_manager, features_version)
    if not rows:
        return []

    raw_features = ["bpm", "brightness", "energy", "rolloff", "beat_strength", "spectral_flatness", "spectral_contrast", "key_mode"]
    N = len(rows)

    # Compute raw feature percentile ranks in memory
    track_ranks = {}
    for f in raw_features:
        vals = np.array([float(r.get(f) or 0.0) for r in rows], dtype=np.float32)
        ranks = np.argsort(np.argsort(vals)) / max(1, N - 1)
        track_ranks[f] = {rows[idx]["path"]: float(ranks[idx]) for idx in range(N)}

    # Prefer a partition-derived target profile when the user has assigned tracks;
    # otherwise load from SQLite adjusted_mood_profiles or throw.
    assigned_paths = await db_manager.get_tracks_in_mood(canonical)
    if assigned_paths:
        stored = {}
        for f in raw_features:
            active_pcts = [track_ranks[f].get(p, 0.5) for p in assigned_paths if p in track_ranks[f]]
            if active_pcts:
                avg_pct = np.mean(active_pcts)
                quartile = int(np.clip(np.floor(avg_pct * 4) + 1, 1, 4))
                stored[f] = (float(quartile), 1.0)
            else:
                stored[f] = (0.0, 0.0)
    else:
        stored = await db_manager.get_adjusted_mood_profile(canonical)
        if stored is None:
            raise ValueError(f"No adjusted mood profile found in database for '{canonical}' and partition is empty.")

    # Calculate distance to target quartiles for each track
    scores = np.zeros(N, dtype=np.float32)
    for idx, r in enumerate(rows):
        path = r["path"]
        dist_sq_sum = 0.0
        active_count = 0
        for f in raw_features:
            if f in REDUNDANT_FEATURES:
                continue
            target, weight = stored.get(f, (0.0, 0.0))
            if weight > 0.0 and target > 0.0:
                pct = track_ranks[f].get(path, 0.5)
                # target represents the quartile:
                # 1: [0.0, 0.25]
                # 2: [0.25, 0.50]
                # 3: [0.50, 0.75]
                # 4: [0.75, 1.0]
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

    if scores.size == 0:
        return []

    # Taste-model re-rank over the top 3×limit window. Cold model
    # (no events yet) skips the blend entirely so first-day behaviour is
    # purely mood-driven.
    rerank_window = max(limit * 3, 12)
    if scores.size > rerank_window:
        cand_idx = np.argpartition(-scores, rerank_window - 1)[:rerank_window]
    else:
        cand_idx = np.arange(scores.size)
        
    taste_scores, has_signal = await taste_scores_for_matrix(
        db_manager, percentiles[cand_idx], features_version,
    )
    if has_signal:
        beta = _MOOD_TASTE_BETA
        # Mood scores are non-positive (negative distance); taste in (0, 1).
        # Subtract 0.5 to centre taste so a neutral track doesn't reward the
        # blend just by existing.
        scores[cand_idx] = (
            (1.0 - beta) * scores[cand_idx]
            + beta * (taste_scores - 0.5)
        )

    k = min(limit, scores.size)
    top_unsorted = np.argpartition(-scores, k - 1)[:k]
    top_ordered = top_unsorted[np.argsort(-scores[top_unsorted])]
    return [rows[int(i)] for i in top_ordered]


async def adjust_mood_profile(
    db_manager,
    mood: str,
    track_path: str,
    feedback: int,
):
    """Compatibility shim for legacy callers.

    Old semantics nudged the mood's (target, weight) via gradient on a
    single like/dislike event. The new model decouples those:

      * `feedback == +1` (like in mood X) → assign the track to X's
        partition AND record an explicit positive event for the global
        taste model.
      * `feedback == -1` (dislike in mood X) → unassign the track from any
        partition AND record an explicit negative taste event.
      * any other value → no-op.

    The centroid for X is re-derived from the partition lazily on the next
    `tracks_by_mood` call; no per-call cost here beyond the two writes.
    """
    canonical = mood_canonical(mood)
    if canonical is None:
        logger.warning("adjust_mood_profile: Unknown mood %s", mood)
        return
    if feedback == 1:
        await assign_track_to_mood(db_manager, track_path, canonical)
        await record_explicit_feedback(db_manager, track_path, True)
    elif feedback == -1:
        await unassign_track_from_mood(db_manager, track_path)
        await record_explicit_feedback(db_manager, track_path, False)
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
    """Members of a named islet, ranked by cosine similarity to the centroid.

    Loads the islet's `custom_moods.json` entry → reads `threshold` and
    `blacklist` → fetches every track with current-version features →
    computes `cosine(track.timbre, islet.centroid)` over non-blacklisted
    tracks → returns rows with sim ≥ threshold, ordered by descending sim.

    This is the only scoring path. Users prune unwanted tracks by adding
    them to the per-islet blacklist (see `blacklist_track_from_islet`); no
    per-islet learning model is involved.

    By default returns [] if fewer than `ISLET_MIN` tracks pass — a centroid
    that doesn't generalise produces no playable queue. Pass `min_count=0`
    when the caller wants honest below-floor membership (e.g. the Library
    view, which still needs to render the accordion so the user can loosen
    a too-tight threshold instead of thinking the islet was deleted).
    """
    cleaned = name.lower().strip()
    cm = load_custom_moods().get(cleaned)
    if cm is None:
        return []
    threshold = float(cm.get("threshold", ISLET_THRESHOLD))
    blacklist = set(cm.get("blacklist") or [])

    centroid_list = cm.get("centroid") or []
    if not centroid_list:
        return []
    centroid = np.array(centroid_list, dtype=np.float32)

    rows = await db_manager.get_tracks_with_features(features_version)
    if not rows:
        return []

    timbres_list: list[np.ndarray] = []
    keep_idx: list[int] = []
    for i, r in enumerate(rows):
        if r.get("path") in blacklist:
            continue
        v = unpack_timbre(r.get("timbre"))
        if v is not None and v.shape == centroid.shape:
            timbres_list.append(v)
            keep_idx.append(i)
    if not timbres_list:
        return []

    timbres = np.stack(timbres_list, axis=0).astype(np.float32)
    sims = _score_against_centroid(centroid, timbres)

    member_mask = sims >= threshold
    if int(member_mask.sum()) < min_count:
        return []
    member_indices = np.where(member_mask)[0]
    ordered = member_indices[np.argsort(-sims[member_indices])][:ISLET_MAX]
    return [rows[keep_idx[int(i)]] for i in ordered]


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
