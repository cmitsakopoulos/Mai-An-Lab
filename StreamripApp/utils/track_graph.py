"""
Track graph: sparse k-NN adjacency over the music library.

Two tiers of edges:

  • acoustic  — cosine similarity over the DSP feature vectors persisted by
                dsp.analyze_track(). One edge per (track, top-K neighbour).
  • metadata  — same-artist and same-album co-occurrence (edge_kind 'artist'
                / 'album'). Weight is fixed at 1.0; ordering inside a tier
                falls back to library order.

The graph is the navigation backbone for the assistant: it routes 'play
something similar' to acoustic neighbours, 'more by this artist' to artist
neighbours, and so on. Replaces the MCL clustering pipeline, which produced
discrete buckets instead of the continuous proximity the assistant needs.

All builders are async (DB-bound) but the numpy work runs synchronously —
no off-thread call is necessary for libraries up to ~20K tracks; cosine over
38-dim vectors is bandwidth-limited and finishes in well under a second.
For very large libraries the caller should wrap build_acoustic_edges in
asyncio.to_thread.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

import numpy as np

from utils.dsp import (
    EMBED_DIMS,
    FEATURES_VERSION,
    analyze_track,
    unpack_timbre,
)

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
        # Append the scalar descriptors so BPM / brightness / energy contribute
        # to the similarity ranking. Without them, two tracks with matching
        # MFCC profile but very different tempos would be neighbours, which
        # is rarely what a listener means by 'similar'.
        scalars = np.array([
            r.get("bpm", 0) or 0,
            r.get("brightness", 0) or 0,
            r.get("energy", 0) or 0,
            r.get("rolloff", 0) or 0,
        ], dtype=np.float32)
        paths.append(r["path"])
        vectors.append(np.concatenate([v.astype(np.float32), scalars]))

    if len(vectors) < 2:
        await db_manager.replace_neighbors_bulk([], KIND_ACOUSTIC)
        return 0

    X = np.stack(vectors, axis=0)  # (N, 42)
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
    edges: list[tuple[str, str, float]] = []

    # Chunked pairwise: avoids materialising an N×N matrix for big libraries.
    chunk = 256
    for i in range(0, N, chunk):
        block = Zn[i:i + chunk]              # (C, D)
        sims = block @ Zn.T                  # (C, N)
        # Mask self-similarity.
        for j, row_idx in enumerate(range(i, i + block.shape[0])):
            sims[j, row_idx] = -np.inf
        # Top-K indices per row, unordered. argpartition is O(N); the final
        # ordering for storage is recovered with argsort over the K slice.
        topk_unsorted = np.argpartition(-sims, k_eff, axis=1)[:, :k_eff]
        for j, row_idx in enumerate(range(i, i + block.shape[0])):
            idx = topk_unsorted[j]
            order = np.argsort(-sims[j, idx])
            ordered = idx[order]
            src = paths[row_idx]
            for nb in ordered:
                weight = float(sims[j, nb])
                # Cosine in [-1, 1]; clamp to (-1, 1) defensively.
                weight = max(-1.0, min(1.0, weight))
                edges.append((src, paths[int(nb)], weight))

    await db_manager.replace_neighbors_bulk(edges, KIND_ACOUSTIC)
    logger.info(
        "track_graph: wrote %d acoustic edges across %d tracks (k=%d)",
        len(edges), N, k_eff,
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


async def walk(
    db_manager,
    seed_path: str,
    length: int = 10,
    edge_kind: Optional[str] = None,
    avoid: Optional[set[str]] = None,
    seed_rng: Optional[random.Random] = None,
) -> list[str]:
    """Random walk starting at `seed_path`. Each step is a weighted random
    choice over the current node's neighbours, biased towards higher cosine.

    `avoid` is an externally-managed exclusion set so the assistant can
    prevent recently-played tracks from being recommended again. The seed
    itself is always avoided.

    Returns a list of paths in walk order, length up to `length`. Stops early
    if the walk runs out of unseen neighbours.
    """
    rng = seed_rng or random.Random()
    visited: set[str] = set(avoid or set())
    visited.add(seed_path)
    path_seq: list[str] = []

    current = seed_path
    for _ in range(length):
        candidates = await db_manager.get_neighbors(current, k=20, edge_kind=edge_kind)
        candidates = [c for c in candidates if c["path"] not in visited]
        if not candidates:
            break

        # Weight selection: square the cosine so high-similarity neighbours
        # are strongly preferred but the walk isn't fully deterministic.
        weights = [max(0.01, float(c["weight"])) ** 2 for c in candidates]
        total = sum(weights)
        if total <= 0:
            chosen = candidates[0]
        else:
            r = rng.random() * total
            acc = 0.0
            chosen = candidates[-1]
            for c, w in zip(candidates, weights):
                acc += w
                if r <= acc:
                    chosen = c
                    break

        next_path = chosen["path"]
        path_seq.append(next_path)
        visited.add(next_path)
        current = next_path

    return path_seq


# ── Mood (DSP-driven) ───────────────────────────────────────────────────────
#
# Maps a mood keyword to a direction-vector over the scalar DSP features. The
# scoring function z-scores each feature column across the library, then
# computes a weighted dot product per track; the top-N highest scores get
# enqueued. Weights are heuristic and library-relative — a "fast" track in a
# library of ambient is slower than a "fast" track in a library of techno.
#
# Adding a mood here AND in assistant_intent.MOOD_KEYWORDS is what makes the
# assistant understand a new vocabulary word; the regex only fires for words
# present in MOOD_KEYWORDS.

MOOD_PROFILES: dict[str, dict[str, float]] = {
    # Calm / low-energy
    "chill":     {"bpm": -1.0, "energy": -1.0, "brightness": -0.5, "beat_strength": -0.5},
    "chilled":   {"bpm": -1.0, "energy": -1.0, "brightness": -0.5, "beat_strength": -0.5},
    "relaxed":   {"bpm": -1.0, "energy": -1.0, "brightness": -0.5},
    "relaxing":  {"bpm": -1.0, "energy": -1.0, "brightness": -0.5},
    "calm":      {"bpm": -1.0, "energy": -1.0, "brightness": -0.5},
    "mellow":    {"bpm": -0.7, "energy": -0.7, "brightness": -0.5},
    "soft":      {"energy": -1.5, "brightness": -0.5},
    "ambient":   {"bpm": -1.5, "energy": -1.5, "beat_strength": -1.5},

    # High-energy
    "upbeat":    {"bpm": 1.0, "energy": 1.0, "brightness": 0.5},
    "energetic": {"bpm": 1.0, "energy": 1.5, "beat_strength": 1.0},
    "intense":   {"energy": 2.0, "beat_strength": 1.5, "brightness": 0.5},
    "hard":      {"energy": 1.5, "beat_strength": 1.5},
    "heavy":     {"energy": 1.5, "rolloff": 0.5, "brightness": -0.3},
    "powerful":  {"energy": 1.5, "beat_strength": 1.0},
    "happy":     {"bpm": 0.8, "brightness": 1.0, "energy": 0.5},
    "uplifting": {"bpm": 0.8, "brightness": 1.0, "energy": 0.8},

    # Tempo-specific
    "fast":      {"bpm": 2.0},
    "quick":     {"bpm": 2.0},
    "slow":      {"bpm": -2.0},
    "lazy":      {"bpm": -1.5, "energy": -1.0},

    # Timbre / spectrum
    "dark":      {"brightness": -1.5, "rolloff": -1.0},
    "moody":     {"brightness": -1.0, "energy": -0.5, "spectral_flatness": -0.5},
    "bright":    {"brightness": 1.5, "rolloff": 1.0},

    # v3 scalars: tonal/noisy axis (spectral_flatness, spectral_contrast).
    # Flatness rises with noise; contrast rises with clear tonal peaks.
    "tonal":     {"spectral_flatness": -1.5, "spectral_contrast": 1.0},
    "melodic":   {"spectral_flatness": -1.0, "spectral_contrast": 1.0, "brightness": 0.3},
    "noisy":     {"spectral_flatness": 1.5},
    "textured":  {"spectral_flatness": 1.0, "spectral_contrast": -0.5},
    "acoustic":  {"spectral_flatness": -1.0, "energy": -0.5, "beat_strength": -0.3},
}

# Feature columns participating in mood scoring. Order matters: weights and
# the z-scored matrix are aligned to this list. Adding a column here means
# every profile may optionally include it. NOTE: key_index is deliberately
# excluded — it's a categorical value where direction-based z-scoring is
# meaningless; key-aware filtering is handled separately (TODO: future
# 'play in <key>' intent).
_MOOD_FEATURES = (
    "bpm", "brightness", "energy", "rolloff", "beat_strength",
    "spectral_flatness", "spectral_contrast",
)


async def tracks_by_mood(
    db_manager,
    mood: str,
    limit: int = 12,
    features_version: int = FEATURES_VERSION,
) -> list[dict]:
    """Rank tracks by how well they match a mood's DSP profile and return
    the top `limit`. Returns library rows (with title/artist/album/path)
    ready for the assistant to enqueue.

    The ranking is library-relative: features are z-scored across all
    analysed tracks before scoring, so the same mood word will pick
    different tracks depending on the library's distribution. This is the
    right behaviour — "energetic" should mean "energetic for this library",
    not against an absolute scale that may not match the user's collection.

    Returns [] for unknown moods or when fewer than 2 tracks have features.
    """
    profile = MOOD_PROFILES.get(mood.lower())
    if profile is None:
        return []

    rows = await db_manager.get_tracks_with_features(features_version)
    if len(rows) < 2:
        return []

    # Build the feature matrix in the canonical column order.
    matrix = np.array(
        [[float(r.get(f, 0) or 0) for f in _MOOD_FEATURES] for r in rows],
        dtype=np.float32,
    )
    mu = matrix.mean(axis=0, keepdims=True)
    sd = matrix.std(axis=0, keepdims=True)
    sd = np.where(sd < 1e-8, 1.0, sd)
    Z = (matrix - mu) / sd

    weights = np.array(
        [profile.get(f, 0.0) for f in _MOOD_FEATURES],
        dtype=np.float32,
    )
    scores = Z @ weights  # (N,)

    k = min(limit, len(scores))
    # argpartition gives the top-K (unordered), argsort orders them by score.
    top_unsorted = np.argpartition(-scores, k - 1)[:k]
    top_ordered = top_unsorted[np.argsort(-scores[top_unsorted])]
    return [rows[int(i)] for i in top_ordered]


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
