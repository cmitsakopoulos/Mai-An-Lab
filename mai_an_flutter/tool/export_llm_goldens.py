#!/usr/bin/env python3
"""Golden values for phase 5 stage b (PORT_MAP rule 3): what the Flet app's
`llm_engine.LLMEngine.chat_completion` and `list_gemini_models` send and
make of the replies, and `llm_tools.get_agent_tools()`.

A local HTTP server stands in for Gemini and Ollama: it answers each
request with the next scripted reply and records what arrived (method,
path and query, the headers the app sets, the body). Every scenario's
inputs and replies are in the file, so test/llm_engine_test.dart replays
them against the port from its own server and compares.

    test/goldens/llm_engine.json   (committed; no personal data)

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_llm_goldens.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(REPO, "StreamripApp"))

from utils import llm_engine  # noqa: E402
from utils.llm_tools import get_agent_tools  # noqa: E402

logging.disable(logging.CRITICAL)

OUT = os.path.join(PORT, "test", "goldens", "llm_engine.json")
JSON = "application/json; charset=UTF-8"
KEEP_HEADERS = ("authorization", "content-type", "x-goog-api-key")


class Recorder:
    def __init__(self):
        self.replies: list[dict] = []
        self.requests: list[dict] = []


REC = Recorder()


class Handler(BaseHTTPRequestHandler):
    def _serve(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8") if n else ""
        REC.requests.append({
            "method": self.command, "path": self.path,
            "headers": {k: self.headers[k] for k in KEEP_HEADERS if self.headers.get(k) is not None},
            "body": body,
        })
        r = REC.replies.pop(0)
        data = r["body"].encode("utf-8")
        self.send_response(r["status"])
        self.send_header("Content-Type", r.get("content_type", JSON))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _serve
    do_POST = _serve

    def log_message(self, *a):
        pass


def reply(obj, status=200, content_type=JSON) -> dict:
    return {"status": status, "content_type": content_type,
            "body": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)}


RAW_ASSISTANT = {
    "role": "assistant", "content": None,
    "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "search_library", "arguments": "{\"query\": \"Αγάπη\"}"},
        "extra_content": {"google": {"thought_signature": "c2lnbmF0dXJl+/="}},
    }],
}

MESSAGES = [
    {"role": "system", "content": "You are Jarvis.\n\nLive context — Now playing: Αγάπη — Χ."},
    {"role": "user", "content": "play something by Björk 🎵"},
    RAW_ASSISTANT,
    {"role": "tool", "tool_call_id": "call_1",
     "content": json.dumps({"success": True, "count": 1, "tracks": [
         {"path": "/m/a.flac", "title": "Αγάπη", "energy": 0.5, "year": 1999}]},
         ensure_ascii=False)},
    {"role": "user", "content": "Answer my request now from the tool results above, briefly."},
]

TOOL_CALLS = [
    {"id": "c1", "type": "function", "function": {"name": "play_music", "arguments": "{\"query\": \"Muse\"}"}},
    {"type": "function", "function": {"name": "enqueue_music", "arguments": {"query": "x", "n": 2}}},
    {"function": {"name": "playback_control", "arguments": "{\"action\": \"skip\"}"}},
    {"function": {"name": "playback_control", "arguments": "{\"action\": \"pause\"}"}, "id": ""},
    {"function": {"name": "web_search", "arguments": "{not json"}},
    {"function": {"name": "get_player_status", "arguments": "   "}},
    {"function": {"name": "list_artists", "arguments": "[1, 2]"}},
    {"function": {"name": "get_top_played"}},
    {"function": {"arguments": "{}"}},
    {"function": None, "id": "z"},
    {"id": "only-id"},
]

SCENARIOS = [
    {"name": "ollama_plain",
     "engine": {"provider": "ollama", "ollama_endpoint": "/v1/", "ollama_model": ""},
     "messages": MESSAGES[:2], "tools": False,
     "replies": [reply({"id": "r", "choices": [{"index": 0, "message": {
         "role": "assistant", "content": "Γεια σου, sir. **Done**."}}]})]},
    {"name": "gemini_tools",
     "engine": {"provider": "gemini", "api_key": "  k-123  ", "model": "gemini-x"},
     "messages": MESSAGES, "tools": True,
     "replies": [reply({"choices": [{"message": {
         "role": "assistant", "content": None, "tool_calls": TOOL_CALLS,
         "extra": {"nested": [1, 2.5, None, True], "s": "ü"}}}]})]},
    {"name": "gemini_default_model",
     "engine": {"provider": "gemini", "api_key": "k", "model": ""},
     "messages": MESSAGES[:2], "tools": True,
     "replies": [reply({"choices": [{"message": {"role": "assistant", "content": "ok",
                                                 "tool_calls": []}}]})]},
    {"name": "ollama_empty_tools_list",
     "engine": {"provider": "ollama", "ollama_endpoint": "/v1", "ollama_model": "qwen"},
     "messages": MESSAGES[:2], "tools": "empty",
     "replies": [reply({"choices": [{"message": {"content": ""}}]})]},
    {"name": "http_400",
     "engine": {"provider": "gemini", "api_key": "k"},
     "messages": MESSAGES[:2], "tools": False,
     "replies": [reply("Σφάλμα " * 60, status=400, content_type="text/plain")]},
    {"name": "http_500_json",
     "engine": {"provider": "ollama", "ollama_endpoint": "/v1"},
     "messages": MESSAGES[:2], "tools": False,
     "replies": [reply({"error": {"message": "model not found"}}, status=500)]},
    {"name": "empty_choices",
     "engine": {"provider": "gemini", "api_key": "k"},
     "messages": MESSAGES[:2], "tools": False,
     "replies": [reply({"choices": []})]},
    {"name": "no_choices_key",
     "engine": {"provider": "gemini", "api_key": "k"},
     "messages": MESSAGES[:2], "tools": False,
     "replies": [reply({"id": "x"})]},
    {"name": "no_message",
     "engine": {"provider": "gemini", "api_key": "k"},
     "messages": MESSAGES[:2], "tools": False,
     "replies": [reply({"choices": [{"finish_reason": "stop"}]})]},
    {"name": "not_configured",
     "engine": {"provider": "gemini", "api_key": "   "},
     "messages": MESSAGES[:2], "tools": True, "replies": []},
    {"name": "unknown_provider",
     "engine": {"provider": "openai", "api_key": "k"},
     "messages": MESSAGES[:2], "tools": True, "replies": []},
    {"name": "wrong_mimetype", "network_error": True,
     "engine": {"provider": "gemini", "api_key": "k"},
     "messages": MESSAGES[:2], "tools": False,
     "replies": [reply("{\"choices\": []}", content_type="text/html")]},
]

MODEL_PAGES = [
    {"models": [
        {"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash",
         "supportedGenerationMethods": ["generateContent", "countTokens"]},
        {"name": "models/gemini-embedding-001", "displayName": "Embedding",
         "supportedGenerationMethods": ["embedContent", "generateContent"]},
        {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemma-3-27b-it", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.0-flash", "displayName": "",
         "supportedGenerationMethods": ["generateContent"]},
    ], "nextPageToken": "tok+/=2"},
    {"models": [
        {"name": "models/gemini-live-2.5", "supportedGenerationMethods": ["generateContent"]},
        {"name": "gemini-3-pro", "displayName": "Gemini 3 Pro", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash", "displayName": "Duplicate",
         "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-1.5-pro", "supportedGenerationMethods": []},
        {"name": "models/gemini-robotics-er", "supportedGenerationMethods": ["generateContent"]},
    ]},
]

MODEL_SCENARIOS = [
    {"name": "two_pages", "api_key": " key-1 ", "replies": [reply(p) for p in MODEL_PAGES]},
    {"name": "forbidden", "api_key": "bad",
     "replies": [reply("{\"error\": {\"code\": 403, \"message\": \"" + "API key not valid. " * 15 + "\"}}",
                       status=403)]},
    {"name": "no_key", "api_key": "  ", "replies": []},
    {"name": "empty", "api_key": "k", "replies": [reply({})]},
]


def _result(r: llm_engine.LLMResponse) -> dict:
    return {"content": r.content, "tool_calls": r.tool_calls, "success": r.success,
            "error_message": r.error_message, "raw_message": r.raw_message}


async def run(base: str) -> dict:
    out = {"tools": get_agent_tools(), "tools_json": json.dumps(get_agent_tools()),
           "chat": [], "models": []}
    llm_engine._GEMINI_URL = base + "/v1beta/openai/chat/completions"
    llm_engine._GEMINI_MODELS_URL = base + "/v1beta/models"
    for sc in SCENARIOS:
        REC.replies = [dict(r) for r in sc["replies"]]
        REC.requests = []
        e = dict(sc["engine"])
        if "ollama_endpoint" in e:
            e["ollama_endpoint"] = base + e["ollama_endpoint"]
        engine = llm_engine.LLMEngine(**e)
        tools = {True: get_agent_tools(), False: None, "empty": []}[sc["tools"]]
        res = await engine.chat_completion(json.loads(json.dumps(sc["messages"])), tools=tools)
        out["chat"].append({**sc, "requests": REC.requests, "result": _result(res)})
    for sc in MODEL_SCENARIOS:
        REC.replies = [dict(r) for r in sc["replies"]]
        REC.requests = []
        try:
            result = {"models": await llm_engine.list_gemini_models(sc["api_key"])}
        except RuntimeError as exc:
            result = {"error": str(exc)}
        out["models"].append({**sc, "requests": REC.requests, "result": result})
    return out


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        data = asyncio.run(run(base))
    finally:
        server.shutdown()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"{OUT}: {len(data['chat'])} chat scenarios, {len(data['models'])} model scenarios")
    for c in data["chat"]:
        print(" ", c["name"], c["result"]["success"], (c["result"]["error_message"] or "")[:70])


if __name__ == "__main__":
    main()
