"""Python queue ↔ native playlist agreement.

The epoch handshake orders ops; these tests cover what it never checked —
that both sides hold the SAME list and that the mirrored row is the track
actually playing — plus the repair path and its rate limit.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, AsyncMock

_STUBS = ("flet", "flet_audio_service")
_saved: dict = {}
AudioEngine = None
engine_mod = None


def setUpModule():
    global AudioEngine, engine_mod
    for name in _STUBS:
        if name in sys.modules:
            _saved[name] = sys.modules[name]
        sys.modules[name] = MagicMock()
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    sys.modules.pop("utils.audio_engine", None)
    import utils.audio_engine as m
    engine_mod = m
    AudioEngine = m.AudioEngine


def tearDownModule():
    for name in _STUBS:
        if name in _saved:
            sys.modules[name] = _saved[name]
        else:
            sys.modules.pop(name, None)
    sys.modules.pop("utils.audio_engine", None)


def _tracks(n, prefix="/m/t"):
    return [{"path": f"{prefix}{i}.flac", "track_title": f"T{i}"} for i in range(n)]


class DivergenceTests(unittest.TestCase):
    def setUp(self):
        self.e = AudioEngine()
        self.e._page = MagicMock()
        self.e._audio = MagicMock()
        self.e.queue = _tracks(5)
        self.e.current_index = 1
        self.e._queue_epoch = 7

    def src(self, i):
        return self.e._to_uri(self.e.queue[i]["path"])

    # ── identity-checked mirroring ──────────────────────────────────────────
    def test_mirror_adopts_index_when_src_matches(self):
        self.e._on_state_change(MagicMock(data=(
            '{"status":"playing","processing_state":"ready","queue_index":3,'
            f'"epoch":7,"current_src":"{self.src(3)}"}}')))
        self.assertEqual(self.e.current_index, 3)
        self.assertEqual(self.e.current_path, self.e.queue[3]["path"])

    def test_mirror_refuses_row_that_is_not_the_playing_track(self):
        self.e._on_state_change(MagicMock(data=(
            '{"status":"playing","processing_state":"ready","queue_index":3,'
            f'"epoch":7,"current_src":"{self.src(4)}"}}')))
        self.assertEqual(self.e.current_index, 1, "must not mirror a mismatched row")
        self.assertTrue(self.e._verify_pending)

    def test_old_native_build_without_src_still_mirrors(self):
        self.e._on_state_change(MagicMock(data=(
            '{"status":"playing","processing_state":"ready","queue_index":2,"epoch":7}')))
        self.assertEqual(self.e.current_index, 2)

    def test_stale_epoch_is_ignored(self):
        self.e._on_state_change(MagicMock(data=(
            '{"status":"playing","processing_state":"ready","queue_index":3,'
            f'"epoch":6,"current_src":"{self.src(3)}"}}')))
        self.assertEqual(self.e.current_index, 1)

    def test_transient_mismatch_resolved_by_newer_event_does_not_resync(self):
        self.e._resync_native = MagicMock()
        self.e._mirror_native(3, self.src(4), 7)          # momentary lag
        self.e._mirror_native(4, self.src(4), 7)          # corrective event
        engine_mod._VERIFY_DELAY_S, saved = 0.0, engine_mod._VERIFY_DELAY_S
        try:
            asyncio.run(self.e._verify_native())
        finally:
            engine_mod._VERIFY_DELAY_S = saved
        self.e._resync_native.assert_not_called()
        self.assertEqual(self.e.current_index, 4)

    def test_persistent_mismatch_resyncs_onto_the_audible_track(self):
        self.e._schedule_push = MagicMock()
        self.e.position = 42.0
        self.e.is_playing = True
        self.e._mirror_native(1, self.src(3), 7)          # Dart plays row 3
        engine_mod._VERIFY_DELAY_S, saved = 0.0, engine_mod._VERIFY_DELAY_S
        try:
            asyncio.run(self.e._verify_native())
        finally:
            engine_mod._VERIFY_DELAY_S = saved
        self.assertEqual(self.e.current_index, 3)
        self.assertEqual(self.e._restore_position, 42.0)
        self.e._schedule_push.assert_called_once_with(3, autoplay=True, keep_listen=True)

    # ── native art ──────────────────────────────────────────────────────────
    def test_native_art_applies_to_the_shown_row_only(self):
        self.e._on_state_change(MagicMock(data=(
            '{"status":"playing","processing_state":"ready","queue_index":1,'
            f'"epoch":7,"current_src":"{self.src(1)}","current_art":"/cache/np_art/a.jpg"}}')))
        self.assertEqual(self.e.current_art, "/cache/np_art/a.jpg")
        # Art for a row Python isn't showing (stale/mismatched) is ignored.
        self.e._on_state_change(MagicMock(data=(
            '{"status":"playing","processing_state":"ready","queue_index":1,'
            f'"epoch":7,"current_src":"{self.src(4)}","current_art":"/cache/np_art/b.jpg"}}')))
        self.assertEqual(self.e.current_art, "/cache/np_art/a.jpg")

    # ── length check on acks ────────────────────────────────────────────────
    def test_ack_length_mismatch_triggers_repair(self):
        self.e._resync_native = MagicMock()
        self.e._apply_ack({"ok": True, "epoch": 7, "playlist_len": 6,
                           "current_index": 1, "current_src": self.src(1)})
        self.e._resync_native.assert_called_once()

    def test_ack_length_ignored_for_older_generation(self):
        self.e._resync_native = MagicMock()
        self.e._apply_ack({"ok": True, "epoch": 6, "playlist_len": 6})
        self.e._resync_native.assert_not_called()

    # ── repair rate limit ───────────────────────────────────────────────────
    def test_resync_is_rate_limited(self):
        self.e._schedule_push = MagicMock()
        self.e._resync_native("a")
        self.e._resync_native("b")
        self.e._resync_native("c")
        self.assertEqual(self.e._schedule_push.call_count, 1)
        # The blocked repair is deferred (once), not dropped.
        deferred = [c for c in self.e._page.run_task.call_args_list
                    if c.args[0] == self.e._deferred_resync]
        self.assertEqual(len(deferred), 1)

    def test_deferred_resync_runs_unless_a_push_already_resynced(self):
        self.e._schedule_push = MagicMock()
        floor = self.e._push_floor
        asyncio.run(self.e._deferred_resync(0.0, floor, "x"))
        self.e._schedule_push.assert_called_once()
        self.e._schedule_push.reset_mock()
        self.e._last_resync_at = float("-inf")
        asyncio.run(self.e._deferred_resync(0.0, floor - 1, "x"))
        self.e._schedule_push.assert_not_called()

    def test_dart_epoch_ahead_is_adopted(self):
        self.e._mirror_native(1, self.src(1), 12)
        self.assertEqual(self.e._queue_epoch, 12)

    # ── op failure policy ───────────────────────────────────────────────────
    def test_mutation_timeout_does_not_repush(self):
        self.e._resync_native = MagicMock()
        self.e._dispatch_op = AsyncMock(return_value={"ok": False, "timeout": True})
        asyncio.run(self.e._run_native_mutation(AsyncMock(), 7))
        self.e._resync_native.assert_not_called()

    def test_rejected_mutation_repairs(self):
        self.e._resync_native = MagicMock()
        self.e._dispatch_op = AsyncMock(return_value={"ok": False, "epoch": 7})
        asyncio.run(self.e._run_native_mutation(AsyncMock(), 7))
        self.e._resync_native.assert_called_once()

    def test_undelivered_skip_repairs(self):
        self.e._resync_native = MagicMock()
        self.e._dispatch_op = AsyncMock(return_value={"ok": False, "undelivered": True})
        asyncio.run(self.e._do_skip(2, True, 8))
        self.e._resync_native.assert_called_once()

    # ── entry filtering / index bookkeeping ─────────────────────────────────
    def test_pathless_tracks_never_enter_the_queue(self):
        self.e._schedule_push = MagicMock()
        tracks = _tracks(4)
        tracks.insert(1, {"track_title": "no path"})
        self.e.set_queue(tracks, start_index=3)            # → original T2
        self.assertEqual(len(self.e.queue), 4)
        self.assertEqual(self.e.queue[self.e.current_index]["track_title"], "T2")

    def test_move_tracks_playing_row_despite_duplicate(self):
        dup = dict(self.e.queue[3])
        self.e.queue[0] = dup                              # equal dict earlier
        self.e.current_index = 3
        self.e.move_queue_item(4, 1)
        self.assertEqual(self.e.current_index, 4)

    def test_set_queue_syncs_metadata_before_observers_run(self):
        self.e._schedule_push = MagicMock()
        seen = []
        self.e.bind(on_queue_mutated=lambda inst, _v: seen.append(inst.current_path))
        new = _tracks(3, prefix="/n/x")
        self.e.set_queue(new, start_index=2)
        self.assertEqual(seen, ["/n/x2.flac"])


class FakeDart:
    """The native side's queue contract, as flet_audio_service.dart implements
    it: ops run strictly one after another, each takes a platform round trip,
    epochs only move forward, and a splice outside the playlist fails."""

    def __init__(self, srcs, cur=0, latency=0.002):
        self.pl = list(srcs)
        self.cur = cur
        self.epoch = 0
        self.latency = latency
        self.ops: list[str] = []
        self._chain = asyncio.Lock()
        self._futs: dict = {}

    def register_op(self, rid):
        self._futs[rid] = asyncio.get_running_loop().create_future()

    async def wait_for_op(self, rid, timeout=8.0):
        return await asyncio.wait_for(self._futs[rid], timeout)

    def _enqueue(self, rid, epoch, name, fn):
        async def run():
            async with self._chain:
                await asyncio.sleep(self.latency)
                ok = True
                try:
                    fn()
                except IndexError:
                    ok = False
                if epoch is not None and epoch > self.epoch:
                    self.epoch = epoch
                self.ops.append(name)
                ack = {"ok": ok, "epoch": self.epoch, "playlist_len": len(self.pl)}
                if self.pl:
                    ack["current_index"] = self.cur
                    ack["current_src"] = self.pl[self.cur]
                self._futs.pop(rid).set_result(ack)
        asyncio.get_running_loop().create_task(run())

    async def set_playlist(self, items, start_index, autoplay, shuffle, epoch,
                           request_id, keep_listen=False):
        def f():
            self.pl = [it["src"] for it in items]
            self.cur = start_index
        self._enqueue(request_id, epoch, "set_playlist", f)

    async def splice(self, edits, epoch, request_id):
        def f():
            for e in edits:
                start, count = e["start"], e["delete_count"]
                if start < 0 or start + count > len(self.pl):
                    raise IndexError(start)
                del self.pl[start:start + count]
                if start + count <= self.cur:
                    self.cur -= count
                elif start <= self.cur:
                    self.cur = min(start, len(self.pl) - 1)  # active removed: advance
                srcs = [it["src"] for it in e["items"]]
                self.pl[start:start] = srcs
                if srcs and start <= self.cur:
                    self.cur += len(srcs)
        self._enqueue(request_id, epoch, "splice", f)

    async def move_queue_item(self, old, new, epoch, request_id):
        def f():
            item = self.pl.pop(old)
            self.pl.insert(new, item)
            cur = self.cur
            if old == cur:
                self.cur = new
            elif old < cur <= new:
                self.cur = cur - 1
            elif new <= cur < old:
                self.cur = cur + 1
        self._enqueue(request_id, epoch, "move", f)

    async def skip_to_index(self, index, autoplay, epoch, request_id):
        def f():
            if not 0 <= index < len(self.pl):
                raise IndexError(index)
            self.cur = index
        self._enqueue(request_id, epoch, "skip", f)

    async def stop(self):
        pass

    def __getattr__(self, name):
        # DSP / transport calls the engine makes on a track change.
        async def noop(*_a, **_k):
            return None
        return noop


class BridgeTests(unittest.TestCase):
    """Engine + FakeDart end to end: whatever the engine does, the two sides
    must end on the same list, the same playing row and the same epoch, with
    no repair needed."""

    def _run(self, scenario, queue, cur=0):
        async def main():
            loop = asyncio.get_running_loop()
            e = AudioEngine()
            e._page = MagicMock()
            e._page.run_task = lambda fn, *a: loop.create_task(fn(*a))
            e.queue = list(queue)
            e.current_index = cur
            dart = FakeDart([e._to_uri(t["path"]) for t in queue], cur)
            e._audio = dart
            e._resync_native = MagicMock(wraps=e._resync_native)
            await scenario(e, dart)
            # Drain: wait until nothing is queued on either side.
            idle = 0
            for _ in range(400):
                await asyncio.sleep(0.005)
                busy = dart._chain.locked() or (e._op_lock and e._op_lock.locked())
                idle = 0 if busy else idle + 1
                if idle >= 3:
                    break
            return e, dart
        return asyncio.run(main())

    def assertAgree(self, e, dart):
        self.assertEqual([e._to_uri(t["path"]) for t in e.queue], dart.pl)
        self.assertEqual(e.current_index, dart.cur)
        self.assertEqual(e._queue_epoch, dart.epoch)
        e._resync_native.assert_not_called()

    def test_autoplay_replan_drop_then_refill(self):
        # The 2026-09-28 regression: dropping a 4-track buffer, with the refill
        # landing while the drop was still being replayed on Dart.
        lib = _tracks(6, "/lib/")
        buf = [dict(t, _autoplay=True) for t in _tracks(4, "/old/")]
        async def scenario(e, dart):
            e.remove_indices([i for i, t in enumerate(e.queue) if t.get("_autoplay")])
            await asyncio.sleep(0.003)
            e.queue_after_current([dict(t, _autoplay=True) for t in _tracks(4, "/new/")])
        e, dart = self._run(scenario, [lib[0]] + buf + lib[1:])
        self.assertAgree(e, dart)
        self.assertEqual(dart.ops, ["splice", "splice"])

    def test_scattered_removal_is_one_native_op(self):
        async def scenario(e, dart):
            e.remove_indices([2, 3, 5, 8, 9])
        e, dart = self._run(scenario, _tracks(10), cur=1)
        self.assertAgree(e, dart)
        self.assertEqual(dart.ops, ["splice"])

    def test_ops_queued_behind_a_push_are_superseded(self):
        async def scenario(e, dart):
            dart.latency = 0.02                     # Dart is slow: ops queue up
            e.queue_last(_tracks(1, "/a/")[0])
            await asyncio.sleep(0.005)              # ... now in flight
            e.queue_next(_tracks(1, "/b/")[0])      # queued ...
            e.play_track_at(3)                      # ... queued
            e.set_queue(_tracks(5, "/s/"), 2)       # push: supersedes both
            e.queue_last(_tracks(1, "/c/")[0])      # after the push: applied on top
        e, dart = self._run(scenario, _tracks(4))
        self.assertAgree(e, dart)
        self.assertEqual(dart.ops, ["splice", "set_playlist", "splice"])

    def test_random_mutation_storm_keeps_both_sides_identical(self):
        import random
        for seed in range(25):
            rng = random.Random(seed)
            fresh = iter(range(10_000))
            def track():
                return {"path": f"/r/{next(fresh)}.flac"}
            async def scenario(e, dart):
                for _ in range(40):
                    n, cur = len(e.queue), e.current_index
                    op = rng.randrange(7)
                    if op == 0:
                        e.queue_next(track())
                    elif op == 1:
                        e.queue_last(track())
                    elif op == 2:
                        e.queue_after_current([track() for _ in range(rng.randint(1, 5))])
                    elif op == 3 and n > 2:
                        e.remove_indices(rng.sample(range(n), rng.randint(1, min(5, n - 1))))
                    elif op == 4 and n > 2:
                        e.move_queue_item(rng.randrange(n), rng.randrange(n))
                    elif op == 5 and n:
                        e.play_track_at(rng.randrange(n))
                    elif op == 6 and n > 2 and rng.random() < 0.3:
                        idx = rng.randrange(n)
                        if idx != cur:
                            e.remove_from_queue(idx)
                    if rng.random() < 0.4:
                        await asyncio.sleep(rng.choice([0, 0.001, 0.004]))
            e, dart = self._run(scenario, [track() for _ in range(8)], cur=2)
            with self.subTest(seed=seed):
                self.assertAgree(e, dart)


if __name__ == "__main__":
    unittest.main()
