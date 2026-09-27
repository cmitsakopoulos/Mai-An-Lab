"""AutoPlay controller: seeding, anchor modes, implicit feedback, buffer upkeep."""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import autoplay as ap_mod  # noqa: E402
from utils.autoplay import AutoPlay, FOLLOW, STAY  # noqa: E402


class FakeEngine:
    """Just the queue surface AutoPlay touches."""

    def __init__(self, paths, current=0):
        self.queue = [{"path": p} for p in paths]
        self.current_index = current
        self.play_similar_seed_path = ""
        self.stopped = False

    @property
    def current_path(self):
        return self.queue[self.current_index]["path"] if self.queue else ""

    def queue_after_current(self, tracks, after_index=None):
        base = self.current_index if after_index is None else after_index
        for off, t in enumerate(tracks):
            self.queue.insert(base + 1 + off, t)

    def queue_extend(self, tracks):
        self.queue.extend(tracks)

    def play_track_at(self, i):
        self.current_index = i

    def remove_indices(self, idxs):
        for i in sorted(idxs, reverse=True):
            self.queue.pop(i)

    def stop(self):
        self.stopped = True


def _rows(paths):
    return {p: {"path": p, "title": p, "artist": "A", "album": "B", "duration": 200.0} for p in paths}


class AutoPlayTests(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine(["/lib/a", "/lib/b", "/lib/c"])
        self.db = MagicMock()
        self.db.get_tracks_brief = AsyncMock(side_effect=_rows)
        self.db.has_current_features = AsyncMock(return_value=True)
        self.tasks = []
        self.notify = MagicMock()
        self.ap = AutoPlay(self.engine, self.db,
                           run_task=lambda fn, *a: self.tasks.append((fn, a)),
                           notify=self.notify)

    def drain(self, walk_result):
        with patch("utils.track_graph.walk", new_callable=AsyncMock) as walk:
            walk.return_value = walk_result
            while self.tasks:
                fn, args = self.tasks.pop(0)
                asyncio.run(fn(*args))
            return walk

    def paths(self):
        return [t["path"] for t in self.engine.queue]

    def enable(self, walk=("/r/1", "/r/2", "/r/3", "/r/4", "/r/5", "/r/6", "/r/7", "/r/8")):
        self.ap.set_enabled(True)
        self.drain(list(walk))

    # ── buffer upkeep ───────────────────────────────────────────────────────
    def test_enable_inserts_tagged_buffer_after_current_keeping_tail(self):
        self.enable()
        self.assertEqual(self.paths()[:2], ["/lib/a", "/r/1"])
        self.assertEqual(self.paths()[-2:], ["/lib/b", "/lib/c"])
        self.assertEqual(len(self.ap.buffer_paths()), 8)
        self.assertEqual(self.engine.play_similar_seed_path, "/lib/a")

    def test_disable_drops_only_the_pending_buffer(self):
        self.enable()
        self.ap.set_enabled(False)
        self.assertEqual(self.paths(), ["/lib/a", "/lib/b", "/lib/c"])
        self.assertEqual(self.engine.play_similar_seed_path, "")

    def test_toggle_off_mid_walk_discards_the_stale_block(self):
        self.ap.set_enabled(True)
        self.ap.set_enabled(False)
        self.drain(["/r/1"])
        self.assertEqual(self.paths(), ["/lib/a", "/lib/b", "/lib/c"])

    def test_background_prefill_tops_up_to_background_target(self):
        self.enable()
        self.ap.prefill_for_background()
        _fn, args = self.tasks[0]
        self.assertEqual(args[1], ap_mod.BACKGROUND_TARGET - 8)

    def test_refill_appends_below_existing_buffer(self):
        self.enable(walk=["/r/1", "/r/2", "/r/3"])
        self.ap.ensure_buffer()
        self.drain(["/r/9"])
        self.assertEqual(self.paths()[1:5], ["/r/1", "/r/2", "/r/3", "/r/9"])

    # ── seeding ─────────────────────────────────────────────────────────────
    def test_follow_refill_seeds_from_last_accepted_not_buffer_tail(self):
        self.enable(walk=["/r/1", "/r/2", "/r/3"])
        self.ap.on_listen("/r/1", listened_s=200, duration_s=200)  # heard through
        self.ap.ensure_buffer()
        _fn, args = self.tasks[0]
        self.assertEqual(args[0], "/r/1")

    def test_stay_refill_always_seeds_from_anchor(self):
        self.ap.set_anchor_mode(STAY)
        self.enable(walk=["/r/1", "/r/2", "/r/3"])
        self.ap.on_listen("/r/1", listened_s=200, duration_s=200)
        self.ap.ensure_buffer()
        _fn, args = self.tasks[0]
        self.assertEqual(args[0], "/lib/a")

    def test_follow_user_choice_restarts_station(self):
        self.enable()
        self.engine.current_index = self.paths().index("/lib/c")
        self.ap.on_user_played("/lib/c")
        self.assertEqual(self.ap.anchor, "/lib/c")
        self.drain(["/r/x"])
        i = self.paths().index("/lib/c")
        self.assertEqual(self.paths()[i + 1], "/r/x")

    def test_stay_user_choice_keeps_station(self):
        self.ap.set_anchor_mode(STAY)
        self.enable()
        self.ap.on_user_played("/lib/b")
        self.assertEqual(self.ap.anchor, "/lib/a")
        self.assertEqual(self.tasks, [])  # buffer still full, nothing to do

    def test_avoid_covers_history_rejections_and_queued_buffer(self):
        self.ap.on_track_changed("/lib/z")                       # heard earlier
        self.ap.reject("/lib/y")                                 # "not this"
        self.ap.on_listen("/lib/w", listened_s=5, duration_s=200)  # early skip
        self.ap.set_enabled(True)
        walk = self.drain(["/r/1"])
        avoid = walk.call_args.kwargs["avoid"]
        self.assertTrue({"/lib/z", "/lib/y", "/lib/w", "/lib/a"} <= avoid)

    # ── implicit feedback ───────────────────────────────────────────────────
    def test_two_fresh_early_skips_of_autoplay_tracks_replan(self):
        self.enable()
        self.ap.on_listen("/r/1", listened_s=4, duration_s=200)
        self.assertEqual(self.tasks, [])
        self.ap.on_listen("/r/2", listened_s=6, duration_s=200)
        self.assertEqual(self.ap.buffer_paths(), [])      # old plan dropped
        self.drain(["/r/9"])
        self.assertIn("/r/9", self.ap.buffer_paths())

    def test_accepted_listen_resets_the_skip_run(self):
        self.enable()
        self.ap.on_listen("/r/1", listened_s=4, duration_s=200)
        self.ap.on_listen("/r/2", listened_s=120, duration_s=200)
        self.ap.on_listen("/r/3", listened_s=4, duration_s=200)
        self.assertEqual(len(self.ap.buffer_paths()), 8)

    def test_stale_ledger_skips_reject_but_do_not_replan(self):
        self.enable()
        old = time.time() - 3600
        self.ap.on_listens([
            {"path": "/r/1", "listened_s": 3, "duration_s": 200, "ended_at": old},
            {"path": "/r/2", "listened_s": 3, "duration_s": 200, "ended_at": old},
        ])
        self.assertEqual(len(self.ap.buffer_paths()), 8)
        self.assertIn("/r/1", self.ap.rejected)

    def test_skipping_library_tracks_does_not_replan(self):
        self.enable()
        self.ap.on_listen("/lib/b", listened_s=3, duration_s=200)
        self.ap.on_listen("/lib/c", listened_s=3, duration_s=200)
        self.assertEqual(len(self.ap.buffer_paths()), 8)

    # ── dry queue / unanalysed seeds ────────────────────────────────────────
    def test_queue_dry_appends_and_plays_first_new(self):
        self.engine = FakeEngine(["/lib/a"])
        self.ap.engine = self.engine
        self.ap.enabled = True
        self.ap.on_queue_dry()
        self.drain(["/r/1", "/r/2"])
        self.assertEqual(self.paths(), ["/lib/a", "/r/1", "/r/2"])
        self.assertEqual(self.engine.current_index, 1)

    def test_unanalysed_anchor_is_explained_and_handed_over(self):
        self.db.has_current_features = AsyncMock(return_value=False)
        self.ap.set_enabled(True)
        with patch("utils.track_graph.walk", new_callable=AsyncMock) as walk:
            walk.return_value = []
            fn, args = self.tasks.pop(0)
            asyncio.run(fn(*args))
        self.notify.assert_called_once()
        self.engine.current_index = 1
        self.ap.on_track_changed("/lib/b")
        self.assertEqual(self.ap.anchor, "/lib/b")
        _fn, args = self.tasks[0]
        self.assertEqual(args[0], "/lib/b")


if __name__ == "__main__":
    unittest.main()
