"""
Real execution tests for the Jarvis AI-agent tool layer.

The previous tool layer was written against an imagined API and every tool
failed at runtime — uncaught because nothing here executed a tool against a
runner. These tests DO: they drive execute_tool against a real AssistantRunner
with an AsyncMock db and a fake engine that mirrors the REAL attribute surface
(queue list, current_index int, is_playing bool — not a method), so an API
regression fails loudly.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from utils.assistant_runner import AssistantRunner
from utils.llm_engine import LLMEngine
from utils import assistant_intent as ai
from utils import llm_tools
from utils.llm_tools import execute_tool, get_agent_tools, strip_markdown


def _fake_engine(queue=None):
    """Stand-in audio engine mirroring the real attribute surface tools/handlers
    touch. is_playing is a bool ATTRIBUTE (the old tools wrongly called it)."""
    eng = MagicMock()
    eng.queue = list(queue or [])
    eng.current_index = 0
    eng.is_playing = False
    eng.current_track = ""
    eng.current_path = ""
    return eng


def _track(path="/music/a.flac", title="Song A", artist="Artist A", genre="Jazz"):
    return {"path": path, "title": title, "artist": artist,
            "album": "Album A", "duration": 200.0, "genre": genre}


def _runner(db=None, engine=None):
    return AssistantRunner(db or AsyncMock(), engine or _fake_engine())


# ── strip_markdown (TTS-safe spoken text) ────────────────────────────────────

def test_strip_markdown():
    assert strip_markdown("**Now playing** _foo_") == "Now playing foo"
    assert strip_markdown("See [AI Studio](https://x) now") == "See AI Studio now"
    assert "#" not in strip_markdown("# Heading")


# ── schema is OpenAI-compatible lowercase JSON-Schema ────────────────────────

def test_agent_tools_are_lowercase_jsonschema():
    for t in get_agent_tools():
        assert t["parameters"]["type"] == "object"
        for prop in t["parameters"].get("properties", {}).values():
            assert prop["type"] in {"string", "integer", "number", "boolean", "array", "object"}


# ── search_library uses the REAL db method ───────────────────────────────────

@pytest.mark.asyncio
async def test_search_library_uses_real_db_method():
    db = AsyncMock()
    db.search_tracks_simple.return_value = [_track()]
    runner = _runner(db=db)
    res = await execute_tool("search_library", {"query": "song a"}, runner)
    assert res["success"] is True
    assert res["count"] == 1
    assert res["tracks"][0]["path"] == "/music/a.flac"
    db.search_tracks_simple.assert_awaited()


@pytest.mark.asyncio
async def test_search_library_genre_filter():
    db = AsyncMock()
    db.get_all_tracks.return_value = [
        _track(path="/j.flac", genre="Jazz"),
        _track(path="/r.flac", genre="Rock"),
    ]
    runner = _runner(db=db)
    res = await execute_tool("search_library", {"genre": "jazz"}, runner)
    assert res["success"] and res["count"] == 1
    assert res["tracks"][0]["genre"].lower() == "jazz"


# ── get_player_status reads real engine attrs (is_playing not callable) ──────

@pytest.mark.asyncio
async def test_get_player_status_reads_engine_attrs():
    eng = _fake_engine(queue=[{"track_title": "Cur", "artist_name": "A"}])
    eng.is_playing = True
    runner = _runner(engine=eng)
    res = await execute_tool("get_player_status", {}, runner)
    assert res["success"] is True
    assert res["is_playing"] is True
    assert res["queue_length"] == 1
    assert res["currently_playing"]["title"] == "Cur"


# ── play_music: empty queue → runs now, stages queue, defers playback ────────

@pytest.mark.asyncio
async def test_play_music_empty_queue_runs_and_defers_play():
    db = AsyncMock()
    db.search_tracks_simple.return_value = [_track()]
    eng = _fake_engine(queue=[])
    runner = _runner(db=db, engine=eng)
    res = await execute_tool("play_music", {"query": "song a"}, runner)
    assert res["success"] is True
    eng.set_queue.assert_called()
    assert runner._agent_deferred_play is True
    assert runner._pending is None


# ── play_music: busy queue → gated behind confirmation, no destruction ───────

@pytest.mark.asyncio
async def test_play_music_busy_queue_is_gated():
    db = AsyncMock()
    db.search_tracks_simple.return_value = [_track()]
    eng = _fake_engine(queue=[{"path": "/existing.flac"}])
    runner = _runner(db=db, engine=eng)
    res = await execute_tool("play_music", {"query": "song a"}, runner)
    assert res.get("awaiting_confirmation") is True
    assert runner._pending is not None
    assert runner._agent_interrupt is not None
    eng.set_queue.assert_not_called()


# ── enqueue_music: non-destructive on a busy queue ───────────────────────────

@pytest.mark.asyncio
async def test_enqueue_is_non_destructive():
    db = AsyncMock()
    db.search_tracks_simple.return_value = [_track()]
    eng = _fake_engine(queue=[{"path": "/existing.flac"}])
    runner = _runner(db=db, engine=eng)
    res = await execute_tool("enqueue_music", {"query": "song a"}, runner)
    assert res["success"] is True
    eng.queue_last.assert_called()
    eng.set_queue.assert_not_called()
    assert runner._pending is None


# ── playback_control bridges to the transport handler ────────────────────────

@pytest.mark.asyncio
async def test_playback_control_pause():
    eng = _fake_engine(queue=[{"path": "/p"}])
    eng.is_playing = True
    runner = _runner(engine=eng)
    res = await execute_tool("playback_control", {"action": "pause"}, runner)
    assert res["success"] is True
    eng.pause.assert_called()


# ── search_online: download offer is gated; plain search returns results ─────

@pytest.mark.asyncio
async def test_search_online_download_is_gated(monkeypatch):
    async def _fake_search(_q):
        return [{"name": "Hit", "artist": "Band", "media_type": "track",
                 "url": "https://www.qobuz.com/track/1"}]
    monkeypatch.setattr(llm_tools, "_search_qobuz", _fake_search)
    runner = _runner()
    res = await execute_tool("search_online", {"query": "hit", "download": True}, runner)
    assert res.get("awaiting_confirmation") is True
    assert runner._pending is not None


@pytest.mark.asyncio
async def test_search_online_without_download_returns_results(monkeypatch):
    async def _fake_search(_q):
        return [{"name": "Hit", "artist": "Band", "media_type": "track", "url": "u"}]
    monkeypatch.setattr(llm_tools, "_search_qobuz", _fake_search)
    runner = _runner()
    res = await execute_tool("search_online", {"query": "hit"}, runner)
    assert res["success"] is True
    assert res["results"][0]["title"] == "Hit"
    assert runner._pending is None


# ── unified engine parses tool_calls for both string- and object-args ────────

def test_engine_parses_tool_calls():
    raw = [
        {"id": "1", "function": {"name": "play_music", "arguments": '{"query": "x"}'}},
        {"id": "2", "function": {"name": "get_player_status", "arguments": {}}},
        {"function": {"name": "skip", "arguments": ""}},
    ]
    parsed = LLMEngine._parse_tool_calls(raw)
    assert parsed[0] == {"id": "1", "name": "play_music", "args": {"query": "x"}}
    assert parsed[1]["args"] == {}
    assert parsed[2]["id"] == "skip"  # falls back to the function name


# ── Gemini thinking-model thought_signature must be echoed back verbatim ──────

@pytest.mark.asyncio
async def test_agent_echoes_raw_message_thought_signature(monkeypatch):
    """Regression: Gemini 2.5+ returns a thought_signature per functionCall that
    must be sent back unchanged on the follow-up turn, else it 400s. The runner
    must echo the RAW assistant message, not a reconstructed one."""
    import utils.llm_engine as le
    from utils.llm_engine import LLMResponse

    db = AsyncMock()
    db.get_all_tracks.return_value = [_track()]
    runner = _runner(db=db)

    class _Acfg:
        llm_enabled = True
        llm_provider = "gemini"
        gemini_api_key = "AIzaTest"
        gemini_model = "gemini-2.5-flash"
        ollama_endpoint = "http://x/v1"
        ollama_model = "llama3.2"

    class _Cfg:
        assistant = _Acfg()

    monkeypatch.setattr(runner, "_load_assistant_cfg", lambda: _Cfg())
    monkeypatch.setattr(runner, "_history_snapshot", lambda: [])

    raw_msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "steer_mood",
                "arguments": '{"mood": "chill"}',
                "thought_signature": "SIG_ABC",
            },
        }],
    }
    state = {"n": 0, "second_messages": None}

    async def fake_chat(self, messages, tools=None):
        state["n"] += 1
        if state["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[{"id": "call_1", "name": "steer_mood", "args": {"mood": "chill"}}],
                success=True,
                raw_message=raw_msg,
            )
        state["second_messages"] = messages
        return LLMResponse(content="Done, sir.", success=True)

    monkeypatch.setattr(le.LLMEngine, "chat_completion", fake_chat)
    monkeypatch.setattr(le.LLMEngine, "is_configured", lambda self: True)

    intent = ai.Intent(name=ai.INTENT_MOOD_STEER, query="chill", raw="play chill")
    resp = await runner._dispatch_llm(intent)

    assert resp.success
    echoed = [m for m in state["second_messages"]
              if m.get("role") == "assistant" and m.get("tool_calls")]
    assert echoed, "raw assistant tool-call message was not echoed"
    assert echoed[0]["tool_calls"][0]["function"].get("thought_signature") == "SIG_ABC"


# ── Library-knowledge (read) tools ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_library_overview():
    db = AsyncMock()
    db.get_total_tracks.return_value = 100
    db.get_all_artists.return_value = [{"name": "A"}, {"name": "B"}]
    db.get_all_albums.return_value = [{"album": "X"}]
    db.get_all_playlists.return_value = []
    db.get_all_tracks.return_value = [{"genre": "Jazz"}, {"genre": "Jazz"}, {"genre": "Rock"}]
    res = await execute_tool("get_library_overview", {}, _runner(db=db))
    assert res["total_tracks"] == 100
    assert res["artists"] == 2 and res["albums"] == 1 and res["playlists"] == 0
    assert res["top_genres"][0] == {"genre": "Jazz", "count": 2}


@pytest.mark.asyncio
async def test_top_played_preserves_count():
    db = AsyncMock()
    db.get_most_played.return_value = [dict(_track(), count=42)]
    res = await execute_tool("get_top_played", {"limit": 5}, _runner(db=db))
    assert res["success"] and res["tracks"][0]["count"] == 42
    db.get_most_played.assert_awaited_with(limit=5)


@pytest.mark.asyncio
async def test_get_track_details_strips_timbre_blob():
    """The timbre BLOB must never reach the tool result (bytes aren't JSON)."""
    import json
    db = AsyncMock()
    db.get_track_full.return_value = {
        "path": "/p", "title": "T", "artist": "A", "album": "Al", "genre": "G",
        "year": 2001, "duration": 200.0, "bpm": 128.0, "energy": 0.8,
        "timbre": b"\x00\x01BINARY",
    }
    res = await execute_tool("get_track_details", {"path": "/p"}, _runner(db=db))
    assert res["success"] is True
    assert "timbre" not in res["track"]
    assert res["track"]["bpm"] == 128.0
    json.dumps(res)  # must not raise


@pytest.mark.asyncio
async def test_get_playlist_tracks_resolves_name():
    db = AsyncMock()
    db.get_all_playlists.return_value = [{"id": 7, "name": "Chill"}]
    db.get_tracks_in_playlist.return_value = [_track(path="/c1")]
    res = await execute_tool("get_playlist_tracks", {"name": "chill"}, _runner(db=db))
    assert res["success"] and res["playlist"] == "Chill" and res["count"] == 1
    db.get_tracks_in_playlist.assert_awaited_with(7)


# ── Album / playlist playback (gated like play_music) ────────────────────────

@pytest.mark.asyncio
async def test_play_album_empty_queue_runs():
    db = AsyncMock()
    db.get_all_albums.return_value = [{"album": "Kid A", "artist": "Radiohead"}]
    db.get_tracks_by_album.return_value = [_track(path="/1"), _track(path="/2")]
    eng = _fake_engine(queue=[])
    runner = _runner(db=db, engine=eng)
    res = await execute_tool("play_album", {"album": "kid a"}, runner)
    assert res["success"] is True
    eng.set_queue.assert_called()
    assert runner._agent_deferred_play is True
    db.get_tracks_by_album.assert_awaited_with("Kid A", "Radiohead")


@pytest.mark.asyncio
async def test_play_album_busy_queue_is_gated():
    db = AsyncMock()
    db.get_all_albums.return_value = [{"album": "Kid A", "artist": "Radiohead"}]
    db.get_tracks_by_album.return_value = [_track(path="/1")]
    eng = _fake_engine(queue=[{"path": "/existing"}])
    runner = _runner(db=db, engine=eng)
    res = await execute_tool("play_album", {"album": "kid a"}, runner)
    assert res.get("awaiting_confirmation") is True
    assert runner._pending is not None
    eng.set_queue.assert_not_called()
