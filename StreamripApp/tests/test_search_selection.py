import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft

from ui.views.search import SearchView
from ui.player.quality_selector import QualitySelectorSheet


def _rec(i=1, m_type="track", source="qobuz"):
    return {"id": str(i), "media_type": m_type, "source": source,
            "ui_title": f"Item {i}", "ui_subtitle": "Artist"}


def _view():
    v = SearchView.__new__(SearchView)
    v.app = MagicMock()
    v.page = MagicMock()
    v.expanded_nodes = set()
    v.selected_source = "qobuz"
    v.selection_mode = False
    v.selected_keys = set()
    v._selected_records = {}
    v._results_list = ft.ListView()
    v._selection_count = ft.Text()
    v._selection_download_btn = ft.Container()
    v._selection_bar = ft.Container()
    v._view_tabs_row = ft.Row()
    v._header_slot = ft.AnimatedSwitcher(content=v._view_tabs_row)
    return v


class TestSelectionLifecycle(unittest.TestCase):
    def test_long_press_enters_selection_and_selects_the_row(self):
        v = _view()
        v.enter_selection(_rec(1))
        self.assertTrue(v.selection_mode)
        self.assertEqual(len(v.selected_keys), 1)

    def test_artists_cannot_seed_a_selection(self):
        """Artists are not downloadable, so they must not start a batch."""
        v = _view()
        v.enter_selection(_rec(1, m_type="artist"))
        self.assertFalse(v.selection_mode)
        self.assertEqual(v.selected_keys, set())

    def test_artist_rows_are_not_selectable_once_mode_is_active(self):
        v = _view()
        v.enter_selection(_rec(1))
        v._toggle_selection(_rec(2, m_type="artist"))
        # _toggle_selection is only reached for selectable rows; assert the
        # predicate that guards it.
        self.assertFalse(v._is_selectable(_rec(2, m_type="artist")))
        self.assertTrue(v._is_selectable(_rec(2, m_type="album")))

    def test_toggle_is_idempotent_in_pairs(self):
        v = _view()
        r = _rec(1)
        v.enter_selection(r)
        v._toggle_selection(r)
        self.assertEqual(v.selected_keys, set())
        self.assertEqual(v._selected_records, {})

    def test_exit_clears_everything(self):
        v = _view()
        v.enter_selection(_rec(1))
        v._toggle_selection(_rec(2))
        v.exit_selection()
        self.assertFalse(v.selection_mode)
        self.assertEqual(v.selected_keys, set())
        self.assertEqual(v._selected_records, {})

    def test_header_slot_swaps_with_mode(self):
        v = _view()
        self.assertIs(v._header_slot.content, v._view_tabs_row)
        v.enter_selection(_rec(1))
        self.assertIs(v._header_slot.content, v._selection_bar)
        v.exit_selection()
        self.assertIs(v._header_slot.content, v._view_tabs_row)


class TestSelectionKeying(unittest.TestCase):
    """Selection survives paging and the type toggle, because it stores the
    record, not a reference to the row that displayed it."""

    def test_key_is_scoped_by_source_and_type(self):
        v = _view()
        self.assertEqual(v._result_key(_rec(5, "track", "qobuz")), "qobuz:track:5")
        self.assertNotEqual(
            v._result_key(_rec(5, "track", "qobuz")),
            v._result_key(_rec(5, "track", "deezer")),
        )
        self.assertNotEqual(
            v._result_key(_rec(5, "track")),
            v._result_key(_rec(5, "album")),
        )

    def test_selection_survives_the_rows_being_destroyed(self):
        v = _view()
        r = _rec(1)
        v._results_list.controls = [MagicMock(data=r)]
        v.enter_selection(r)
        # Simulate turning a page: every rendered row is discarded.
        v._results_list.controls = []
        self.assertEqual(len(v.selected_keys), 1)
        self.assertEqual(list(v._selected_records.values()), [r])

    def test_the_same_item_from_two_pages_is_one_selection(self):
        v = _view()
        v.enter_selection(_rec(1))
        v._toggle_selection(dict(_rec(1)))  # equal content, different object
        self.assertEqual(v.selected_keys, set())


class TestSelectAll(unittest.TestCase):
    def test_select_all_covers_only_the_rendered_page(self):
        v = _view()
        rows = [MagicMock(data=_rec(i)) for i in range(5)]
        v._results_list.controls = rows
        v.enter_selection()
        v._select_all_on_page()
        self.assertEqual(len(v.selected_keys), 5)

    def test_select_all_skips_artists(self):
        v = _view()
        v._results_list.controls = [
            MagicMock(data=_rec(1, "track")),
            MagicMock(data=_rec(2, "artist")),
            MagicMock(data=_rec(3, "album")),
        ]
        v.enter_selection()
        v._select_all_on_page()
        self.assertEqual(len(v.selected_keys), 2)

    def test_select_all_does_not_duplicate(self):
        v = _view()
        v._results_list.controls = [MagicMock(data=_rec(i)) for i in range(3)]
        v.enter_selection()
        v._select_all_on_page()
        v._select_all_on_page()
        self.assertEqual(len(v.selected_keys), 3)


class TestBatchDownload(unittest.TestCase):
    def test_download_selection_submits_every_record_once(self):
        v = _view()
        for i in range(3):
            v._toggle_selection(_rec(i), refresh=False)
        v.selection_mode = True
        v._download_selection()

        v.app.quality_selector_sheet.show_batch.assert_called_once()
        records = v.app.quality_selector_sheet.show_batch.call_args[0][0]
        self.assertEqual(len(records), 3)

    def test_empty_selection_opens_nothing(self):
        v = _view()
        v.selection_mode = True
        v._download_selection()
        v.app.quality_selector_sheet.show_batch.assert_not_called()


class TestQualitySheetBatching(unittest.TestCase):
    def _sheet(self):
        app = MagicMock()
        s = QualitySelectorSheet(app)
        s.page = MagicMock()
        return s, app

    def test_batch_uses_enqueue_many_not_n_enqueues(self):
        s, app = self._sheet()
        records = [_rec(i) for i in range(4)]
        with patch.object(s, "_default_tier", return_value="cd"):
            s.show_batch(records)
        app.queue.enqueue_many.assert_called_once()
        app.queue.enqueue.assert_not_called()
        self.assertEqual(app.queue.enqueue_many.call_args[1]["quality_tier"], "cd")

    def test_single_item_still_uses_enqueue(self):
        s, app = self._sheet()
        with patch.object(s, "_default_tier", return_value="mp3"):
            s.show(_rec(1))
        app.queue.enqueue.assert_called_once()
        app.queue.enqueue_many.assert_not_called()

    def test_remembered_default_skips_the_sheet(self):
        s, app = self._sheet()
        with patch.object(s, "_default_tier", return_value="hires"):
            s.show(_rec(1))
        # The sheet was never built, let alone opened.
        self.assertFalse(s._initialized)
        self.assertEqual(app.queue.enqueue.call_args[1]["quality_tier"], "hires")

    def test_no_default_opens_the_sheet(self):
        s, app = self._sheet()
        with patch.object(s, "_default_tier", return_value=None):
            s.show_batch([_rec(1), _rec(2)])
        self.assertTrue(s._initialized)
        # Presented through the Flet 0.86 dialog stack, not the legacy
        # page.overlay + `.open = True` path, which never installs the dismiss
        # lifecycle.
        s.page.show_dialog.assert_called_once_with(s._sheet)
        app.queue.enqueue_many.assert_not_called()

    def test_sheet_has_no_remember_control(self):
        """A persistent preference belongs in Settings, where it is visible and
        undoable, not behind a checkbox on a transient sheet."""
        s, _ = self._sheet()
        s._ensure_initialized()
        self.assertFalse(hasattr(s, "_remember_check"))

        def walk(ctrl):
            yield ctrl
            child = getattr(ctrl, "content", None)
            if isinstance(child, ft.Control):
                yield from walk(child)
            for c in (getattr(ctrl, "controls", None) or []):
                if isinstance(c, ft.Control):
                    yield from walk(c)

        self.assertFalse([c for c in walk(s._sheet) if isinstance(c, ft.Checkbox)])

    def test_unknown_stored_tier_falls_back_to_asking(self):
        """Guards a hand-edited config: an unrecognised value must not be
        passed through to the downloader as a quality tier."""
        s, _ = self._sheet()
        with patch("utils.streamrip_api.load_config",
                   return_value={"general": {"default_quality": "lossless-ultra"}}):
            self.assertIsNone(s._default_tier())

    def test_enqueue_waits_for_the_sheet_to_actually_close(self):
        """Closing a BottomSheet and raising a snackbar in the same Flutter
        frame makes the outgoing sheet's bare Navigator.pop() take the toast's
        route instead, which blanks the view. The submit must be deferred until
        Flutter confirms the sheet is gone."""
        s, app = self._sheet()
        with patch.object(s, "_default_tier", return_value=None):
            s.show_batch([_rec(1)])

        s._sheet.open = True
        s._confirm("hires")
        # Nothing submitted yet: the handoff is still waiting on on_dismiss.
        app.queue.enqueue.assert_not_called()
        app.show_snackbar.assert_not_called()

        s._on_dismiss()
        app.queue.enqueue.assert_called_once()
        app.show_snackbar.assert_called_once()

    def test_submit_runs_immediately_when_nothing_was_open(self):
        """If the sheet was already gone, on_dismiss will never arrive."""
        s, app = self._sheet()
        with patch.object(s, "_default_tier", return_value=None):
            s.show_batch([_rec(1)])
        s._sheet.open = False
        app.dismiss_dialog.return_value = False

        s._confirm("cd")
        app.queue.enqueue.assert_called_once()

    def test_on_done_fires_so_selection_mode_exits(self):
        s, _ = self._sheet()
        done = []
        with patch.object(s, "_default_tier", return_value="cd"):
            s.show_batch([_rec(1)], on_done=lambda: done.append(1))
        self.assertEqual(done, [1])

    def test_pending_records_are_not_submitted_twice(self):
        s, app = self._sheet()
        with patch.object(s, "_default_tier", return_value="cd"):
            s.show_batch([_rec(1), _rec(2)])
        s._enqueue("cd")  # a stray second call must be a no-op
        self.assertEqual(app.queue.enqueue_many.call_count, 1)


if __name__ == "__main__":
    unittest.main()
