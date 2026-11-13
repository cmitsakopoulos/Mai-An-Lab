"""AI metadata research: validation, caching, accept/dismiss, agent tools.

The network call (`_generate`) is faked with a response shaped like Gemini's
real generateContent output — candidates[0].content.parts + groundingMetadata —
so the parsing path under test is the one production runs."""

import importlib
import json
import os
import re
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


def _answer_of(resp: dict) -> dict:
    """The answer object inside a fake response built by _gemini_response."""
    text = resp["candidates"][0]["content"]["parts"][-1]["text"]
    return json.loads(text.strip("`").removeprefix("json").strip())


@pytest.fixture
def fake_gemini(monkeypatch):
    """Answers per artist name. A one-artist prompt gets the stored response
    as-is (grounding metadata and all); a batch gets one {"artists": [...]}
    reply built from each artist's stored answer."""
    state = {"calls": [], "answers": {}, "raise": None, "kwargs": []}

    async def fake_generate(api_key, model, prompt, *, session=None, **kw):
        state["calls"].append(prompt)
        state["kwargs"].append(kw)
        if state["raise"]:
            raise state["raise"]
        names = re.findall(r"^ARTIST (\d+): (.+)$", prompt, re.MULTILINE)
        if len(names) == 1:
            return state["answers"].get(names[0][1], _gemini_response(_GOOD))
        entries = [{**_answer_of(state["answers"].get(n, _gemini_response(_GOOD))), "id": int(i)}
                   for i, n in names]
        return _gemini_response({"artists": entries}, sources=0)

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
        assert "Live web search snippets:" in fake_gemini["calls"][-1]
        await db.close()



# ── web search + corroboration ──────────────────────────────────────────────

_DDG_HTML = """<table>
<tr><td><a rel="nofollow" href="//duckduckgo.com/y.js?ad_domain=shop.com" class='result-link'>Buy stuff</a></td></tr>
<tr><td class='result-snippet'>An ad.</td></tr>
<tr><td><a rel="nofollow" href="https://en.wikipedia.org/wiki/Mad_Clip" class='result-link'>Mad Clip - Wikipedia</a></td></tr>
<tr><td>&nbsp;</td><td class='result-snippet'><b>Mad</b> Clip was a Greek rapper.</td></tr>
<tr><td><a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fopen.spotify.com%2Fartist%2Fx" class='result-link'>Mad Clip | Spotify</a></td></tr>
<tr><td class='result-snippet'>Listen to Mad Clip on Spotify.</td></tr>
</table>"""


def test_ddg_parser_pairs_links_with_snippets_and_skips_ads():
    res = _ai()._parse_ddg_lite(_DDG_HTML)
    assert [r["uri"] for r in res] == ["https://en.wikipedia.org/wiki/Mad_Clip",
                                       "https://open.spotify.com/artist/x"]
    assert res[0]["snippet"] == "Mad Clip was a Greek rapper."
    assert res[0]["title"] == "Mad Clip - Wikipedia"


def test_search_query_leads_with_the_users_album():
    ev = {"albums": [{"title": "Intro"}, {"title": "Mad Clip"}, {"title": "Ανήκω"}]}
    assert _ai()._search_queries("Mad Clip", ev) == ["Mad Clip Ανήκω album", "Mad Clip music artist"]
    assert _ai()._search_queries("Mad Clip", {}) == ["Mad Clip music artist"]


class _Resp:
    def __init__(self, status, text="", data=None):
        self.status, self._text, self._data = status, text, data or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._text

    async def json(self):
        return self._data


class _Session:
    """Records each request's headers; answers from a per-host script."""
    def __init__(self, ddg, wiki):
        self.ddg, self.wiki, self.headers = ddg, wiki, []

    def post(self, url, **kw):
        self.headers.append(kw.get("headers") or {})
        return self.ddg()

    def get(self, url, **kw):
        self.headers.append(kw.get("headers") or {})
        return self.wiki()


@pytest.mark.asyncio
async def test_fetch_names_a_browser_even_on_a_shared_session():
    # DuckDuckGo 403s aiohttp's default User-Agent; a batch passes its own session.
    sess = _Session(lambda: _Resp(200, _DDG_HTML), lambda: _Resp(500))
    res = await _ai()._fetch_web_snippets("Mad Clip", session=sess)
    assert len(res) == 2
    assert "Mozilla" in sess.headers[0]["User-Agent"]


@pytest.mark.asyncio
async def test_fetch_tells_unreachable_from_empty():
    blocked = _Session(lambda: _Resp(403), lambda: _Resp(503))
    assert await _ai()._fetch_web_snippets("X", session=blocked) is None
    empty = _Session(lambda: _Resp(200, "<html></html>"),
                     lambda: _Resp(200, data={"query": {"search": []}}))
    assert await _ai()._fetch_web_snippets("X", session=empty) == []


_EV = {"artist": "Mad Clip", "albums": [{"title": "Ανήκω", "year": "2019"}],
       "tracks": ["Fuego"]}


def test_on_target_needs_one_of_the_users_titles():
    hit = {"title": "Mad Clip", "snippet": "His album ΑΝΗΚΩ topped the charts."}
    miss = {"title": "Mad Clip", "snippet": "Mad Clip is a clothing brand."}
    # Seen live: an album named after a city matches hotel listings.
    hotel = {"title": "Mad Clip, Ανήκω (updated prices)", "snippet": "Rooms near the metro."}
    assert _ai().on_target_snippets([hit, miss, hotel], _EV) == [hit]


def test_country_basis():
    cb = _ai().country_basis
    greek = [{"title": "Mad Clip", "snippet": "Έλληνας ράπερ, known for Fuego."}]
    us = [{"title": "Mad Clip", "snippet": "American producer from Atlanta."}]
    assert cb("GR", greek, _EV) == "web"
    assert cb("US", greek, {**_EV, "albums": [], "tracks": []}) == "contradicted"
    # Greek titles back GR even when the snippets don't mention a country.
    assert cb("GR", [], _EV) == "script"
    # Greek titles still back GR when an off-script snippet names another country.
    assert cb("GR", us, _EV) == "script"
    assert cb("US", us, _EV) == "web"
    assert cb("FR", [], {"artist": "x", "albums": [], "tracks": []}) == ""
    # Language words back a claim only when no country is named outright.
    nobody = {"artist": "x"}
    assert cb("ES", [{"snippet": "third album by Spanish singer Rosalía"}], nobody) == "web"
    assert cb("ES", [{"snippet": "Spanish-language Mexican singer"}], nobody) == "contradicted"
    # "Latin American" names no country; "UK drill" isn't a British claim.
    assert cb("MX", [{"snippet": "Latin American trap star, UK drill fan"}], {"artist": "x"}) == ""


def test_validate_country_needs_backing():
    v = _ai().validate
    assert v(_GOOD, [{"uri": "u"}], [], allow_memory_country=True, basis="web")["country"] == "GR"
    bad = v(_GOOD, [{"uri": "u"}], [], allow_memory_country=True, basis="contradicted")
    assert bad["country"] == "" and bad["country_withheld"] == "contradicted"
    # Medium confidence from memory alone is no longer enough.
    med = v({**_GOOD, "confidence": "medium"}, [], [], allow_memory_country=True, basis="")
    assert med["country"] == "" and med["country_withheld"] == "unsupported"
    assert v(_GOOD, [], [], allow_memory_country=True, basis="")["country_basis"] == "memory"


@pytest.mark.asyncio
async def test_snippets_naming_another_country_veto_it(fake_gemini, monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "Snik")
        fake_gemini["answers"]["Snik"] = _gemini_response(_GOOD, sources=0)

        async def us_snippets(*a, **k):
            return [{"title": "Snik", "uri": "https://x.com", "snippet": "American rapper from Ohio."}]
        monkeypatch.setattr(_ai(), "_fetch_web_snippets", us_snippets)
        res = await _ai().research_artist(db, "Snik", mode="hybrid")
        assert res["country"] == "" and res["country_withheld"] == "contradicted"
        assert res["genres"] and res["status"] == "ok"
        assert not res["grounded"]  # no snippet names "Snik LP"
        await db.close()


@pytest.mark.asyncio
async def test_empty_answer_while_search_was_down_is_retried(fake_gemini, monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "Nobody", "Other")
        fake_gemini["answers"]["Nobody"] = _gemini_response({"identified": False}, sources=0)

        async def unreachable(*a, **k):
            return None
        monkeypatch.setattr(_ai(), "_fetch_web_snippets", unreachable)
        res = await _ai().research_artist(db, "Nobody")
        assert res["search_failed"] and res["status"] == "unknown"
        await _ai().research_artist(db, "Nobody")
        assert len(fake_gemini["calls"]) == 2  # not served from cache

        summary = await _ai().research_artists(db, ["Nobody"], spacing_s=0)
        assert summary["skipped"] == 0 and len(fake_gemini["calls"]) == 3

        # Search worked and still nothing: settled, never asked again.
        async def empty(*a, **k):
            return []
        monkeypatch.setattr(_ai(), "_fetch_web_snippets", empty)
        await _ai().research_artist(db, "Nobody", force=True)
        summary = await _ai().research_artists(db, ["Nobody"], spacing_s=0)
        assert summary["skipped"] == 1 and len(fake_gemini["calls"]) == 4
        await db.close()


@pytest.mark.asyncio
async def test_research_evidence_needs_no_db(monkeypatch, fake_gemini):
    # The blind eval calls this directly, so it must be the whole pipeline.
    fake_gemini["answers"]["Mad Clip"] = _gemini_response(_GOOD, sources=0)
    res = await _ai().research_evidence(_EV, ["hip hop"], api_key="k", model="m", mode="weights")
    assert res["country"] == "GR" and res["country_basis"] == "script"


# ── batching ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_is_one_call_per_chunk(fake_gemini):
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "A", "B", "C")
        fake_gemini["answers"]["B"] = _gemini_response({"identified": False})
        seen = []
        summary = await _ai().research_artists(
            db, ["A", "B", "C"], spacing_s=0,
            progress=lambda i, t, n, r: seen.append((i, n, r.get("status"))))
        assert len(fake_gemini["calls"]) == 1
        assert fake_gemini["kwargs"][0].get("structured") is True
        assert summary["found"] == 2 and summary["unknown"] == 1
        assert [s for s in seen if s[2] != "searching"] == [
            (1, "A", "ok"), (2, "B", "unknown"), (3, "C", "ok")]
        # Search progress doesn't advance the count.
        assert [s for s in seen if s[2] == "searching"] == [
            (0, "A", "searching"), (0, "B", "searching"), (0, "C", "searching")]
        assert set(await db.get_pending_ai_research()) == {"A", "C"}

        fake_gemini["calls"].clear()
        await _ai().research_artists(db, ["A", "B", "C"], spacing_s=0, force=True, batch_size=2)
        assert len(fake_gemini["calls"]) == 2
        await db.close()


@pytest.mark.asyncio
async def test_artist_missing_from_batch_reply_is_retried(fake_gemini, monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "A", "B")

        async def drops_b(api_key, model, prompt, *, session=None, **kw):
            fake_gemini["calls"].append(prompt)
            return _gemini_response({"artists": [{**_GOOD, "id": 1}]}, sources=0)
        monkeypatch.setattr(_ai(), "_generate", drops_b)
        await _ai().research_artists(db, ["A", "B"], spacing_s=0)
        assert (await db.get_ai_research("B"))["payload"]["status"] == "unparsed"
        await _ai().research_artists(db, ["A", "B"], spacing_s=0)
        assert "ARTIST 1: B" in fake_gemini["calls"][-1]   # A stays cached
        await db.close()


@pytest.mark.asyncio
async def test_google_grounding_is_never_batched(fake_gemini):
    evs = [{"artist": "A"}, {"artist": "B"}]
    res = await _ai().research_evidence_batch(evs, [], api_key="k", model="m",
                                              mode="google_grounding")
    assert len(fake_gemini["calls"]) == 2 and all(r["grounded"] for r in res)
    assert fake_gemini["kwargs"][0] == {"use_grounding": True, "structured": False}


def test_parse_answers_shapes():
    pa = _ai().parse_answers
    assert set(pa('{"artists": [{"id": 2, "identified": true}, {"id": 1}]}', 2)) == {1, 2}
    assert set(pa('```json\n[{"identified": false}, {"identified": true}]\n```', 2)) == {1, 2}
    assert pa('{"identified": true}', 1) == {1: {"identified": True}}
    assert pa('{"identified": true}', 2) == {}           # ambiguous for a batch
    assert pa('{"artists": [{"id": 9}]}', 2) == {}        # out of range
    assert pa("not json", 1) == {}


class _GenSession:
    """Scripted generateContent replies; records each request's payload."""
    def __init__(self, *replies):
        self.replies, self.payloads = list(replies), []

    def post(self, url, json=None, **kw):
        import copy
        self.payloads.append(copy.deepcopy(json))
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_generate_falls_back_when_schema_rejected(monkeypatch):
    sess = _GenSession(_Resp(400, '{"error": "Unknown name \\"propertyOrdering\\""}'),
                       _Resp(200, data={"ok": 1}))
    assert await _ai()._generate("k", "m", "p", session=sess, structured=True) == {"ok": 1}
    assert "responseSchema" in sess.payloads[0]["generationConfig"]
    assert "responseSchema" not in sess.payloads[1]["generationConfig"]


@pytest.mark.asyncio
async def test_generate_waits_once_on_a_per_minute_429(monkeypatch):
    waits = []

    async def no_sleep(s):
        waits.append(s)
    monkeypatch.setattr(_ai().asyncio, "sleep", no_sleep)
    body = '{"error": {"details": [{"retryDelay": "17s"}]}}'
    sess = _GenSession(_Resp(429, body), _Resp(200, data={"ok": 1}))
    assert await _ai()._generate("k", "m", "p", session=sess) == {"ok": 1}
    assert waits == [18]
    # A second 429 (or one with no delay) stops the run.
    sess = _GenSession(_Resp(429, body), _Resp(429, body))
    with pytest.raises(_ai().ResearchUnavailable):
        await _ai()._generate("k", "m", "p", session=sess)


@pytest.mark.asyncio
async def test_agent_sets_many_artists_and_replaces(fake_gemini, monkeypatch):
    """One call corrects a whole scene, and 'set' removes the wrong genre."""
    from utils.llm_tools import execute_tool
    monkeypatch.setattr("utils.metadata_enrich.refresh_walk_models", lambda *a, **k: None)
    with tempfile.TemporaryDirectory() as d:
        db = await _db_with(d, "Moose")
        await db.insert_track_hierarchical({"artist": "Vlospa", "album": "V", "title": "v",
                                            "path": "/m/v.flac", "duration": 200.0})
        await db.upsert_artist_enrichment("Moose", country="GB",
                                          genres=[{"name": "shoegaze", "count": 1}],
                                          source="musicbrainz", status="ok", score=100)
        r = _Runner(db)
        gated = await execute_tool("apply_artist_metadata", {
            "artists": ["moose", "vlospa"], "genres": ["trap", "hip hop"], "country": "GR"}, r)
        assert gated["awaiting_confirmation"]
        await r.confirm()
        for name in ("Moose", "Vlospa"):
            row = await db.get_artist_enrichment(name)
            assert [g["name"] for g in row["genres"]] == ["trap", "hip hop"]
            assert row["country"] == "GR"
        await db.close()
