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
from ui.player.autoplay_sheet import AutoPlaySheet, anchor_label  # noqa: E402
from ui.player.queue_sheet import QueueSheet  # noqa: E402
from utils.autoplay import AutoPlay  # noqa: E402

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
            {"path": "/m/b", "track_title": "Beta", "artist_name": "Y", "_queued": True},
            {"path": "/r/1", "track_title": "Rec 1", "artist_name": "Z", "_autoplay": True},
            {"path": "/r/2", "track_title": "Rec 2", "artist_name": "Z", "_autoplay": True},
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
    def test_sheet_shows_anchor_and_no_mode_choice(self):
        sheet = AutoPlaySheet(self.app)
        bs = sheet._build()
        self.assertIsInstance(bs, ft.BottomSheet)
        self.assertTrue(bs.draggable)   # swipe-down to dismiss
        texts = _texts(bs)
        self.assertFalse({"Focused", "Cross-genre"} & set(texts))
        self.assertIn("Alpha — X", texts)
        self.assertIn("2 songs lined up", texts)
        self.assertEqual(anchor_label(self.app), "Alpha — X")

    def test_toggle_goes_through_the_app_hook(self):
        sheet = AutoPlaySheet(self.app)
        sheet._build()
        sheet._on_toggle(MagicMock(control=MagicMock(value=False)))
        self.app.set_play_similar_mode.assert_called_once_with(False)

    def test_anchor_hidden_while_off(self):
        self.app.play_similar_mode = False
        sheet = AutoPlaySheet(self.app)
        sheet._build()
        self.assertFalse(sheet._anchor_section.visible)


class QueueHeaderTests(_Base):
    """Queue (setUp): Alpha playing, Beta (the listener's pick), Rec 1, Rec 2.
    Controls: Alpha 0, Beta 1, AUTO-PLAY 2, Rec 1 3, Rec 2 4."""

    def _sheet(self):
        qs = QueueSheet(self.app)
        qs._ensure_initialized()
        qs.refresh()
        return qs

    def test_header_sits_above_first_recommendation(self):
        qs = self._sheet()
        ctrls = qs._queue_list.controls
        self.assertEqual(qs._header_positions, [2])
        self.assertEqual(ctrls[2].key, "q_autoplay_header")
        self.assertIn("from Alpha — X", _texts(ctrls[2]))
        self.assertEqual(qs._count_text.value, "3 tracks remaining")

    def test_no_sections_when_autoplay_off(self):
        self.app.play_similar_mode = False
        qs = self._sheet()
        self.assertEqual(qs._header_positions, [])
        keys = {getattr(c, "key", None) for c in qs._queue_list.controls}
        self.assertNotIn("q_autoplay_header", keys)

    def test_pending_header_sits_at_the_end_while_a_block_is_on_its_way(self):
        audio_engine.queue = [t for t in audio_engine.queue if not t.get("_autoplay")]
        qs = self._sheet()
        keys = [getattr(c, "key", None) for c in qs._queue_list.controls]
        self.assertEqual(keys[-1], "q_autoplay_header")
        self.assertIn("finding similar songs…", _texts(qs._queue_list.controls[-1]))

    def test_reorder_skips_the_header(self):
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
            # Dragging the header is ignored.
            qs._handle_queue_reorder(MagicMock(old_index=2, new_index=4, control=MagicMock(controls=[])))
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
    def test_tap_toggles_both_ways(self):
        from ui.player import now_playing as np_mod
        np = np_mod.NowPlayingSheet(self.app)
        np._ensure_initialized()
        self.app.play_similar_mode = False
        np._play_similar_btn.on_click(MagicMock())       # off -> on
        self.app.page.run_task.assert_called_with(np._toggle_play_similar_async)
        self.app.page.run_task.reset_mock()
        self.app.play_similar_mode = True
        np._play_similar_btn.on_click(MagicMock())       # on -> off, no sheet
        self.app.page.run_task.assert_called_with(np._toggle_play_similar_async)
        self.assertFalse(hasattr(np, "open_autoplay_sheet"))
        # The label rides above the icon in the same row; no status pill.
        self.assertIn(np._autoplay_label, np._autoplay_cell.controls)
        self.assertFalse(hasattr(np, "_autoplay_pill"))


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
