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
    def _sheet(self):
        qs = QueueSheet(self.app)
        qs._ensure_initialized()
        qs.refresh()
        return qs

    def test_header_sits_above_first_recommendation(self):
        qs = self._sheet()
        ctrls = qs._queue_list.controls
        self.assertEqual(qs._header_at, 2)
        self.assertEqual(ctrls[2].key, "q_autoplay_header")
        self.assertIn("from Alpha — X", _texts(ctrls[2]))

    def test_no_header_when_autoplay_off(self):
        self.app.play_similar_mode = False
        qs = self._sheet()
        self.assertIsNone(qs._header_at)
        self.assertFalse(any(getattr(c, "key", None) == "q_autoplay_header"
                             for c in qs._queue_list.controls))

    def test_reorder_skips_the_header_row(self):
        qs = self._sheet()
        moved = MagicMock()
        orig = audio_engine.move_queue_item
        audio_engine.move_queue_item = moved
        try:
            # Drag "Gamma" (control 5 = queue row 4) to just below "Alpha"
            # (insertion slot 1): header at control 2 must not shift the math.
            qs._handle_queue_reorder(MagicMock(old_index=5, new_index=1,
                                               control=MagicMock(controls=list(qs._queue_list.controls))))
            moved.assert_called_once_with(4, 1)
            moved.reset_mock()
            # Dragging the header itself is ignored.
            qs._handle_queue_reorder(MagicMock(old_index=2, new_index=4, control=MagicMock(controls=[])))
            moved.assert_not_called()
        finally:
            audio_engine.move_queue_item = orig

    def test_row_actions_resolve_the_drawn_row_after_the_queue_shifts(self):
        qs = self._sheet()
        gamma = audio_engine.queue[4]
        # A refill lands between the render and the tap: every row shifts by 2.
        audio_engine.queue[1:1] = [{"path": "/r/9"}, {"path": "/r/8"}]
        removed, moved = MagicMock(), MagicMock()
        saved = (audio_engine.remove_from_queue, audio_engine.move_queue_item)
        audio_engine.remove_from_queue, audio_engine.move_queue_item = removed, moved
        try:
            # Drawn "Gamma" (control 5) dragged to below "Alpha" (slot 1):
            # resolves to Gamma and Beta's CURRENT rows, not the drawn ones.
            qs._handle_queue_reorder(MagicMock(old_index=5, new_index=1,
                                               control=MagicMock(controls=list(qs._queue_list.controls))))
            moved.assert_called_once_with(6, 3)
            qs._remove(gamma)
            removed.assert_called_once_with(6)
            removed.reset_mock()
            qs._remove({"path": "/m/c"})               # equal, but not a queued row
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
