"""Unit tests for mood feedback, profile adjustments, and recalculated partition routing."""

import asyncio
import os
import sys
import tempfile
import unittest
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Isolate config
import types as _types
_cfg = _types.ModuleType("utils.config")
_cfg.APP_DIR = tempfile.mkdtemp(prefix="dsptest_app_")
sys.modules["utils.config"] = _cfg

from utils.db_manager import DatabaseManager
from utils import track_graph as tg


def _run(coro):
    return asyncio.run(coro)


class TestMoodFeedbackAndLearning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DatabaseManager(self.tmp.name)
        _run(self.db.initialize())
        tg.invalidate_mood_cache()

    def tearDown(self):
        _run(self.db.close())
        os.unlink(self.tmp.name)

    def test_database_feedback_crud(self):
        # 1. Initial should be empty
        fb = _run(self.db.get_mood_feedback())
        self.assertEqual(fb, {})

        # 2. Save a like
        _run(self.db.save_mood_feedback("/tracks/a.flac", "chill", 1))
        fb = _run(self.db.get_mood_feedback())
        self.assertEqual(fb, {"/tracks/a.flac": {"chill": 1}})

        # 3. Save a dislike and update
        _run(self.db.save_mood_feedback("/tracks/a.flac", "chill", -1))
        _run(self.db.save_mood_feedback("/tracks/b.flac", "energetic", 1))
        fb = _run(self.db.get_mood_feedback())
        self.assertEqual(fb["/tracks/a.flac"]["chill"], -1)
        self.assertEqual(fb["/tracks/b.flac"]["energetic"], 1)

        # 4. Clear all feedback
        _run(self.db.clear_all_mood_feedback())
        fb = _run(self.db.get_mood_feedback())
        self.assertEqual(fb, {})

    def test_database_profiles_crud(self):
        # 1. Initial should be empty
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertIsNone(prof)

        # 2. Save v1-shaped profile (float targets); reader must promote to
        # v2 (target, weight=1.0). Confirms backwards-compat for callers
        # that have not yet migrated to the tuple form.
        v1_prof = {"bpm": 0.25, "energy": 0.45}
        _run(self.db.save_adjusted_mood_profile("chill", v1_prof))
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertEqual(prof, {"bpm": (0.25, 1.0), "energy": (0.45, 1.0)})

        # 3. Save v2-shaped profile (target, weight) and round-trip intact.
        v2_prof = {"bpm": (0.3, 1.5), "spectral_flatness": (0.7, 2.0)}
        _run(self.db.save_adjusted_mood_profile("chill", v2_prof))
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertEqual(prof, v2_prof)

        # 4. Get all profiles (v2-shaped)
        all_profs = _run(self.db.get_all_adjusted_mood_profiles())
        self.assertEqual(all_profs, {"chill": v2_prof})

        # 5. Clear profiles
        _run(self.db.clear_all_adjusted_mood_profiles())
        all_profs = _run(self.db.get_all_adjusted_mood_profiles())
        self.assertEqual(all_profs, {})

    def test_online_gradient_shifting(self):
        # We need a track inside a mock library to calculate percentiles
        # Set up a track so that its features are deterministically ranked.
        # Let's populate the database with play_counts containing DSP features.
        # We will write features version and timbre so it's recognized.
        timbre_dummy = np.zeros(24, dtype=np.float32).tobytes()
        
        # We need 3 tracks to have meaningful percentile rankings.
        # Col order: bpm, energy, brightness, rolloff, beat_strength, spectral_flatness, spectral_contrast, key_index
        # _MOOD_FEATURES = ["bpm", "energy", "brightness", "rolloff", "beat_strength", "spectral_flatness", "spectral_contrast"]
        
        # Insert tracks with distinct features to define distinct percentile ranks:
        # T0: bpm=60.0 (rank 0), energy=0.1 (rank 0)
        # T1: bpm=120.0 (rank 1), energy=0.5 (rank 1)
        # T2: bpm=180.0 (rank 2), energy=0.9 (rank 2)
        async def insert_tracks():
            # SQLite tracks
            async with self.db._write_lock:
                conn = await self.db.get_connection()
                # Create dummy artist
                cursor = await conn.execute("INSERT INTO artists (name) VALUES ('dummy_artist')")
                artist_id = cursor.lastrowid
                # Create dummy album
                cursor = await conn.execute("INSERT INTO albums (artist_id, title) VALUES (?, 'dummy_album')", (artist_id,))
                album_id = cursor.lastrowid
                
                await conn.execute("INSERT INTO tracks (path, title, album_id) VALUES ('t0', 't0', ?)", (album_id,))
                await conn.execute("INSERT INTO tracks (path, title, album_id) VALUES ('t1', 't1', ?)", (album_id,))
                await conn.execute("INSERT INTO tracks (path, title, album_id) VALUES ('t2', 't2', ?)", (album_id,))
                await conn.commit()
            
            # play_counts / features
            await self.db.update_track_features('t0', 60.0, 0.1, 0.5, 0.5, 0.5, 0.5, 0.5, 0, timbre_dummy, tg.FEATURES_VERSION)
            await self.db.update_track_features('t1', 120.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0, timbre_dummy, tg.FEATURES_VERSION)
            await self.db.update_track_features('t2', 180.0, 0.9, 0.5, 0.5, 0.5, 0.5, 0.5, 0, timbre_dummy, tg.FEATURES_VERSION)
        
        _run(insert_tracks())

        # Verify percentile matrix loaded correctly
        rows, matrix = _run(tg._load_percentile_matrix(self.db, tg.FEATURES_VERSION))
        self.assertEqual(len(rows), 3)
        # Row 1 ('t1') should be at percentile 0.5 for both bpm and energy
        self.assertAlmostEqual(matrix[1][0], 0.5, places=5)
        self.assertAlmostEqual(matrix[1][1], 0.5, places=5)

        # Pin a known starting profile so the gradient math is deterministic
        # regardless of future MOODS retunes. We test the *update rule*, not
        # the prior values.
        _run(self.db.save_adjusted_mood_profile(
            "chill", {"bpm": (0.10, 1.0), "energy": (0.10, 1.0)}))

        # 1. LIKE on 't2' (bpm/energy percentile 1.0)
        # T_new = T_old + 0.15 * (P_track - T_old)
        #   bpm:    0.10 + 0.15 * (1.0 - 0.10) = 0.235
        #   energy: 0.10 + 0.15 * (1.0 - 0.10) = 0.235
        _run(tg.adjust_mood_profile(self.db, "chill", "t2", 1))

        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertIsNotNone(prof)
        self.assertAlmostEqual(prof["bpm"][0], 0.235, places=5)
        self.assertAlmostEqual(prof["energy"][0], 0.235, places=5)

        # 2. DISLIKE on 't0' (bpm/energy percentile 0.0)
        # T_new = T_old - 0.15 * (P_track - T_old)
        #   bpm:    0.235 - 0.15 * (0.0 - 0.235) = 0.27025
        #   energy: 0.235 - 0.15 * (0.0 - 0.235) = 0.27025
        _run(tg.adjust_mood_profile(self.db, "chill", "t0", -1))

        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertAlmostEqual(prof["bpm"][0], 0.27025, places=5)
        self.assertAlmostEqual(prof["energy"][0], 0.27025, places=5)

        # 3. Test target clamping
        # A. Clamp to 1.0: starting at 0.9, shift away from percentile 0.0
        _run(self.db.save_adjusted_mood_profile(
            "chill", {"bpm": (0.9, 1.0), "energy": (0.5, 1.0)}))
        _run(tg.adjust_mood_profile(self.db, "chill", "t0", -1))
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertAlmostEqual(prof["bpm"][0], 1.0, places=5)

        # B. Clamp to 0.0: starting at 0.1, shift away from percentile 1.0
        _run(self.db.save_adjusted_mood_profile(
            "chill", {"bpm": (0.1, 1.0), "energy": (0.5, 1.0)}))
        _run(tg.adjust_mood_profile(self.db, "chill", "t2", -1))
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertAlmostEqual(prof["bpm"][0], 0.0, places=5)

    def test_adjust_mood_profile_updates_weight(self):
        """Like on a track close to the current target should increase that
        feature's weight; dislike should decrease it. Far-from-target
        features barely move thanks to the alignment-scaled eta."""
        timbre_dummy = np.zeros(24, dtype=np.float32).tobytes()

        async def insert_tracks():
            async with self.db._write_lock:
                conn = await self.db.get_connection()
                cursor = await conn.execute("INSERT INTO artists (name) VALUES ('w')")
                artist_id = cursor.lastrowid
                cursor = await conn.execute(
                    "INSERT INTO albums (artist_id, title) VALUES (?, 'w')", (artist_id,))
                album_id = cursor.lastrowid
                for path in ("w0", "w1", "w2"):
                    await conn.execute(
                        "INSERT INTO tracks (path, title, album_id) VALUES (?, ?, ?)",
                        (path, path, album_id))
                await conn.commit()
            await self.db.update_track_features('w0', 60.0, 0.1, 0.5, 0.5, 0.5, 0.5, 0.5, 0, timbre_dummy, tg.FEATURES_VERSION)
            await self.db.update_track_features('w1', 120.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0, timbre_dummy, tg.FEATURES_VERSION)
            await self.db.update_track_features('w2', 180.0, 0.9, 0.5, 0.5, 0.5, 0.5, 0.5, 0, timbre_dummy, tg.FEATURES_VERSION)
        _run(insert_tracks())

        # Pin a known starting profile so the math is deterministic.
        _run(self.db.save_adjusted_mood_profile(
            "chill", {"bpm": (0.5, 1.0), "energy": (0.5, 1.0)}))

        # LIKE on w1 (bpm/energy percentile 0.5 — perfect alignment with target).
        # alignment = 1.0 - |0.5 - 0.5| = 1.0
        # w_new = 1.0 * (1.0 + 0.05 * 1.0) = 1.05
        _run(tg.adjust_mood_profile(self.db, "chill", "w1", 1))
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertAlmostEqual(prof["bpm"][1], 1.05, places=5)
        self.assertAlmostEqual(prof["energy"][1], 1.05, places=5)

        # DISLIKE on w1 — weights drop back symmetrically.
        # w_new = 1.05 * (1.0 - 0.05 * alignment)
        # After the LIKE, target shifted slightly so alignment is no longer
        # exactly 1.0 — just assert the direction (weight decreased).
        _run(tg.adjust_mood_profile(self.db, "chill", "w1", -1))
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertLess(prof["bpm"][1], 1.05)
        self.assertLess(prof["energy"][1], 1.05)

    def test_feedback_loop_persists_regressor(self):
        """A like event must create a mood_regressors row, and a follow-up
        like must increment n_samples — confirming the feedback path is
        actually feeding the phase-2 learner."""
        timbre_dummy = np.zeros(24, dtype=np.float32).tobytes()

        async def insert_tracks():
            async with self.db._write_lock:
                conn = await self.db.get_connection()
                cursor = await conn.execute("INSERT INTO artists (name) VALUES ('rl')")
                artist_id = cursor.lastrowid
                cursor = await conn.execute(
                    "INSERT INTO albums (artist_id, title) VALUES (?, 'rl')", (artist_id,))
                album_id = cursor.lastrowid
                for path in ("r0", "r1"):
                    await conn.execute(
                        "INSERT INTO tracks (path, title, album_id) VALUES (?, ?, ?)",
                        (path, path, album_id))
                await conn.commit()
            await self.db.update_track_features('r0', 60.0, 0.1, 0.5, 0.5, 0.5, 0.5, 0.5, 0, timbre_dummy, tg.FEATURES_VERSION)
            await self.db.update_track_features('r1', 180.0, 0.9, 0.5, 0.5, 0.5, 0.5, 0.5, 0, timbre_dummy, tg.FEATURES_VERSION)
        _run(insert_tracks())

        # Before any feedback: no regressor row exists.
        reg = _run(self.db.get_mood_regressor("chill", tg.FEATURES_VERSION))
        self.assertIsNone(reg)

        # First like → row materialises with n_samples=1.
        _run(tg.adjust_mood_profile(self.db, "chill", "r0", 1))
        reg = _run(self.db.get_mood_regressor("chill", tg.FEATURES_VERSION))
        self.assertIsNotNone(reg)
        self.assertEqual(reg[2], 1)

        # Second like (different track) → n_samples increments.
        _run(tg.adjust_mood_profile(self.db, "chill", "r1", 1))
        reg = _run(self.db.get_mood_regressor("chill", tg.FEATURES_VERSION))
        self.assertEqual(reg[2], 2)

    def test_regressor_blend_dormant_at_cold_start(self):
        """Until n_samples ≥ N_CONFIDENT, the prior dominates — a freshly
        seeded mood should rank identically with or without the bootstrap
        regressor in the loop (γ=0 → blend returns the prior unchanged)."""
        from utils import mood_regressor as mr
        timbre_dummy = np.zeros(24, dtype=np.float32).tobytes()

        async def insert_tracks():
            async with self.db._write_lock:
                conn = await self.db.get_connection()
                cursor = await conn.execute("INSERT INTO artists (name) VALUES ('cs')")
                artist_id = cursor.lastrowid
                cursor = await conn.execute(
                    "INSERT INTO albums (artist_id, title) VALUES (?, 'cs')", (artist_id,))
                album_id = cursor.lastrowid
                for i in range(5):
                    await conn.execute(
                        "INSERT INTO tracks (path, title, album_id) VALUES (?, ?, ?)",
                        (f"c{i}", f"c{i}", album_id))
                await conn.commit()
            # Spread bpm + flatness so chill (low energy + high flatness for
            # target 0.40) produces a clean ranking.
            for i in range(5):
                await self.db.update_track_features(
                    f"c{i}", 60.0 + i * 30, 0.1 + i * 0.2,
                    0.5, 0.5, 0.5, 0.1 + i * 0.2, 0.5, 0, timbre_dummy,
                    tg.FEATURES_VERSION,
                )
        _run(insert_tracks())

        # No regressor row, no feedback → cold start. Top result must be
        # whichever track wins the prior alone.
        chill_results = _run(tg.tracks_by_mood(self.db, "chill", limit=1))
        cold_top = chill_results[0]["path"]

        # Manually plant a regressor with n_samples=0; result should match.
        # (Pre-condition: blend at n_samples=0 returns the prior unchanged.)
        w = np.zeros(mr.MOOD_REGRESSOR_DIM, dtype=np.float32)
        _run(self.db.save_mood_regressor(
            "chill", mr.pack_weights(w), 0.0, 0, tg.FEATURES_VERSION,
        ))
        tg.invalidate_mood_cache()
        chill_results = _run(tg.tracks_by_mood(self.db, "chill", limit=1))
        self.assertEqual(chill_results[0]["path"], cold_top,
                         "n_samples=0 regressor must not change ranking.")

    def test_regressor_overrides_prior_when_confident(self):
        """A regressor with n_samples ≥ N_CONFIDENT and weights that flip
        the ranking must override the prior."""
        from utils import mood_regressor as mr
        timbre_dummy = np.zeros(24, dtype=np.float32).tobytes()

        async def insert_tracks():
            async with self.db._write_lock:
                conn = await self.db.get_connection()
                cursor = await conn.execute("INSERT INTO artists (name) VALUES ('ov')")
                artist_id = cursor.lastrowid
                cursor = await conn.execute(
                    "INSERT INTO albums (artist_id, title) VALUES (?, 'ov')", (artist_id,))
                album_id = cursor.lastrowid
                for path in ("low", "high"):
                    await conn.execute(
                        "INSERT INTO tracks (path, title, album_id) VALUES (?, ?, ?)",
                        (path, path, album_id))
                await conn.commit()
            # `low` matches chill prior (low bpm/energy); `high` is the opposite.
            await self.db.update_track_features(
                'low', 60.0, 0.1, 0.1, 0.1, 0.1, 0.5, 0.1, 0, timbre_dummy,
                tg.FEATURES_VERSION,
            )
            await self.db.update_track_features(
                'high', 180.0, 0.9, 0.9, 0.9, 0.9, 0.5, 0.9, 0, timbre_dummy,
                tg.FEATURES_VERSION,
            )
        _run(insert_tracks())

        # Verify the prior picks 'low' first (sanity).
        prior_top = _run(tg.tracks_by_mood(self.db, "chill", limit=1))[0]["path"]
        self.assertEqual(prior_top, "low")

        # Plant a confident regressor that strongly prefers high values on
        # the same features — large positive weights on bpm/energy/etc.
        # n_samples=N_CONFIDENT clamps γ=1.0 so the regressor wins outright.
        w = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 0.0, 10.0, 0.0],
                     dtype=np.float32)
        _run(self.db.save_mood_regressor(
            "chill", mr.pack_weights(w), 0.0, mr.N_CONFIDENT,
            tg.FEATURES_VERSION,
        ))
        tg.invalidate_mood_cache()
        flipped_top = _run(tg.tracks_by_mood(self.db, "chill", limit=1))[0]["path"]
        self.assertEqual(flipped_top, "high",
                         "Confident regressor must outrank the prior.")

    def test_islet_threshold_is_calibrated_probability(self):
        """When tracks_in_islet runs against an islet that has a regressor
        (lazy-bootstrapped from the exemplar), membership is calibrated
        probability: higher threshold → fewer members; lowering reveals more.
        The exemplar itself must always score above any reasonable threshold."""
        import json
        from utils import mood_regressor as mr
        timbre_dummy = np.zeros(52, dtype=np.float32).tobytes()

        # Isolated custom_moods.json so we don't touch the user's file.
        cm_path = self.tmp.name + ".custom_moods.json"
        original_path = tg.CUSTOM_MOODS_PATH
        tg.CUSTOM_MOODS_PATH = cm_path
        self.addCleanup(lambda: setattr(tg, "CUSTOM_MOODS_PATH", original_path))
        if os.path.exists(cm_path):
            os.remove(cm_path)

        async def insert():
            async with self.db._write_lock:
                conn = await self.db.get_connection()
                cursor = await conn.execute("INSERT INTO artists (name) VALUES ('isl')")
                artist_id = cursor.lastrowid
                cursor = await conn.execute(
                    "INSERT INTO albums (artist_id, title) VALUES (?, 'isl')", (artist_id,))
                album_id = cursor.lastrowid
                # Exemplar `ex` at one extreme, near-neighbour `near` close to
                # it, far outlier `far` at the opposite extreme.
                for path in ("ex", "near", "far"):
                    await conn.execute(
                        "INSERT INTO tracks (path, title, album_id) VALUES (?, ?, ?)",
                        (path, path, album_id))
                await conn.commit()
            await self.db.update_track_features(
                'ex',   60.0, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0,
                timbre_dummy, tg.FEATURES_VERSION)
            await self.db.update_track_features(
                'near', 70.0, 0.15, 0.15, 0.15, 0.15, 0.15, 0.15, 0,
                timbre_dummy, tg.FEATURES_VERSION)
            await self.db.update_track_features(
                'far',  200.0, 0.95, 0.95, 0.95, 0.95, 0.95, 0.95, 0,
                timbre_dummy, tg.FEATURES_VERSION)
        _run(insert())

        # Write the islet JSON pointing at 'ex' as exemplar. Centroid is
        # dummy zeros (52-dim) so the cosine fallback would never match —
        # this forces tracks_in_islet down the regressor path.
        with open(cm_path, "w", encoding="utf-8") as f:
            json.dump({
                "test_islet": {
                    "centroid":      [0.0] * 52,
                    "exemplar_path": "ex",
                    "threshold":     0.5,
                }
            }, f)

        # First call lazy-bootstraps the regressor from the exemplar's
        # percentile vector. Threshold=0.5 → exemplar + close neighbour pass.
        members = _run(tg.tracks_in_islet(self.db, "test_islet", min_count=1))
        member_paths = {m["path"] for m in members}
        self.assertIn("ex", member_paths,
                      "Exemplar must always survive its own islet threshold.")

        # Confirm the regressor row was actually persisted by the lazy path.
        reg = _run(self.db.get_mood_regressor("test_islet", tg.FEATURES_VERSION))
        self.assertIsNotNone(reg, "Lazy bootstrap should persist a regressor row.")

        # Tighten threshold → fewer members (probability-calibrated, so 0.99
        # is a strict 'almost certainly this mood' bar).
        with open(cm_path, "w", encoding="utf-8") as f:
            json.dump({
                "test_islet": {
                    "centroid":      [0.0] * 52,
                    "exemplar_path": "ex",
                    "threshold":     0.99,
                }
            }, f)
        strict_members = _run(tg.tracks_in_islet(self.db, "test_islet", min_count=0))
        # Strict threshold must not return more tracks than the lenient one.
        self.assertLessEqual(len(strict_members), len(members),
                             "Tightening threshold should not expand membership.")

    def test_record_islet_negative_increments_regressor(self):
        """record_islet_negative must feed a y=0 update when a regressor
        exists, and be a no-op when one does not."""
        import json
        timbre_dummy = np.zeros(52, dtype=np.float32).tobytes()

        cm_path = self.tmp.name + ".islet_neg.json"
        original_path = tg.CUSTOM_MOODS_PATH
        tg.CUSTOM_MOODS_PATH = cm_path
        self.addCleanup(lambda: setattr(tg, "CUSTOM_MOODS_PATH", original_path))
        if os.path.exists(cm_path):
            os.remove(cm_path)

        async def setup():
            async with self.db._write_lock:
                conn = await self.db.get_connection()
                cursor = await conn.execute("INSERT INTO artists (name) VALUES ('n')")
                artist_id = cursor.lastrowid
                cursor = await conn.execute(
                    "INSERT INTO albums (artist_id, title) VALUES (?, 'n')", (artist_id,))
                album_id = cursor.lastrowid
                for path in ("anchor", "victim"):
                    await conn.execute(
                        "INSERT INTO tracks (path, title, album_id) VALUES (?, ?, ?)",
                        (path, path, album_id))
                await conn.commit()
            await self.db.update_track_features(
                'anchor', 60.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0,
                timbre_dummy, tg.FEATURES_VERSION)
            await self.db.update_track_features(
                'victim', 180.0, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0,
                timbre_dummy, tg.FEATURES_VERSION)
        _run(setup())

        with open(cm_path, "w", encoding="utf-8") as f:
            json.dump({
                "neg_islet": {
                    "centroid":      [0.0] * 52,
                    "exemplar_path": "anchor",
                    "threshold":     0.5,
                }
            }, f)

        # No regressor yet → record_islet_negative is a no-op.
        ok = _run(tg.record_islet_negative(self.db, "neg_islet", "victim"))
        self.assertFalse(ok, "Should return False when no regressor exists.")

        # Trigger the lazy bootstrap by calling tracks_in_islet once.
        _run(tg.tracks_in_islet(self.db, "neg_islet", min_count=0))
        reg_before = _run(self.db.get_mood_regressor("neg_islet", tg.FEATURES_VERSION))
        self.assertIsNotNone(reg_before)
        n_before = reg_before[2]

        # Now the regressor exists → negative feedback bumps n_samples.
        ok = _run(tg.record_islet_negative(self.db, "neg_islet", "victim"))
        self.assertTrue(ok)
        reg_after = _run(self.db.get_mood_regressor("neg_islet", tg.FEATURES_VERSION))
        self.assertEqual(reg_after[2], n_before + 1,
                         "Negative feedback must increment n_samples.")

    def test_partition_recalculator_routing(self):
        timbre_dummy = np.zeros(24, dtype=np.float32).tobytes()
        
        async def setup_routing_tracks():
            # SQLite tracks
            async with self.db._write_lock:
                conn = await self.db.get_connection()
                cursor = await conn.execute("INSERT INTO artists (name) VALUES ('routing_artist')")
                artist_id = cursor.lastrowid
                cursor = await conn.execute("INSERT INTO albums (artist_id, title) VALUES (?, 'routing_album')", (artist_id,))
                album_id = cursor.lastrowid
                
                await conn.execute("INSERT INTO tracks (path, title, album_id) VALUES ('track_chill', 'track_chill', ?)", (album_id,))
                await conn.execute("INSERT INTO tracks (path, title, album_id) VALUES ('track_energetic', 'track_energetic', ?)", (album_id,))
                await conn.commit()
            
            # features for 'track_chill' (low bpm, low energy)
            await self.db.update_track_features('track_chill', 60.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0, timbre_dummy, tg.FEATURES_VERSION)
            # features for 'track_energetic' (high bpm, high energy)
            await self.db.update_track_features('track_energetic', 180.0, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0, timbre_dummy, tg.FEATURES_VERSION)
            
        _run(setup_routing_tracks())

        async def run_recalculate():
            rows, percentile_matrix = await tg._load_percentile_matrix(self.db, tg.FEATURES_VERSION)
            all_tracks = await self.db.get_all_tracks()
            all_paths_to_track = {t["path"]: t for t in all_tracks}
            
            analysed_rows = []
            analysed_indices = []
            for idx, r in enumerate(rows):
                if r["path"] in all_paths_to_track:
                    analysed_rows.append(r)
                    analysed_indices.append(idx)
            
            mood_assignments = {}
            if analysed_rows and len(analysed_indices) > 0:
                filtered_percentiles = percentile_matrix[analysed_indices]
                feedback_map = await self.db.get_mood_feedback()
                adjusted_profiles = await self.db.get_all_adjusted_mood_profiles()
                
                mood_scores = {}
                for mood, spec in tg.MOODS.items():
                    profile = adjusted_profiles.get(mood) or spec.profile
                    if profile:
                        mood_scores[mood] = tg._score_against_profile(profile, filtered_percentiles)
                    else:
                        mood_scores[mood] = np.full(len(analysed_rows), -np.inf, dtype=np.float32)
                        
                for i, track in enumerate(analysed_rows):
                    path = track["path"]
                    track_feedback = feedback_map.get(path, {})
                    
                    track_likes = [m for m, fb in track_feedback.items() if fb == 1]
                    if track_likes:
                        best_mood = None
                        best_score = -np.inf
                        for mood in track_likes:
                            if mood in mood_scores:
                                score = mood_scores[mood][i]
                                if score > best_score:
                                    best_score = score
                                    best_mood = mood
                        if best_mood is None:
                            best_mood = track_likes[0]
                    else:
                        dislikes = {m for m, fb in track_feedback.items() if fb == -1}
                        best_mood = None
                        best_score = -np.inf
                        for mood in tg.MOODS.keys():
                            if mood in dislikes:
                                continue
                            score = mood_scores[mood][i]
                            if score > best_score:
                                best_score = score
                                best_mood = mood
                                
                    if best_mood is not None:
                        mood_assignments[path] = best_mood
            return mood_assignments

        # 1. Natural routing (no feedback)
        assignments = _run(run_recalculate())
        self.assertIn("track_chill", assignments)
        self.assertIn("track_energetic", assignments)
        natural_chill_mood = assignments["track_chill"]
        natural_energetic_mood = assignments["track_energetic"]
        
        # Chill track should route to a low-energy family mood; energetic to
        # a high-energy family.
        self.assertIn(natural_chill_mood, ["chill", "dark"])
        self.assertIn(natural_energetic_mood, ["intense", "upbeat", "rock", "beats"])

        # 2. Pin energetic track to 'chill' (LIKE)
        _run(self.db.save_mood_feedback("track_energetic", "chill", 1))
        assignments = _run(run_recalculate())
        self.assertEqual(assignments["track_energetic"], "chill")

        # 3. Dislike natural chill mood for the chill track
        _run(self.db.save_mood_feedback("track_chill", natural_chill_mood, -1))
        assignments = _run(run_recalculate())
        self.assertNotEqual(assignments["track_chill"], natural_chill_mood)
        # Should route to second best matching mood
        new_chill_mood = assignments["track_chill"]
        self.assertIsNotNone(new_chill_mood)
        self.assertIn(new_chill_mood, ["chill", "dark"])


if __name__ == "__main__":
    unittest.main()
