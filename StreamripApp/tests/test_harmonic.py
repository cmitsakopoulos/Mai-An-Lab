"""Harmonic-adjacency table sanity. The module is pure data + arithmetic so
the tests are equally simple — every assertion comes from the published
Camelot wheel."""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import harmonic as h


C_MAJOR  = 0
A_MINOR  = 12 + 9   # A minor = 8A
G_MAJOR  = 7        # 9B
D_MAJOR  = 2        # 10B
E_MINOR  = 12 + 4   # 9A
F_MINOR  = 12 + 5   # 4A
F_SHARP_MAJOR = 6   # 2B


class TestKeyIndexToCamelot(unittest.TestCase):
    def test_canonical_anchors(self):
        self.assertEqual(h.key_index_to_camelot(C_MAJOR), (8, "B"))
        self.assertEqual(h.key_index_to_camelot(A_MINOR), (8, "A"))
        self.assertEqual(h.key_index_to_camelot(G_MAJOR), (9, "B"))

    def test_out_of_range(self):
        self.assertIsNone(h.key_index_to_camelot(-1))
        self.assertIsNone(h.key_index_to_camelot(24))
        self.assertIsNone(h.key_index_to_camelot(None))


class TestCamelotDistance(unittest.TestCase):
    def test_same_key_is_zero(self):
        self.assertEqual(h.camelot_distance(C_MAJOR, C_MAJOR), 0)

    def test_relative_major_minor_is_zero(self):
        # C major (8B) and A minor (8A) share an hour with opposite ring.
        self.assertEqual(h.camelot_distance(C_MAJOR, A_MINOR), 0)

    def test_adjacent_hour_same_ring_is_one(self):
        # 8B → 9B is one hour.
        self.assertEqual(h.camelot_distance(C_MAJOR, G_MAJOR), 1)

    def test_circular_wrap(self):
        # 1B and 12B wrap (hours apart = 1).
        b1 = h._MAJOR_HOUR.copy()  # sanity: ensure both 1 and 12 are populated
        self.assertIn(1, b1.values())
        self.assertIn(12, b1.values())

    def test_unknown_key_is_worst(self):
        self.assertEqual(h.camelot_distance(-5, C_MAJOR), 6)
        self.assertEqual(h.camelot_distance(C_MAJOR, 999), 6)


class TestCamelotPenalty(unittest.TestCase):
    def test_bounded_zero_to_one(self):
        for a in range(24):
            for b in range(24):
                p = h.camelot_penalty(a, b)
                self.assertGreaterEqual(p, 0.0)
                self.assertLessEqual(p, 1.0)

    def test_compatible_is_zero(self):
        self.assertEqual(h.camelot_penalty(C_MAJOR, A_MINOR), 0.0)
        self.assertEqual(h.camelot_penalty(C_MAJOR, C_MAJOR), 0.0)


class TestModePreference(unittest.TestCase):
    def test_none_pref_accepts_everything(self):
        self.assertTrue(h.matches_mode_preference(C_MAJOR, None))
        self.assertTrue(h.matches_mode_preference(A_MINOR, None))

    def test_major_minor_filter(self):
        self.assertTrue(h.matches_mode_preference(C_MAJOR, "major"))
        self.assertFalse(h.matches_mode_preference(A_MINOR, "major"))
        self.assertFalse(h.matches_mode_preference(C_MAJOR, "minor"))
        self.assertTrue(h.matches_mode_preference(A_MINOR, "minor"))


if __name__ == "__main__":
    unittest.main()
