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


@pytest.mark.asyncio
async def test_metadata_editor_dialog_lifecycle():
    """Verify MetadataEditorDialog page binding and overlay open/close lifecycle."""
    mock_app = MagicMock()
    mock_page = MagicMock()
    mock_page.overlay = []
    mock_app.page = mock_page

    from ui.player.dialogs import MetadataEditorDialog
    editor = MetadataEditorDialog(mock_app)

    # Dynamic page property resolves live page
    assert editor.page == mock_page

    meta = {"path": "/test/song.mp3", "track_title": "Test Title", "artist_name": "Test Artist", "album_title": "Test Album"}
    editor.open("track", meta)

    mock_page.show_dialog.assert_called_once_with(editor._dlg)

    # Closing dialog cleans up dialog
    editor._close()
    mock_page.pop_dialog.assert_called_once()
