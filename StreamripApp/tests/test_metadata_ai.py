"""AI metadata research: validation, caching, accept/dismiss, agent tools.

The network call (`_generate`) is faked with a response shaped like Gemini's
real generateContent output — candidates[0].content.parts + groundingMetadata —
so the parsing path under test is the one production runs."""

import importlib
import json
import os
import tempfile

import pytest

from utils.db_manager import DatabaseManager


def _ai():
    # Resolved per test: other suites swap modules in sys.modules.
    return importlib.import_module("utils.metadata_ai")


def _gemini_response(answer: dict | str, *, sources=2, fenced=True):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    if fenced:
        text = f"```json\n{text}\n```"
    chunks = [{"web": {"uri": f"https://redirect/{i}", "title": f"site{i}.org"}}
              for i in range(sources)]
    return {"candidates": [{
        "content": {"role": "model", "parts": [
            {"text": "thinking…", "thought": True},
            {"text": text},
        ]},
        "groundingMetadata": {"groundingChunks": chunks,
                              "webSearchQueries": ["artist greek rapper"]},
    }]}


_GOOD = {"identified": True, "matched_as": "Mad Clip (Greek rapper)",
         "summary": "Greek rapper from Athens.", "genres": ["Hip Hop", "trap", "drill"],
         "country": "GR", "confidence": "high"}


async def _db_with(tmpdir, *names):
    db = DatabaseManager(os.path.join(tmpdir, "test.db"))
    await db.initialize()
    conn = await db.get_connection()
    for i, n in enumerate(names, 1):
        await conn.execute(
            "INSERT INTO artists (id, name, track_count) VALUES (?, ?, ?)", (i, n, 10 - i))
        await conn.execute(
            "INSERT INTO albums (id, artist_id, title, year) VALUES (?, ?, ?, ?)",
            (i, i, f"{n} LP", "2020"))
    await conn.commit()
    return db


@pytest.fixture
def fake_gemini(monkeypatch):
    state = {"calls": [], "answers": {}, "raise": None}

    async def fake_generate(api_key, model, prompt, *, session=None, timeout=60):
        state["calls"].append(prompt)
        if state["raise"]:
            raise state["raise"]
        for name, resp in state["answers"].items():
            if f"ARTIST: {name}\n" in prompt:
                return resp
        return _gemini_response(_GOOD)

    monkeypatch.setattr(_ai(), "_generate", fake_generate)
    monkeypatch.setattr(_ai(), "load_research_settings", lambda: ("k", "gemini-test"))
    async def fake_snippets(*a, **k):
        return []
    monkeypatch.setattr(_ai(), "_fetch_web_snippets", fake_snippets)
    return state


# ── validation ──────────────────────────────────────────────────────────────

def test_validate_keeps_grounded_answer_and_flags_new_genres():
    v = _ai().validate(_GOOD, [{"title": "a", "uri": "u"}], ["hip hop", "rock"])
    assert v["status"] == "ok"
    assert v["country"] == "GR"
    assert v["genres"] == ["hip hop", "trap", "drill"]
    assert v["new_genres"] == ["trap", "drill"]


def test_validate_withholds_country_without_sources():
    v = _ai().validate(_GOOD, [], ["hip hop"])
    assert v["country"] == ""
    assert v["confidence"] == "low"
    assert v["genres"]  # genres still offered


def test_validate_country_codes():
    m = _ai()
    assert m.validate({**_GOOD, "country": "UK"}, [{"uri": "u"}], [])["country"] == "GB"
    assert m.validate({**_GOOD, "country": "Greece"}, [{"uri": "u"}], [])["country"] == ""
    assert m.validate({**_GOOD, "country": "ZZ"}, [{"uri": "u"}], [])["country"] == ""


def test_validate_unidentified_is_unknown():
    v = _ai().validate({**_GOOD, "identified": False}, [{"uri": "u"}], [])
    assert v["status"] == "unknown"
    assert not v["genres"] and not v["country"]


def test_validate_caps_genres_and_bad_confidence():
    v = _ai().validate({**_GOOD, "genres": ["a", "b", "c", "d"], "confidence": "certain"},
                       [{"uri": "u"}], [])
    assert len(v["genres"]) == 3
    assert v["confidence"] == "low"


def test_parse_answer_tolerates_prose_and_fences():
    m = _ai()
    assert m.parse_answer('Here you go:\n```json\n{"identified": true}\n```')["identified"]
    assert m.parse_answer("no json here") is None
    assert m.parse_answer("{broken") is None


def test_thought_parts_are_ignored():
    text = _ai()._response_text(_gemini_response(_GOOD))
    assert "thinking" not in text


# ── research + cache ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_research_caches_pending_and_never_writes_enrichment(fake_gemini):
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "Mad Clip")
        res = await _ai().research_artist(db, "Mad Clip")
        assert res["status"] == "ok" and res["grounded"]
        assert len(res["sources"]) == 2
        assert "Mad Clip LP (2020)" in fake_gemini["calls"][0]  # evidence in prompt
        assert await db.get_artist_enrichment("Mad Clip") is None
        assert "Mad Clip" in await db.get_pending_ai_research()
        # Cached: no second call.
        await _ai().research_artist(db, "Mad Clip")
        assert len(fake_gemini["calls"]) == 1
        await db.close()


@pytest.mark.asyncio
async def test_unknown_answer_cached_as_dismissed(fake_gemini):
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "Nobody")
        fake_gemini["answers"]["Nobody"] = _gemini_response({"identified": False})
        res = await _ai().research_artist(db, "Nobody")
        assert res["status"] == "unknown"
        assert (await db.get_ai_research("Nobody"))["state"] == "dismissed"
        assert "Nobody" not in await db.get_pending_ai_research()
        await db.close()


@pytest.mark.asyncio
async def test_batch_skips_researched_and_stops_on_unavailable(fake_gemini):
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "A", "B", "C")
        await _ai().research_artist(db, "A")
        seen = []
        summary = await _ai().research_artists(
            db, ["A", "B", "C"], spacing_s=0,
            progress=lambda i, t, n, r: seen.append((n, r.get("status"))))
        assert summary["skipped"] == 1 and summary["found"] == 2
        assert seen[0] == ("A", "skipped")

        fake_gemini["raise"] = _ai().ResearchUnavailable("Gemini quota reached")
        summary = await _ai().research_artists(db, ["X1", "X2"], spacing_s=0)
        assert summary["status"].startswith("error") and "quota" in summary["status"]
        await db.close()


@pytest.mark.asyncio
async def test_no_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(_ai(), "load_research_settings", lambda: ("", "m"))
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "A")
        with pytest.raises(_ai().ResearchUnavailable):
            await _ai().research_artist(db, "A")
        summary = await _ai().research_artists(db, ["A"], spacing_s=0)
        assert summary["status"].startswith("error")
        await db.close()


# ── accept / dismiss ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_accept_merges_and_labels_ai(fake_gemini):
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "Mad Clip")
        await db.upsert_artist_enrichment("Mad Clip", genres=[{"name": "rap", "count": 1}],
                                          status="ok")
        await _ai().research_artist(db, "Mad Clip")
        await _ai().accept_suggestion(db, "Mad Clip")
        row = await db.get_artist_enrichment("Mad Clip")
        names = [g["name"] for g in row["genres"]]
        assert names[0] == "rap" and "trap" in names  # merged, not replaced
        assert row["country"] == "GR"
        assert row["source"] == "manual"
        assert row["provenance"] == {"country": "ai", "genres": "ai"}
        assert (await db.get_ai_research("Mad Clip"))["state"] == "accepted"
        assert not await db.get_pending_ai_research()
        gaps = await db.get_metadata_gap_artists()
        assert all(g["artist_name"] != "Mad Clip" for g in gaps)
        await db.close()


@pytest.mark.asyncio
async def test_accept_without_ai_country_keeps_mb_provenance(fake_gemini):
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "A")
        await db.upsert_artist_enrichment("A", country="FR", status="ok",
                                          provenance={"country": "musicbrainz"})
        fake_gemini["answers"]["A"] = _gemini_response({**_GOOD, "country": ""})
        await _ai().research_artist(db, "A")
        await _ai().accept_suggestion(db, "A")
        row = await db.get_artist_enrichment("A")
        assert row["country"] == "FR"
        assert row["provenance"] == {"country": "musicbrainz", "genres": "ai"}
        await db.close()


@pytest.mark.asyncio
async def test_manual_answer_hides_pending_suggestion(fake_gemini):
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "A")
        await _ai().research_artist(db, "A")
        await db.set_manual_artist_enrichment("A", genres=["laiko"], country="GR")
        assert not await db.get_pending_ai_research()
        await db.close()


# ── agent tools ─────────────────────────────────────────────────────────────

class _Runner:
    def __init__(self, db):
        self.db = db
        self._agent_tools_used = []
        self.confirm = None

    def agent_request_confirmation(self, spoken, displayed, on_yes):
        self.confirm = on_yes
        return {"success": True, "awaiting_confirmation": True, "message": displayed}


@pytest.mark.asyncio
async def test_agent_research_then_gated_apply(fake_gemini, monkeypatch):
    from utils.llm_tools import execute_tool
    monkeypatch.setattr("utils.metadata_enrich.refresh_walk_models", lambda *a, **k: None)
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "Mad Clip")
        r = _Runner(db)
        res = await execute_tool("research_artist_metadata", {"artist": "mad clip"}, r)
        assert res["success"] and res["country"] == "GR"
        assert await db.get_artist_enrichment("Mad Clip") is None

        status = await execute_tool("get_metadata_status", {}, r)
        assert status["ai_suggestions_pending"] == ["Mad Clip"]

        gated = await execute_tool("apply_artist_metadata", {"artist": "Mad Clip"}, r)
        assert gated["awaiting_confirmation"]
        assert await db.get_artist_enrichment("Mad Clip") is None  # nothing yet
        reply = await r.confirm()
        assert "Mad Clip" in reply.displayed
        row = await db.get_artist_enrichment("Mad Clip")
        assert row["country"] == "GR" and row["provenance"]["genres"] == "ai"
        await db.close()


@pytest.mark.asyncio
async def test_agent_apply_rejects_bad_country_and_unknown_artist(fake_gemini):
    from utils.llm_tools import execute_tool
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "A")
        r = _Runner(db)
        bad = await execute_tool("apply_artist_metadata", {"artist": "A", "country": "Greece"}, r)
        assert not bad["success"]
        missing = await execute_tool("get_artist_metadata", {"artist": "Zzz"}, r)
        assert not missing["success"]
        nothing = await execute_tool("apply_artist_metadata", {"artist": "A"}, r)
        assert not nothing["success"] and r.confirm is None
        await db.close()


def test_validate_allows_memory_country_when_enabled():
    # When allow_memory_country=True and confidence is high, keep country even without web sources
    v = _ai().validate(_GOOD, [], ["hip hop"], allow_memory_country=True)
    assert v["status"] == "ok"
    assert v["country"] == "GR"
    assert v["confidence"] == "high"

    # When confidence is low in memory mode, drop country to protect Auto-Play regional fencing
    v_low = _ai().validate({**_GOOD, "confidence": "low"}, [], ["hip hop"], allow_memory_country=True)
    assert v_low["country"] == ""
    assert v_low["confidence"] == "low"


@pytest.mark.asyncio
async def test_research_dual_paradigm_weights_mode(fake_gemini, monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "Snik")
        # In weights mode, no web chunks in response
        fake_gemini["answers"]["Snik"] = _gemini_response(_GOOD, sources=0)
        res = await _ai().research_artist(db, "Snik", mode="weights")
        assert res["status"] == "ok"
        assert res["origin_mode"] == "weights"
        assert res["country"] == "GR"
        assert "internal training knowledge" in fake_gemini["calls"][-1]
        await db.close()


@pytest.mark.asyncio
async def test_research_dual_paradigm_snippets_mode(fake_gemini, monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "Snik")
        fake_gemini["answers"]["Snik"] = _gemini_response(_GOOD, sources=0)
        async def mock_snippets(*a, **k):
            return [{"title": "Snik Bio", "uri": "https://example.com/snik", "snippet": "Greek trap artist."}]
        monkeypatch.setattr(_ai(), "_fetch_web_snippets", mock_snippets)
        res = await _ai().research_artist(db, "Snik", mode="web_snippets")
        assert res["status"] == "ok"
        assert res["origin_mode"] == "web_snippets"
        assert len(res["sources"]) == 1
        assert "LIVE WEB SEARCH SNIPPETS" in fake_gemini["calls"][-1]
        await db.close()

