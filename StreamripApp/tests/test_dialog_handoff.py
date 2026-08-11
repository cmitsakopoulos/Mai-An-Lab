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
