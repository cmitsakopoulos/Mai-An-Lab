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

        # 2. Save profile
        test_prof = {"bpm": 0.25, "energy": 0.45}
        _run(self.db.save_adjusted_mood_profile("chill", test_prof))
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertEqual(prof, test_prof)

        # 3. Get all profiles
        all_profs = _run(self.db.get_all_adjusted_mood_profiles())
        self.assertEqual(all_profs, {"chill": test_prof})

        # 4. Clear profiles
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

        # Let's test adjust_mood_profile for "chill" (default spec bpm: 0.20, energy: 0.20)
        # 1. LIKE on 't2' (bpm percentile 1.0, energy percentile 1.0)
        # Shift towards: T_new = T_old + 0.15 * (P_track - T_old)
        # bpm: 0.20 + 0.15 * (1.0 - 0.20) = 0.20 + 0.12 = 0.32
        # energy: 0.20 + 0.15 * (1.0 - 0.20) = 0.20 + 0.12 = 0.32
        _run(tg.adjust_mood_profile(self.db, "chill", "t2", 1))
        
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertIsNotNone(prof)
        self.assertAlmostEqual(prof["bpm"], 0.32, places=5)
        self.assertAlmostEqual(prof["energy"], 0.32, places=5)

        # 2. DISLIKE on 't0' (bpm percentile 0.0, energy percentile 0.0)
        # Shift away: T_new = T_old - 0.15 * (P_track - T_old)
        # bpm: 0.32 - 0.15 * (0.0 - 0.32) = 0.32 + 0.048 = 0.368
        # energy: 0.32 - 0.15 * (0.0 - 0.32) = 0.32 + 0.048 = 0.368
        _run(tg.adjust_mood_profile(self.db, "chill", "t0", -1))
        
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertAlmostEqual(prof["bpm"], 0.368, places=5)
        self.assertAlmostEqual(prof["energy"], 0.368, places=5)

        # 3. Test clamping
        # A. Clamp to 1.0: starting at 0.9, shift away from percentile 0.0 (t0)
        _run(self.db.save_adjusted_mood_profile("chill", {"bpm": 0.9, "energy": 0.5}))
        _run(tg.adjust_mood_profile(self.db, "chill", "t0", -1))
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertAlmostEqual(prof["bpm"], 1.0, places=5)

        # B. Clamp to 0.0: starting at 0.1, shift away from percentile 1.0 (t2)
        _run(self.db.save_adjusted_mood_profile("chill", {"bpm": 0.1, "energy": 0.5}))
        _run(tg.adjust_mood_profile(self.db, "chill", "t2", -1))
        prof = _run(self.db.get_adjusted_mood_profile("chill"))
        self.assertAlmostEqual(prof["bpm"], 0.0, places=5)

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
        
        # Chill track should route to chill/relaxed/calm/ambient, energetic track to aggressive/energetic/fast/noisy/upbeat/happy/powerful/hard
        self.assertIn(natural_chill_mood, ["chill", "relaxed", "calm", "ambient", "mellow", "soft", "slow", "moody"])
        self.assertIn(natural_energetic_mood, ["energetic", "aggressive", "fast", "noisy", "upbeat", "happy", "powerful", "hard"])

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
        self.assertIn(new_chill_mood, ["chill", "relaxed", "calm", "ambient", "mellow", "soft", "slow", "moody"])


if __name__ == "__main__":
    unittest.main()
