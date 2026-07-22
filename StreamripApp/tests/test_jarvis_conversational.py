import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock

from utils import assistant_intent as ai
from utils.assistant_runner import AssistantRunner, AssistantResponse, PendingChoice, ChoiceOption


def test_intent_parsing_conversational_features():
    # Choice selection
    intent = ai.parse("1")
    assert intent.name == ai.INTENT_CHOICE_SELECT
    assert intent.query == "1"

    intent = ai.parse("second")
    assert intent.name == ai.INTENT_CHOICE_SELECT
    assert intent.query == "second"

    intent = ai.parse("option 3")
    assert intent.name == ai.INTENT_CHOICE_SELECT
    assert intent.query == "3"

    # Queue remove
    intent = ai.parse("remove track 2 from queue")
    assert intent.name == ai.INTENT_QUEUE_REMOVE
    assert intent.query == "2"

    intent = ai.parse("remove Echoes")
    assert intent.name == ai.INTENT_QUEUE_REMOVE
    assert intent.query == "Echoes"

    # Queue move
    intent = ai.parse("move Echoes to top")
    assert intent.name == ai.INTENT_QUEUE_MOVE
    assert intent.query == "Echoes"

    # Save queue
    intent = ai.parse("save queue as playlist Chill Vibe")
    assert intent.name == ai.INTENT_SAVE_QUEUE
    assert intent.query == "Chill Vibe"

    # Mood steer
    intent = ai.parse("play something chill")
    assert intent.name == ai.INTENT_MOOD_STEER
    assert intent.query == "chill"

    # Track info
    intent = ai.parse("tell me about this track")
    assert intent.name == ai.INTENT_TRACK_INFO


@pytest.mark.asyncio
async def test_pending_choice_resolution():
    db = AsyncMock()
    engine = MagicMock()
    engine.queue = []
    runner = AssistantRunner(db, engine)

    selected_payload = None

    async def on_select(opt: ChoiceOption):
        nonlocal selected_payload
        selected_payload = opt.payload
        return AssistantResponse(spoken="Chosen", displayed="Chosen")

    opts = [
        ChoiceOption(id="1", title="Echoes — Pink Floyd", payload={"artist": "Pink Floyd"}),
        ChoiceOption(id="2", title="Echoes — Young the Giant", payload={"artist": "Young the Giant"}),
    ]

    runner.queue_choice(PendingChoice(
        prompt="Pick one",
        options=opts,
        on_select_callback=on_select
    ))

    # User replies "2"
    intent = ai.parse("2")
    res = await runner.dispatch(intent)
    assert res.spoken == "Chosen"
    assert selected_payload == {"artist": "Young the Giant"}
    assert runner._pending_choice is None


@pytest.mark.asyncio
async def test_queue_remove_and_move():
    db = AsyncMock()
    engine = MagicMock()
    engine.queue = [
        {"track_title": "Track 1", "artist_name": "Artist 1", "path": "/p1"},
        {"track_title": "Track 2", "artist_name": "Artist 2", "path": "/p2"},
        {"track_title": "Track 3", "artist_name": "Artist 3", "path": "/p3"},
    ]
    engine.current_index = 0
    runner = AssistantRunner(db, engine)

    # Remove track 2
    intent = ai.parse("remove track 2 from queue")
    res = await runner.dispatch(intent)
    assert res.success is True
    assert len(engine.queue) == 2
    assert engine.queue[1]["track_title"] == "Track 3"

    # Move track 3 to top
    intent = ai.parse("move Track 3 to top")
    res = await runner.dispatch(intent)
    assert res.success is True
    assert engine.queue[1]["track_title"] == "Track 3"


@pytest.mark.asyncio
async def test_track_info_handler():
    db = AsyncMock()
    engine = MagicMock()
    engine.current_track = {
        "track_title": "Comfortably Numb",
        "artist_name": "Pink Floyd",
        "album_title": "The Wall",
        "duration": 382.0
    }
    runner = AssistantRunner(db, engine)

    intent = ai.parse("tell me about this song")
    res = await runner.dispatch(intent)
    assert res.success is True
    assert "Comfortably Numb" in res.spoken
    assert "Pink Floyd" in res.spoken
    assert "The Wall" in res.spoken


@pytest.mark.asyncio
async def test_save_current_walk():
    db = AsyncMock()
    db.create_playlist.return_value = 42
    engine = MagicMock()
    engine.queue = [
        {"path": "/p1", "title": "Old Track"},
        {"path": "/p2", "title": "Walk Track 1"},
        {"path": "/p3", "title": "Walk Track 2"},
    ]
    engine.current_index = 1
    runner = AssistantRunner(db, engine)

    intent = ai.parse("save current walk as Chill Vibe")
    res = await runner.dispatch(intent)
    assert res.success is True
    assert "Chill Vibe" in res.displayed
    assert db.add_track_to_playlist.call_count == 2
    db.add_track_to_playlist.assert_any_call(42, "/p2")
    db.add_track_to_playlist.assert_any_call(42, "/p3")
