"""
LLM client engine for Jarvis in Mai An Lab.

Talks to Google Gemini (via its OpenAI-compatible endpoint, user-provided API
key) or a local Ollama / LM Studio server. Both providers speak the same
OpenAI chat-completions dialect, so a single request/response path serves both
— including multi-turn tool (function) calling. Zero hardcoded credentials.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    success: bool = True
    error_message: Optional[str] = None
    # The provider's raw assistant message, echoed back verbatim on the next
    # turn. Gemini "thinking" models (2.5+) embed an opaque thought_signature in
    # each functionCall part that MUST be returned unchanged, or the follow-up
    # request 400s. Reconstructing the message from `tool_calls` drops it, so we
    # keep the original.
    raw_message: Optional[Dict[str, Any]] = None


class LLMEngine:
    def __init__(
        self,
        provider: str = "gemini",
        api_key: str = "",
        model: str = "gemini-2.5-flash",
        ollama_endpoint: str = "http://localhost:11434/v1",
        ollama_model: str = "llama3.2",
    ):
        self.provider = provider
        self.api_key = api_key
        self.model = model or "gemini-2.5-flash"
        self.ollama_endpoint = ollama_endpoint.rstrip("/")
        self.ollama_model = ollama_model or "llama3.2"

    def is_configured(self) -> bool:
        """True if the engine has enough config to attempt an LLM call."""
        if self.provider == "gemini":
            return bool(self.api_key and self.api_key.strip())
        if self.provider == "ollama":
            return bool(self.ollama_endpoint)
        return False

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        """Send a chat-completion (optionally with tools) to the active provider.
        Both Gemini and local servers go through one OpenAI-compatible path so
        tool calling works identically for each."""
        if not self.is_configured():
            return LLMResponse(
                success=False,
                error_message="AI Agent not configured. Set your Gemini API key or local server in Settings → AI Assistant.",
            )

        if self.provider == "gemini":
            url = _GEMINI_URL
            headers = {
                "Authorization": f"Bearer {self.api_key.strip()}",
                "Content-Type": "application/json",
            }
            model = self.model
            timeout = 30
        else:
            url = f"{self.ollama_endpoint}/chat/completions"
            headers = {"Content-Type": "application/json"}
            model = self.ollama_model
            timeout = 45

        return await self._call_openai_compatible(url, headers, model, messages, tools, timeout)

    async def _call_openai_compatible(
        self,
        url: str,
        headers: Dict[str, str],
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        timeout: int,
    ) -> LLMResponse:
        """One OpenAI chat-completions request. `tools` are plain JSON-Schema
        declarations (from llm_tools.get_agent_tools) wrapped in the OpenAI
        function envelope. Parses any returned tool_calls into a uniform shape."""
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {}),
                    },
                }
                for t in tools
            ]

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        logger.error("LLM API error (%d): %s", resp.status, err_text)
                        return LLMResponse(
                            success=False,
                            error_message=f"Provider returned HTTP {resp.status}: {err_text[:200]}",
                        )

                    data = await resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        return LLMResponse(success=False, error_message="Empty response from provider.")

                    message = choices[0].get("message", {})
                    text_content = message.get("content") or ""
                    parsed_tool_calls = self._parse_tool_calls(message.get("tool_calls", []))
                    return LLMResponse(
                        content=text_content,
                        tool_calls=parsed_tool_calls,
                        success=True,
                        raw_message=message,
                    )

        except Exception as e:
            logger.exception("Failed to reach LLM provider: %s", e)
            return LLMResponse(success=False, error_message=f"Network error: {e}")

    @staticmethod
    def _parse_tool_calls(raw_tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalise OpenAI-style tool_calls into {id, name, args} dicts, tolerating
        arguments delivered as a JSON string (Gemini/OpenAI) or an object (some
        local servers)."""
        parsed: List[Dict[str, Any]] = []
        for tc in raw_tool_calls or []:
            fn = tc.get("function", {}) or {}
            fn_name = fn.get("name")
            if not fn_name:
                continue
            fn_args = fn.get("arguments", {})
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args) if fn_args.strip() else {}
                except Exception:
                    fn_args = {}
            if not isinstance(fn_args, dict):
                fn_args = {}
            parsed.append({"id": tc.get("id") or fn_name, "name": fn_name, "args": fn_args})
        return parsed
