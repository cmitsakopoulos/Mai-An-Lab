import os
import pytest
import asyncio
from utils.config import ConfigData, AssistantConfig
from utils.llm_engine import LLMEngine
from utils.llm_tools import get_agent_tools, execute_tool
from utils.assistant_runner import AssistantRunner
from utils import assistant_intent as ai


def test_config_assistant_section():
    cfg = ConfigData.defaults()
    assert hasattr(cfg, "assistant")
    assert isinstance(cfg.assistant, AssistantConfig)
    assert cfg.assistant.llm_enabled is True
    assert cfg.assistant.gemini_api_key == ""
    assert cfg.assistant.llm_provider == "gemini"


def test_llm_engine_configuration():
    engine_unconfigured = LLMEngine(provider="gemini", api_key="")
    assert engine_unconfigured.is_configured() is False

    engine_configured = LLMEngine(provider="gemini", api_key="AIzaSyDummyTestKey123")
    assert engine_configured.is_configured() is True


def test_agent_tools_schema():
    tools = get_agent_tools()
    tool_names = [t["name"] for t in tools]
    for expected in (
        "search_library", "play_music", "enqueue_music", "play_similar",
        "steer_mood", "playback_control", "get_player_status", "search_online",
    ):
        assert expected in tool_names, expected

    # Schemas must be OpenAI-compatible lowercase JSON-Schema, not native-Gemini
    # uppercase type enums (the bug the previous version shipped).
    for t in tools:
        params = t["parameters"]
        assert params["type"] == "object"
        for prop in params.get("properties", {}).values():
            assert prop["type"] in {"string", "integer", "number", "boolean", "array", "object"}


@pytest.mark.asyncio
async def test_assistant_runner_unconfigured_fallback():
    class DummyEngine:
        pass

    class DummyDB:
        pass

    runner = AssistantRunner(db_manager=DummyDB(), audio_engine=DummyEngine())
    intent = ai.Intent(name="play_now", query="Jazz", raw="play Jazz")
    
    # When unconfigured, dispatching complex intent falls back to deterministic resolution
    response = await runner.dispatch(intent)
    assert response is not None
