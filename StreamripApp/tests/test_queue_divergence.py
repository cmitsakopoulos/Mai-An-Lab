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
        self.e._schedule_push.assert_called_once_with(3, autoplay=True)

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
        self.assertEqual(self.e._schedule_push.call_count, 1)

    # ── op failure policy ───────────────────────────────────────────────────
    def test_mutation_timeout_does_not_repush(self):
        self.e._resync_native = MagicMock()
        self.e._dispatch_op = AsyncMock(return_value={"ok": False, "timeout": True})
        asyncio.run(self.e._run_native_mutation(AsyncMock()))
        self.e._resync_native.assert_not_called()

    def test_rejected_mutation_repairs(self):
        self.e._resync_native = MagicMock()
        self.e._dispatch_op = AsyncMock(return_value={"ok": False, "epoch": 7})
        asyncio.run(self.e._run_native_mutation(AsyncMock()))
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


if __name__ == "__main__":
    unittest.main()
