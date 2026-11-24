#!/usr/bin/env python3
"""Golden values for phase 5 stage e (PORT_MAP rule 3): what the Flet app's
`web_research` (`ask`, `search_snippets`, `related_artists`),
`metadata_ai` (`_parse_ddg_lite`, `_generate`, `_http_reason`,
`_response_text`, `_grounding`) and `llm_tools._web_tool` (`web_search`,
`learn_about`, `find_related_artists`) send and return.

A local HTTP server stands in for DuckDuckGo Lite, Wikipedia and Gemini's
`generateContent` endpoint. Every scenario records the exact HTTP requests
(method, path, selected headers, body) and outputs into:

    test/goldens/web_research.json   (committed; no personal data)

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_web_research_goldens.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(REPO, "StreamripApp"))

from utils import metadata_ai, web_research  # noqa: E402
from utils.db_manager import DatabaseManager  # noqa: E402
from utils.llm_tools import execute_tool  # noqa: E402

logging.disable(logging.CRITICAL)

OUT = os.path.join(PORT, "test", "goldens", "web_research.json")
JSON = "application/json; charset=UTF-8"
HTML = "text/html; charset=utf-8"
KEEP_HEADERS = ("user-agent", "content-type", "x-goog-api-key")


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
            "method": self.command,
            "path": self.path,
            "headers": {
                k: self.headers[k]
                for k in KEEP_HEADERS
                if self.headers.get(k) is not None
            },
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
    return {
        "status": status,
        "content_type": content_type,
        "body": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False),
    }


DDG_SAMPLE_HTML = """<!doctype html>
<html><head><title>DuckDuckGo</title>
<script>var x = '<a class="result-link" href="https://bad.example">nope</a>';</script>
</head><body>
<table>
  <tr><td><a class="result-link" href="//duckduckgo.com/y.js?ad_domain=ads.example">Sponsored Ad</a></td></tr>
  <tr><td class="result-snippet">Buy shoegaze pedals now!</td></tr>

  <tr><td><a rel="nofollow" class="extra result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FSlowdive&amp;rut=123"><b>Slowdive</b> &mdash; Wikipedia</a></td></tr>
  <tr><td class="result-snippet">Slowdive are an English rock band formed in Reading in <b>1989</b>.   They&#x27;re known for &quot;Souvlaki&quot; &amp; shoegaze&hellip;</td></tr>

  <tr><td><a class="result-link" href="https://pitchfork.com/reviews/albums/slowdive/">Slowdive: Souvlaki</a></td></tr>
  <tr><td class="result-snippet">   </td></tr>

  <tr><td><a class="result-link" href="https://example.org/orphan">Orphan link with no snippet</a></td></tr>
  <tr><td><a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fallmusic.com%2Fartist%2F%CE%91%CE%B3%CE%AC%CF%80%CE%B7">AllMusic &nbsp; Bio</a></td></tr>
  <tr><td class="result-snippet">Similar to Slowdive: Ride, Lush, Cocteau Twins &amp; Beyoncé.</td></tr>
</table>
</body></html>
"""

DDG_EMPTY_HTML = "<html><body><table><tr><td>No results</td></tr></table></body></html>"

WIKI_SAMPLE_JSON = {
    "query": {
        "search": [
            {
                "title": "Slowdive (band)",
                "snippet": '<span class="searchmatch">Slowdive</span> formed in Reading in 1989.',
            },
            {
                "title": "Empty Page",
                "snippet": "   <b></b>  ",
            },
            {
                "title": "Μουσική Ελλάδας",
                "snippet": "Greek <b>music</b> history &amp; artists.",
            },
        ]
    }
}

WIKI_EMPTY_JSON = {"query": {"search": []}}

GEMINI_GROUNDED_JSON = {
    "candidates": [
        {
            "content": {
                "parts": [
                    {"text": "internal thinking...", "thought": True},
                    {"text": "Slowdive formed in Reading, England in 1989. "},
                    {"text": "Their album Souvlaki (1993) is a shoegaze landmark."},
                ]
            },
            "groundingMetadata": {
                "groundingChunks": [
                    {"web": {"uri": "https://en.wikipedia.org/wiki/Slowdive", "title": "Slowdive - Wikipedia"}},
                    {"web": {"uri": "https://en.wikipedia.org/wiki/Slowdive", "title": "Duplicate"}},
                    {"web": {"uri": "https://pitchfork.com/slowdive", "title": ""}},
                    {"web": {"uri": "", "title": "No URI"}},
                ],
                "webSearchQueries": ["Slowdive band origin", "", "Souvlaki release year", "q3", "q4", "q5"],
            },
        }
    ]
}

GEMINI_RELATED_JSON = {
    "candidates": [
        {
            "content": {
                "parts": [
                    {"text": "Artists similar to Slowdive include Ride, Lush, Cocteau Twins, and Beyoncé."}
                ]
            },
            "groundingMetadata": {
                "groundingChunks": [
                    {"web": {"uri": "https://last.fm/music/Slowdive/+similar", "title": "Last.fm"}},
                ],
                "webSearchQueries": ["artists similar to Slowdive"],
            },
        }
    ]
}

DDG_PARSE_CASES = [
    {"name": "mixed_ads_entities_orphan", "html": DDG_SAMPLE_HTML},
    {"name": "empty", "html": DDG_EMPTY_HTML},
    {
        "name": "single_quotes_and_unquoted_attrs",
        "html": "<A CLASS='result-link' HREF='https://ex.com/a?x=1&amp;y=2'>Title &#39;A&#39;</A><TD class=result-snippet>Line 1\n\tLine 2</TD>",
    },
    {
        "name": "ddg_empty_uddg",
        "html": '<a class="result-link" href="https://duckduckgo.com/l/?uddg=">Empty</a><td class="result-snippet">Skip me</td>',
    },
]

HTTP_REASON_CASES = [
    {"status": 429, "text": "RESOURCE_EXHAUSTED", "model": "gemini-2.5-flash"},
    {"status": 401, "text": "Unauthorized", "model": "gemini-2.5-flash"},
    {"status": 403, "text": "Forbidden", "model": "gemini-2.5-flash"},
    {"status": 400, "text": "API key not valid", "model": "gemini-2.5-flash"},
    {"status": 404, "text": "Not Found", "model": "gemini-old"},
    {"status": 400, "text": "model not found", "model": "gemini-old"},
    {"status": 400, "text": "Tool googleSearch is not supported", "model": "gemini-lite"},
    {"status": 400, "text": "google_search unavailable", "model": "gemini-lite"},
    {"status": 400, "text": "Bad request", "model": "gemini-2.5-flash"},
]


class _FakeEngine:
    def __init__(self, path=""):
        self.current_path = path


class _FakeRunner:
    def __init__(self, db, path=""):
        self.db = db
        self.engine = _FakeEngine(path)
        self._agent_tools_used = []


async def _build_db(d: str) -> DatabaseManager:
    db = DatabaseManager(os.path.join(d, "t.db"))
    await db.initialize()
    tracks = [
        {"artist": "Slowdive", "album": "Souvlaki", "title": "Alison", "genre": "Shoegaze", "year": 1993, "path": "/m/0.flac", "duration": 231.0},
        {"artist": "Slowdive", "album": "Souvlaki", "title": "When the Sun Hits", "genre": "Shoegaze", "year": 1993, "path": "/m/1.flac", "duration": 287.0},
        {"artist": "Ride", "album": "Nowhere", "title": "Vapour Trail", "genre": "Shoegaze", "year": 1990, "path": "/m/2.flac", "duration": 258.0},
        {"artist": "Lush", "album": "Split", "title": "Desire Lines", "genre": "Dream Pop", "year": 1994, "path": "/m/3.flac", "duration": 457.0},
        {"artist": "Beyoncé", "album": "Lemonade", "title": "Hold Up", "genre": "Pop", "year": 2016, "path": "/m/4.flac", "duration": 221.0},
        {"artist": "Air", "album": "Moon Safari", "title": "Sexy Boy", "genre": "Electronic", "year": 1998, "path": "/m/5.flac", "duration": 298.0},
    ]
    for t in tracks:
        await db.insert_track_hierarchical(t)
    await db.upsert_artist_enrichment(
        "Slowdive",
        country="GB",
        genres=[{"name": "shoegaze", "count": 10}, {"name": "dream pop", "count": 5}],
        source="musicbrainz",
        status="ok",
        score=100,
    )
    return db


async def run(base: str) -> dict:
    # Patch URLs in web_research and metadata_ai by monkeypatching aiohttp session methods
    # via URL rewriting in ClientSession._request, AND patching metadata_ai._GENERATE_URL.
    import aiohttp

    orig_request = aiohttp.ClientSession._request

    async def _rewritten_request(self, method, str_or_url, **kwargs):
        url = str(str_or_url)
        if url.startswith("https://lite.duckduckgo.com"):
            url = base + "/ddg" + url[len("https://lite.duckduckgo.com"):]
        elif url.startswith("https://en.wikipedia.org"):
            url = base + "/wiki" + url[len("https://en.wikipedia.org"):]
        return await orig_request(self, method, url, **kwargs)

    aiohttp.ClientSession._request = _rewritten_request
    metadata_ai._GENERATE_URL = base + "/v1beta/models/{model}:generateContent"

    # Control monotonic clock & asyncio.sleep for deterministic cache & 429 tests.
    fake_clock = [1000.0]
    web_research.time.monotonic = lambda: fake_clock[0]
    orig_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def _fake_sleep(dt):
        sleeps.append(dt)

    metadata_ai.asyncio.sleep = _fake_sleep

    # Active config state
    cfg = {"mode": "hybrid", "key": "", "model": "gemini-2.5-flash"}
    metadata_ai.load_research_mode = lambda: cfg["mode"]
    metadata_ai.load_research_settings = lambda: (cfg["key"], cfg["model"])
    metadata_ai.research_available = lambda: bool(cfg["key"])

    out: dict = {
        "ddg_parse": [
            {"name": c["name"], "html": c["html"], "result": metadata_ai._parse_ddg_lite(c["html"])}
            for c in DDG_PARSE_CASES
        ],
        "http_reasons": [
            {**c, "result": metadata_ai._http_reason(c["status"], c["text"], c["model"])}
            for c in HTTP_REASON_CASES
        ],
        "ask_scenarios": [],
        "tool_scenarios": [],
    }

    def reset_state(mode="hybrid", key="", model="gemini-2.5-flash", t=1000.0):
        web_research._cache.clear()
        web_research._google_off_reason = None
        cfg.update(mode=mode, key=key, model=model)
        fake_clock[0] = t
        sleeps.clear()

    # ── ask scenarios ────────────────────────────────────────────────────────
    ask_defs = [
        {
            "name": "ddg_default_and_cache_ttl",
            "config": {"mode": "hybrid", "key": "", "model": "gemini-2.5-flash"},
            "steps": [
                {
                    "time": 1000.0,
                    "args": {"question": "When did Slowdive form?"},
                    "replies": [reply(DDG_SAMPLE_HTML, content_type=HTML)],
                },
                {
                    "time": 1000.0 + 1799.0,
                    "args": {"question": "when did slowdive form?"},
                    "replies": [],  # cache hit
                },
                {
                    "time": 1000.0 + 1800.0,
                    "args": {"question": "When did Slowdive form?"},
                    "replies": [reply(DDG_SAMPLE_HTML, content_type=HTML)],  # expired
                },
            ],
        },
        {
            "name": "ddg_403_wiki_fallback",
            "config": {"mode": "hybrid", "key": "", "model": "gemini-2.5-flash"},
            "steps": [
                {
                    "time": 1000.0,
                    "args": {"question": "Slowdive band", "search_query": "Slowdive & shoegaze"},
                    "replies": [
                        reply("Forbidden", status=403, content_type="text/plain"),
                        reply(WIKI_SAMPLE_JSON),
                    ],
                },
            ],
        },
        {
            "name": "web_empty_falls_back_to_google",
            "config": {"mode": "hybrid", "key": "k-secret", "model": "gemini-2.5-flash"},
            "steps": [
                {
                    "time": 1000.0,
                    "args": {"question": "When did Slowdive form?", "context": "artist_country: GB"},
                    "replies": [
                        reply(DDG_EMPTY_HTML, content_type=HTML),
                        reply(WIKI_EMPTY_JSON),
                        reply(GEMINI_GROUNDED_JSON),
                    ],
                },
            ],
        },
        {
            "name": "google_grounding_mode_and_latch",
            "config": {"mode": "google_grounding", "key": "k-1", "model": "gemini-lite"},
            "steps": [
                {
                    "time": 1000.0,
                    "args": {"question": "When did Slowdive form?"},
                    "replies": [reply(GEMINI_GROUNDED_JSON)],
                },
                {
                    "time": 1010.0,
                    "args": {"question": "Who produced Souvlaki?"},
                    "replies": [
                        reply("Tool googleSearch is not supported", status=400, content_type="text/plain"),
                        reply(DDG_SAMPLE_HTML, content_type=HTML),
                    ],
                },
                {
                    "time": 1020.0,
                    "args": {"question": "Third question after latch?"},
                    "replies": [
                        # Gemini must NOT be called again!
                        reply(DDG_SAMPLE_HTML, content_type=HTML),
                    ],
                },
            ],
        },
        {
            "name": "google_429_retry_then_quota_latch",
            "config": {"mode": "google_grounding", "key": "k-2", "model": "gemini-2.5-flash"},
            "steps": [
                {
                    "time": 1000.0,
                    "args": {"question": "First retry question?"},
                    "replies": [
                        reply('{"error":{"details":[{"retryDelay":"2.5s"}]}}', status=429),
                        reply(GEMINI_GROUNDED_JSON),
                    ],
                },
                {
                    "time": 1010.0,
                    "args": {"question": "Second quota exhausted?"},
                    "replies": [
                        reply('{"error":{"message":"Quota exceeded"}}', status=429),
                        reply(DDG_SAMPLE_HTML, content_type=HTML),
                    ],
                },
                {
                    "time": 1020.0,
                    "args": {"question": "Third skips google?"},
                    "replies": [reply(DDG_SAMPLE_HTML, content_type=HTML)],
                },
            ],
        },
        {
            "name": "web_empty_not_cached_and_unreachable",
            "config": {"mode": "hybrid", "key": "", "model": "gemini-2.5-flash"},
            "steps": [
                {
                    "time": 1000.0,
                    "args": {"question": "Obscure query"},
                    "replies": [
                        reply(DDG_EMPTY_HTML, content_type=HTML),
                        reply(WIKI_EMPTY_JSON),
                    ],
                },
                {
                    "time": 1005.0,
                    "args": {"question": "Obscure query"},
                    "replies": [
                        reply("err", status=500, content_type="text/plain"),
                        reply("err", status=503, content_type="text/plain"),
                    ],
                },
                {
                    "time": 1010.0,
                    "args": {"question": "   "},
                    "replies": [],
                },
            ],
        },
    ]

    for sc in ask_defs:
        reset_state(**sc["config"])
        recorded_steps = []
        for st in sc["steps"]:
            fake_clock[0] = st["time"]
            REC.replies = [dict(r) for r in st["replies"]]
            REC.requests = []
            sleeps.clear()
            res = await web_research.ask(**st["args"])
            recorded_steps.append({
                **st,
                "requests": REC.requests,
                "sleeps": list(sleeps),
                "google_off_reason": web_research._google_off_reason,
                "result": res,
            })
        out["ask_scenarios"].append({
            "name": sc["name"],
            "config": sc["config"],
            "steps": recorded_steps,
        })

    # ── execute_tool web scenarios ───────────────────────────────────────────
    with tempfile.TemporaryDirectory() as d:
        db = await _build_db(d)
        tool_defs = [
            {
                "name": "web_search_empty",
                "config": {"mode": "hybrid", "key": "", "model": "gemini-2.5-flash"},
                "current_path": "",
                "tool": "web_search",
                "args": {"query": "   "},
                "replies": [],
            },
            {
                "name": "web_search_ok",
                "config": {"mode": "hybrid", "key": "", "model": "gemini-2.5-flash"},
                "current_path": "",
                "tool": "web_search",
                "args": {"query": "Slowdive Souvlaki"},
                "replies": [reply(DDG_SAMPLE_HTML, content_type=HTML)],
            },
            {
                "name": "learn_about_current_track_google",
                "config": {"mode": "google_grounding", "key": "k-1", "model": "gemini-2.5-flash"},
                "current_path": "/m/0.flac",
                "tool": "learn_about",
                "args": {},
                "replies": [reply(GEMINI_GROUNDED_JSON)],
            },
            {
                "name": "learn_about_current_album_same_name_case",
                "config": {"mode": "google_grounding", "key": "k-1", "model": "gemini-2.5-flash"},
                "current_path": "/m/0.flac",
                "tool": "learn_about",
                "args": {"subject": "album", "name": "souvlaki"},
                "replies": [reply(GEMINI_GROUNDED_JSON)],
            },
            {
                "name": "learn_about_other_album",
                "config": {"mode": "hybrid", "key": "", "model": "gemini-2.5-flash"},
                "current_path": "/m/0.flac",
                "tool": "learn_about",
                "args": {"subject": "album", "name": "Pygmalion"},
                "replies": [reply(DDG_SAMPLE_HTML, content_type=HTML)],
            },
            {
                "name": "learn_about_explicit_artist",
                "config": {"mode": "hybrid", "key": "", "model": "gemini-2.5-flash"},
                "current_path": "/m/0.flac",
                "tool": "learn_about",
                "args": {"subject": "artist", "name": "Ride"},
                "replies": [reply(DDG_SAMPLE_HTML, content_type=HTML)],
            },
            {
                "name": "learn_about_nothing_playing",
                "config": {"mode": "hybrid", "key": "", "model": "gemini-2.5-flash"},
                "current_path": "",
                "tool": "learn_about",
                "args": {},
                "replies": [],
            },
            {
                "name": "find_related_artists_current_ddg",
                "config": {"mode": "hybrid", "key": "", "model": "gemini-2.5-flash"},
                "current_path": "/m/0.flac",
                "tool": "find_related_artists",
                "args": {},
                "replies": [reply(DDG_SAMPLE_HTML, content_type=HTML)],
            },
            {
                "name": "find_related_artists_explicit_google",
                "config": {"mode": "google_grounding", "key": "k-1", "model": "gemini-2.5-flash"},
                "current_path": "",
                "tool": "find_related_artists",
                "args": {"artist": "Slowdive"},
                "replies": [reply(GEMINI_RELATED_JSON)],
            },
            {
                "name": "find_related_artists_nothing_playing",
                "config": {"mode": "hybrid", "key": "", "model": "gemini-2.5-flash"},
                "current_path": "",
                "tool": "find_related_artists",
                "args": {},
                "replies": [],
            },
        ]

        for sc in tool_defs:
            reset_state(**sc["config"])
            REC.replies = [dict(r) for r in sc["replies"]]
            REC.requests = []
            runner = _FakeRunner(db, path=sc["current_path"])
            res = await execute_tool(sc["tool"], dict(sc["args"]), runner)
            out["tool_scenarios"].append({
                **sc,
                "requests": REC.requests,
                "result": res,
            })
        await db.close()

    metadata_ai.asyncio.sleep = orig_sleep
    aiohttp.ClientSession._request = orig_request
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
    print(
        f"{OUT}: {len(data['ddg_parse'])} ddg_parse, "
        f"{len(data['http_reasons'])} http_reasons, "
        f"{len(data['ask_scenarios'])} ask_scenarios, "
        f"{len(data['tool_scenarios'])} tool_scenarios"
    )


if __name__ == "__main__":
    main()
