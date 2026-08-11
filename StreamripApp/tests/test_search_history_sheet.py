import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft

from ui.views.search import SearchView


def _make_view():
    view = SearchView.__new__(SearchView)
    view.app = MagicMock()
    view.page = MagicMock()
    return view


class TestRecentSearchesSheet(unittest.TestCase):
    """Guards the two defects in the recent-searches sheet.

    1. Its body was a tight Column with no scroll, so entries past the fold
       were clipped off-screen and unreachable.
    2. It was presented via page.overlay + `.open = True`, which never installs
       the dismiss lifecycle: a scrim tap closed it in Flutter but left `.open`
       True in Python, so reopening was a silent no-op.
    """

    def test_body_is_bounded_and_scrollable(self):
        view = _make_view()
        searches = [f"query {i}" for i in range(10)]

        with patch("utils.search_history.load_searches", return_value=searches):
            view._show_recent_searches(None)

        lv = view._history_list
        self.assertEqual(len(lv.controls), 10)
        # Bounded height + explicit scroll. ScrollMode.ALWAYS is required:
        # Android's mobile ScrollBehavior renders no scrollbar without it.
        self.assertIsNotNone(lv.height)
        self.assertEqual(lv.scroll, ft.ScrollMode.ALWAYS)

    def test_presented_through_dialog_stack_not_overlay(self):
        view = _make_view()
        with patch("utils.search_history.load_searches", return_value=["a", "b"]):
            view._show_recent_searches(None)

        view.page.show_dialog.assert_called_once()
        sheet = view.page.show_dialog.call_args[0][0]
        self.assertIsInstance(sheet, ft.BottomSheet)
        # Must not use the legacy overlay + `.open` path.
        view.page.overlay.append.assert_not_called()

    def test_reopens_after_dismissal(self):
        """The `.open` state bug: a second open must present a sheet again."""
        view = _make_view()
        with patch("utils.search_history.load_searches", return_value=["a", "b"]):
            view._show_recent_searches(None)
            view._show_recent_searches(None)

        self.assertEqual(view.page.show_dialog.call_count, 2)

    def test_list_is_rebuilt_not_reparented(self):
        """Each open must build a fresh ListView; re-parenting a control that a
        previous sheet mounted is the kind of thing Flet handles badly."""
        view = _make_view()
        with patch("utils.search_history.load_searches", return_value=["a"]):
            view._show_recent_searches(None)
            first = view._history_list
            view._show_recent_searches(None)

        self.assertIsNot(first, view._history_list)

    def test_empty_history_presents_nothing(self):
        view = _make_view()
        with patch("utils.search_history.load_searches", return_value=[]):
            view._show_recent_searches(None)
        view.page.show_dialog.assert_not_called()

    def test_selecting_an_entry_pops_and_searches(self):
        """The sheet must be named, not popped: page.pop_dialog() closes the
        topmost open dialog, and toasts share that stack, so a notification on
        screen would absorb the close and leave the sheet up."""
        view = _make_view()
        view._do_recent = MagicMock()

        with patch("utils.search_history.load_searches", return_value=["daft punk"]):
            view._show_recent_searches(None)

        sheet = view.page.show_dialog.call_args[0][0]
        view._history_list.controls[0].on_click(None)

        view.app.dismiss_dialog.assert_called_once_with(sheet)
        view.page.pop_dialog.assert_not_called()
        view._do_recent.assert_called_once_with("daft punk")


if __name__ == "__main__":
    unittest.main()
