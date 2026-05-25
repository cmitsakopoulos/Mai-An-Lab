"""Global user-preference (taste) model.

A single logistic-regression model over PC features that predicts
`P(user likes this track | features)`. Distinct from the (now-retired)
per-mood regressor that previously backed islet membership:

  * **One model**, not one-per-mood. Taste cuts across moods.
  * **Sample-weight aware** — explicit like/dislike events weigh more than
    implicit "played past the threshold" signals.
  * **No bootstrap, no blend.** Cold start is `w=0, b=0` → σ ≈ 0.5 for
    everything, which is the right behaviour (no preference signal yet).
    Callers must check `n_explicit + n_implicit == 0` and skip the re-rank
    in that case.

Persistence lives in `db_manager.user_taste_model`. This module is pure
NumPy and stateless; the in-memory cache is owned by `track_graph`.
"""

from __future__ import annotations

import numpy as np


# Must match `track_graph._MOOD_FEATURES` length. Bumping requires bumping
# FEATURES_VERSION so stale weights get invalidated by the version gate.
TASTE_MODEL_DIM = 3

# SGD hyperparameters. Slightly more conservative than the per-mood
# regressor — taste drifts more slowly than mood and is used in more
# places, so a noisy update would have wider blast radius.
DEFAULT_ETA = 0.04
DEFAULT_L2 = 1e-4

# Default per-sample weights. Implicit signals are noisier (a 45 s play
# could be background listening, a skip could be a phone call) so they
# count for less than an explicit like/dislike. A long-form replay or a
# very-fast skip is upweighted by the caller.
WEIGHT_EXPLICIT = 1.0
WEIGHT_IMPLICIT = 0.5


# ─── (de)serialisation ─────────────────────────────────────────────────────

def pack_weights(w: np.ndarray) -> bytes:
    if w.shape != (TASTE_MODEL_DIM,):
        raise ValueError(
            f"pack_weights: expected shape ({TASTE_MODEL_DIM},), got {w.shape}"
        )
    return w.astype("<f4").tobytes()


def unpack_weights(blob: bytes) -> np.ndarray:
    expected = TASTE_MODEL_DIM * 4
    if len(blob) != expected:
        raise ValueError(
            f"unpack_weights: expected {expected} bytes, got {len(blob)}"
        )
    return np.frombuffer(blob, dtype="<f4").copy()


def fresh() -> tuple[np.ndarray, float]:
    """Cold-start weights. σ(0) = 0.5 for every track until the first event."""
    return np.zeros(TASTE_MODEL_DIM, dtype=np.float32), 0.0


# ─── Scoring ────────────────────────────────────────────────────────────────

def _sigmoid(z):
    z = np.clip(z, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-z))


def score(
    x: np.ndarray,
    weights: np.ndarray,
    bias: float,
) -> np.ndarray | float:
    """σ(w·x + b). Vector or matrix input."""
    if x.ndim == 1:
        return _sigmoid(float(np.dot(weights, x)) + bias)
    return _sigmoid(x @ weights + bias)


# ─── Online update ──────────────────────────────────────────────────────────

def online_update(
    x: np.ndarray,
    y: int,
    weights: np.ndarray,
    bias: float,
    sample_weight: float = 1.0,
    eta: float = DEFAULT_ETA,
    l2: float = DEFAULT_L2,
) -> tuple[np.ndarray, float]:
    """One weighted SGD step of logistic regression with L2 ridge.

    Gradient for a single sample with weight α:
        ∂L/∂w = α · (y - σ(w·x + b)) · x
        ∂L/∂b = α · (y - σ(w·x + b))

    `sample_weight` lets the caller down-weight implicit signals — passing
    0.5 means an implicit play counts for half an explicit like.
    """
    if y not in (0, 1):
        raise ValueError(f"online_update: y must be 0 or 1, got {y!r}")
    p = score(x, weights, bias)
    err = float(sample_weight) * (float(y) - float(p))
    new_w = weights + eta * (err * x - l2 * weights)
    new_b = bias + eta * err
    return new_w.astype(np.float32), float(new_b)


# ─── Implicit-feedback classification ──────────────────────────────────────

# A 60 s ambient piece played in full is different from bailing on a
# 4 min pop song at 45 s. Use the stricter of the two thresholds.
POSITIVE_PLAY_SECONDS = 45.0
POSITIVE_PLAY_FRACTION = 0.30
# Skips inside the first NEGATIVE_SKIP_MIN_SECONDS are usually accidental
# taps or queue corrections — discard rather than learning from them.
NEGATIVE_SKIP_MIN_SECONDS = 5.0


def classify_play_event(
    played_seconds: float,
    duration_seconds: float,
) -> int | None:
    """Map a single playback event to a y label, or None to discard.

    Returns:
        1 — listener engaged with the track (treat as positive sample).
        0 — listener bailed deliberately (treat as negative sample).
        None — too short to interpret, do not train.

    `duration_seconds == 0` (unknown track length) falls back to the
    absolute-time threshold; better to under-train than mis-train when
    the metadata is incomplete.
    """
    if played_seconds < NEGATIVE_SKIP_MIN_SECONDS:
        return None
    if played_seconds >= POSITIVE_PLAY_SECONDS:
        return 1
    if duration_seconds > 0 and (played_seconds / duration_seconds) >= POSITIVE_PLAY_FRACTION:
        return 1
    return 0
