"""Resume after the process dies, and auto-play memory across sessions.

The native player writes its own resume record; Python's position file goes
stale once the UI session is gone (background auto-advance), so the newer of
the two must win."""
import asyncio
import os
import sys
import unittest
import unittest.mock
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
from utils import play_ledger  # noqa: E402
from utils.autoplay import AutoPlay  # noqa: E402

choose = main.StreamripFletApp._choose_resume

QUEUE = [{"path": f"/m/{i}", "duration": 100.0 + i} for i in range(6)]


class ChooseResumeTests(unittest.TestCase):
    def test_newer_native_record_wins_over_a_stale_position_file(self):
        pstate = {"path": "/m/1", "current_index": 1, "position": 42.0, "duration": 101.0}
        native = ("/m/4", 4, 3.0, 2000.0)
        self.assertEqual(choose(QUEUE, 0, 0.0, 0.0, pstate, native, pos_mtime=1000.0),
                         (4, 3.0, 104.0))

    def test_older_native_record_loses(self):
        pstate = {"path": "/m/1", "current_index": 1, "position": 42.0, "duration": 101.0}
        native = ("/m/4", 4, 3.0, 500.0)
        self.assertEqual(choose(QUEUE, 0, 0.0, 0.0, pstate, native, pos_mtime=1000.0),
                         (1, 42.0, 101.0))

    def test_native_track_no_longer_queued_is_ignored(self):
        native = ("/gone", 9, 3.0, 2000.0)
        self.assertEqual(choose(QUEUE, 2, 5.0, 102.0, None, native, pos_mtime=0.0),
                         (2, 5.0, 102.0))

    def test_index_hint_disambiguates_a_track_queued_twice(self):
        q = QUEUE + [{"path": "/m/1", "duration": 101.0}]
        native = ("/m/1", 6, 0.0, 2000.0)
        self.assertEqual(choose(q, 0, 0.0, 0.0, None, native, pos_mtime=0.0)[0], 6)

    def test_without_records_the_queue_file_index_stands(self):
        self.assertEqual(choose(QUEUE, 3, 7.0, 103.0, None, None, pos_mtime=0.0),
                         (3, 7.0, 103.0))

    def test_native_src_maps_to_a_library_path(self):
        self.assertEqual(play_ledger.src_to_path("file:///music/a%20b.flac"), "/music/a b.flac")
        self.assertEqual(play_ledger.src_to_path("https://stream/x"), "")


class QueueSnapshotTests(unittest.TestCase):
    """The saved queue is the only copy until this process has loaded it."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "queue_state.json")
        with open(self.path, "w") as fh:
            fh.write('{"queue": [{"path": "/m/1"}], "current_index": 0}')
        self._patches = [
            unittest.mock.patch.object(main, "DATA_DIR", self.dir),
            unittest.mock.patch.object(main.audio_engine, "queue", []),
            unittest.mock.patch.dict(main._QUEUE_STATE, {"held": False}),
        ]
        for p in self._patches:
            p.start()
        self.app = main.StreamripFletApp.__new__(main.StreamripFletApp)

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_an_empty_queue_before_any_restore_keeps_the_snapshot(self):
        self.app._save_queue_state()
        self.assertTrue(os.path.exists(self.path))

    def test_clearing_a_queue_this_process_held_removes_it(self):
        main._QUEUE_STATE["held"] = True
        self.app._save_queue_state()
        self.assertFalse(os.path.exists(self.path))


class AutoPlayMemoryTests(unittest.TestCase):
    def _ap(self, recent):
        db = MagicMock()
        db.get_autoplay_hidden = AsyncMock(return_value=[])
        db.get_hop_rejects = AsyncMock(return_value=[])
        db.get_recent_tracks = AsyncMock(return_value=recent)
        engine = MagicMock(queue=[], current_index=0)
        return AutoPlay(engine, db, run_task=MagicMock())

    def test_cold_start_seeds_history_from_recent_plays_oldest_first(self):
        ap = self._ap([{"path": "/new"}, {"path": "/old"}])
        asyncio.run(ap.load_feedback())
        self.assertEqual(list(ap.history), ["/old", "/new"])

    def test_a_live_history_is_not_reseeded(self):
        ap = self._ap([{"path": "/x"}])
        ap.history.append("/live")
        asyncio.run(ap.load_feedback())
        self.assertEqual(list(ap.history), ["/live"])

    def test_listens_heard_while_the_ui_was_away_join_history(self):
        ap = self._ap([])
        ap.on_listens([{"path": "/bg/1", "listened_s": 200, "duration_s": 200},
                       {"path": "/bg/1", "listened_s": 200, "duration_s": 200}])
        self.assertEqual(list(ap.history), ["/bg/1"])

    def test_bind_ui_repoints_the_session_hooks(self):
        ap = self._ap([])
        run, notify = MagicMock(), MagicMock()
        ap.bind_ui(run, notify)
        ap._notify("hi")
        notify.assert_called_once_with("hi")
        self.assertIs(ap._run_task, run)


if __name__ == "__main__":
    unittest.main()
