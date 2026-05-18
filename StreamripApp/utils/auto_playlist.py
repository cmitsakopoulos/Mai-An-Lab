"""
KNN-based playlist generator over the library's DSP features.

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
     track (BPM, dynamics, MFCC mean/std, chroma).
  2. Take the K = target_length nearest tracks to the seed by Euclidean
     distance in that weighted space.
  3. Order the K via greedy nearest-neighbour walk anchored at the seed so
     adjacent tracks in the playlist sound close to each other.

Per-track cost on a 2000-row library: ~30 ms encode, <5 ms distance, <5 ms
walk. Memory: a single (N, 43) float64 matrix (≈680 KB at 2000 rows).
"""
from __future__ import annotations

import numpy as np

from utils.dsp import N_MFCC, N_CHROMA, unpack_embedding_groups, unpack_timbre

# Per-axis weights. Same shape as the old AutoPlaylistEngine defaults; the
# user explicitly wanted sound-profile (MFCC + chroma) to dominate, BPM to
# be a strong secondary, and the four dynamics descriptors to barely move
# the needle. Squared contribution per group:
#   BPM:        1 dim × 4.0² = 16.0
#   dynamics:   4 dims × 0.5² =  1.0   (energy, brightness, rolloff, beat)
#   MFCC mean: 13 dims × 1.5² = 29.25
#   MFCC std:  13 dims × 1.0² = 13.0
#   chroma:    12 dims × 1.5² = 27.0
_W_BPM = 4.0
_W_DYNAMICS = 0.5
_W_MFCC_MEAN = 1.5
_W_MFCC_STD = 1.0
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
        mfcc_mean, mfcc_std, chroma = groups
        paths.append(d["path"])
        rows.append(
            [float(d.get("bpm", 0) or 0),
             float(d.get("energy", 0) or 0),
             float(d.get("brightness", 0) or 0),
             float(d.get("rolloff", 0) or 0),
             float(d.get("beat_strength", 0) or 0)]
            + mfcc_mean.tolist()
            + mfcc_std.tolist()
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
    weights[s + N_MFCC:s + 2 * N_MFCC] = _W_MFCC_STD
    weights[s + 2 * N_MFCC:s + 2 * N_MFCC + N_CHROMA] = _W_CHROMA
    return paths, z * weights


def _greedy_sequence(seed_path: str,
                     paths: list[str],
                     vectors: np.ndarray) -> list[str]:
    """Order `paths` so each next track is the closest remaining one to the
    previously-selected track. Produces a smooth listening arc instead of a
    distance-sorted ramp away from the seed."""
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
        nxt = remaining[int(np.argmin(dists))]
        ordered.append(paths[nxt])
        current_vec = vectors[nxt]
        remaining.remove(nxt)
    return ordered


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
