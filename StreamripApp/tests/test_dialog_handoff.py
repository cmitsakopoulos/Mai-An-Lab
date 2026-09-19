"""Guards the Flet 0.86 dialog-stack rules that stranded the Android UI.

Both rules exist because of how Flet 0.86 pops routes. `BottomSheetControl`
closes itself with a bare `Navigator.pop()` and `AlertDialogControl` pops the
topmost route once its own is active, so neither reliably takes down the route
it means to. Close a dialog and push another in the same Flutter frame — which
`page.run_task` does NOT escape — and the outgoing dialog's pop claims the
incoming one's route: Flutter keeps rendering a dialog Python has recorded as
closed, `pop_dialog()` then finds nothing open, and the app has to be
force-stopped. Separately, `page.pop_dialog()` closes whichever dialog is
topmost, and `NotificationSystem.show()` puts every toast in that same stack.
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ui.widgets import dialog_handoff


def _handoff():
    app = MagicMock()
    app.dismiss_dialog.return_value = True
    dialog = object()
    on_dismiss, close = dialog_handoff(app, lambda: dialog)
    return app, dialog, on_dismiss, close


def test_follow_up_waits_for_the_dismiss_event():
    app, dialog, on_dismiss, close = _handoff()
    ran = []

    close(lambda: ran.append("go"))

    app.dismiss_dialog.assert_called_once_with(dialog)
    assert ran == [], "follow-up must not run in the closing frame"

    on_dismiss()
    assert ran == ["go"]


def test_follow_up_runs_only_once():
    _, _, on_dismiss, close = _handoff()
    ran = []

    close(lambda: ran.append("go"))
    on_dismiss()
    on_dismiss()

    assert ran == ["go"]


def test_follow_up_runs_inline_when_nothing_was_closed():
    """An already-dismissed dialog emits no on_dismiss, so waiting for one would
    strand the follow-up forever."""
    app, _, _, close = _handoff()
    app.dismiss_dialog.return_value = False
    ran = []

    close(lambda: ran.append("go"))

    assert ran == ["go"]


def test_close_without_follow_up_is_a_plain_close():
    app, dialog, on_dismiss, close = _handoff()

    close()
    on_dismiss()  # must not raise

    app.dismiss_dialog.assert_called_once_with(dialog)


def test_a_stale_follow_up_is_not_replayed_by_a_later_dismiss():
    """close() then an unrelated dismiss must not re-fire a spent follow-up."""
    _, _, on_dismiss, close = _handoff()
    ran = []

    close(lambda: ran.append("first"))
    on_dismiss()
    close()
    on_dismiss()

    assert ran == ["first"]


def test_network_row_menu_defers_the_playlist_sheet():
    """The network view's long-press menu pushes another BottomSheet, which is
    the same sheet-on-sheet collision as the library Delete Track menu."""
    from ui.views.library import LibraryView

    view = LibraryView.__new__(LibraryView)
    view.app = MagicMock()
    view.app.dismiss_dialog.return_value = True
    view.page = MagicMock()
    view._node_to_track = lambda nd: {"path": nd.get("path")}

    view._net_row_context_menu(0, {"path": "/a.mp3", "title": "A"})

    sheet = view.page.show_dialog.call_args[0][0]
    tile = next(
        t for t in sheet.content.content.controls
        if getattr(getattr(t, "title", None), "value", None) == "Add to Playlist"
    )

    tile.on_click(None)
    view.app.dismiss_dialog.assert_called_once_with(sheet)
    view.page.run_task.assert_not_called()

    sheet.on_dismiss(None)
    view.page.run_task.assert_called_once()


def test_track_context_menu_redownload_defers_to_sheet_dismissal():
    """Tapping Redownload in track context menu must dismiss the sheet first
    before switching tabs and starting search, avoiding route collisions."""
    from ui.views.library import LibraryView

    view = LibraryView.__new__(LibraryView)
    view.app = MagicMock()
    view.app.dismiss_dialog.return_value = True
    view.page = MagicMock()
    view.app.search_view = MagicMock()
    view._build_context_menu_track_card = lambda meta: MagicMock()

    meta = {"track_title": "Bohemian Rhapsody", "artist_name": "Queen"}
    view._open_track_context_menu(meta)

    sheet = view.page.show_dialog.call_args[0][0]
    tile = next(
        t for t in sheet.content.content.controls
        if getattr(getattr(t, "title", None), "value", None) == "Redownload (Different Quality)"
    )

    tile.on_click(None)
    view.app.dismiss_dialog.assert_called_once_with(sheet)
    view.app._switch_tab.assert_not_called()
    view.page.run_task.assert_not_called()

    # Once Flutter finishes dismissal, the follow-up runs:
    sheet.on_dismiss(None)
    view.app._switch_tab.assert_called_once_with(1)
    assert view.app.search_view._search_field.value == "Bohemian Rhapsody Queen"
    assert view.app.search_view.view_mode == "tracks"
    view.app.search_view._update_view_tabs.assert_called_once()
    view.page.run_task.assert_called_once_with(view.app.search_view.start_search)


def test_cupertino_segmented_bar_unmounted_updates_do_not_crash():
    """CupertinoSegmentedBar.set_selected and _select must not raise
    'Control must be added to the page first' when called on unmounted instances."""
    import pytest
    from ui.widgets import CupertinoSegmentedBar

    bar = CupertinoSegmentedBar(
        segments=[("a", "A", None, None), ("b", "B", None, None)],
        selected_key="a",
        on_change=None,
    )
    # In Flet, accessing .page on an unmounted control raises RuntimeError
    with pytest.raises(RuntimeError, match="Control must be added to the page first"):
        _ = bar.page

    # Should update state and styles without raising
    bar.set_selected("b")
    assert bar.selected_key == "b"

    bar._select("a")
    assert bar.selected_key == "a"


def test_source_segment_unmounted_update_does_not_crash():
    """SourceSegment.update_state must not raise when unmounted."""
    import pytest
    from ui.widgets import SourceSegment

    seg = SourceSegment("flac", selected=False)
    with pytest.raises(RuntimeError, match="Control must be added to the page first"):
        _ = seg.page

    seg.update_state(True)
    assert seg.selected is True


def test_playlist_row_bin_icon_requests_delete_confirmation():
    """Pressing the bin icon on a playlist row must ask for confirmation rather than
    deleting immediately."""
    from ui.views.library import LibraryView
    import flet as ft

    view = LibraryView.__new__(LibraryView)
    view.app = MagicMock()
    view.page = MagicMock()

    tile = view._playlist_row({"id": 42, "name": "Chill Beats", "track_count": 5}, "pl_42", False)

    # Find the bin icon button in trailing
    bin_btn = next(
        btn for btn in tile.trailing.controls
        if isinstance(btn, ft.IconButton) and btn.icon == ft.Icons.DELETE_OUTLINE
    )

    # Click the bin icon
    bin_btn.on_click(None)

    # Must invoke confirm_delete_playlist on the app
    view.app.confirm_delete_playlist.assert_called_once_with(42, "Chill Beats")


def test_main_app_confirm_delete_playlist_dialog_behavior():
    """MainApp.confirm_delete_playlist shows a confirmation AlertDialog with Cancel and Delete."""
    from main import StreamripFletApp
    import flet as ft

    app = StreamripFletApp.__new__(StreamripFletApp)
    app.page = MagicMock()
    app.dismiss_dialog = MagicMock()

    app.confirm_delete_playlist(42, "Chill Beats")

    app.page.show_dialog.assert_called_once()
    dlg = app.page.show_dialog.call_args[0][0]
    assert isinstance(dlg, ft.AlertDialog)
    assert dlg.title.value == "Delete Playlist?"
    assert "Chill Beats" in dlg.content.value

    cancel_btn = next(a for a in dlg.actions if isinstance(a, ft.TextButton) and a.content == "Cancel")
    delete_btn = next(a for a in dlg.actions if isinstance(a, ft.Button))

    # Cancel must dismiss dialog without deleting
    cancel_btn.on_click(None)
    app.dismiss_dialog.assert_called_once_with(dlg)
    app.page.run_task.assert_not_called()

    # Delete must dismiss dialog and run task _delete_playlist
    delete_btn.on_click(None)
    assert app.dismiss_dialog.call_count == 2
    app.page.run_task.assert_called_once_with(app._delete_playlist, 42)


