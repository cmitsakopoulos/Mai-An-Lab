"""Per-mood logistic regression layer (phase 2 of the DSP plan).

Each mood owns one linear model `P(track is mood | features) = σ(w·x + b)`
over the percentile vector defined by `track_graph._MOOD_FEATURES`. The
model is trained online from the user's like/dislike feedback events and
persisted via `db_manager.save_mood_regressor`.

Design constraints:
  * On-device only (no server, no external model files).
  * NumPy only (already in the dep tree; OpenBLAS gives free NEON on ARM).
  * Cold-start by bootstrapping from the phase-1 weighted profile, so a
    brand-new library scores like the hand-tuned prior on day one.
  * Output blended with the phase-1 prior via a confidence ramp on
    `n_samples` — the regressor only takes over once it has enough data.

The module is intentionally stateless; it operates on plain arrays and
returns plain arrays. Persistence is the caller's job.
"""

from __future__ import annotations

import numpy as np


# Dimension of the regressor input vector. Tracks `_MOOD_FEATURES` length
# in track_graph; bumping either requires bumping FEATURES_VERSION too so
# stale regressor rows get invalidated by the version gate in db_manager.
MOOD_REGRESSOR_DIM = 8

# After this many like/dislike events on one mood, the regressor takes
# over from the phase-1 weighted-Euclidean prior. Below this, the prior
# dominates. Chosen so a user can train one mood in a single evening of
# listening — under linear models on noisy single-user data, ~30 samples
# is the inflection point where SGD weights stabilise.
N_CONFIDENT = 30

# Online SGD hyperparameters. Conservative defaults — users notice rapid
# drift in their playlists when eta is too high. L2 ridge keeps a single
# correlated feature from running away during a like-streak.
DEFAULT_ETA = 0.05
DEFAULT_L2  = 1e-4


# ─── (de)serialisation ─────────────────────────────────────────────────────

def pack_weights(w: np.ndarray) -> bytes:
    """Float32 LE blob, MOOD_REGRESSOR_DIM elements. Layout owned here so
    callers don't have to know the dtype/endianness."""
    if w.shape != (MOOD_REGRESSOR_DIM,):
        raise ValueError(
            f"pack_weights: expected shape ({MOOD_REGRESSOR_DIM},), got {w.shape}"
        )
    return w.astype("<f4").tobytes()


def unpack_weights(blob: bytes) -> np.ndarray:
    """Inverse of pack_weights. Returns a float32 array of length
    MOOD_REGRESSOR_DIM, or raises ValueError on length mismatch."""
    expected_bytes = MOOD_REGRESSOR_DIM * 4
    if len(blob) != expected_bytes:
        raise ValueError(
            f"unpack_weights: expected {expected_bytes} bytes, got {len(blob)}"
        )
    return np.frombuffer(blob, dtype="<f4").copy()


# ─── Bootstrap ──────────────────────────────────────────────────────────────

def bootstrap_from_profile(
    profile: dict[str, float | tuple[float, float]],
    feature_order: tuple[str, ...],
) -> tuple[np.ndarray, float]:
    """Derive initial (weights, bias) from a phase-1 weighted profile.

    Rule:  w_i = (target_i - 0.5) · weight_i

    Features with target above the library median (>0.5) get positive
    weights — high values of that feature mean "this is the mood". Features
    below the median get negative weights — low values mean "this is the
    mood". The weight channel scales the magnitude so high-signal features
    contribute more.

    The bias starts at zero. The regressor will adjust both during online
    updates; until ~N_CONFIDENT events accumulate, the blend() ramp keeps
    the phase-1 prior dominant anyway, so the initial values just have to
    be in the right ballpark.

    Features absent from the profile contribute zero to the score —
    consistent with the phase-1 scorer that masks them out entirely.

    Both v1 (float target) and v2 (target, weight) profile shapes are
    accepted; v1 entries are promoted to weight=1.0.
    """
    w = np.zeros(MOOD_REGRESSOR_DIM, dtype=np.float32)
    for i, feat in enumerate(feature_order):
        entry = profile.get(feat)
        if entry is None:
            continue
        if isinstance(entry, tuple) and len(entry) == 2:
            target, weight = float(entry[0]), float(entry[1])
        else:
            target, weight = float(entry), 1.0
        if weight <= 0.0:
            continue
        w[i] = (target - 0.5) * weight
    return w, 0.0


# ─── Scoring ────────────────────────────────────────────────────────────────

def _sigmoid(z: np.ndarray | float) -> np.ndarray | float:
    """Numerically-stable sigmoid. Single-precision is fine for ranking;
    the |z| clip prevents overflow on large library extremes."""
    z = np.clip(z, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-z))


def score(
    x: np.ndarray,
    weights: np.ndarray,
    bias: float,
) -> np.ndarray | float:
    """Compute σ(w·x + b). Accepts either a single feature vector (returns
    scalar) or a stack of vectors (returns 1-D array). Used both for one-shot
    "rank this track against this mood" calls and the library-wide ranking
    matrix-vector product."""
    if x.ndim == 1:
        z = float(np.dot(weights, x)) + bias
        return _sigmoid(z)
    z = x @ weights + bias
    return _sigmoid(z)


# ─── Online update ──────────────────────────────────────────────────────────

def online_update(
    x: np.ndarray,
    y: int,
    weights: np.ndarray,
    bias: float,
    eta: float = DEFAULT_ETA,
    l2: float = DEFAULT_L2,
) -> tuple[np.ndarray, float]:
    """One SGD step of logistic regression with L2 ridge.

    Gradient of log-likelihood for one sample:
        ∂/∂w = (y - σ(w·x + b)) · x
        ∂/∂b = (y - σ(w·x + b))

    With L2 ridge term -λ·w on the gradient, weights shrink toward zero
    in absence of confirming signal — guards against a few like-events
    blowing one feature's coefficient to infinity.

    Returns the updated (weights, bias). Pure function; caller persists.
    """
    if y not in (0, 1):
        raise ValueError(f"online_update: y must be 0 or 1, got {y!r}")
    p = score(x, weights, bias)
    err = float(y) - float(p)
    new_w = weights + eta * (err * x - l2 * weights)
    new_b = bias + eta * err
    return new_w.astype(np.float32), float(new_b)


# ─── Prior blending ─────────────────────────────────────────────────────────

def blend(
    prior_score: np.ndarray,
    regressor_score: np.ndarray,
    n_samples: int,
    n_confident: int = N_CONFIDENT,
) -> np.ndarray:
    """Convex combination of the phase-1 prior and the regressor output,
    weighted by how many feedback events the regressor has seen.

        γ = min(1.0, n_samples / n_confident)
        final = γ · regressor + (1-γ) · prior

    Both inputs are expected on a higher-is-better scale; the phase-1
    prior is a negative distance (closer to 0 = better) while the
    regressor returns σ(·) ∈ (0, 1). Scales differ but ranking is
    preserved within each — callers that need calibrated outputs should
    use only the regressor side. For ranking-only callers (every current
    consumer), the blend works as-is because ordering is what matters.
    """
    if n_samples <= 0:
        return prior_score.astype(np.float32, copy=True)
    gamma = min(1.0, n_samples / float(n_confident))
    return ((1.0 - gamma) * prior_score + gamma * regressor_score).astype(np.float32)
