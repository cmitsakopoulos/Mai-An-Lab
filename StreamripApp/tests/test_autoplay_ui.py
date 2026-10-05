"""Auto-play UI: settings sheet, Now Playing pill, queue-sheet section header,
Start Radio."""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft  # noqa: E402

import main  # noqa: E402
from ui.player.autoplay_sheet import AutoPlaySheet, MODE_COPY, anchor_label  # noqa: E402
from ui.player.queue_sheet import QueueSheet  # noqa: E402
from utils.autoplay import AutoPlay, DETERMINISTIC, RANDOM  # noqa: E402

audio_engine = main.audio_engine


def _texts(ctrl):
    """Every Text value under a control (walks content/controls)."""
    out = []
    stack = [ctrl]
    while stack:
        c = stack.pop()
        if isinstance(c, ft.Text):
            out.append(c.value)
        for attr in ("content", "controls"):
            v = getattr(c, attr, None)
            if isinstance(v, list):
                stack.extend(v)
            elif isinstance(v, ft.Control):
                stack.append(v)
    return out


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = (audio_engine.queue, audio_engine.current_index)
        audio_engine.queue = [
            {"path": "/m/a", "track_title": "Alpha", "artist_name": "X"},
            {"path": "/m/b", "track_title": "Beta", "artist_name": "Y"},
            {"path": "/r/1", "track_title": "Rec 1", "artist_name": "Z", "_autoplay": True},
            {"path": "/r/2", "track_title": "Rec 2", "artist_name": "Z", "_autoplay": True},
            {"path": "/m/c", "track_title": "Gamma", "artist_name": "W"},
        ]
        audio_engine.current_index = 0
        self.app = MagicMock()
        self.app.page = MagicMock()
        self.app.play_similar_mode = True
        self.app.safe_update = lambda fn, target=None: fn()
        self.app.autoplay = AutoPlay(audio_engine, MagicMock(), run_task=MagicMock())
        self.app.autoplay.enabled = True
        self.app.autoplay.anchor = "/m/a"

    def tearDown(self):
        audio_engine.queue, audio_engine.current_index = self._saved


class AutoPlaySheetTests(_Base):
    def test_sheet_shows_mode_copy_and_anchor(self):
        sheet = AutoPlaySheet(self.app)
        bs = sheet._build()
        self.assertIsInstance(bs, ft.BottomSheet)
        self.assertTrue(bs.draggable)   # swipe-down to dismiss
        texts = _texts(bs)
        self.assertIn(MODE_COPY[DETERMINISTIC], texts)
        self.assertIn("Alpha — X", texts)
        self.assertIn("2 songs lined up", texts)
        self.assertEqual(anchor_label(self.app), "Alpha — X")

    def test_toggle_and_mode_go_through_the_app_hooks(self):
        sheet = AutoPlaySheet(self.app)
        sheet._build()
        sheet._on_toggle(MagicMock(control=MagicMock(value=False)))
        self.app.set_play_similar_mode.assert_called_once_with(False)
        sheet._on_mode(RANDOM)
        self.app.set_autoplay_variety.assert_called_once_with(RANDOM)
        self.assertEqual(sheet._mode_copy.value, MODE_COPY[RANDOM])

    def test_anchor_hidden_while_off(self):
        self.app.play_similar_mode = False
        sheet = AutoPlaySheet(self.app)
        sheet._build()
        self.assertFalse(sheet._anchor_section.visible)


class QueueHeaderTests(_Base):
    """Queue (setUp): Alpha playing, Beta, Rec 1, Rec 2, then Gamma, which is
    held: auto-play paused it. Controls: Alpha 0, Beta 1, AUTO-PLAY 2,
    Rec 1 3, Rec 2 4, YOUR QUEUE 5, Gamma 6."""

    def _sheet(self):
        qs = QueueSheet(self.app)
        qs._ensure_initialized()
        qs.refresh()
        return qs

    def test_header_sits_above_first_recommendation(self):
        qs = self._sheet()
        ctrls = qs._queue_list.controls
        self.assertEqual(qs._header_positions, [2, 5])
        self.assertEqual(ctrls[2].key, "q_autoplay_header")
        self.assertIn("from Alpha — X", _texts(ctrls[2]))

    def test_held_queue_is_labelled_dimmed_and_inert(self):
        qs = self._sheet()
        ctrls = qs._queue_list.controls
        self.assertEqual(ctrls[5].key, "q_held_header")
        self.assertIn("1 on hold · resumes when auto-play is off", _texts(ctrls[5]))
        self.assertIsNone(getattr(ctrls[5], "on_click", None))   # not a way back
        gamma = ctrls[6].content                     # AnimatedEntry -> card
        while not isinstance(gamma, ft.Container) or gamma.bgcolor is None:
            gamma = gamma.content
        self.assertTrue(gamma.disabled)
        self.assertLess(gamma.opacity, 1.0)
        actions = gamma.content.controls[-1].controls      # drag handle, remove
        self.assertFalse(any(c.visible for c in actions))
        self.assertEqual(qs._count_text.value, "3 up next · 1 on hold")

    def test_no_sections_when_autoplay_off(self):
        self.app.play_similar_mode = False
        qs = self._sheet()
        self.assertEqual(qs._header_positions, [])
        keys = {getattr(c, "key", None) for c in qs._queue_list.controls}
        self.assertFalse({"q_autoplay_header", "q_held_header"} & keys)
        self.assertEqual(qs._count_text.value, "4 tracks remaining")

    def test_pending_station_header_sits_between_live_and_held(self):
        audio_engine.queue = [t for t in audio_engine.queue if not t.get("_autoplay")]
        qs = self._sheet()
        keys = [getattr(c, "key", None) for c in qs._queue_list.controls]
        # Alpha, AUTO-PLAY (finding…), YOUR QUEUE, Beta, Gamma: with nothing
        # pending, everything after the playing row is held.
        self.assertEqual(keys[1:3], ["q_autoplay_header", "q_held_header"])

    def test_reorder_skips_headers_and_never_crosses_into_the_held_queue(self):
        qs = self._sheet()
        moved = MagicMock()
        orig = audio_engine.move_queue_item
        audio_engine.move_queue_item = moved
        try:
            # Drag "Rec 2" (control 4 = queue row 3) to just below "Alpha"
            # (insertion slot 1): the header at control 2 must not shift it.
            qs._handle_queue_reorder(MagicMock(old_index=4, new_index=1,
                                               control=MagicMock(controls=list(qs._queue_list.controls))))
            moved.assert_called_once_with(3, 1)
            moved.reset_mock()
            # Dragging a header is ignored.
            qs._handle_queue_reorder(MagicMock(old_index=2, new_index=4, control=MagicMock(controls=[])))
            # A held row can't move, and nothing can be dropped below the
            # held header.
            qs._handle_queue_reorder(MagicMock(old_index=6, new_index=1, control=MagicMock(controls=[])))
            qs._handle_queue_reorder(MagicMock(old_index=1, new_index=7, control=MagicMock(controls=[])))
            moved.assert_not_called()
        finally:
            audio_engine.move_queue_item = orig

    def test_row_actions_resolve_the_drawn_row_after_the_queue_shifts(self):
        qs = self._sheet()
        rec2 = audio_engine.queue[3]
        # A refill lands between the render and the tap: every row shifts by 2.
        audio_engine.queue[1:1] = [{"path": "/r/9"}, {"path": "/r/8"}]
        removed, moved = MagicMock(), MagicMock()
        saved = (audio_engine.remove_from_queue, audio_engine.move_queue_item)
        audio_engine.remove_from_queue, audio_engine.move_queue_item = removed, moved
        try:
            # Drawn "Rec 2" (control 4) dragged to below "Alpha" (slot 1):
            # resolves to Rec 2 and Beta's CURRENT rows, not the drawn ones.
            qs._handle_queue_reorder(MagicMock(old_index=4, new_index=1,
                                               control=MagicMock(controls=list(qs._queue_list.controls))))
            moved.assert_called_once_with(5, 3)
            qs._remove(rec2)
            removed.assert_called_once_with(5)
            removed.reset_mock()
            qs._remove({"path": "/r/2"})               # equal, but not a queued row
            removed.assert_not_called()
        finally:
            audio_engine.remove_from_queue, audio_engine.move_queue_item = saved


class NowPlayingChainTests(_Base):
    def test_tap_opens_sheet_and_long_press_toggles(self):
        from ui.player import now_playing as np_mod
        np = np_mod.NowPlayingSheet(self.app)
        np._ensure_initialized()
        opened = MagicMock()
        np.open_autoplay_sheet = opened
        np._play_similar_btn.on_click(MagicMock())
        opened.assert_called_once()
        self.assertEqual(np._play_similar_btn.on_long_press, np._toggle_play_similar)
        # No status pill any more: nothing extra in the vertical layout.
        self.assertFalse(hasattr(np, "_autoplay_pill"))


class AutoPlaySettingsPaneTests(unittest.TestCase):
    def test_lists_hidden_songs_and_blocked_hops_and_resets_a_hop(self):
        from ui.views.autoplay_hidden import HiddenTracksPane
        app = MagicMock()
        app.safe_update = lambda fn, target=None: fn()
        app.db_manager.get_autoplay_hidden = AsyncMock(return_value=[
            {"path": "/m/x.flac", "title": "X", "artist": "Y", "album": "Z", "hidden_at": 0}])
        blocked = [{"src": "Classical", "dst": "Pop", "rejects": 3, "artists": 2}]
        app.autoplay.blocked_hops = MagicMock(return_value=blocked)
        app.autoplay.unblock_hops = AsyncMock()
        pane = HiddenTracksPane(app)
        pane.build()
        asyncio.run(pane._load())
        texts = _texts(pane._hops)
        self.assertIn("Classical → Pop", texts)
        self.assertIn("Blocked after you turned down 3 songs by 2 artists", texts)
        self.assertIn("X", _texts(pane._list))
        asyncio.run(pane._unblock([("Classical", "Pop")]))
        app.autoplay.unblock_hops.assert_awaited_once_with([("Classical", "Pop")])


class StartRadioTests(unittest.TestCase):
    def test_start_radio_plays_track_and_enables_autoplay(self):
        app = MagicMock(spec=main.StreamripFletApp)
        app.play_similar_mode = False
        app.autoplay = MagicMock()
        app.db_manager = MagicMock(get_tracks_brief=AsyncMock(return_value={}))
        saved = (audio_engine.queue, audio_engine.current_index, audio_engine.play_track_at)
        audio_engine.queue = [{"path": "/m/a"}, {"path": "/m/b"}]
        audio_engine.current_index = 0
        audio_engine.play_track_at = MagicMock()
        try:
            asyncio.run(main.StreamripFletApp.start_radio(app, "/m/b"))
            audio_engine.play_track_at.assert_called_once_with(1)
            app.set_play_similar_mode.assert_called_once_with(True)
            app.play_similar_mode = True
            asyncio.run(main.StreamripFletApp.start_radio(app, "/m/b"))
            app.autoplay.start.assert_called_once_with("/m/b")
        finally:
            audio_engine.queue, audio_engine.current_index, audio_engine.play_track_at = saved


if __name__ == "__main__":
    unittest.main()
