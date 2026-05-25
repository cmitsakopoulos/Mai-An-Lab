"""Unit tests for utils.taste_model.

Pure-function tests — no DB, no track_graph. The module is intentionally
stateless; integration with persistence and the walk re-rank is covered
by test_mood_feedback / future test_walk_taste.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
from utils import taste_model as tm


class TestPackRoundTrip(unittest.TestCase):
    def test_round_trip_preserves_values(self):
        w = np.array([0.1, -0.2, 0.3], dtype=np.float32)
        unpacked = tm.unpack_weights(tm.pack_weights(w))
        np.testing.assert_array_equal(unpacked, w)

    def test_pack_wrong_shape_raises(self):
        with self.assertRaises(ValueError):
            tm.pack_weights(np.zeros(2, dtype=np.float32))

    def test_unpack_wrong_length_raises(self):
        with self.assertRaises(ValueError):
            tm.unpack_weights(b"\x00" * 8)


class TestFresh(unittest.TestCase):
    def test_fresh_is_zero(self):
        w, b = tm.fresh()
        self.assertEqual(b, 0.0)
        np.testing.assert_array_equal(w, np.zeros(tm.TASTE_MODEL_DIM, dtype=np.float32))

    def test_fresh_scores_half(self):
        """A cold model should predict σ(0) = 0.5 for every input."""
        w, b = tm.fresh()
        x = np.array([2.0, -1.5, 0.7], dtype=np.float32)
        self.assertAlmostEqual(float(tm.score(x, w, b)), 0.5, places=5)


class TestScore(unittest.TestCase):
    def test_score_vector_returns_scalar(self):
        x = np.full(tm.TASTE_MODEL_DIM, 0.5, dtype=np.float32)
        w = np.zeros(tm.TASTE_MODEL_DIM, dtype=np.float32)
        s = tm.score(x, w, 0.0)
        self.assertAlmostEqual(float(s), 0.5, places=5)

    def test_score_matrix_returns_array(self):
        X = np.full((4, tm.TASTE_MODEL_DIM), 0.5, dtype=np.float32)
        w = np.zeros(tm.TASTE_MODEL_DIM, dtype=np.float32)
        s = tm.score(X, w, 0.0)
        self.assertEqual(s.shape, (4,))
        np.testing.assert_allclose(s, 0.5, atol=1e-5)

    def test_score_in_unit_interval_even_with_large_weights(self):
        x = np.full(tm.TASTE_MODEL_DIM, 1.0, dtype=np.float32)
        w = np.full(tm.TASTE_MODEL_DIM, 100.0, dtype=np.float32)
        s = tm.score(x, w, 0.0)
        self.assertGreater(float(s), 0.0)
        self.assertLessEqual(float(s), 1.0)


class TestOnlineUpdate(unittest.TestCase):
    def test_positive_update_raises_score(self):
        x = np.full(tm.TASTE_MODEL_DIM, 0.5, dtype=np.float32)
        w, b = tm.fresh()
        before = float(tm.score(x, w, b))
        w, b = tm.online_update(x, y=1, weights=w, bias=b)
        after = float(tm.score(x, w, b))
        self.assertGreater(after, before)

    def test_negative_update_lowers_score(self):
        x = np.full(tm.TASTE_MODEL_DIM, 0.5, dtype=np.float32)
        w, b = tm.fresh()
        before = float(tm.score(x, w, b))
        w, b = tm.online_update(x, y=0, weights=w, bias=b)
        after = float(tm.score(x, w, b))
        self.assertLess(after, before)

    def test_invalid_label_raises(self):
        x = np.zeros(tm.TASTE_MODEL_DIM, dtype=np.float32)
        w, b = tm.fresh()
        with self.assertRaises(ValueError):
            tm.online_update(x, y=2, weights=w, bias=b)

    def test_sample_weight_scales_step(self):
        """A sample with weight 0.5 should move the bias half as far as one
        with weight 1.0 (same x, same y, same starting model)."""
        x = np.full(tm.TASTE_MODEL_DIM, 0.5, dtype=np.float32)
        w0, b0 = tm.fresh()
        _, b_full = tm.online_update(x, y=1, weights=w0, bias=b0, sample_weight=1.0)
        _, b_half = tm.online_update(x, y=1, weights=w0, bias=b0, sample_weight=0.5)
        # Both positive because σ(0)=0.5, label=1; both raise the bias.
        self.assertGreater(b_full, 0.0)
        self.assertGreater(b_half, 0.0)
        self.assertAlmostEqual(b_half, 0.5 * b_full, places=6)

    def test_l2_decays_unsupported_weights(self):
        """Components with zero input should shrink under L2 alone."""
        x = np.zeros(tm.TASTE_MODEL_DIM, dtype=np.float32)
        x[0] = 1.0
        w = np.full(tm.TASTE_MODEL_DIM, 0.5, dtype=np.float32)
        new_w, _ = tm.online_update(x, y=1, weights=w, bias=0.0, l2=0.5)
        for i in range(1, tm.TASTE_MODEL_DIM):
            self.assertLess(new_w[i], w[i])


class TestClassifyPlayEvent(unittest.TestCase):
    def test_skip_under_threshold_discarded(self):
        """A <5 s play is an accidental tap and must not train the model."""
        self.assertIsNone(tm.classify_play_event(2.0, 240.0))
        self.assertIsNone(tm.classify_play_event(4.99, 240.0))

    def test_long_absolute_play_is_positive(self):
        self.assertEqual(tm.classify_play_event(50.0, 240.0), 1)

    def test_fraction_threshold_triggers_positive_for_short_tracks(self):
        """A 60 s ambient piece played for 30 s (50 %) is a positive even
        though it never crosses the 45 s absolute threshold."""
        self.assertEqual(tm.classify_play_event(30.0, 60.0), 1)

    def test_short_play_long_track_is_negative(self):
        """45 s into a 4-min track is a deliberate skip → y=0."""
        self.assertEqual(tm.classify_play_event(20.0, 240.0), 0)

    def test_unknown_duration_falls_back_to_absolute(self):
        """duration=0 (missing metadata) must not crash; rely on the
        absolute threshold alone."""
        self.assertEqual(tm.classify_play_event(50.0, 0.0), 1)
        self.assertEqual(tm.classify_play_event(10.0, 0.0), 0)


if __name__ == "__main__":
    unittest.main()
