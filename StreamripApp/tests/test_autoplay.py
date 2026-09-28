"""AutoPlay controller on a resumable station: buffer upkeep, choosing tracks,
negative feedback (skips + removals), hidden tracks, Deterministic / Random."""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import autoplay as ap_mod  # noqa: E402
from utils.autoplay import AutoPlay, DETERMINISTIC, RANDOM  # noqa: E402


class FakeEngine:
    """Just the queue surface AutoPlay touches."""

    def __init__(self, paths, current=0):
        self.queue = [{"path": p, "artist_name": "Lib"} for p in paths]
        self.current_index = current
        self.continues_at_end = False
        self.stopped = False
        self.replace_calls = 0

    @property
    def current_path(self):
        return self.queue[self.current_index]["path"] if self.queue else ""

    def queue_after_current(self, tracks, after_index=None):
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
    """A resumable station over a fixed ranking: never emits a path twice;
    end_leg() switches to `next_leg` (the hop)."""

    def __init__(self, ranking, next_leg=()):
        self._ranking = list(ranking)
        self._next_leg = list(next_leg)
        self.used: set = set()
        self.node = "Rock"
        self.end_legs: list = []
        self.accepted: list = []
        self.banned: list = []
        self.hops: dict = {}             # path -> (from, to) genre hop it came in on
        self.hop_weights: dict = {}

    async def take(self, n):
        out = [p for p in self._ranking if p not in self.used][:n]
        self.used.update(out)
        return out

    def end_leg(self, reject_paths=()):
        self.end_legs.append(list(reject_paths))
        self.used.update(reject_paths)
        self._ranking = self._next_leg
        self.node = "Alt"

    def exclude(self, paths):
        self.used.update(paths)

    def accept(self, path):
        self.accepted.append(path)

    def ban_artist_of(self, path):
        self.banned.append(path)

    def hop_of(self, path):
        return self.hops.get(path)

    def weight_hop(self, src, dst, weight):
        self.hop_weights[(src, dst)] = weight


class FakeHopStore:
    """The autoplay_hop_rejects table: one row per (hop, track)."""

    def __init__(self):
        self.rows: dict = {}

    async def record(self, src, dst, path, artist):
        self.rows[(src, dst, path)] = artist

    async def summary(self):
        out: dict = {}
        for (src, dst, _path), artist in self.rows.items():
            rec = out.setdefault((src, dst), {"src": src, "dst": dst, "rejects": 0, "names": set()})
            rec["rejects"] += 1
            rec["names"].add(artist)
        return [{"src": r["src"], "dst": r["dst"], "rejects": r["rejects"],
                 "artists": len(r["names"])} for r in out.values()]

    async def clear(self, pairs=None):
        self.rows = {k: v for k, v in self.rows.items()
                     if pairs is not None and (k[0], k[1]) not in {tuple(p) for p in pairs}}


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
        self.db.hide_from_autoplay = AsyncMock()
        self.db.unhide_from_autoplay = AsyncMock()
        self.db.get_autoplay_hidden = AsyncMock(return_value=[])
        self.hop_store = FakeHopStore()
        self.db.record_hop_reject = self.hop_store.record
        self.db.get_hop_rejects = self.hop_store.summary
        self.db.clear_hop_rejects = self.hop_store.clear
        self.tasks = []
        self.notify = MagicMock()
        self.ap = AutoPlay(self.engine, self.db,
                           run_task=lambda fn, *a: self.tasks.append((fn, a)),
                           notify=self.notify)
        self.stations = []
        self.next_ranking = RANKING
        self.next_leg = [f"/h/{i}" for i in range(1, 30)]
        self.open_calls = []

    async def _open(self, db, seed, **kw):
        self.open_calls.append((seed, kw))
        st = FakeStation(self.next_ranking, self.next_leg)
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

    # ── buffer upkeep ───────────────────────────────────────────────────────
    def test_enable_inserts_tagged_buffer_after_current_keeping_tail(self):
        self.enable()
        self.assertEqual(self.paths(), ["/lib/a", "/r/1", "/r/2", "/r/3", "/r/4",
                                        "/lib/b", "/lib/c"])
        self.assertEqual(self.ap.buffer_paths(), ["/r/1", "/r/2", "/r/3", "/r/4"])
        self.assertTrue(all(t["_autoplay"] for t in self.engine.queue[1:5]))
        self.assertTrue(self.engine.continues_at_end)
        self.assertEqual(self.open_calls[0][0], "/lib/a")

    def test_disable_drops_only_the_pending_buffer(self):
        self.enable()
        self.ap.set_enabled(False)
        self.assertEqual(self.paths(), ["/lib/a", "/lib/b", "/lib/c"])
        self.assertFalse(self.engine.continues_at_end)
        self.assertIsNone(self.ap.station)

    def test_toggle_off_mid_fill_discards_the_stale_block(self):
        self.ap.set_enabled(True)
        self.ap.set_enabled(False)
        self.drain()
        self.assertEqual(self.paths(), ["/lib/a", "/lib/b", "/lib/c"])

    def test_refills_continue_the_same_station(self):
        self.enable()
        self.engine.current_index = 3                       # two recs played
        self.ap.ensure_buffer()
        self.drain()
        self.assertEqual(len(self.open_calls), 1)            # resumed, not reopened
        self.assertEqual(self.paths()[4:7], ["/r/4", "/r/5", "/r/6"])

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

    def test_choosing_anything_else_starts_a_new_station_in_one_op(self):
        self.enable()
        self.engine.current_index = self.paths().index("/lib/c")
        self.next_ranking = ["/x/1", "/x/2", "/x/3", "/x/4"]
        before = self.engine.replace_calls
        self.ap.on_user_chose("/lib/c")
        self.drain()
        self.assertEqual(self.ap.anchor, "/lib/c")
        self.assertEqual(self.open_calls[-1][0], "/lib/c")
        self.assertEqual(self.engine.replace_calls, before + 1)
        i = self.paths().index("/lib/c")
        self.assertEqual(self.paths()[i + 1:], ["/x/1", "/x/2", "/x/3", "/x/4"])

    def test_new_station_avoids_history_hidden_and_queued(self):
        self.ap.on_track_changed("/lib/z")                   # heard earlier
        self.ap.hidden.add("/lib/y")                         # removed before
        self.enable()
        avoid = self.open_calls[0][1]["avoid"]
        self.assertTrue({"/lib/z", "/lib/y", "/lib/a"} <= avoid)

    # ── negative feedback ───────────────────────────────────────────────────
    def test_two_negatives_end_the_leg_and_replace_the_buffer_once(self):
        self.enable()
        self.engine.current_index = 1                        # /r/1 playing
        before = self.engine.replace_calls
        self.ap.on_listen("/r/1", listened_s=4, duration_s=200)   # early skip
        self.assertEqual(self.stations[0].end_legs, [])
        self.engine.queue.pop(2)                             # listener removes /r/2
        self.ap.reject("/r/2")
        st = self.stations[0]
        self.assertEqual(st.end_legs, [["/r/1", "/r/2"]])
        self.drain()
        self.assertEqual(self.engine.replace_calls, before + 1)
        self.assertEqual(self.ap.buffer_paths(), ["/h/1", "/h/2", "/h/3", "/h/4"])
        # nothing from the rejected leg comes back
        self.assertFalse({"/r/3", "/r/4"} & set(self.ap.buffer_paths()))

    def test_one_removal_hides_it_and_tops_up(self):
        self.enable()
        self.engine.queue.pop(2)
        self.ap.reject("/r/2")
        self.assertIn("/r/2", self.ap.hidden)
        self.drain()
        self.db.hide_from_autoplay.assert_awaited_once_with("/r/2")
        self.assertEqual(self.stations[0].end_legs, [])
        self.assertEqual(self.ap.buffer_paths(), ["/r/1", "/r/3", "/r/4", "/r/5"])

    def test_same_act_rejected_twice_is_banned(self):
        self.db.get_tracks_brief = AsyncMock(side_effect=lambda ps: {
            p: {"path": p, "title": p, "artist": "Same", "album": p} for p in ps})
        self.enable()
        self.engine.queue.pop(1)
        self.ap.reject("/r/1")
        self.engine.queue.pop(1)
        self.ap.reject("/r/2")
        self.assertEqual(self.stations[0].banned, ["/r/2"])

    def test_accepted_listen_re_anchors_the_leg(self):
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
        self.assertEqual(self.stations[0].end_legs, [])

    def test_skipping_library_tracks_does_not_count(self):
        self.enable()
        self.ap.on_listen("/lib/b", listened_s=3, duration_s=200)
        self.ap.on_listen("/lib/c", listened_s=3, duration_s=200)
        self.assertEqual(self.stations[0].end_legs, [])

    # ── hidden tracks ───────────────────────────────────────────────────────
    def test_hidden_tracks_load_and_unhide(self):
        self.db.get_autoplay_hidden = AsyncMock(return_value=[{"path": "/h/x"}, {"path": "/h/y"}])
        asyncio.run(self.ap.load_feedback())
        self.assertEqual(self.ap.hidden, {"/h/x", "/h/y"})
        asyncio.run(self.ap.unhide(["/h/x"]))
        self.assertEqual(self.ap.hidden, {"/h/y"})
        asyncio.run(self.ap.unhide(None))
        self.assertEqual(self.ap.hidden, set())

    # ── learned genre hops ──────────────────────────────────────────────────
    def _reject_all(self, paths):
        for p in paths:
            self.engine.queue = [t for t in self.engine.queue if t["path"] != p]
            self.ap.reject(p)
            self.drain()

    def test_rejections_after_a_hop_weaken_then_block_it(self):
        self.enable()
        st = self.stations[0]
        st.hops = {p: ("Rock", "Pop") for p in ("/r/1", "/r/2", "/r/3")}
        self._reject_all(["/r/1"])
        self.assertEqual(st.hop_weights[("Rock", "Pop")], ap_mod.HOP_REJECT_WEIGHT)
        self._reject_all(["/r/2", "/r/3"])            # three songs, three artists
        self.assertEqual(st.hop_weights[("Rock", "Pop")], 0.0)
        self.assertEqual([(h["src"], h["dst"]) for h in self.ap.blocked_hops()], [("Rock", "Pop")])
        # The next station starts with the hop blocked.
        self.ap.start("/lib/b")
        self.drain()
        self.assertEqual(self.open_calls[-1][1]["hop_weights"], {("Rock", "Pop"): 0.0})

    def test_one_artist_cannot_block_a_hop(self):
        self.db.get_tracks_brief = AsyncMock(side_effect=lambda ps: {
            p: {"path": p, "title": p, "artist": "Same", "album": p} for p in ps})
        self.enable()
        st = self.stations[0]
        st.hops = {p: ("Rock", "Pop") for p in ("/r/1", "/r/2", "/r/3")}
        self._reject_all(["/r/1", "/r/2", "/r/3"])
        self.assertEqual(st.hop_weights[("Rock", "Pop")], ap_mod.HOP_REJECT_WEIGHT ** 3)
        self.assertEqual(self.ap.blocked_hops(), [])

    def test_rejection_away_from_a_hop_is_not_blamed_on_one(self):
        self.enable()
        self._reject_all(["/r/1"])
        self.assertEqual(self.hop_store.rows, {})

    def test_reset_unblocks_the_hop_everywhere(self):
        self.enable()
        st = self.stations[0]
        st.hops = {p: ("Rock", "Pop") for p in ("/r/1", "/r/2", "/r/3")}
        self._reject_all(["/r/1", "/r/2", "/r/3"])
        asyncio.run(self.ap.unblock_hops([("Rock", "Pop")]))
        self.assertEqual(st.hop_weights[("Rock", "Pop")], 1.0)
        self.assertEqual(self.ap.blocked_hops(), [])
        self.assertEqual(self.hop_store.rows, {})
        # and it stays reset after a reload
        asyncio.run(self.ap.load_feedback())
        self.assertEqual(self.ap.hop_weights(), {})

    # ── variety ─────────────────────────────────────────────────────────────
    def test_deterministic_opens_without_rng(self):
        self.enable()
        self.assertIsNone(self.open_calls[0][1]["rng"])
        self.assertIsNone(self.ap.station_seed)

    def test_random_opens_a_seeded_station_and_switching_rebuilds(self):
        self.enable()
        self.ap.set_variety(RANDOM)
        self.drain()
        self.assertEqual(len(self.open_calls), 2)
        self.assertIsNotNone(self.open_calls[1][1]["rng"])
        self.assertIsNotNone(self.ap.station_seed)
        self.assertEqual(self.engine.replace_calls, 2)       # enable + switch
        self.ap.set_variety(DETERMINISTIC)
        self.drain()
        self.assertIsNone(self.open_calls[2][1]["rng"])

    def test_station_shape_is_passed_through(self):
        self.enable()
        kw = self.open_calls[0][1]
        self.assertEqual((kw["leg_len"], kw["max_per_artist"], kw["max_per_album"], kw["window"]),
                         (ap_mod.LEG_LEN, ap_mod.ARTIST_CAP, ap_mod.ALBUM_CAP, ap_mod.CAP_WINDOW))

    # ── dry queue / unanalysed seeds ────────────────────────────────────────
    def test_queue_dry_appends_and_plays_first_new(self):
        self.engine = FakeEngine(["/lib/a"])
        self.ap.engine = self.engine
        self.ap.enabled = True
        self.ap.on_queue_dry()
        self.drain()
        self.assertEqual(self.paths(), ["/lib/a", "/r/1", "/r/2", "/r/3", "/r/4"])
        self.assertEqual(self.engine.current_index, 1)

    def test_unanalysed_anchor_is_explained_and_handed_over(self):
        self.db.has_current_features = AsyncMock(return_value=False)
        self.next_ranking = []
        self.enable()
        self.notify.assert_called_once()
        self.engine.current_index = 1
        self.next_ranking = RANKING
        self.ap.on_track_changed("/lib/b")
        self.assertEqual(self.ap.anchor, "/lib/b")
        self.drain()
        self.assertEqual(self.open_calls[-1][0], "/lib/b")


if __name__ == "__main__":
    unittest.main()
