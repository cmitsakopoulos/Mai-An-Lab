"""Unit tests for utils.mood_regressor.

Pure-function tests — no DB, no track_graph; the module is intentionally
stateless. Persistence and integration are covered by test_mood_feedback.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
from utils import mood_regressor as mr


# Mirrors track_graph._MOOD_FEATURES; duplicated here to keep this suite
# free of the track_graph import chain.
_FEATURE_ORDER = (
    "bpm", "brightness", "energy", "rolloff", "beat_strength",
    "spectral_flatness", "spectral_contrast", "key_mode",
)


class TestPackRoundTrip(unittest.TestCase):
    def test_round_trip_preserves_values(self):
        w = np.array([0.1, -0.2, 0.3, -0.4, 0.5, -0.6, 0.7, -0.8], dtype=np.float32)
        unpacked = mr.unpack_weights(mr.pack_weights(w))
        np.testing.assert_array_equal(unpacked, w)

    def test_pack_wrong_shape_raises(self):
        with self.assertRaises(ValueError):
            mr.pack_weights(np.zeros(7, dtype=np.float32))

    def test_unpack_wrong_length_raises(self):
        with self.assertRaises(ValueError):
            mr.unpack_weights(b"\x00" * 12)


class TestBootstrap(unittest.TestCase):
    def test_bootstrap_recovers_profile_sign(self):
        """Features with target above 0.5 should get positive weights;
        below 0.5 should get negative weights. Above-median values of that
        feature mean 'this is the mood'."""
        profile = {
            "energy":            (0.9, 1.0),   # target high → positive w
            "spectral_flatness": (0.1, 1.0),   # target low  → negative w
        }
        w, b = mr.bootstrap_from_profile(profile, _FEATURE_ORDER)
        self.assertEqual(b, 0.0)
        # energy at index 2 of _FEATURE_ORDER
        self.assertGreater(w[_FEATURE_ORDER.index("energy")], 0.0)
        # spectral_flatness at index 5
        self.assertLess(w[_FEATURE_ORDER.index("spectral_flatness")], 0.0)

    def test_bootstrap_weight_scales_magnitude(self):
        """A feature with weight 2× should bootstrap to 2× the coefficient
        magnitude vs weight 1×."""
        profile_light = {"energy": (0.9, 1.0)}
        profile_heavy = {"energy": (0.9, 2.0)}
        w_light, _ = mr.bootstrap_from_profile(profile_light, _FEATURE_ORDER)
        w_heavy, _ = mr.bootstrap_from_profile(profile_heavy, _FEATURE_ORDER)
        i = _FEATURE_ORDER.index("energy")
        self.assertAlmostEqual(w_heavy[i], 2.0 * w_light[i], places=5)

    def test_bootstrap_absent_features_are_zero(self):
        """Features missing from the profile must contribute zero coefficient
        — consistent with the phase-1 scorer which masks them out."""
        profile = {"energy": (0.9, 1.0)}
        w, _ = mr.bootstrap_from_profile(profile, _FEATURE_ORDER)
        for i, feat in enumerate(_FEATURE_ORDER):
            if feat == "energy":
                continue
            self.assertEqual(w[i], 0.0, f"feature {feat!r} should bootstrap to 0")

    def test_bootstrap_zero_weight_silences_feature(self):
        """weight=0 in the profile must not contribute, matching the
        phase-1 'silenced feature' semantics."""
        profile = {"energy": (0.9, 0.0)}
        w, _ = mr.bootstrap_from_profile(profile, _FEATURE_ORDER)
        self.assertEqual(w[_FEATURE_ORDER.index("energy")], 0.0)


class TestScore(unittest.TestCase):
    def test_score_returns_scalar_for_vector_input(self):
        x = np.full(mr.MOOD_REGRESSOR_DIM, 0.5, dtype=np.float32)
        w = np.zeros(mr.MOOD_REGRESSOR_DIM, dtype=np.float32)
        s = mr.score(x, w, 0.0)
        self.assertIsInstance(s, float)
        self.assertAlmostEqual(s, 0.5, places=5)   # σ(0) = 0.5

    def test_score_returns_array_for_matrix_input(self):
        X = np.full((3, mr.MOOD_REGRESSOR_DIM), 0.5, dtype=np.float32)
        w = np.zeros(mr.MOOD_REGRESSOR_DIM, dtype=np.float32)
        s = mr.score(X, w, 0.0)
        self.assertEqual(s.shape, (3,))
        np.testing.assert_allclose(s, 0.5, atol=1e-5)

    def test_score_in_unit_interval(self):
        x = np.full(mr.MOOD_REGRESSOR_DIM, 1.0, dtype=np.float32)
        w = np.full(mr.MOOD_REGRESSOR_DIM, 100.0, dtype=np.float32)
        # Without clipping this would overflow; the stable sigmoid clips
        # the logit so we still get a probability in (0, 1).
        s = mr.score(x, w, 0.0)
        self.assertGreater(s, 0.0)
        self.assertLessEqual(s, 1.0)


class TestOnlineUpdate(unittest.TestCase):
    def test_online_update_increases_score_for_positive_label(self):
        """After one positive update, σ(w·x + b) for the same x should rise."""
        x = np.full(mr.MOOD_REGRESSOR_DIM, 0.5, dtype=np.float32)
        w = np.zeros(mr.MOOD_REGRESSOR_DIM, dtype=np.float32)
        b = 0.0
        s_before = mr.score(x, w, b)
        w, b = mr.online_update(x, y=1, weights=w, bias=b)
        s_after = mr.score(x, w, b)
        self.assertGreater(s_after, s_before)

    def test_online_update_decreases_score_for_negative_label(self):
        x = np.full(mr.MOOD_REGRESSOR_DIM, 0.5, dtype=np.float32)
        w = np.zeros(mr.MOOD_REGRESSOR_DIM, dtype=np.float32)
        b = 0.0
        s_before = mr.score(x, w, b)
        w, b = mr.online_update(x, y=0, weights=w, bias=b)
        s_after = mr.score(x, w, b)
        self.assertLess(s_after, s_before)

    def test_online_update_invalid_label_raises(self):
        x = np.zeros(mr.MOOD_REGRESSOR_DIM, dtype=np.float32)
        w = np.zeros(mr.MOOD_REGRESSOR_DIM, dtype=np.float32)
        with self.assertRaises(ValueError):
            mr.online_update(x, y=2, weights=w, bias=0.0)

    def test_l2_pulls_unsupported_weights_toward_zero(self):
        """A weight component with no input signal should decay under L2."""
        # Vector has a value only in the first slot; only that index gets
        # gradient. With large L2 the other (untouched) components shouldn't
        # move — but if we seed them non-zero, ridge alone should shrink them.
        x = np.zeros(mr.MOOD_REGRESSOR_DIM, dtype=np.float32)
        x[0] = 1.0
        w = np.full(mr.MOOD_REGRESSOR_DIM, 0.5, dtype=np.float32)
        b = 0.0
        # Use a large L2 so the shrinkage is visible in one step.
        new_w, _ = mr.online_update(x, y=1, weights=w, bias=b, l2=0.5)
        # Index 1+ get no gradient term, only -η·λ·w shrinkage.
        for i in range(1, mr.MOOD_REGRESSOR_DIM):
            self.assertLess(new_w[i], w[i],
                            f"index {i} should have shrunk under L2")


class TestBlend(unittest.TestCase):
    def test_blend_dominated_by_prior_at_zero_samples(self):
        prior = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        reg   = np.array([10.0, 20.0, 30.0], dtype=np.float32)
        out = mr.blend(prior, reg, n_samples=0)
        np.testing.assert_array_equal(out, prior)

    def test_blend_dominated_by_regressor_at_full_confidence(self):
        prior = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        reg   = np.array([10.0, 20.0, 30.0], dtype=np.float32)
        # n_samples ≥ N_CONFIDENT clamps γ to 1.0
        out = mr.blend(prior, reg, n_samples=mr.N_CONFIDENT)
        np.testing.assert_array_equal(out, reg)

    def test_blend_half_confidence(self):
        prior = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        reg   = np.array([1.0, 1.0, 1.0], dtype=np.float32)
        half = mr.N_CONFIDENT // 2
        out = mr.blend(prior, reg, n_samples=half)
        expected = (half / mr.N_CONFIDENT) * 1.0
        np.testing.assert_allclose(out, expected, atol=1e-5)


if __name__ == "__main__":
    unittest.main()
