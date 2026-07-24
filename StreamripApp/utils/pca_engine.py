"""Acoustic feature covariance analysis + genre-taxonomy re-exports.

Two things live here now:
  • The unsupervised redundancy cleaver (`redundant_raw_features` /
    `_redundant_features_from`): given a library's raw scalar descriptors it
    finds the collinear ones (|r| ≥ 0.70) and returns the redundant set, so the
    acoustic graph build (`track_graph.build_acoustic_edges`) can drop them from
    the feature space instead of double-counting them toward distance.
  • Backward-compatible re-exports of the coarse genre taxonomy, which was split
    out to `genre_taxonomy` (pure stdlib) so the metadata layer can reach it
    without importing numpy. `from utils.pca_engine import genre_bucket` and
    friends keep working unchanged.

History — this module used to also hold `calculate_pca_projection` (the
mood/taste 8×3 PCA, dead since the mood redesign retired the per-mood regressor)
and two matplotlib report builders (`plot_pca_report`, `plot_genre_report`). All
three were removed: they had no callers, and the on-device diagnostics moved to
tools/ (`pca_analysis.py`, `projection_diagnostic.py`, `genre_audit.py`). The
scalar feature list also dropped `key_mode` — the graph geometry stopped using
key entirely (see `track_graph._SCALAR_ORDER`), so key can no longer belong to
the redundancy feature space.
"""

import numpy as np
import logging

logger = logging.getLogger(__name__)

# Participative raw features in the covariance redundancy analysis. Mirrors
# `track_graph._SCALAR_ORDER` exactly — the 7 scalar descriptors the acoustic
# graph actually projects. (`key_mode` was removed alongside the harmonic block:
# key carried no genre information and is no longer part of the geometry, so
# keeping it here only added a phantom column that could never touch distance.)
_RAW_FEATURES = [
    "bpm", "brightness", "energy", "rolloff", "beat_strength",
    "spectral_flatness", "spectral_contrast",
]

def extract_feature_vector(row: dict) -> list[float]:
    """Extract raw DB scalar features as a consistent float list, in
    `_RAW_FEATURES` order."""
    return [float(row.get(f, 0) or 0) for f in _RAW_FEATURES]


def _redundant_features_from(
    corr_matrix: np.ndarray,
    threshold: float = 0.70,
) -> set[str]:
    """Centrality-based redundancy cleaving over ``_RAW_FEATURES``.

    Two features correlating at ``|r| >= threshold`` are redundant; within a
    correlated cluster we keep the most *central* feature — the one with the
    highest mean |r| to its clustermates, i.e. the best single proxy for the
    ones dropped — and drop the rest. Greedy in descending centrality so each
    cluster's representative survives. Pure function of the Pearson matrix, so
    the acoustic graph build (`redundant_raw_features`) and any diagnostic tool
    agree on "redundant".

    Why centrality and not PCA variance: after z-scoring every feature has the
    same total variance, so a variance ranking only differentiates via the top
    few PCs — a fragile, near-tie signal that can elect a noisy axis (e.g.
    spectral_flatness) over an interpretable, representative one (brightness).
    Centrality answers the actual question: which feature best stands in for
    its redundant group.
    """
    C = np.nan_to_num(np.asarray(corr_matrix, dtype=float))  # zero-var cols → 0
    D = len(_RAW_FEATURES)

    # Clustermates: the features each one is redundant with (|r| ≥ threshold).
    mates: list[list[int]] = [[] for _ in range(D)]
    for i in range(D):
        for j in range(D):
            if i != j and abs(C[i, j]) >= threshold:
                mates[i].append(j)

    # Centrality = mean |r| to clustermates (0 ⇒ isolated, never dropped).
    centrality = np.zeros(D, dtype=float)
    for i in range(D):
        if mates[i]:
            centrality[i] = float(np.mean([abs(C[i, j]) for j in mates[i]]))

    # Greedy: process most-central first; a kept feature drops its mates.
    # Tie-break on lower feature index for determinism.
    order = sorted(range(D), key=lambda i: (centrality[i], -i), reverse=True)
    redundant: set[str] = set()
    kept: set[int] = set()
    for i in order:
        if not mates[i] or _RAW_FEATURES[i] in redundant:
            continue
        kept.add(i)
        for j in mates[i]:
            if j in kept or _RAW_FEATURES[j] in redundant:
                continue
            redundant.add(_RAW_FEATURES[j])
            logger.warning(
                "PCA Engine: Cleaving redundant feature '%s' (|r|=%.2f with "
                "more-central '%s')", _RAW_FEATURES[j], abs(C[i, j]), _RAW_FEATURES[i],
            )
    return redundant


def redundant_raw_features(rows: list[dict], threshold: float = 0.70) -> set[str]:
    """Raw scalar features the covariance analysis deems redundant for *this*
    library. Empty for libraries < 50 tracks (too little data to trust the
    correlations). Used by the acoustic graph build so the projected feature
    space excludes collinear scalars.
    """
    N = len(rows)
    D = len(_RAW_FEATURES)
    if N < 50:
        return set()
    X = np.zeros((N, D), dtype=np.float32)
    for idx, r in enumerate(rows):
        X[idx, :] = extract_feature_vector(r)
    stds = np.std(X, axis=0)
    stds[stds == 0] = 1.0
    X_scaled = (X - np.mean(X, axis=0)) / stds
    with np.errstate(invalid="ignore", divide="ignore"):
        corr_matrix = np.corrcoef(X_scaled.T)
    return _redundant_features_from(corr_matrix, threshold)


# ─── Genre taxonomy re-exports ──────────────────────────────────────────────
#
# The coarse genre taxonomy lives in `genre_taxonomy` (pure stdlib) so the
# metadata layer can import it without dragging numpy onto the walk's hot path.
# Re-exported here unchanged so `from utils.pca_engine import genre_bucket` and
# friends keep working exactly as before.
from utils.genre_taxonomy import (           # noqa: F401,E402  (re-export)
    _GENRE_RULES,
    _GENRE_PALETTE,
    GENRE_BUCKET_LABELS,
    NON_FAMILIES,
    genre_bucket,
    genre_tokens,
    genre_families,
    genre_display_label,
)
