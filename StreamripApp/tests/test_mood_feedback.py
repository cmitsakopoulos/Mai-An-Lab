"""Integration tests for the partition + EQ + taste-model pipeline.

Covers:
  * legacy DB CRUD (mood_feedback, mood_profiles) — still backing the
    feedback button until the UI migrates.
  * partition API (assign/unassign/list/recompute_centroid/set_mood_eq).
  * `adjust_mood_profile` compatibility shim (+1 → assign, -1 → unassign,
    both → record an explicit taste event).
  * `tracks_by_mood` honouring the partition centroid over the seeded one.
  * the taste model: explicit feedback, implicit play classification,
    cold-start no-op.
  * islets — unchanged in this refactor, sanity tests retained.
"""

import asyncio
import json
import os
import sys
import tempfile
import types as _types
import unittest

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_cfg = _types.ModuleType("utils.config")
_cfg.APP_DIR = tempfile.mkdtemp(prefix="dsptest_app_")
sys.modules["utils.config"] = _cfg

from utils.db_manager import DatabaseManager
from utils import track_graph as tg


def _run(coro):
    return asyncio.run(coro)


TIMBRE_DUMMY = np.zeros(24, dtype=np.float32).tobytes()


async def _seed_tracks(db, paths_and_features):
    """Insert tracks with given DSP features. `paths_and_features` is a list
    of (path, bpm, brightness, energy, rolloff, beat_strength,
    spectral_flatness, spectral_contrast, key_index)."""
    async with db._write_lock:
        conn = await db.get_connection()
        cursor = await conn.execute("INSERT INTO artists (name) VALUES ('a')")
        artist_id = cursor.lastrowid
        cursor = await conn.execute(
            "INSERT INTO albums (artist_id, title) VALUES (?, 'al')", (artist_id,))
        album_id = cursor.lastrowid
        for spec in paths_and_features:
            path = spec[0]
            await conn.execute(
                "INSERT INTO tracks (path, title, album_id, duration) "
                "VALUES (?, ?, ?, 240.0)",
                (path, path, album_id),
            )
        await conn.commit()
    for spec in paths_and_features:
        path, bpm, brightness, energy, rolloff, beat, flat, contrast, key = spec
        await db.update_track_features(
            path, bpm, brightness, energy, rolloff, beat, flat, contrast, key,
            TIMBRE_DUMMY, tg.FEATURES_VERSION,
        )


class TestLegacyCRUD(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DatabaseManager(self.tmp.name)
        _run(self.db.initialize())
        tg.invalidate_mood_cache()
        tg.invalidate_taste_cache()

    def tearDown(self):
        _run(self.db.close())
        os.unlink(self.tmp.name)

    def test_mood_feedback_crud(self):
        self.assertEqual(_run(self.db.get_mood_feedback()), {})
        _run(self.db.save_mood_feedback("/a", "chill", 1))
        self.assertEqual(
            _run(self.db.get_mood_feedback()),
            {"/a": {"chill": 1}},
        )
        _run(self.db.clear_all_mood_feedback())
        self.assertEqual(_run(self.db.get_mood_feedback()), {})

    def test_adjusted_mood_profile_crud(self):
        self.assertIsNone(_run(self.db.get_adjusted_mood_profile("chill")))
        v2 = {"PC1": (0.3, 1.5), "PC2": (0.7, 2.0)}
        _run(self.db.save_adjusted_mood_profile("chill", v2))
        self.assertEqual(_run(self.db.get_adjusted_mood_profile("chill")), v2)
        _run(self.db.clear_all_adjusted_mood_profiles())
        self.assertIsNone(_run(self.db.get_adjusted_mood_profile("chill")))


class TestPartitionAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DatabaseManager(self.tmp.name)
        _run(self.db.initialize())
        tg.invalidate_mood_cache()
        tg.invalidate_taste_cache()
        _run(_seed_tracks(self.db, [
            ("p_low",  60.0,  0.1, 0.1, 0.1, 0.1, 0.5, 0.1, 0),
            ("p_mid",  120.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0),
            ("p_high", 180.0, 0.9, 0.9, 0.9, 0.9, 0.5, 0.9, 0),
        ]))
        _run(tg.optimize_pca_spacing(self.db, tg.FEATURES_VERSION))

    def tearDown(self):
        _run(self.db.close())
        os.unlink(self.tmp.name)

    def test_assign_and_unassign(self):
        _run(tg.assign_track_to_mood(self.db, "p_low", "chill"))
        paths = _run(tg.tracks_in_partition(self.db, "chill"))
        self.assertEqual(paths, ["p_low"])

        _run(tg.assign_track_to_mood(self.db, "p_low", "dark"))
        chill_paths = _run(tg.tracks_in_partition(self.db, "chill"))
        dark_paths = _run(tg.tracks_in_partition(self.db, "dark"))
        self.assertEqual(chill_paths, [])
        self.assertEqual(dark_paths, ["p_low"])

        _run(tg.unassign_track_from_mood(self.db, "p_low"))
        self.assertEqual(_run(tg.tracks_in_partition(self.db, "dark")), [])

    def test_recompute_centroid_is_partition_mean(self):
        _run(tg.assign_track_to_mood(self.db, "p_low", "chill"))
        _run(tg.assign_track_to_mood(self.db, "p_high", "chill"))
        profile = _run(tg.recompute_mood_centroid(self.db, "chill"))
        self.assertIsNotNone(profile)
        # Average percentile rank of p_low and p_high is 0.5, mapping to Quartile 3
        self.assertEqual(profile["bpm"][0], 3.0)

    def test_recompute_centroid_empty_partition_returns_none(self):
        out = _run(tg.recompute_mood_centroid(self.db, "chill"))
        self.assertIsNone(out)

    def test_set_mood_eq_stores_quartiles(self):
        _run(tg.set_mood_eq(self.db, "chill", {"bpm": 3.0, "energy": 1.0}))
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertEqual(prof["bpm"][0], 3.0)
        self.assertEqual(prof["bpm"][1], 1.0)
        self.assertEqual(prof["energy"][0], 1.0)
        self.assertEqual(prof["energy"][1], 1.0)


class TestAdjustMoodProfileShim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DatabaseManager(self.tmp.name)
        _run(self.db.initialize())
        tg.invalidate_mood_cache()
        tg.invalidate_taste_cache()
        _run(_seed_tracks(self.db, [
            ("s_low",  60.0,  0.1, 0.1, 0.1, 0.1, 0.5, 0.1, 0),
            ("s_high", 180.0, 0.9, 0.9, 0.9, 0.9, 0.5, 0.9, 0),
        ]))
        _run(tg.optimize_pca_spacing(self.db, tg.FEATURES_VERSION))

    def tearDown(self):
        _run(self.db.close())
        os.unlink(self.tmp.name)

    def test_like_assigns_track_to_mood(self):
        _run(tg.adjust_mood_profile(self.db, "chill", "s_low", 1))
        self.assertEqual(
            _run(tg.tracks_in_partition(self.db, "chill")), ["s_low"],
        )
        # Taste model should also have one explicit event.
        row = _run(self.db.get_taste_model(tg.FEATURES_VERSION))
        self.assertIsNotNone(row)
        self.assertEqual(row[2], 1)  # n_explicit

    def test_dislike_unassigns_and_records_negative(self):
        _run(tg.adjust_mood_profile(self.db, "chill", "s_low", 1))
        _run(tg.adjust_mood_profile(self.db, "chill", "s_low", -1))
        self.assertEqual(
            _run(tg.tracks_in_partition(self.db, "chill")), [],
        )
        row = _run(self.db.get_taste_model(tg.FEATURES_VERSION))
        self.assertEqual(row[2], 2)  # two explicit events

    def test_neutral_feedback_is_noop(self):
        _run(tg.adjust_mood_profile(self.db, "chill", "s_low", 0))
        self.assertEqual(_run(tg.tracks_in_partition(self.db, "chill")), [])
        self.assertIsNone(_run(self.db.get_taste_model(tg.FEATURES_VERSION)))


class TestTracksByMoodWithPartition(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DatabaseManager(self.tmp.name)
        _run(self.db.initialize())
        tg.invalidate_mood_cache()
        tg.invalidate_taste_cache()
        # Build a small library with one clearly-low and one clearly-high
        # track on PC1; PCA will spread them along the dominant axis.
        _run(_seed_tracks(self.db, [
            ("t_low",  60.0,  0.1, 0.1, 0.1, 0.1, 0.5, 0.1, 0),
            ("t_mid",  120.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0),
            ("t_high", 180.0, 0.9, 0.9, 0.9, 0.9, 0.5, 0.9, 0),
        ]))
        _run(tg.optimize_pca_spacing(self.db, tg.FEATURES_VERSION))
        mock_profiles = {
            "chill": {
                "bpm": (1.0, 1.0),      # Very Low
                "energy": (1.0, 1.0),   # Very Low
            },
            "energetic": {
                "bpm": (4.0, 1.0),      # Very High
                "energy": (4.0, 1.0),   # Very High
            },
            "dark": {
                "energy": (1.0, 1.0),   # Very Low
            },
            "upbeat": {
                "bpm": (4.0, 1.0),      # Very High
                "energy": (4.0, 1.0),   # Very High
            },
            "rock": {
                "bpm": (3.0, 1.0),      # High
                "brightness": (4.0, 1.0), # Very High
            },
            "beats": {
                "beat_strength": (4.0, 1.0), # Very High
            },
            "intense": {
                "energy": (4.0, 1.0),   # Very High
                "beat_strength": (4.0, 1.0), # Very High
            }
        }
        for m in tg.MOODS.keys():
            prof = mock_profiles.get(m, {})
            _run(self.db.save_adjusted_mood_profile(m, prof))

    def tearDown(self):
        _run(self.db.close())
        os.unlink(self.tmp.name)

    def test_partition_overrides_seeded_centroid(self):
        # Assign t_high to chill. Without the partition the seeded chill
        # centroid (PC1=-1.5) would put t_low at the top; with the
        # partition the centroid shifts and t_high should rank first.
        _run(tg.assign_track_to_mood(self.db, "t_high", "chill"))
        results = _run(tg.tracks_by_mood(self.db, "chill", limit=3))
        self.assertEqual(results[0]["path"], "t_high")

    def test_empty_partition_uses_seeded_centroid(self):
        results = _run(tg.tracks_by_mood(self.db, "chill", limit=3))
        # Seeded chill profile targets PC1=-1.5; t_low sits in the negative
        # extreme of PC1 → it should rank first.
        self.assertEqual(results[0]["path"], "t_low")


class TestTasteModelIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DatabaseManager(self.tmp.name)
        _run(self.db.initialize())
        tg.invalidate_mood_cache()
        tg.invalidate_taste_cache()
        _run(_seed_tracks(self.db, [
            ("k_low",  60.0,  0.1, 0.1, 0.1, 0.1, 0.5, 0.1, 0),
            ("k_high", 180.0, 0.9, 0.9, 0.9, 0.9, 0.5, 0.9, 0),
        ]))
        _run(tg.optimize_pca_spacing(self.db, tg.FEATURES_VERSION))
        mock_profiles = {
            "chill": {
                "bpm": (1.0, 1.0),      # Very Low
                "energy": (1.0, 1.0),   # Very Low
            },
            "energetic": {
                "bpm": (4.0, 1.0),      # Very High
                "energy": (4.0, 1.0),   # Very High
            },
            "dark": {
                "energy": (1.0, 1.0),   # Very Low
            },
            "upbeat": {
                "bpm": (4.0, 1.0),      # Very High
                "energy": (4.0, 1.0),   # Very High
            },
            "rock": {
                "bpm": (3.0, 1.0),      # High
                "brightness": (4.0, 1.0), # Very High
            },
            "beats": {
                "beat_strength": (4.0, 1.0), # Very High
            },
            "intense": {
                "energy": (4.0, 1.0),   # Very High
                "beat_strength": (4.0, 1.0), # Very High
            }
        }
        for m in tg.MOODS.keys():
            prof = mock_profiles.get(m, {})
            _run(self.db.save_adjusted_mood_profile(m, prof))

    def tearDown(self):
        _run(self.db.close())
        os.unlink(self.tmp.name)

    def test_explicit_feedback_persists(self):
        _run(tg.record_explicit_feedback(self.db, "k_low", True))
        row = _run(self.db.get_taste_model(tg.FEATURES_VERSION))
        self.assertIsNotNone(row)
        self.assertEqual(row[2], 1)  # n_explicit
        self.assertEqual(row[3], 0)  # n_implicit

    def test_implicit_play_event_routes_through_classifier(self):
        # 50 s / 240 s → positive (≥ 45 s absolute)
        _run(tg.record_play_event(self.db, "k_high", 50.0, 240.0))
        row = _run(self.db.get_taste_model(tg.FEATURES_VERSION))
        self.assertIsNotNone(row)
        self.assertEqual(row[3], 1)  # n_implicit
        self.assertEqual(row[2], 0)  # n_explicit

    def test_short_skip_does_not_train(self):
        _run(tg.record_play_event(self.db, "k_high", 2.0, 240.0))
        self.assertIsNone(_run(self.db.get_taste_model(tg.FEATURES_VERSION)))

    def test_unknown_track_is_skipped(self):
        _run(tg.record_explicit_feedback(self.db, "/nonexistent.flac", True))
        self.assertIsNone(_run(self.db.get_taste_model(tg.FEATURES_VERSION)))

    def test_cold_taste_model_does_not_change_ranking(self):
        # With no feedback events, tracks_by_mood for 'chill' should rank
        # purely by mood score (seed centroid favours k_low).
        baseline = _run(tg.tracks_by_mood(self.db, "chill", limit=1))[0]["path"]
        self.assertEqual(baseline, "k_low")


class TestIsletsUnchanged(unittest.TestCase):
    """Islet path was explicitly left untouched in this refactor — these
    are smoke tests guarding against accidental regression."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DatabaseManager(self.tmp.name)
        _run(self.db.initialize())
        tg.invalidate_mood_cache()
        tg.invalidate_taste_cache()

        self.cm_path = self.tmp.name + ".custom_moods.json"
        self.original = tg.CUSTOM_MOODS_PATH
        tg.CUSTOM_MOODS_PATH = self.cm_path
        if os.path.exists(self.cm_path):
            os.remove(self.cm_path)

        # Construct distinguishable timbres so the cosine path can order them:
        #   ex and near point in roughly the same direction as the centroid;
        #   far points the opposite way and should fall below threshold.
        ex_timbre   = np.full(52, 1.0,  dtype=np.float32).tobytes()
        near_timbre = np.full(52, 0.9,  dtype=np.float32).tobytes()
        far_timbre  = np.full(52, -1.0, dtype=np.float32).tobytes()
        self._timbres = {
            "ex":   ex_timbre,
            "near": near_timbre,
            "far":  far_timbre,
        }

        async def insert():
            async with self.db._write_lock:
                conn = await self.db.get_connection()
                cursor = await conn.execute("INSERT INTO artists (name) VALUES ('isl')")
                aid = cursor.lastrowid
                cursor = await conn.execute(
                    "INSERT INTO albums (artist_id, title) VALUES (?, 'isl')", (aid,))
                alb = cursor.lastrowid
                for p in ("ex", "near", "far"):
                    await conn.execute(
                        "INSERT INTO tracks (path, title, album_id, duration) "
                        "VALUES (?, ?, ?, 240.0)",
                        (p, p, alb))
                await conn.commit()
            await self.db.update_track_features(
                'ex',   60.0, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0,
                self._timbres["ex"], tg.FEATURES_VERSION)
            await self.db.update_track_features(
                'near', 70.0, 0.15, 0.15, 0.15, 0.15, 0.15, 0.15, 0,
                self._timbres["near"], tg.FEATURES_VERSION)
            await self.db.update_track_features(
                'far',  200.0, 0.95, 0.95, 0.95, 0.95, 0.95, 0.95, 0,
                self._timbres["far"], tg.FEATURES_VERSION)
        _run(insert())

    def tearDown(self):
        tg.CUSTOM_MOODS_PATH = self.original
        if os.path.exists(self.cm_path):
            os.remove(self.cm_path)
        _run(self.db.close())
        os.unlink(self.tmp.name)

    def test_islet_membership_via_cosine(self):
        """Cosine path returns the exemplar and excludes blacklisted tracks."""
        with open(self.cm_path, "w", encoding="utf-8") as f:
            json.dump({
                "test_islet": {
                    # Centroid aligned with the 'ex'/'near' timbre direction.
                    "centroid":      [1.0] * 52,
                    "exemplar_path": "ex",
                    "threshold":     0.5,
                    "blacklist":     ["far"],
                }
            }, f)
        members = _run(tg.tracks_in_islet(self.db, "test_islet", min_count=1))
        paths = {m["path"] for m in members}
        self.assertIn("ex", paths)
        self.assertNotIn("far", paths,
                         "Blacklisted track must be excluded from members.")


if __name__ == "__main__":
    unittest.main()
