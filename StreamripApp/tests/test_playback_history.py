"""Round-trip tests for the playback_history table and its helpers.

Uses a real on-disk SQLite database (in a tempdir) so the migration runs
against the real schema; the helpers under test are async, so we wrap each
in asyncio.run."""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.db_manager import DatabaseManager


def _run(coro):
    return asyncio.run(coro)


class TestPlaybackHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DatabaseManager(self.tmp.name)
        _run(self.db.initialize())

    def tearDown(self):
        _run(self.db.close())
        os.unlink(self.tmp.name)

    def test_record_and_recall_recent(self):
        _run(self.db.record_playback("/m/a.flac", "played"))
        _run(self.db.record_playback("/m/b.flac", "played", seed_path="/m/a.flac"))
        recent = _run(self.db.recent_played_paths(window_seconds=3600))
        self.assertEqual(recent, {"/m/a.flac", "/m/b.flac"})

    def test_recent_window_excludes_old(self):
        # window=0 means "played strictly after now" — every record was
        # written ≥0 seconds ago, so an exact-now window excludes them all.
        _run(self.db.record_playback("/m/a.flac", "played"))
        recent = _run(self.db.recent_played_paths(window_seconds=-1))
        self.assertEqual(recent, set())

    def test_listen_signal_balance(self):
        # Two completions, one skip, two plays → numerator = 2 − 1 = 1,
        # denominator = max(plays=2, 5) = 5 → signal = 0.2.
        _run(self.db.record_playback("/m/x.flac", "played"))
        _run(self.db.record_playback("/m/x.flac", "played"))
        _run(self.db.record_playback("/m/x.flac", "completed"))
        _run(self.db.record_playback("/m/x.flac", "completed"))
        _run(self.db.record_playback("/m/x.flac", "skipped_early"))
        signal = _run(self.db.listen_signal_map())
        self.assertAlmostEqual(signal["/m/x.flac"], 0.2, places=5)

    def test_listen_signal_floor_softens_single_skip(self):
        # A single skip on a brand-new track yields signal −0.2, not −1.0,
        # because the denominator is floored at 5.
        _run(self.db.record_playback("/m/y.flac", "played"))
        _run(self.db.record_playback("/m/y.flac", "skipped_early"))
        signal = _run(self.db.listen_signal_map())
        self.assertAlmostEqual(signal["/m/y.flac"], -0.2, places=5)

    def test_empty_path_is_noop(self):
        _run(self.db.record_playback("", "played"))
        self.assertEqual(_run(self.db.recent_played_paths(3600)), set())


if __name__ == "__main__":
    unittest.main()
