"""AutoPlay controller: while on, the player holds only the listener's picks and
auto-play's buffer after the playing track (the rest is set aside); buffer
upkeep, choosing tracks, negative feedback, starting the genre over."""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import autoplay as ap_mod  # noqa: E402
from utils.autoplay import AutoPlay  # noqa: E402


class FakeEngine:
    """Just the queue surface AutoPlay touches."""

    def __init__(self, paths, current=0):
        self.queue = [{"path": p, "artist_name": "Lib"} for p in paths]
        self.current_index = current
        self.continues_at_end = False
        self.stopped = False
        self.replace_calls = 0
        self.insert_calls = 0

    @property
    def buffer_ops(self):
        """Single-op buffer writes: a swap of pending rows, or an insert when
        nothing was pending."""
        return self.replace_calls + self.insert_calls

    @property
    def current_path(self):
        return self.queue[self.current_index]["path"] if self.queue else ""

    def queue_after_current(self, tracks, after_index=None):
        self.insert_calls += 1
        base = self.current_index if after_index is None else after_index
        self.queue[base + 1:base + 1] = tracks

    def queue_extend(self, tracks):
        self.queue.extend(tracks)

    def play_track_at(self, i):
        self.current_index = i

    def remove_indices(self, idxs):
        for i in sorted(idxs, reverse=True):
            self.queue.pop(i)

    def replace_rows(self, idxs, tracks):
        self.replace_calls += 1
        first = min(idxs) if idxs else self.current_index + 1
        for i in sorted(idxs, reverse=True):
            self.queue.pop(i)
        self.queue[first:first] = tracks

    def stop(self):
        self.stopped = True


class FakeStation:
    """A resumable station over a fixed ranking: never emits a path twice."""

    def __init__(self, ranking):
        self._ranking = list(ranking)
        self.used: set = set()
        self.flavour = "Rock"
        self.rejected: list = []
        self.accepted: list = []

    async def take(self, n):
        out = [p for p in self._ranking if p not in self.used][:n]
        self.used.update(out)
        return out

    def reject(self, paths):
        self.rejected.append(list(paths))
        self.used.update(paths)

    def exclude(self, paths):
        self.used.update(paths)

    def accept(self, path):
        self.accepted.append(path)


def _rows(paths):
    return {p: {"path": p, "title": p, "artist": "A" + p[-1], "album": "B", "duration": 200.0}
            for p in paths}


RANKING = [f"/r/{i}" for i in range(1, 30)]


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
        self.stations = []
        self.next_ranking = RANKING
        self.open_calls = []

    async def _open(self, db, seed, **kw):
        self.open_calls.append((seed, kw))
        st = FakeStation(self.next_ranking)
        st.used.update(kw.get("avoid") or ())
        self.stations.append(st)
        return st

    def drain(self):
        with patch.object(ap_mod.track_graph, "open_station", side_effect=self._open):
            while self.tasks:
                fn, args = self.tasks.pop(0)
                asyncio.run(fn(*args))

    def paths(self):
        return [t["path"] for t in self.engine.queue]

    def enable(self):
        self.ap.set_enabled(True)
        self.drain()

    def ahead(self):
        """Rows after the playing one: only picks and recommendations, ever."""
        q = self.engine.queue[self.engine.current_index + 1:]
        self.assertTrue(all(t.get("_queued") or t.get("_autoplay") for t in q), q)
        return [t["path"] for t in q]

    def advance(self, i):
        self.engine.current_index = i
        self.ap.on_track_changed(self.engine.current_path)
        self.drain()

    # ── the queue auto-play takes over ──────────────────────────────────────
    def test_enable_sets_the_queue_aside_and_queues_a_buffer(self):
        self.enable()
        self.assertEqual(self.paths(), ["/lib/a", "/r/1", "/r/2", "/r/3", "/r/4"])
        self.assertEqual([t["path"] for t in self.ap.held], ["/lib/b", "/lib/c"])
        self.assertTrue(self.engine.continues_at_end)
        self.assertEqual(self.open_calls[0][0], "/lib/a")
        self.assertTrue(self.open_calls[0][1]["focused"])

    def test_disable_drops_the_buffer_and_puts_the_queue_back(self):
        self.enable()
        self.ap.set_enabled(False)
        self.assertEqual(self.paths(), ["/lib/a", "/lib/b", "/lib/c"])
        self.assertEqual(self.ap.held, [])
        self.assertFalse(self.engine.continues_at_end)
        self.assertIsNone(self.ap.station)

    def test_toggle_off_mid_fill_discards_the_stale_block(self):
        self.ap.set_enabled(True)
        self.ap.set_enabled(False)
        self.drain()
        self.assertEqual(self.paths(), ["/lib/a", "/lib/b", "/lib/c"])

    def test_the_listeners_picks_play_first_and_stay(self):
        self.engine.queue[1:1] = [{"path": "/u/1", "_queued": True},
                                  {"path": "/u/2", "_queued": True}]
        self.enable()
        self.assertEqual(self.ahead(), ["/u/1", "/u/2", "/r/1", "/r/2", "/r/3", "/r/4"])
        self.ap.set_enabled(False)
        self.assertEqual(self.paths(), ["/lib/a", "/u/1", "/u/2", "/lib/b", "/lib/c"])

    def test_playing_through_picks_and_buffer_never_reaches_the_old_queue(self):
        self.engine.queue[1:1] = [{"path": "/u/1", "_queued": True}]
        self.enable()
        for _ in range(12):
            self.advance(self.engine.current_index + 1)
            self.ahead()
        self.assertEqual(len(self.open_calls), 1)            # one station throughout
        self.assertFalse(any(p.startswith("/lib/") for p in self.paths()[1:]))

    def test_a_new_queue_set_elsewhere_starts_a_station_and_is_set_aside(self):
        self.enable()
        # Jarvis / an album "play all": a whole new queue, no on_user_chose.
        self.engine.queue = [{"path": f"/x/{i}"} for i in range(1, 4)]
        self.engine.current_index = 0
        self.ap.on_track_changed("/x/1")
        self.drain()
        self.assertEqual(self.ap.anchor, "/x/1")
        self.assertEqual(len(self.ahead()), 4)
        self.assertEqual([t["path"] for t in self.ap.held], ["/x/2", "/x/3"])

    def test_refills_continue_the_same_station(self):
        self.enable()
        self.advance(2)                                      # /r/2 playing
        self.assertEqual(len(self.open_calls), 1)            # resumed, not reopened
        self.assertEqual(self.ahead(), ["/r/3", "/r/4", "/r/5", "/r/6"])

    # ── session teardown / restore ──────────────────────────────────────────
    def test_a_fill_that_never_ran_stops_blocking_refills(self):
        # Scheduled on a session the OS tore down: the coroutine never ran,
        # so nothing cleared the flag.
        self.ap.set_enabled(True)
        self.tasks.clear()
        self.ap.ensure_buffer()
        self.assertEqual(self.tasks, [])                     # still "filling"
        with patch.object(ap_mod.time, "monotonic",
                          return_value=time.monotonic() + ap_mod.FILL_STALE_S + 1):
            self.ap.ensure_buffer()
        self.drain()
        self.assertEqual(self.ap.buffer_paths(), ["/r/1", "/r/2", "/r/3", "/r/4"])

    def test_a_dead_session_run_task_does_not_wedge_the_controller(self):
        def dead(*_a):
            raise RuntimeError("Page has been destroyed session.")
        self.ap.bind_ui(dead)
        self.ap.set_enabled(True)
        self.assertFalse(self.ap._filling)
        self.ap.bind_ui(lambda fn, *a: self.tasks.append((fn, a)))
        self.ap.ensure_buffer()
        self.drain()
        self.assertEqual(len(self.ap.buffer_paths()), ap_mod.BUFFER_TARGET)

    def test_a_new_session_supersedes_a_fill_from_the_old_one(self):
        self.ap.set_enabled(True)                            # queued on the old session
        stale = self.tasks.pop()
        self.ap.bind_ui(lambda fn, *a: self.tasks.append((fn, a)))
        self.ap.resume(self.engine.current_path)
        self.tasks.insert(0, stale)                          # the old one runs late
        self.drain()
        self.assertEqual(self.ap.buffer_paths(), ["/r/1", "/r/2", "/r/3", "/r/4"])

    def test_resume_tops_up_a_restored_queue_with_nothing_pending(self):
        self.engine.queue = [{"path": "/lib/a"}]
        self.ap.enabled = True
        self.ap.held = [{"path": "/lib/b"}]
        self.ap.resume("/lib/a")
        self.drain()
        self.assertEqual(self.ap.anchor, "/lib/a")
        self.assertEqual(self.ap.buffer_paths(), ["/r/1", "/r/2", "/r/3", "/r/4"])
        self.assertEqual(self.ap.held, [{"path": "/lib/b"}])   # restored, kept

    def test_resume_leaves_a_full_restored_buffer_alone(self):
        self.engine.queue = [{"path": "/lib/a"}] + [
            {"path": f"/s/{i}", "_autoplay": True} for i in range(4)]
        self.ap.enabled = True
        self.ap.resume("/lib/a")
        self.assertEqual(self.tasks, [])

    def test_resume_sets_aside_a_legacy_tail(self):
        self.ap.enabled = True
        self.ap.resume("/lib/a")
        self.drain()
        self.ahead()
        self.assertEqual([t["path"] for t in self.ap.held], ["/lib/b", "/lib/c"])

    def test_resume_does_nothing_while_disabled(self):
        self.ap.resume("/lib/a")
        self.assertEqual((self.ap.anchor, self.tasks), ("", []))

    def test_background_prefill_tops_up_to_background_target(self):
        self.enable()
        self.ap.prefill_for_background()
        _fn, args = self.tasks[0]
        self.assertEqual(args[0], ap_mod.BACKGROUND_TARGET - ap_mod.BUFFER_TARGET)

    # ── choosing tracks ─────────────────────────────────────────────────────
    def test_choosing_a_pending_recommendation_keeps_the_station(self):
        self.enable()
        self.engine.current_index = 2                        # tapped /r/2
        self.ap.on_user_chose("/r/2")
        self.drain()
        self.assertEqual(self.ap.anchor, "/lib/a")
        self.assertEqual(len(self.open_calls), 1)

    def test_choosing_a_library_track_starts_a_new_station_in_one_op(self):
        self.enable()
        # A library tap: the whole list is set as the queue, then chosen.
        self.engine.queue = [{"path": p} for p in ("/lib/a", "/lib/b", "/lib/c", "/lib/d")]
        self.engine.current_index = 2
        self.next_ranking = ["/x/1", "/x/2", "/x/3", "/x/4"]
        before = self.engine.buffer_ops
        self.ap.on_user_chose("/lib/c")
        self.drain()
        self.assertEqual(self.ap.anchor, "/lib/c")
        self.assertEqual(self.engine.buffer_ops, before + 1)
        self.assertEqual(self.ahead(), ["/x/1", "/x/2", "/x/3", "/x/4"])
        self.assertEqual([t["path"] for t in self.ap.held], ["/lib/d"])
        # The late native track-change event for the same tap changes nothing.
        self.ap.on_track_changed("/lib/c")
        self.drain()
        self.assertEqual(len(self.open_calls), 2)

    def test_new_station_avoids_history_and_pending(self):
        self.ap.on_track_changed("/lib/z")                   # heard earlier
        self.enable()
        avoid = self.open_calls[0][1]["avoid"]
        self.assertTrue({"/lib/z", "/lib/a"} <= avoid)

    # ── the genre runs out ──────────────────────────────────────────────────
    def test_a_played_out_station_starts_the_genre_over(self):
        self.next_ranking = RANKING[:8]
        self.enable()
        self.notify.reset_mock()
        for i in range(1, 8):
            self.advance(i)
        self.assertEqual(self.notify.call_count, 1)
        self.assertIn("Starting over", self.notify.call_args[0][0])
        self.assertIn("Rock", self.notify.call_args[0][0])
        ahead = self.ahead()
        self.assertTrue(ahead)                               # still playing on
        # The last few heard are kept out of the restart.
        self.assertFalse(set(ahead) & set(self.paths()[3:self.engine.current_index + 1]))

    def test_a_station_with_nothing_left_even_starting_over_ends_quietly(self):
        self.next_ranking = RANKING[:2]
        self.enable()
        self.notify.reset_mock()
        self.advance(1)
        self.advance(2)
        self.assertEqual(self.ahead(), [])
        self.assertEqual(self.notify.call_count, 0)
        self.ap.on_queue_dry()
        self.drain()
        self.assertTrue(self.engine.stopped)

    # ── negative feedback ───────────────────────────────────────────────────
    def test_each_negative_steers_and_two_replace_the_buffer_once(self):
        self.enable()
        self.engine.current_index = 1                        # /r/1 playing
        before = self.engine.replace_calls
        self.ap.on_listen("/r/1", listened_s=4, duration_s=200)   # early skip
        st = self.stations[0]
        self.assertEqual(st.rejected, [["/r/1"]])
        self.assertEqual(self.engine.replace_calls, before)
        self.engine.queue.pop(2)                             # listener removes /r/2
        self.ap.reject("/r/2")
        self.assertEqual(st.rejected, [["/r/1"], ["/r/2"]])
        self.drain()
        self.assertEqual(self.engine.replace_calls, before + 1)
        self.assertEqual(self.ap.buffer_paths(), ["/r/5", "/r/6", "/r/7", "/r/8"])

    def test_one_removal_steers_the_station_and_tops_up(self):
        self.enable()
        self.engine.queue.pop(2)
        self.ap.reject("/r/2")
        self.drain()
        self.assertEqual(self.stations[0].rejected, [["/r/2"]])
        self.assertEqual(self.ap.buffer_paths(), ["/r/1", "/r/3", "/r/4", "/r/5"])

    def test_a_removal_is_forgotten_by_the_next_station(self):
        self.enable()
        self.engine.queue.pop(2)
        self.ap.reject("/r/2")
        self.drain()
        self.engine.queue = [{"path": "/lib/c"}]
        self.engine.current_index = 0
        self.ap.on_user_chose("/lib/c")
        self.drain()
        self.assertNotIn("/r/2", self.open_calls[-1][1]["avoid"])

    def test_accepted_listen_pulls_the_station(self):
        self.enable()
        self.ap.on_listen("/r/1", listened_s=120, duration_s=200)
        self.assertEqual(self.stations[0].accepted, ["/r/1"])

    def test_stale_ledger_skips_do_not_count(self):
        self.enable()
        old = time.time() - 3600
        self.ap.on_listens([
            {"path": "/r/1", "listened_s": 3, "duration_s": 200, "ended_at": old},
            {"path": "/r/2", "listened_s": 3, "duration_s": 200, "ended_at": old},
        ])
        self.assertEqual(self.stations[0].rejected, [])

    def test_skipping_library_tracks_does_not_count(self):
        self.enable()
        self.ap.on_listen("/lib/b", listened_s=3, duration_s=200)
        self.ap.on_listen("/lib/c", listened_s=3, duration_s=200)
        self.assertEqual(self.stations[0].rejected, [])

    def test_station_shape_is_passed_through(self):
        self.enable()
        kw = self.open_calls[0][1]
        self.assertEqual((kw["max_per_artist"], kw["max_per_album"], kw["window"]),
                         (ap_mod.ARTIST_CAP, ap_mod.ALBUM_CAP, ap_mod.CAP_WINDOW))

    # ── dry queue / no station possible ─────────────────────────────────────
    def test_queue_dry_appends_and_plays_first_new(self):
        self.engine = FakeEngine(["/lib/a"])
        self.ap.engine = self.engine
        self.ap.enabled = True
        self.ap.on_queue_dry()
        self.drain()
        self.assertEqual(self.paths(), ["/lib/a", "/r/1", "/r/2", "/r/3", "/r/4"])
        self.assertEqual(self.engine.current_index, 1)

    def test_no_station_gives_the_queue_back_and_retries_from_the_next_song(self):
        self.db.has_current_features = AsyncMock(return_value=False)
        self.next_ranking = []
        self.enable()
        self.assertEqual(self.paths(), ["/lib/a", "/lib/b", "/lib/c"])   # given back
        self.notify.assert_called_once()
        self.assertIn("isn't analysed", self.notify.call_args[0][0])
        # The late native event for the same song doesn't retry it.
        self.ap.on_track_changed("/lib/a")
        self.drain()
        self.assertEqual(len(self.open_calls), 1)
        # The next song that plays gets a fresh start.
        self.next_ranking = RANKING
        self.advance(1)
        self.assertEqual(self.ap.anchor, "/lib/b")
        self.assertEqual(self.ahead(), ["/r/1", "/r/2", "/r/3", "/r/4"])
        self.assertEqual([t["path"] for t in self.ap.held], ["/lib/c"])
        self.assertEqual(self.notify.call_count, 1)


if __name__ == "__main__":
    unittest.main()
