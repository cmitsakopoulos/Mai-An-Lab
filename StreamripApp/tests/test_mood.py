"""Mood-system tests: MOODS source-of-truth wiring, percentile caching,
listen-feedback re-rank, and Camelot-aware playlist sequencing."""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Isolate APP_DIR so the custom-mood JSON probe doesn't see the user's real
# moods file.
import types as _types
_cfg = _types.ModuleType("utils.config")
_cfg.APP_DIR = tempfile.mkdtemp(prefix="dsptest_app_")
sys.modules["utils.config"] = _cfg

from utils import track_graph as tg
from utils import auto_playlist as ap

import numpy as np


def _run(coro):
    return asyncio.run(coro)


def _blob(vec: np.ndarray) -> bytes:
    return vec.astype("<f4").tobytes()


class FakeMoodDB:
    """In-memory db_manager surface needed by tg.tracks_by_mood + the
    percentile cache. Tracks are passed in directly; signal map is mutable
    so tests can install per-track feedback to verify re-ranking."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.signal: dict[str, float] = {}

    async def get_tracks_with_features(self, features_version):
        return list(self.rows)

    async def listen_signal_map(self):
        return dict(self.signal)

    async def get_adjusted_mood_profile(self, mood: str) -> dict[str, float] | None:
        return None


def _row(path, **kwargs):
    base = {
        "path": path, "bpm": 120.0, "energy": 0.5, "brightness": 0.5,
        "rolloff": 0.5, "beat_strength": 0.5,
        "spectral_flatness": 0.5, "spectral_contrast": 0.5,
        "key_index": 0, "timbre": None,
        "title": path, "artist": "fake", "album": "fake", "duration": 180.0,
    }
    base.update(kwargs)
    return base


class TestMoodVocabulary(unittest.TestCase):
    def test_canonical_resolves_alias(self):
        self.assertEqual(tg.mood_canonical("chilled"), "chill")
        self.assertEqual(tg.mood_canonical("relaxing"), "relaxed")
        self.assertEqual(tg.mood_canonical("CHILL"), "chill")

    def test_unknown_returns_none(self):
        self.assertIsNone(tg.mood_canonical("nonsense_mood_xyz"))
        self.assertIsNone(tg.mood_canonical(""))

    def test_mood_profiles_includes_aliases(self):
        # Backwards-compat invariant: every alias resolves via MOOD_PROFILES.
        self.assertIn("chill", tg.MOOD_PROFILES)
        self.assertIn("chilled", tg.MOOD_PROFILES)
        self.assertIs(tg.MOOD_PROFILES["chill"], tg.MOOD_PROFILES["chilled"])

    def test_mood_keywords_matches_intent(self):
        # MOOD_KEYWORDS in assistant_intent should be derived from
        # track_graph.MOODS (or its static fallback). Either way, every
        # canonical mood should be present.
        from utils import assistant_intent as ai
        for canonical in tg.MOODS.keys():
            self.assertIn(canonical, ai.MOOD_KEYWORDS)


class TestPercentileCache(unittest.TestCase):
    def setUp(self):
        # Clear cache between tests so prior fixtures don't bleed across.
        tg.invalidate_mood_cache()

    def test_cache_hit_on_repeated_query(self):
        rows = [_row(f"t{i}", bpm=60 + i * 5, energy=0.1 + i * 0.05) for i in range(10)]
        db = FakeMoodDB(rows)
        a = _run(tg._load_percentile_matrix(db, tg.FEATURES_VERSION))
        b = _run(tg._load_percentile_matrix(db, tg.FEATURES_VERSION))
        # Same cached tuple object on second call.
        self.assertIs(a[0], b[0])
        self.assertIs(a[1], b[1])

    def test_cache_invalidates_on_row_change(self):
        rows = [_row(f"t{i}", bpm=60 + i * 5) for i in range(5)]
        db = FakeMoodDB(rows)
        first = _run(tg._load_percentile_matrix(db, tg.FEATURES_VERSION))
        # Append a new track. The sentinel changes → cache key changes → miss.
        db.rows.append(_row("z_new_track", bpm=200))
        second = _run(tg._load_percentile_matrix(db, tg.FEATURES_VERSION))
        self.assertIsNot(first[1], second[1])
        self.assertEqual(second[1].shape[0], len(db.rows))


class TestMoodScoring(unittest.TestCase):
    def setUp(self):
        tg.invalidate_mood_cache()

    def _library(self):
        # 5 slow + 5 fast tracks. Each gets unique scalar values so percentile
        # ranks are unique and the top-K selection is deterministic.
        rows = []
        for i in range(5):
            rows.append(_row(f"slow_{i}", bpm=60 + i, energy=0.1 + i * 0.01,
                             brightness=0.2, beat_strength=0.2))
        for i in range(5):
            rows.append(_row(f"fast_{i}", bpm=160 + i, energy=0.85 + i * 0.01,
                             brightness=0.85, beat_strength=0.85))
        return rows

    def test_chill_picks_slow(self):
        db = FakeMoodDB(self._library())
        results = _run(tg.tracks_by_mood(db, "chill", limit=3))
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertTrue(r["path"].startswith("slow"),
                            f"chill mood picked {r['path']}")

    def test_energetic_picks_fast(self):
        db = FakeMoodDB(self._library())
        results = _run(tg.tracks_by_mood(db, "energetic", limit=3))
        for r in results:
            self.assertTrue(r["path"].startswith("fast"),
                            f"energetic mood picked {r['path']}")

    def test_unknown_mood_returns_empty(self):
        db = FakeMoodDB(self._library())
        self.assertEqual(_run(tg.tracks_by_mood(db, "blorp")), [])

    def test_alias_resolves_to_same_results(self):
        db = FakeMoodDB(self._library())
        a = _run(tg.tracks_by_mood(db, "chill", limit=3))
        # Clear cache to force re-compute; signal map identical so results
        # must match exactly.
        b = _run(tg.tracks_by_mood(db, "chilled", limit=3))
        self.assertEqual([r["path"] for r in a], [r["path"] for r in b])


class TestListenFeedback(unittest.TestCase):
    def setUp(self):
        tg.invalidate_mood_cache()

    def test_signal_demotes_skipped_track(self):
        # Use the single-feature "noisy" mood (target spectral_flatness percentile 0.90) so
        # the ranking is deterministic from a one-column percentile rank.
        # 8 quiet distractors land at percentiles 0.0 .. 0.78; "skipped"
        # (spectral_flatness 0.89) ends up at 0.89 (≈ target) and "kept" at 1.0 (slightly
        # above target). Without feedback, skipped wins by ~0.09 on the
        # mood distance; β=0.20 listen-signal of −1 on skipped is more than
        # enough to flip the ranking.
        rows = [_row(f"d{i}", spectral_flatness=0.1 + i * 0.05) for i in range(8)]
        rows.append(_row("skipped", spectral_flatness=0.90))
        rows.append(_row("kept", spectral_flatness=0.95))
        db = FakeMoodDB(rows)

        baseline = _run(tg.tracks_by_mood(db, "noisy", limit=2))
        baseline_top = {r["path"] for r in baseline}
        self.assertEqual(baseline_top, {"kept", "skipped"})

        tg.invalidate_mood_cache()
        db.signal["skipped"] = -1.0
        db.signal["kept"] = +0.5
        biased = _run(tg.tracks_by_mood(db, "noisy", limit=1))
        self.assertEqual(biased[0]["path"], "kept")


class TestGreedySequenceTransitionCost(unittest.TestCase):
    def test_pure_distance_when_no_cost_hook(self):
        # Three vectors on a line; greedy starting at the middle should
        # produce [middle, neighbour, far_neighbour] in either direction.
        paths = ["A", "B", "C"]
        vectors = np.array([[0.0], [1.0], [2.0]])
        out = ap._greedy_sequence("B", paths, vectors)
        # B → A (dist 1) → C (next remaining), or B → C → A — both have the
        # same total distance, just verify the seed comes first.
        self.assertEqual(out[0], "B")
        self.assertEqual(sorted(out), ["A", "B", "C"])

    def test_transition_cost_overrides_distance(self):
        # Pure distance would pick A second (closer to B than C is). A large
        # transition penalty on B→A flips the order to B→C→A.
        paths = ["A", "B", "C"]
        vectors = np.array([[0.0], [1.0], [3.0]])  # B-A is closer than B-C.
        penalties = {
            ("B", "A"): 100.0,  # massive cost — should always be avoided
            ("B", "C"): 0.0,
        }

        def cost(a_idx: int, b_idx: int) -> float:
            return penalties.get((paths[a_idx], paths[b_idx]), 0.0)

        out = ap._greedy_sequence("B", paths, vectors, transition_cost=cost)
        self.assertEqual(out, ["B", "C", "A"])


if __name__ == "__main__":
    unittest.main()
