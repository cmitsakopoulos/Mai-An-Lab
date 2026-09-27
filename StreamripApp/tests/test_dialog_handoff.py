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
import pytest

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


def test_metadata_workbench_open_editor_handoff():
    from ui.player.metadata_workbench import MetadataWorkbenchPane
    import flet as ft
    from unittest.mock import MagicMock

    app = MagicMock()
    app.page = MagicMock()
    app.db_manager = MagicMock()
    app.dismiss_dialog.return_value = True

    pane = MetadataWorkbenchPane.__new__(MetadataWorkbenchPane)
    pane.app = app
    pane.db = app.db_manager
    pane.vocab = []
    pane.countries = []
    pane.filter = "all"
    pane.selected = set()
    pane.all_gaps = []
    pane.coverage = {}
    pane.low_conf = []
    pane._row_refs = {}
    pane._list = ft.Column()
    pane._seg = MagicMock()
    pane._schedule_walk_refresh = MagicMock()
    pane._reload_async = MagicMock()
    pane._safe_update = MagicMock()

    item = {"artist_name": "Pink Floyd", "genres": ["Classic Rock"], "source_genres": ["Rock"], "country": "GB"}
    pane._open_editor(item)

    # Sheet should have been presented
    app.page.show_dialog.assert_called_once()
    sheet = app.page.show_dialog.call_args[0][0]
    assert isinstance(sheet, ft.BottomSheet)
    assert sheet.on_dismiss is not None

    # Find the Save button in the sheet header
    content_col = sheet.content.content
    header_row = content_col.controls[1].content
    save_container = next(c for c in header_row.controls if isinstance(c, ft.Container) and getattr(c, "on_click", None))

    # Click save
    save_container.on_click(None)

    # dismiss_dialog should have been called, but task not run yet (waiting for on_dismiss)
    app.dismiss_dialog.assert_called_once_with(sheet)
    app.page.run_task.assert_not_called()

    # Now simulate Flutter firing on_dismiss
    sheet.on_dismiss()
    app.page.run_task.assert_called_once()


def test_metadata_workbench_visible_genre_filter():
    from ui.player.metadata_workbench import MetadataWorkbenchPane

    pane = MetadataWorkbenchPane.__new__(MetadataWorkbenchPane)
    pane.filter = "all"
    pane.search = "psychedelic"
    pane.all_gaps = [
        {"artist_name": "Pink Floyd", "genres": ["Classic Rock", "Psychedelic Rock"], "source_genres": []},
        {"artist_name": "Metallica", "genres": ["Heavy Metal"], "source_genres": ["Metal"]},
        {"artist_name": "The Doors", "genres": [], "source_genres": ["psychedelic rock"]},
    ]
    visible = pane._visible()
    assert len(visible) == 2
    names = {v["artist_name"] for v in visible}
    assert names == {"Pink Floyd", "The Doors"}


@pytest.mark.asyncio
async def test_metadata_workbench_focus_artist_non_gap():
    from ui.player.metadata_workbench import MetadataWorkbenchPane
    from unittest.mock import AsyncMock

    app = MagicMock()
    app.page = MagicMock()
    app.db_manager = MagicMock()
    app.dismiss_dialog.return_value = True

    pane = MetadataWorkbenchPane.__new__(MetadataWorkbenchPane)
    pane.app = app
    pane.db = app.db_manager
    pane._pending_focus = "Pink Floyd"
    pane.search = "Pink Floyd"
    pane.all_gaps = []
    pane.coverage = {}
    pane.low_conf = []
    pane.vocab = []
    pane.countries = []
    pane._row_refs = {}
    pane._list = MagicMock()
    pane._render = MagicMock()
    pane._open_editor = MagicMock()

    app.db_manager.get_metadata_gap_artists = AsyncMock(return_value=[])
    app.db_manager.get_metadata_coverage = AsyncMock(return_value={})
    app.db_manager.get_low_confidence_artists = AsyncMock(return_value=[])
    app.db_manager.get_genre_vocabulary = AsyncMock(return_value=[])
    app.db_manager.get_library_countries = AsyncMock(return_value=[])
    app.db_manager.get_artists_by_genre = AsyncMock(return_value=[])
    app.db_manager.get_artist_workbench_item = AsyncMock(return_value={
        "artist_name": "Pink Floyd", "genres": ["Rock"], "source_genres": [], "country": "GB"
    })

    await pane._reload_async()

    app.db_manager.get_artist_workbench_item.assert_called_with("Pink Floyd")
    pane._open_editor.assert_called_once()
    assert any(g["artist_name"] == "Pink Floyd" for g in pane.all_gaps)


@pytest.mark.asyncio
async def test_metadata_workbench_unsubmitted_and_comma_genres():
    from ui.player.metadata_workbench import MetadataWorkbenchPane
    import flet as ft
    from unittest.mock import MagicMock, AsyncMock

    app = MagicMock()
    app.page = MagicMock()
    app.db_manager = MagicMock()
    app.library_view = MagicMock()
    app.dismiss_dialog.return_value = True

    pane = MetadataWorkbenchPane.__new__(MetadataWorkbenchPane)
    pane.app = app
    pane.db = app.db_manager
    pane.vocab = []
    pane.countries = [{"code": "GB"}]
    pane.filter = "all"
    pane.selected = set()
    pane.all_gaps = []
    pane.coverage = {}
    pane.low_conf = []
    pane._row_refs = {}
    pane._list = ft.Column()
    pane._reload_async = AsyncMock()
    pane._safe_update = MagicMock()

    item = {"artist_name": "New Order", "genres": ["Post-Punk"], "source_genres": [], "country": "GB"}
    pane._open_editor(item)

    sheet = app.page.show_dialog.call_args[0][0]
    content_col = sheet.content.content
    body_col = content_col.controls[3].content
    chips_row = body_col.controls[0].controls[1]
    custom_field = chips_row.controls[-1]
    assert isinstance(custom_field, ft.TextField)

    # User types comma-separated tags and clicks save WITHOUT pressing enter
    custom_field.value = "Synthpop, New Wave"

    header_row = content_col.controls[1].content
    save_container = next(c for c in header_row.controls if isinstance(c, ft.Container) and getattr(c, "on_click", None))

    save_container.on_click(None)
    app.dismiss_dialog.assert_called_once_with(sheet)

    # Simulate Flutter dismissal
    sheet.on_dismiss()
    task_fn = app.page.run_task.call_args[0][0]

    app.db_manager.set_manual_artist_enrichment = AsyncMock()
    await task_fn()

    app.db_manager.set_manual_artist_enrichment.assert_called_once()
    called_kwargs = app.db_manager.set_manual_artist_enrichment.call_args[1]
    saved_genres = called_kwargs["genres"]
    # Must contain original plus both parsed tags
    assert "post-punk" in saved_genres
    assert "synthpop" in saved_genres
    assert "new wave" in saved_genres
    # Must flag library view reload
    assert app.library_view._needs_reload is True

    from utils.metadata_enrich import _refresh_pending
    for t in list(_refresh_pending.values()):
        t.cancel()
    _refresh_pending.clear()


@pytest.mark.asyncio
async def test_bulk_tag_artists_comma_separated():
    from utils.db_manager import DatabaseManager
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db = DatabaseManager(tmp.name)
    await db.initialize()

    conn = await db.get_connection()
    await conn.execute("INSERT INTO artists (id, name, track_count) VALUES (1, 'Joy Division', 10)")
    await conn.commit()

    n = await db.bulk_tag_artists(["Joy Division"], genre="Goth, Post-Punk", refresh_model=False)
    assert n == 1

    item = await db.get_artist_enrichment("Joy Division")
    genre_names = [g["name"] if isinstance(g, dict) else g for g in item["genres"]]
    assert "goth" in genre_names
    assert "post-punk" in genre_names
    await db.close()




