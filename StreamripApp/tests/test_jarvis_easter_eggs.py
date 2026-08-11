import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from utils import assistant_intent as ai
from utils.assistant_runner import AssistantRunner

def test_easter_egg_intent_parsing():
    """Verify that regex parser correctly maps conversational & easter-egg utterances."""
    assert ai.parse("who created you").name == ai.INTENT_CREATOR
    assert ai.parse("who built you").name == ai.INTENT_CREATOR
    assert ai.parse("make me a coffee").name == ai.INTENT_COFFEE
    assert ai.parse("brew tea").name == ai.INTENT_COFFEE
    assert ai.parse("open pod bay doors").name == ai.INTENT_HAL
    assert ai.parse("hal 9000").name == ai.INTENT_HAL
    assert ai.parse("i am iron man").name == ai.INTENT_IRON_MAN
    assert ai.parse("i'm iron man").name == ai.INTENT_IRON_MAN
    assert ai.parse("tell me a joke").name == ai.INTENT_JOKE
    assert ai.parse("say something funny").name == ai.INTENT_JOKE
    assert ai.parse("what time is it").name == ai.INTENT_TIME_DATE
    assert ai.parse("what's the date").name == ai.INTENT_TIME_DATE
    assert ai.parse("system status").name == ai.INTENT_STATUS
    assert ai.parse("how are you").name == ai.INTENT_STATUS
    assert ai.parse("thank you").name == ai.INTENT_THANKS
    assert ai.parse("thanks jarvis").name == ai.INTENT_THANKS
    assert ai.parse("give me a quote").name == ai.INTENT_QUOTE
    assert ai.parse("say something wise").name == ai.INTENT_QUOTE
    assert ai.parse("sing a song").name == ai.INTENT_PLAY_RANDOM
    assert ai.parse("can you sing").name == ai.INTENT_PLAY_RANDOM

@pytest.mark.asyncio
async def test_easter_egg_dispatch_responses():
    """Verify that AssistantRunner returns proper responses for all easter eggs."""
    mock_db = MagicMock()
    mock_engine = MagicMock()
    runner = AssistantRunner(db_manager=mock_db, audio_engine=mock_engine)

    res_creator = await runner.dispatch(ai.Intent(ai.INTENT_CREATOR))
    assert res_creator.success
    assert "Jarvis Audio Assistant" in res_creator.displayed

    res_coffee = await runner.dispatch(ai.Intent(ai.INTENT_COFFEE))
    assert res_coffee.success
    assert "Functionality unavailable" in res_coffee.displayed

    res_hal = await runner.dispatch(ai.Intent(ai.INTENT_HAL))
    assert res_hal.success
    assert "Systems operational" in res_hal.displayed

    res_iron_man = await runner.dispatch(ai.Intent(ai.INTENT_IRON_MAN))
    assert res_iron_man.success
    assert "Systems online" in res_iron_man.displayed

    res_joke = await runner.dispatch(ai.Intent(ai.INTENT_JOKE))
    assert res_joke.success
    assert "music curation" in res_joke.spoken

    res_time = await runner.dispatch(ai.Intent(ai.INTENT_TIME_DATE))
    assert res_time.success
    assert "Time:" in res_time.displayed

    res_status = await runner.dispatch(ai.Intent(ai.INTENT_STATUS))
    assert res_status.success
    assert "Operational" in res_status.displayed

    res_thanks = await runner.dispatch(ai.Intent(ai.INTENT_THANKS))
    assert res_thanks.success
    assert "welcome" in res_thanks.spoken

    res_quote = await runner.dispatch(ai.Intent(ai.INTENT_QUOTE))
    assert res_quote.success
    assert len(res_quote.displayed) > 0


def _context_menu_view():
    """A LibraryView stub whose app.dismiss_dialog reports a real close."""
    from ui.views.library import LibraryView

    mock_app = MagicMock()
    mock_page = MagicMock()
    mock_app.page = mock_page
    # The real helper returns True when it actually closed something; the sheet
    # only defers its follow-up to on_dismiss when that is the case.
    mock_app.dismiss_dialog.return_value = True

    view = LibraryView.__new__(LibraryView)
    view.app = mock_app
    view.page = mock_page
    return view, mock_app, mock_page


def _delete_tile(sheet):
    return next(
        t for t in sheet.content.content.controls
        if getattr(getattr(t, "title", None), "value", None) == "Delete Track"
    )


@pytest.mark.asyncio
async def test_track_context_menu_delete_flow():
    """Delete Track must close its own sheet and raise the confirm dialog only
    from the sheet's on_dismiss — never in the same frame.

    BottomSheetControl closes itself with an unguarded Navigator.pop(), so a
    dialog route pushed before the sheet's route is really gone gets popped
    instead of the sheet. Flutter then keeps rendering the sheet while Python
    has already recorded it as closed, and nothing on screen can be dismissed
    again — the app has to be force-stopped. show_dialog() fires on_dismiss only
    after Flutter reports the sheet gone and unmounts its stack entry, so that
    handler is the one ordering that is actually guaranteed.
    """
    view, mock_app, mock_page = _context_menu_view()

    meta = {"path": "/test/song.mp3", "track_title": "Test Title"}
    view._open_track_context_menu(meta)

    # The sheet is presented through the dialog stack, not page.overlay.
    mock_page.show_dialog.assert_called_once()
    sheet = mock_page.show_dialog.call_args[0][0]

    _delete_tile(sheet).on_click(None)

    # The sheet is named explicitly, so a toast sitting on top of the dialog
    # stack cannot absorb the close the way page.pop_dialog() would let it.
    mock_app.dismiss_dialog.assert_called_once_with(sheet)
    mock_page.pop_dialog.assert_not_called()
    mock_app.confirm_delete_track.assert_not_called()

    # Only once Flutter reports the sheet dismissed does the dialog go up.
    sheet.on_dismiss(None)
    mock_app.confirm_delete_track.assert_called_once_with("/test/song.mp3", "Test Title")

    # And exactly once: a second dismiss must not re-raise it.
    sheet.on_dismiss(None)
    assert mock_app.confirm_delete_track.call_count == 1


@pytest.mark.asyncio
async def test_track_context_menu_close_runs_inline_when_sheet_already_gone():
    """A sheet that is already closed will never emit on_dismiss, so the
    follow-up has to run immediately rather than be stranded."""
    view, mock_app, mock_page = _context_menu_view()
    mock_app.dismiss_dialog.return_value = False

    view._open_track_context_menu({"path": "/test/song.mp3", "track_title": "T"})
    sheet = mock_page.show_dialog.call_args[0][0]
    _delete_tile(sheet).on_click(None)

    mock_app.confirm_delete_track.assert_called_once_with("/test/song.mp3", "T")


@pytest.mark.asyncio
async def test_track_context_menu_delete_requires_path():
    """A track row with no path must close the sheet but raise no confirmation."""
    view, mock_app, mock_page = _context_menu_view()

    view._open_track_context_menu({"track_title": "Orphan", "path": ""})
    sheet = mock_page.show_dialog.call_args[0][0]
    _delete_tile(sheet).on_click(None)

    mock_app.dismiss_dialog.assert_called_once_with(sheet)
    sheet.on_dismiss(None)
    mock_app.confirm_delete_track.assert_not_called()


def test_dismiss_dialog_closes_the_named_dialog():
    """dismiss_dialog must close the dialog it is handed, not consult the stack.

    page.pop_dialog() closes the topmost entry that is still open, and every
    toast goes into that same stack (SnackBar is a DialogControl on 0.86), so a
    toast raised by background work while a confirmation is up would eat the
    close and leave the dialog stranded on screen.
    """
    import main as m

    app = MagicMock()
    app.page = MagicMock()
    dialog = MagicMock()
    dialog.open = True

    assert m.StreamripFletApp.dismiss_dialog(app, dialog) is True
    assert dialog.open is False
    dialog.update.assert_called_once()
    app.page.pop_dialog.assert_not_called()

    # Already closed: nothing to do, and no neighbouring dialog gets touched.
    assert m.StreamripFletApp.dismiss_dialog(app, dialog) is False
    dialog.update.assert_called_once()

    assert m.StreamripFletApp.dismiss_dialog(app, None) is False
