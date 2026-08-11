import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.text_match import levenshtein_distance
from ui.views.settings import SettingsView


class MockApp:
    page = None

    def safe_update(self, fn):
        pass

    def show_snackbar(self, msg):
        pass


class TestLevenshtein(unittest.TestCase):
    def test_identical_and_empty(self):
        self.assertEqual(levenshtein_distance("haptic", "haptic"), 0)
        self.assertEqual(levenshtein_distance("", ""), 0)
        self.assertEqual(levenshtein_distance("eq", ""), 2)

    def test_single_edits(self):
        self.assertEqual(levenshtein_distance("haptik", "haptic"), 1)   # substitution
        self.assertEqual(levenshtein_distance("equalizr", "equalizer"), 1)  # deletion
        self.assertEqual(levenshtein_distance("basss", "bass"), 1)      # insertion

    def test_symmetric(self):
        self.assertEqual(
            levenshtein_distance("qobuz", "qobzu"),
            levenshtein_distance("qobzu", "qobuz"),
        )


class TestSettingsFuzzyFallback(unittest.TestCase):
    """The WordPiece VSM was retired; these lock in what the port must preserve.

    'haptik' was the single query in the evaluation set where the old matcher
    beat plain substring matching, so it is the regression case that matters.
    """

    def setUp(self):
        self.view = SettingsView(app=MockApp())

    def _titles(self, query):
        return [e.title for _, e in self.view._get_semantic_matches(query)]

    def test_typo_still_resolves(self):
        self.assertIn("Haptic Feedback", self._titles("haptik"))

    def test_exact_domain_terms_resolve(self):
        self.assertIn("Audio & DSP", self._titles("equalizer"))
        self.assertIn("Authentication", self._titles("qobuz"))

    def test_no_wordpiece_dependency(self):
        """The retired module must not be reachable from the shipped tree."""
        with self.assertRaises(ImportError):
            import utils.semantic_intent  # noqa: F401

    def test_gibberish_scores_nothing(self):
        self.assertEqual(self._titles("xyzzy"), [])


if __name__ == "__main__":
    unittest.main()
