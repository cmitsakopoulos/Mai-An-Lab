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
    KIND_ACOUSTIC: 1.0,
    KIND_ARTIST:   0.4,
    KIND_ALBUM:    0.2,
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
    temperature: float = 0.08,
    seed_rng: Optional[random.Random] = None,
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
        seed_path, kinds, k=40,
    )
    horizon: dict[str, list[dict]] = {seed_path: seed_neighbours}
    second_hop_paths = [
        n["path"] for n in seed_neighbours if n["path"] not in horizon
    ]
    # De-dup the 1-hop fan-out before issuing 2-hop queries.
    second_hop_paths = list(dict.fromkeys(second_hop_paths))
    for p in second_hop_paths:
        horizon[p] = await db_manager.get_neighbors_multi(p, kinds, k=40)

    # ── MMR setup ─────────────────────────────────────────────────────────
    # Pull embeddings for the seed + any node we might reach in two hops.
    # The visited-embedding set is what the diversity term scores against.
    diversity_active = diversity_lambda > 0
    seed_emb: Optional[np.ndarray] = None
    candidate_embs: dict[str, np.ndarray] = {}
    if diversity_active:
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
    for _ in range(length):
        # Restart roll. We always step from `current` afterwards, so toggling
        # current back to the seed implements the personalised-PageRank
        # teleport without special-casing the selection.
        if restart_prob > 0 and rng.random() < restart_prob:
            current = seed_path

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
            # If we're stuck mid-walk but the seed still has fresh options,
            # one-shot restart and try again. If both are dry, we're done.
            if current != seed_path:
                current = seed_path
                cands = _merge_candidates(horizon.get(seed_path, []))
            if not cands:
                break

        # Compute logits: effective weight, optionally penalised by the
        # max cosine to any already-visited node in the timbre sub-space.
        logits = np.empty(len(cands), dtype=np.float32)
        for i, c in enumerate(cands):
            eff = float(c["_eff"])
            if diversity_active and visited_embs:
                emb = candidate_embs.get(c["path"])
                if emb is not None:
                    sims = np.array(
                        [float(np.dot(emb, v)) for v in visited_embs],
                        dtype=np.float32,
                    )
                    eff -= float(diversity_lambda) * float(sims.max())
            logits[i] = eff

        # Softmax with temperature. Subtract max for numerical stability.
        if temperature <= 0:
            chosen_idx = int(np.argmax(logits))
        else:
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


@dataclass(frozen=True)
class MoodSpec:
    """Per-mood configuration. `profile` is the percentile-target dict over
    `_MOOD_FEATURES`; missing keys mean "this feature doesn't matter for this
    mood" and are masked out of the distance computation.

    `camelot_pref` biases the *starting anchor* of the playlist sequencer
    toward a track in that mode ("major" / "minor"). None means no preference.

    `bpm_smooth_weight` boosts the BPM-continuity penalty during sequencing
    for moods where listeners notice tempo jumps more (slow / chill / ambient).
    """
    canonical: str
    profile: dict[str, float]
    aliases: tuple[str, ...] = ()
    camelot_pref: str | None = None
    bpm_smooth_weight: float = 1.0
    # Optional dense centroid in the timbre sub-space (mfcc_mean + mfcc_delta
    # + chroma). Currently always empty for built-ins; populated for custom
    # moods so both code paths share `score_tracks_by_mood`.
    centroid: tuple[float, ...] = ()


MOODS: dict[str, MoodSpec] = {
    "chill": MoodSpec(
        "chill",
        {"bpm": 0.18, "energy": 0.15, "brightness": 0.25, "beat_strength": 0.20, "spectral_flatness": 0.20, "rolloff": 0.25, "key_mode": 0.0},
        aliases=("chilled", "relaxed", "relaxing", "calm", "mellow", "ambient", "soft"),
        camelot_pref="minor",
        bpm_smooth_weight=1.5,
    ),
    "upbeat": MoodSpec(
        "upbeat",
        {"bpm": 0.65, "energy": 0.70, "brightness": 0.75, "beat_strength": 0.75, "rolloff": 0.75, "spectral_flatness": 0.25, "spectral_contrast": 0.75, "key_mode": 1.0},
        aliases=("happy", "bright", "uplifting"),
        camelot_pref="major",
        bpm_smooth_weight=1.0,
    ),
    "energetic": MoodSpec(
        "energetic",
        {"bpm": 0.88, "energy": 0.85, "beat_strength": 0.85, "spectral_contrast": 0.75, "spectral_flatness": 0.35},
        aliases=("fast", "quick"),
        camelot_pref=None,
        bpm_smooth_weight=1.0,
    ),
    "intense": MoodSpec(
        "intense",
        {"energy": 0.90, "beat_strength": 0.82, "spectral_flatness": 0.85, "brightness": 0.75, "rolloff": 0.80, "spectral_contrast": 0.85},
        aliases=("hard", "heavy", "powerful", "noisy"),
        camelot_pref=None,
        bpm_smooth_weight=1.0,
    ),
    "moody": MoodSpec(
        "moody",
        {"bpm": 0.20, "energy": 0.30, "brightness": 0.20, "spectral_flatness": 0.25, "spectral_contrast": 0.25, "key_mode": 0.0},
        aliases=("slow", "dark", "somber"),
        camelot_pref="minor",
        bpm_smooth_weight=1.5,
    ),
    "acoustic": MoodSpec(
        "acoustic",
        {"spectral_flatness": 0.15, "spectral_contrast": 0.40, "beat_strength": 0.30, "energy": 0.35},
        aliases=("organic", "clean"),
        camelot_pref=None,
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
    both shapes are accepted so an upgrade path is unnecessary."""
    cleaned = name.lower().strip()
    cm = load_custom_moods().get(cleaned)
    if cm is None:
        return None
    centroid = tuple(cm.get("centroid") or ())
    profile = dict(cm.get("profile") or {})
    return MoodSpec(
        canonical=cleaned,
        profile=profile,
        centroid=centroid,
    )


# Derived view: every alias and every canonical name maps to its profile
# dict. Preserves the `mood in MOOD_PROFILES` and `MOOD_PROFILES.keys()`
# API for the existing call sites; do not write to this directly.
MOOD_PROFILES: dict[str, dict[str, float]] = {}
for _spec in MOODS.values():
    MOOD_PROFILES[_spec.canonical] = _spec.profile
    for _alias in _spec.aliases:
        MOOD_PROFILES[_alias] = _spec.profile
del _spec


# Public alias list, importable by assistant_intent without pulling MoodSpec.
MOOD_KEYWORDS: tuple[str, ...] = tuple(MOOD_PROFILES.keys())


# Feature columns participating in mood scoring. Order matters: weights and
# the z-scored matrix are aligned to this list. Adding a column here means
# every profile may optionally include it. NOTE: key_mode is a projected
# continuous binary mode (1.0 for major, 0.0 for minor) from key_index.
_MOOD_FEATURES = (
    "bpm", "brightness", "energy", "rolloff", "beat_strength",
    "spectral_flatness", "spectral_contrast", "key_mode",
)


# Cache for the percentile matrix. Recomputing argsort(argsort(...)) over a
# 5k-row × 8-col matrix on every mood query is wasteful when the library
# rarely changes between turns. Key is (features_version, row_count, max_path);
# `max_path` is a cheap library-change proxy — when any new track is analysed,
# the row set changes and so does the lexicographic max path.
_PERCENTILE_CACHE: dict[tuple, tuple] = {}


def _build_percentile_matrix(rows: list[dict]) -> np.ndarray:
    """Column-wise percentile ranks ∈ [0, 1] for every analysed track over
    `_MOOD_FEATURES`. Returned shape (N, len(_MOOD_FEATURES))."""
    N = len(rows)

    def get_val(r, f):
        if f == "key_mode":
            ki = r.get("key_index", 0) or 0
            return 1.0 if ki < 12 else 0.0
        return float(r.get(f, 0) or 0)

    matrix = np.array(
        [[get_val(r, f) for f in _MOOD_FEATURES] for r in rows],
        dtype=np.float32,
    )
    percentiles = np.zeros_like(matrix)
    if N <= 1:
        return percentiles
    for col in range(matrix.shape[1]):
        ranks = np.argsort(np.argsort(matrix[:, col]))
        percentiles[:, col] = ranks / float(N - 1)
    return percentiles


async def _load_percentile_matrix(
    db_manager,
    features_version: int,
) -> tuple[list[dict], np.ndarray]:
    """Returns (rows, percentile_matrix). Cached on `(features_version, N,
    sentinel_path)`; cheap to invalidate by simply checking those keys
    against the latest fetch. Cache hit → one dict-lookup; cache miss → one
    DB fetch + one N×7 percentile pass."""
    rows = await db_manager.get_tracks_with_features(features_version)
    if not rows:
        return [], np.zeros((0, len(_MOOD_FEATURES)), dtype=np.float32)
    sentinel = max(r["path"] for r in rows)
    key = (features_version, len(rows), sentinel)
    cached = _PERCENTILE_CACHE.get(key)
    if cached is not None:
        cached_rows, cached_matrix = cached
        return cached_rows, cached_matrix
    matrix = _build_percentile_matrix(rows)
    _PERCENTILE_CACHE.clear()  # only ever hold the latest snapshot
    _PERCENTILE_CACHE[key] = (rows, matrix)
    logger.info(
        "track_graph: percentile cache rebuilt (N=%d, features_version=%d)",
        len(rows), features_version,
    )
    return rows, matrix


def _score_against_profile(
    profile: dict[str, float],
    percentiles: np.ndarray,
) -> np.ndarray:
    """Negative Euclidean distance from each row's percentile vector to the
    profile target, restricted to features the profile actually specifies.
    Higher is better. Returns shape (N,)."""
    if percentiles.shape[0] == 0:
        return np.zeros(0, dtype=np.float32)
    mask = np.array([f in profile for f in _MOOD_FEATURES], dtype=bool)
    if not mask.any():
        return np.zeros(percentiles.shape[0], dtype=np.float32)
    target = np.array(
        [profile.get(f, 0.0) for f in _MOOD_FEATURES],
        dtype=np.float32,
    )
    diff = percentiles[:, mask] - target[mask]
    return -np.sqrt((diff * diff).sum(axis=1))


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


# Weight balance for the unified scorer. β controls how much the listener-
# feedback signal (B3) re-ranks the top candidates. α controls the
# scalar-profile vs timbre-centroid balance for moods that carry both.
_MOOD_BETA_LISTEN = 0.20
_MOOD_ALPHA_SCALAR = 0.50


async def tracks_by_mood(
    db_manager,
    mood: str,
    limit: int = 12,
    features_version: int = FEATURES_VERSION,
) -> list[dict]:
    """Rank tracks by how well they match a mood's DSP profile, with
    optional listener-feedback re-rank.

    Scoring pipeline:
      1. Resolve the mood alias to a canonical MoodSpec or to a custom-mood
         spec built from custom_moods.json.
      2. Look up (or compute + cache) the library's percentile matrix over
         `_MOOD_FEATURES`.
      3. If the spec has a percentile profile, score by negative Euclidean
         distance in percentile space. If the spec has a timbre centroid,
         score by cosine. Specs with both (typical for new custom moods)
         get the convex combination α·scalar + (1-α)·centroid.
      4. Re-rank the top candidates by (1-β)·mood_score + β·listen_signal
         where listen_signal ∈ [-1, 1] is built from playback_history.

    Library-relative throughout — the same word picks different tracks in
    different libraries. Returns [] for unknown moods or empty libraries.
    """
    spec = _mood_spec(mood)
    if spec is None:
        spec = _custom_mood_spec(mood)
    if spec is None:
        return []

    # Substitute customized profile from the database if present
    custom_profile = await db_manager.get_adjusted_mood_profile(spec.canonical)
    if custom_profile is not None:
        spec = MoodSpec(
            canonical=spec.canonical,
            profile=custom_profile,
            aliases=spec.aliases,
            camelot_pref=spec.camelot_pref,
            bpm_smooth_weight=spec.bpm_smooth_weight,
            centroid=spec.centroid
        )

    rows, percentiles = await _load_percentile_matrix(db_manager, features_version)
    if not rows:
        return []

    has_profile = bool(spec.profile)
    has_centroid = bool(spec.centroid)

    scalar_score = (
        _score_against_profile(spec.profile, percentiles)
        if has_profile else np.zeros(len(rows), dtype=np.float32)
    )

    # Centroid scoring needs the timbre matrix aligned to `rows`. Skip the
    # work entirely when no centroid is set (every built-in mood).
    if has_centroid:
        centroid = np.array(spec.centroid, dtype=np.float32)
        timbres_list: list[np.ndarray] = []
        keep_idx: list[int] = []
        for i, r in enumerate(rows):
            v = unpack_timbre(r.get("timbre"))
            if v is not None:
                timbres_list.append(v)
                keep_idx.append(i)
        if timbres_list:
            timbres = np.stack(timbres_list, axis=0)
            cent_scores_partial = _score_against_centroid(centroid, timbres)
            centroid_score = np.full(len(rows), -np.inf, dtype=np.float32)
            for j, src_i in enumerate(keep_idx):
                centroid_score[src_i] = cent_scores_partial[j]
        else:
            centroid_score = np.zeros(len(rows), dtype=np.float32)
            has_centroid = False
    else:
        centroid_score = np.zeros(len(rows), dtype=np.float32)

    if has_profile and has_centroid:
        alpha = _MOOD_ALPHA_SCALAR
        scores = alpha * scalar_score + (1.0 - alpha) * centroid_score
    elif has_centroid:
        scores = centroid_score
    elif has_profile:
        scores = scalar_score
    else:
        return []

    # B3: Listener feedback. We only re-rank the top 3× limit candidates so
    # signal noise on the long tail (a single skip on a low-scoring track
    # shouldn't yank an irrelevant track into the result).
    rerank_window = max(limit * 3, 12)
    if scores.size > rerank_window:
        cand_idx = np.argpartition(-scores, rerank_window - 1)[:rerank_window]
    else:
        cand_idx = np.arange(scores.size)
    try:
        signal = await db_manager.listen_signal_map()
    except Exception:
        signal = {}
    if signal:
        beta = _MOOD_BETA_LISTEN
        for i in cand_idx:
            s = signal.get(rows[int(i)]["path"])
            if s is not None:
                scores[int(i)] = (1.0 - beta) * float(scores[int(i)]) + beta * float(s)

    k = min(limit, scores.size)
    top_unsorted = np.argpartition(-scores, k - 1)[:k]
    top_ordered = top_unsorted[np.argsort(-scores[top_unsorted])]
    return [rows[int(i)] for i in top_ordered]


async def adjust_mood_profile(db_manager, mood: str, track_path: str, feedback: int):
    """
    Perform target percentiles gradient shifts on a mood profile.
    feedback: 1 = like (shift towards), -1 = dislike (shift away).
    clamped to [0, 1]. Saves to mood_profiles.
    """
    # 1. Resolve canonical mood name
    canonical = mood_canonical(mood)
    if canonical is None:
        logger.warning("adjust_mood_profile: Unknown mood %s", mood)
        return

    # 2. Get the current spec to find the features we care about and default targets
    spec = _mood_spec(canonical)
    if spec is None:
        spec = _custom_mood_spec(canonical)
    if spec is None:
        logger.warning("adjust_mood_profile: No MoodSpec found for %s", canonical)
        return

    # Load any already adjusted profile, or fall back to spec.profile
    current_profile = await db_manager.get_adjusted_mood_profile(canonical)
    if current_profile is None:
        current_profile = dict(spec.profile)
    else:
        current_profile = dict(current_profile)

    # 3. Load the percentile matrix to find this track's percentile vector
    rows, percentiles = await _load_percentile_matrix(db_manager, FEATURES_VERSION)
    track_idx = None
    for i, r in enumerate(rows):
        if r["path"] == track_path:
            track_idx = i
            break

    if track_idx is None:
        logger.warning("adjust_mood_profile: Track %s not found in percentile matrix", track_path)
        return

    # Extract track's percentile vector for _MOOD_FEATURES
    track_percentiles = percentiles[track_idx]
    track_feat_map = {f: float(track_percentiles[col]) for col, f in enumerate(_MOOD_FEATURES)}

    # 4. Perform gradient shift for all features defined in current_profile
    # T_new = T_old +/- 0.15 * (P_track - T_old), clamped to [0, 1]
    eta = 0.15
    new_profile = {}
    for feat, t_old in current_profile.items():
        if feat not in track_feat_map:
            new_profile[feat] = t_old
            continue
        p_track = track_feat_map[feat]
        if feedback == 1:
            # Shift towards
            t_new = t_old + eta * (p_track - t_old)
        elif feedback == -1:
            # Shift away
            t_new = t_old - eta * (p_track - t_old)
        else:
            t_new = t_old
        # Clamp to [0, 1]
        t_new = max(0.0, min(1.0, float(t_new)))
        new_profile[feat] = t_new

    # 5. Save the adjusted profile
    await db_manager.save_adjusted_mood_profile(canonical, new_profile)
    logger.info("adjust_mood_profile: Adjusted profile for mood '%s' based on track '%s' (feedback: %d)",
                canonical, track_path, feedback)


async def tracks_in_islet(
    db_manager,
    name: str,
    features_version: int = FEATURES_VERSION,
    min_count: int = ISLET_MIN,
) -> list[dict]:
    """Members of a named islet, ranked by similarity to the centroid.

    Membership rule: cosine(track.timbre, islet.centroid) >= islet.threshold.
    Returns at most `ISLET_MAX` rows, sorted high-to-low similarity.

    By default returns [] if fewer than `ISLET_MIN` tracks pass — a centroid
    that doesn't generalise produces no playable queue. Pass `min_count=0`
    when the caller wants honest below-floor membership (e.g. the Library
    view, which still needs to render the accordion so the user can loosen
    a too-tight threshold instead of thinking the islet was deleted).
    """
    cm = load_custom_moods().get(name.lower().strip())
    if cm is None:
        return []
    centroid_list = cm.get("centroid") or []
    if not centroid_list:
        return []
    threshold = float(cm.get("threshold", ISLET_THRESHOLD))
    centroid = np.array(centroid_list, dtype=np.float32)

    rows = await db_manager.get_tracks_with_features(features_version)
    if not rows:
        return []

    timbres_list: list[np.ndarray] = []
    keep_idx: list[int] = []
    for i, r in enumerate(rows):
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
