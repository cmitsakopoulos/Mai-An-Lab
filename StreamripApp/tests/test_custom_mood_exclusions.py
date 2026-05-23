"""Unit tests for custom mood track exclusions and blacklist filtering.

Ensures that blacklisted tracks never rejoin the custom mood, even if the similarity threshold is loosened.
"""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Isolate APP_DIR so the custom-mood JSON probe doesn't see the user's real moods file.
import types as _types
_cfg = _types.ModuleType("utils.config")
_cfg.APP_DIR = tempfile.mkdtemp(prefix="dsptest_app_")
sys.modules["utils.config"] = _cfg

from utils import track_graph as tg
import numpy as np

def _run(coro):
    return asyncio.run(coro)

def _blob(vec: np.ndarray) -> bytes:
    return vec.astype("<f4").tobytes()

class FakeIsletDB:
    """In-memory db_manager surface needed by tg.tracks_in_islet."""
    def __init__(self, rows: list[dict]):
        self.rows = rows

    async def get_tracks_with_features(self, features_version):
        return list(self.rows)

class TestCustomMoodExclusions(unittest.TestCase):
    def setUp(self):
        # Reset custom moods path for isolation
        tg.CUSTOM_MOODS_PATH = os.path.join(_cfg.APP_DIR, "custom_moods.json")
        if os.path.exists(tg.CUSTOM_MOODS_PATH):
            os.remove(tg.CUSTOM_MOODS_PATH)

    def test_exclude_and_clear_blacklist(self):
        # 1. Setup mock centroid and timbre embeddings
        # We use a 52-dimensional vector matching track_graph.FEATURES_DIM (52)
        centroid = np.zeros(52, dtype=np.float32)
        centroid[0] = 1.0  # simple seed
        
        timbre_a = np.zeros(52, dtype=np.float32)
        timbre_a[0] = 1.0  # Cosine similarity is 1.0
        
        timbre_b = np.zeros(52, dtype=np.float32)
        timbre_b[0] = 0.8  # Cosine similarity will be 0.8
        timbre_b[1] = 0.6
        
        rows = [
            {
                "path": "/path/to/song_a.mp3",
                "title": "Song A",
                "timbre": _blob(timbre_a)
            },
            {
                "path": "/path/to/song_b.mp3",
                "title": "Song B",
                "timbre": _blob(timbre_b)
            }
        ]
        db = FakeIsletDB(rows)
        
        # 2. Save new custom mood "chill islet" with threshold 0.95
        tg.save_custom_mood("chill islet", centroid.tolist(), "/path/to/song_a.mp3", threshold=0.95)
        
        # Verify natural membership before exclusion
        # Only Song A should be member (since similarity ~0.99 >= 0.95, while Song B similarity ~0.94 < 0.95)
        members = _run(tg.tracks_in_islet(db, "chill islet", min_count=0))
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["path"], "/path/to/song_a.mp3")
        
        # Loosen threshold to 0.75 -> Song B should now also join the islet
        tg.update_custom_mood("chill islet", "chill islet", threshold=0.75)
        members = _run(tg.tracks_in_islet(db, "chill islet", min_count=0))
        self.assertEqual(len(members), 2)
        
        # 3. Exclude/Blacklist Song A from the islet
        ok = tg.blacklist_track_from_islet("chill islet", "/path/to/song_a.mp3")
        self.assertTrue(ok)
        
        # Verify that Song A is excluded under threshold 0.75
        members = _run(tg.tracks_in_islet(db, "chill islet", min_count=0))
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["path"], "/path/to/song_b.mp3")
        
        # 4. Loosen range/threshold even further to 0.70 (increasing islet range)
        # Song A must STILL remain excluded (never rejoin)
        tg.update_custom_mood("chill islet", "chill islet", threshold=0.70)
        members = _run(tg.tracks_in_islet(db, "chill islet", min_count=0))
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["path"], "/path/to/song_b.mp3")
        
        # 5. Clear the islet blacklist exclusions
        ok = tg.clear_islet_blacklist("chill islet")
        self.assertTrue(ok)
        
        # Song A should instantly rejoin the islet
        members = _run(tg.tracks_in_islet(db, "chill islet", min_count=0))
        self.assertEqual(len(members), 2)
        paths = [m["path"] for m in members]
        self.assertIn("/path/to/song_a.mp3", paths)
        self.assertIn("/path/to/song_b.mp3", paths)

if __name__ == "__main__":
    unittest.main()
