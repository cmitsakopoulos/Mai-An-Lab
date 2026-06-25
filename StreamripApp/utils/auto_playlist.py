"""
KNN-based playlist generator over the library's DSP features, with
mood-aware harmonic sequencing.

Replaces the previous MCL clustering pipeline (Markov Clustering with
string-similarity blending). MCL spent most of its runtime on matrix
powers + column-stochastic normalisation that boiled down to "find the K
nearest neighbours of the seed and order them smoothly". This module does
exactly that, in a few numpy ops, with no convergence loop.

Inputs are the rows returned by `db_manager.get_tracks_with_features` —
every track in the library that's been DSP-analysed. The assistant's
library-wide sweep is the only producer of those features, so the playlist
generator and the assistant share one source of truth.

Pipeline:
  1. Encode tracks into a single weighted z-scored feature vector per
     track (BPM, dynamics, MFCC mean/delta, chroma).
  2. Take the K = target_length nearest tracks to the seed by Euclidean
     distance in that weighted space (KNN path) OR pull the top mood-ranked
     rows from `track_graph.tracks_by_mood` (mood path).
  3. Greedily order the K with `_greedy_sequence`. The sequencer accepts an
     optional `transition_cost(a, b)` callable that's added to the raw
     feature distance; the mood path layers a Camelot harmonic-adjacency
     penalty + a BPM-smoothness penalty (scaled by the mood spec's
     `bpm_smooth_weight`) so playlists don't jar between adjacent tracks
     even when the timbre profile is similar.
  4. For mood playlists with a `camelot_pref`, the sequencer anchors on
     the highest-ranked track that matches the requested mode (major /
     minor) so the playlist's opening sets the harmonic tone.

Per-track cost on a 2000-row library: ~30 ms encode, <5 ms distance, <5 ms
walk. Memory: a single (N, 57) float64 matrix (≈900 KB at 2000 rows).
"""
from __future__ import annotations

from typing import Callable, Optional

import numpy as np

from utils.dsp import N_MFCC, N_CHROMA, unpack_embedding_groups, unpack_timbre
from utils.harmonic import camelot_penalty, matches_mode_preference

# Per-axis weights. Same shape as the old AutoPlaylistEngine defaults; the
# user explicitly wanted sound-profile (MFCC + chroma) to dominate, BPM to
# be a strong secondary, and the four dynamics descriptors to barely move
# the needle. Squared contribution per group (v3 layout):
#   BPM:         1 dim  × 4.0² = 16.0
#   dynamics:    4 dims × 0.5² =  1.0   (energy, brightness, rolloff, beat)
#   MFCC mean:  20 dims × 1.5² = 45.0
#   MFCC delta: 20 dims × 1.0² = 20.0   (temporal evolution, replaces std)
#   chroma:     12 dims × 1.5² = 27.0
_W_BPM = 4.0
_W_DYNAMICS = 0.5
_W_MFCC_MEAN = 1.5
_W_MFCC_DELTA = 1.0
_W_CHROMA = 1.5


def _encode(tracks: list[dict]) -> tuple[list[str], np.ndarray]:
    """Returns (paths, weighted_z_scored_matrix). Entries without features
    are silently dropped — callers should pre-filter for clarity."""
    paths: list[str] = []
    rows: list[list[float]] = []
    for d in tracks:
        groups = unpack_embedding_groups(d.get("timbre"))
        if groups is None:
            continue
        # v4 returns 5 groups; this selector keeps the v3 weighting (mean /
        # delta / chroma). mfcc_std and rhythm are carried in the BLOB for the
        # graph build but intentionally not re-weighted here yet.
        mfcc_mean, _mfcc_std, mfcc_delta, chroma, _rhythm = groups
        paths.append(d["path"])
        rows.append(
            [float(d.get("bpm", 0) or 0),
             float(d.get("energy", 0) or 0),
             float(d.get("brightness", 0) or 0),
             float(d.get("rolloff", 0) or 0),
             float(d.get("beat_strength", 0) or 0)]
            + mfcc_mean.tolist()
            + mfcc_delta.tolist()
            + chroma.tolist()
        )

    raw = np.array(rows, dtype=np.float64)
    means = raw.mean(axis=0)
    stds = raw.std(axis=0) + 1e-6
    z = (raw - means) / stds

    n_dynamics = 4
    weights = np.empty(z.shape[1], dtype=np.float64)
    weights[0] = _W_BPM
    weights[1:1 + n_dynamics] = _W_DYNAMICS
    s = 1 + n_dynamics
    weights[s:s + N_MFCC] = _W_MFCC_MEAN
    weights[s + N_MFCC:s + 2 * N_MFCC] = _W_MFCC_DELTA
    weights[s + 2 * N_MFCC:s + 2 * N_MFCC + N_CHROMA] = _W_CHROMA
    return paths, z * weights


def _greedy_sequence(seed_path: str,
                     paths: list[str],
                     vectors: np.ndarray,
                     transition_cost: Optional[
                         Callable[[int, int], float]
                     ] = None) -> list[str]:
    """Order `paths` so each next track is the lowest-cost remaining one
    given the previously-selected track. Produces a smooth listening arc
    instead of a distance-sorted ramp away from the seed.

    `transition_cost(from_idx, to_idx) -> float` is an optional extra cost
    added to the feature-space distance. Used by the mood path to layer
    Camelot-adjacency and BPM-smoothness penalties on top of the timbre
    distance; defaults to pure feature distance when omitted."""
    if len(paths) <= 1:
        return list(paths)

    ordered = [seed_path]
    current_idx = paths.index(seed_path)
    remaining = list(range(len(paths)))
    remaining.remove(current_idx)
    current_vec = vectors[current_idx]

    while remaining:
        candidates = vectors[remaining]
        dists = np.linalg.norm(candidates - current_vec, axis=1)
        if transition_cost is not None:
            extras = np.array(
                [transition_cost(current_idx, j) for j in remaining],
                dtype=np.float64,
            )
            dists = dists + extras
        nxt = remaining[int(np.argmin(dists))]
        ordered.append(paths[nxt])
        current_vec = vectors[nxt]
        current_idx = nxt
        remaining.remove(nxt)
    return ordered


async def generate_mood_playlist(db_manager,
                                  mood: str,
                                  target_length: int = 20) -> list[dict]:
    """Library-wide mood-driven playlist generator.

    Pipeline:
      1. `track_graph.tracks_by_mood` ranks every analysed track by the
         mood profile (percentile distance + optional centroid + listen
         feedback) and returns the top `target_length` rows.
      2. Encode those rows into the same weighted feature space the KNN
         selector uses, then greedily re-order them with a transition cost
         that combines timbre distance, Camelot-adjacency penalty, and a
         BPM-smoothness penalty scaled by the mood's `bpm_smooth_weight`.
         For moods with a `camelot_pref`, the anchor is biased toward the
         highest-ranked track in that mode (falling back to plain rank if
         none survives feature encoding).

    Returns the ordered list of full track-row dicts (path + metadata),
    not just paths, so callers can both persist them and surface their
    titles in confirmation messages without a second DB round-trip.

    Returns [] for unknown moods, libraries with < 2 analysed tracks, or
    when ranking produces nothing usable. Callers should check the length.
    """
    from utils import track_graph as tg

    if mood not in tg.MOOD_PROFILES:
        return []
    spec = tg._mood_spec(mood) or tg._custom_mood_spec(mood)

    ranked = await tg.tracks_by_mood(db_manager, mood, limit=max(target_length, 2))
    if len(ranked) < 2:
        return ranked  # nothing to order; caller decides whether 1 track is useful

    # Re-use the KNN selector's encoding so the sequencing step operates in
    # the same weighted space the mood ranker effectively scored over.
    paths, vectors = _encode(ranked)
    if not paths:
        return ranked

    by_path = {r["path"]: r for r in ranked}
    # Anchor on the highest-ranked track whose mode matches the mood's
    # camelot_pref, if any. Falls back to plain rank order if none match.
    seed_path = paths[0]
    if spec and spec.camelot_pref:
        for p in paths:
            row = by_path.get(p)
            if row is None:
                continue
            if matches_mode_preference(int(row.get("key_index", 0) or 0),
                                       spec.camelot_pref):
                seed_path = p
                break

    # Per-path metadata used by the transition cost. Pre-extracted so the
    # inner loop is a few dict-lookups, not a re-scan of `ranked`.
    key_idx = [int(by_path[p].get("key_index", 0) or 0) for p in paths]
    bpm = [float(by_path[p].get("bpm", 0) or 0) for p in paths]
    bpm_smooth = spec.bpm_smooth_weight if spec else 1.0
    # Reference scale: a 50 BPM jump roughly equals one full unit of
    # additional cost (the timbre distance term sits in ~[0, 5] after
    # weighting, so 1.0 is significant but not dominant).
    BPM_REF = 50.0
    # Harmonic clash penalty cap. Camelot distance ∈ [0, 6] → penalty in
    # [0, 1] from `camelot_penalty`; scale to roughly the same magnitude as
    # the BPM term so neither dominates.
    HARM_SCALE = 1.0

    def _transition_cost(a: int, b: int) -> float:
        bpm_pen = abs(bpm[a] - bpm[b]) / BPM_REF * bpm_smooth
        harm_pen = camelot_penalty(key_idx[a], key_idx[b]) * HARM_SCALE
        return float(bpm_pen + harm_pen)

    ordered_paths = _greedy_sequence(
        seed_path, paths, vectors, transition_cost=_transition_cost,
    )
    return [by_path[p] for p in ordered_paths if p in by_path]


def generate_knn_playlist(seed_path: str,
                           tracks: list[dict],
                           target_length: int = 20) -> list[str]:
    """Return up to `target_length` paths, starting at `seed_path`, picked as
    its K nearest neighbours in the weighted feature space and ordered by a
    greedy nearest-neighbour walk for a smooth flow.

    `tracks` is the analysed-library pool — typically the full output of
    `db_manager.get_tracks_with_features(FEATURES_VERSION)`. Robust to
    missing seeds / short input: returns [seed_path] when there's not
    enough usable data to form a meaningful selection. Callers can fall
    back to a single-track playlist in that case.
    """
    if not tracks or target_length <= 0:
        return [seed_path]

    usable = [
        d for d in tracks
        if (d.get("bpm") or 0) > 0 and unpack_timbre(d.get("timbre")) is not None
    ]
    if not usable:
        return [seed_path]

    paths, vectors = _encode(usable)
    if seed_path not in paths or len(paths) < 2:
        return [seed_path]

    seed_idx = paths.index(seed_path)
    dists = np.linalg.norm(vectors - vectors[seed_idx], axis=1)

    # argpartition with k-1 gives the indices of the K smallest distances
    # (unordered). target_length includes the seed itself so we ask for
    # target_length entries — the seed sits at distance 0 and is always in.
    k = min(target_length, len(paths))
    top = np.argpartition(dists, k - 1)[:k]
    selected_paths = [paths[i] for i in top]
    selected_vecs = vectors[top]
    return _greedy_sequence(seed_path, selected_paths, selected_vecs)
