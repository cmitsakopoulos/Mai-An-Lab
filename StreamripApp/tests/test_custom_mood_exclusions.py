"""Unit tests for custom-mood (islet) blacklist filtering in the unified Zr geometry.

A blacklisted track must never rejoin the islet — even as the threshold loosens —
and clearing the blacklist lets it rejoin. Islets now score by a self-tuning Zr
affinity to the exemplar (1.0 = the exemplar itself), so thresholds are on the
affinity scale, not raw-timbre cosine. With two tracks the single neighbour sits
at affinity ≈ e⁻¹ ≈ 0.37 by construction, which the thresholds below straddle.
"""

import asyncio
import os
import sys
import tempfile
import types as _types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import utils.config as _orig_config
_cfg = _types.ModuleType("utils.config")
for _k, _v in _orig_config.__dict__.items():
    _cfg.__dict__[_k] = _v
_cfg.APP_DIR = tempfile.mkdtemp(prefix="dsptest_app_")
sys.modules["utils.config"] = _cfg

import numpy as np

from utils.db_manager import DatabaseManager
from utils import track_graph as tg


def _run(coro):
    return asyncio.run(coro)


def _blob(vec: np.ndarray) -> bytes:
    return vec.astype("<f4").tobytes()


async def _seed(db, specs):
    async with db._write_lock:
        conn = await db.get_connection()
        cur = await conn.execute("INSERT INTO artists (name) VALUES ('a')")
        aid = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO albums (artist_id, title) VALUES (?, 'al')", (aid,))
        alb = cur.lastrowid
        for s in specs:
            await conn.execute(
                "INSERT INTO tracks (path, title, album_id, duration) VALUES (?, ?, ?, 240.0)",
                (s["path"], s["path"], alb))
        await conn.commit()
    for s in specs:
        await db.update_track_features(
            s["path"], s["bpm"], s["br"], s["en"], s["ro"], s["bs"], s["fl"], s["co"], s["ki"],
            _blob(s["timbre"]), tg.FEATURES_VERSION)


class TestCustomMoodExclusions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DatabaseManager(self.tmp.name)
        _run(self.db.initialize())
        tg.invalidate_mood_cache()
        tg.CUSTOM_MOODS_PATH = os.path.join(_cfg.APP_DIR, "custom_moods.json")
        if os.path.exists(tg.CUSTOM_MOODS_PATH):
            os.remove(tg.CUSTOM_MOODS_PATH)
        _run(_seed(self.db, [
            {"path": "/song_a.mp3", "timbre": np.full(52, 1.0, dtype=np.float32),
             "bpm": 60.0,  "br": 0.1, "en": 0.1, "ro": 0.1, "bs": 0.1, "fl": 0.5, "co": 0.1, "ki": 0},
            {"path": "/song_b.mp3", "timbre": np.full(52, 0.2, dtype=np.float32),
             "bpm": 120.0, "br": 0.5, "en": 0.5, "ro": 0.5, "bs": 0.5, "fl": 0.5, "co": 0.5, "ki": 0},
        ]))
        # Islets score in the unified Zr space → build/persist the geometry.
        _run(tg.build_metadata_edges(self.db))
        _run(tg.build_acoustic_edges(self.db))

    def tearDown(self):
        _run(self.db.close())
        os.unlink(self.tmp.name)
        if os.path.exists(tg.CUSTOM_MOODS_PATH):
            os.remove(tg.CUSTOM_MOODS_PATH)

    def _members(self):
        return [m["path"] for m in _run(tg.tracks_in_islet(self.db, "chill islet", min_count=0))]

    def test_exclude_and_clear_blacklist(self):
        tg.save_custom_mood("chill islet", [], "/song_a.mp3", threshold=0.5)
        # Tight threshold: only the exemplar (song_b ≈ 0.37 < 0.5).
        self.assertEqual(self._members(), ["/song_a.mp3"])

        # Loosen → song_b joins.
        tg.update_custom_mood("chill islet", "chill islet", threshold=0.3)
        self.assertEqual(set(self._members()), {"/song_a.mp3", "/song_b.mp3"})

        # Blacklist the exemplar → it drops, song_b remains.
        self.assertTrue(tg.blacklist_track_from_islet("chill islet", "/song_a.mp3"))
        self.assertEqual(self._members(), ["/song_b.mp3"])

        # Loosen further → the blacklisted track must NOT rejoin.
        tg.update_custom_mood("chill islet", "chill islet", threshold=0.2)
        self.assertEqual(self._members(), ["/song_b.mp3"])

        # Clear the blacklist → it rejoins.
        self.assertTrue(tg.clear_islet_blacklist("chill islet"))
        self.assertEqual(set(self._members()), {"/song_a.mp3", "/song_b.mp3"})


if __name__ == "__main__":
    unittest.main()
