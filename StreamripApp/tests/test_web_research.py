"""Jarvis's web tools: general search, 'tell me about this', related artists
matched against the library — and the library-search genre fix. The network is
faked; what's tested is the routing, fallbacks and library matching."""
import importlib
import os
import tempfile

import pytest


class _Web:
    """Resolved per access: other suites swap modules in sys.modules, and the
    tools import `utils.web_research` at call time."""
    def __getattr__(self, name):
        return getattr(importlib.import_module("utils.web_research"), name)


web = _Web()


def _mod():
    return importlib.import_module("utils.web_research")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    _mod()._cache.clear()
    monkeypatch.setattr(_mod(), "_google_off_reason", None)


def _snips(*texts):
    return [{"title": f"Page {i}", "uri": f"https://ex.com/{i}", "snippet": t}
            for i, t in enumerate(texts)]


@pytest.mark.asyncio
async def test_free_search_by_default_and_cached(monkeypatch):
    calls = []

    async def fake_search(q, **kw):
        calls.append(q)
        return _snips("Slowdive formed in Reading in 1989.")
    monkeypatch.setattr(_mod(), "search_snippets", fake_search)
    monkeypatch.setattr(_mod().metadata_ai, "load_research_mode", lambda: "hybrid")
    res = await web.ask("When did Slowdive form?")
    assert res["success"] and res["backend"] == "web" and res["sources"]
    await web.ask("When did Slowdive form?")
    assert calls == ["When did Slowdive form?"]          # second hit is cached


@pytest.mark.asyncio
async def test_google_when_chosen_and_free_search_when_google_fails(monkeypatch):
    monkeypatch.setattr(_mod().metadata_ai, "load_research_mode", lambda: "google_grounding")
    monkeypatch.setattr(_mod().metadata_ai, "load_research_settings", lambda: ("k", "m"))

    async def grounded(key, model, prompt, **kw):
        assert kw["use_grounding"]
        return {"candidates": [{"content": {"parts": [{"text": "In 1989."}]},
                                "groundingMetadata": {"groundingChunks": [
                                    {"web": {"uri": "https://w.org/x", "title": "w.org"}}]}}]}
    monkeypatch.setattr(_mod().metadata_ai, "_generate", grounded)
    res = await web.ask("When did Slowdive form?")
    assert res["backend"] == "google" and res["answer"] == "In 1989."
    assert res["sources"][0]["uri"] == "https://w.org/x"

    async def no_search_model(*a, **kw):
        raise web.metadata_ai.ResearchUnavailable("Model 'm' can't use Google Search — …")

    async def fake_search(q, **kw):
        return _snips("They formed in 1989.")
    monkeypatch.setattr(_mod().metadata_ai, "_generate", no_search_model)
    monkeypatch.setattr(_mod(), "search_snippets", fake_search)
    res = await web.ask("Another question?")
    assert res["backend"] == "web"
    assert web._google_off_reason                         # not retried every call


@pytest.mark.asyncio
async def test_unreachable_web_reports_an_error(monkeypatch):
    async def none(q, **kw):
        return None
    monkeypatch.setattr(_mod(), "search_snippets", none)
    monkeypatch.setattr(_mod().metadata_ai, "load_research_mode", lambda: "hybrid")
    monkeypatch.setattr(_mod().metadata_ai, "research_available", lambda: False)
    res = await web.ask("x?")
    assert not res["success"] and "reachable" in res["error"]


def test_names_mentioned_is_whole_word_and_skips_noise():
    names = ["Ride", "Air", "Lush", "Pride", "My Bloody Valentine", "Beyoncé", "A, B"]
    text = "Fans of Slowdive also like RIDE, lush and my bloody valentine; also Beyonce. Air."
    assert web.names_mentioned(text, names, exclude=["Slowdive"]) == [
        "Ride", "Lush", "My Bloody Valentine", "Beyoncé"]


async def _db(d, artists):
    from utils.db_manager import DatabaseManager
    db = DatabaseManager(os.path.join(d, "t.db"))
    await db.initialize()
    for i, (artist, genre) in enumerate(artists):
        await db.insert_track_hierarchical({"artist": artist, "album": f"{artist} LP",
                                            "title": f"t{i}", "genre": genre,
                                            "path": f"/m/{i}.flac", "duration": 200.0})
    return db


class _Engine:
    def __init__(self, path=""):
        self.current_path = path


class _Runner:
    def __init__(self, db, path=""):
        self.db, self.engine = db, _Engine(path)
        self._agent_tools_used = []


@pytest.mark.asyncio
async def test_related_artists_split_owned_from_the_rest(monkeypatch):
    from utils.llm_tools import execute_tool

    async def fake_search(q, **kw):
        assert q == "artists similar to Slowdive"
        return _snips("Similar to Slowdive: Ride, Lush, Cocteau Twins, Mojave 3.")
    monkeypatch.setattr(_mod(), "search_snippets", fake_search)
    monkeypatch.setattr(_mod().metadata_ai, "load_research_mode", lambda: "hybrid")
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [("Slowdive", "Shoegaze"), ("Ride", "Shoegaze"),
                           ("Lush", "Dream Pop"), ("Mad Clip", "Trap")])
        r = _Runner(db, path="/m/0.flac")                 # Slowdive is playing
        res = await execute_tool("find_related_artists", {}, r)
        assert res["artist"] == "Slowdive"
        assert [a["name"] for a in res["in_your_library"]] == ["Ride", "Lush"]
        assert res["sources"]                             # the model names the others
        await db.close()


@pytest.mark.asyncio
async def test_learn_about_the_current_track_carries_library_facts(monkeypatch):
    from utils.llm_tools import execute_tool
    seen = {}

    async def fake_ask(question, *, context="", search_query=""):
        seen.update(question=question, sq=search_query, context=context)
        return {"success": True, "backend": "web", "sources": _snips("…")}
    monkeypatch.setattr(_mod(), "ask", fake_ask)
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [("Slowdive", "Shoegaze")])
        await db.upsert_artist_enrichment("Slowdive", country="GB",
                                          genres=[{"name": "shoegaze", "count": 1}],
                                          source="musicbrainz", status="ok", score=100)
        res = await execute_tool("learn_about", {}, _Runner(db, path="/m/0.flac"))
        assert res["success"] and res["title"] == "t0" and res["artist"] == "Slowdive"
        assert res["library"]["artist_country"] == "GB"
        assert seen["sq"] == '"t0" by Slowdive song meaning'
        res = await execute_tool("learn_about", {"subject": "artist", "name": "Ride"},
                                 _Runner(db))
        assert seen["sq"] == "Ride musician biography"
        nothing = await execute_tool("learn_about", {}, _Runner(db))
        assert not nothing["success"]
        await db.close()


@pytest.mark.asyncio
async def test_search_library_genre_filter_keeps_matches():
    """search_tracks_simple rows have no genre: filtering them by genre used to
    drop every result."""
    from utils.llm_tools import execute_tool
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [("Slowdive", "Shoegaze"), ("Mad Clip", "Trap")])
        res = await execute_tool("search_library", {"query": "slowdive", "genre": "shoegaze"},
                                 _Runner(db))
        assert res["count"] == 1 and res["tracks"][0]["artist"] == "Slowdive"
        await db.close()


def test_a_real_zero_energy_is_not_missing():
    from utils.llm_tools import _num
    assert _num(0.0, 0.5) == 0.0 and _num(None, 0.5) == 0.5 and _num("x", 0.5) == 0.5
