"""AI metadata research — a grounded web lookup for artists the cascade can't place.

MusicBrainz → files → Qobuz leaves a residue of artists nobody catalogues: the
Greek laiko singer with country but no tags, the drill MC with neither. People
on the web DO know them — Wikipedia, Discogs, label pages, Reddit threads — so
this asks Gemini to search and answer from what it finds.

── Rules that make it safe to put next to the walk ───────────────────────────
  • Suggestions only. Answers land in `artist_ai_research`, never in
    `artist_enrichment`; nothing reaches Auto-Play until the user accepts.
  • Grounded or nothing. One native `generateContent` call with the
    `googleSearch` tool (the OpenAI-compatible endpoint the chat agent uses
    can't ground). An answer with no search sources keeps no country, because
    country is the field `_pool_foreign` fences on.
  • Constrained. Genres are steered to the library's own vocabulary (the NPMI
    model only understands tokens it has seen); country must be a real ISO
    3166 code; "not identified" is always a valid answer and the prompt says
    a blank beats a guess.
  • The library tells the model WHICH artist this is. Several acts share most
    names, so album and track titles go in with the question.
  • Researched once. The cache is consulted before every call; a re-run is an
    explicit choice.

Gemini only for now: a local model has no web access, which makes it weakest
exactly where this is needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Provenance label for fields the user accepted from a research answer.
AI_ORIGIN = "ai"

_MAX_GENRES = 3
_VOCAB_IN_PROMPT = 60
# Gemini's free tier allows a handful of requests per minute; a short gap keeps
# a "research all" run clear of 429s without making a single lookup slow.
_BATCH_SPACING_S = 4.0

# ISO 3166-1 alpha-2, plus XK (Kosovo, user-assigned but universal in music
# catalogues). A model that answers "UK" or "EL" is answering in a dialect the
# walk's country fence would never match.
_ISO_ALPHA2 = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL
BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV
CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD
GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM
IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK
LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW
MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR
PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS
ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY
UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW XK
""".split())
# Common non-ISO answers, folded rather than dropped.
_COUNTRY_ALIASES = {"UK": "GB", "EL": "GR", "EN": "GB"}

_CONFIDENCE = ("high", "medium", "low")


class ResearchUnavailable(RuntimeError):
    """Research can't run at all (no key, bad key, quota). Distinct from an
    artist simply not being found, so a batch stops instead of burning
    through every artist with the same failure."""


# ── configuration ─────────────────────────────────────────────────────────────

def load_research_settings() -> tuple[str, str]:
    """(api_key, model) from the live config. Research uses the Gemini key even
    when the chat agent is set to a local server — a local model can't search."""
    from utils.llm_engine import DEFAULT_GEMINI_MODEL
    try:
        from utils.streamrip_api import load_config
        cfg = load_config() or {}
        acfg = dict(cfg.get("assistant") or {})
    except Exception as exc:
        logger.warning("AI research: couldn't read config: %s", exc)
        acfg = {}
    key = str(acfg.get("gemini_api_key") or "").strip()
    model = str(acfg.get("gemini_model") or "").strip() or DEFAULT_GEMINI_MODEL
    return key, model


def research_available() -> bool:
    return bool(load_research_settings()[0])


# ── prompt ────────────────────────────────────────────────────────────────────

def build_prompt(evidence: dict, vocabulary: List[str]) -> str:
    albums = "; ".join(
        f"{a['title']} ({a['year']})" if a.get("year") else a["title"]
        for a in evidence.get("albums") or []
    ) or "none"
    tracks = "; ".join(evidence.get("tracks") or []) or "none"
    tags = ", ".join(evidence.get("file_tags") or []) or "none"
    known = []
    if evidence.get("known_country"):
        known.append(f"country {evidence['known_country']}")
    if evidence.get("known_area"):
        known.append(f"area {evidence['known_area']}")
    if evidence.get("known_genres"):
        known.append("genres " + ", ".join(evidence["known_genres"]))
    vocab = ", ".join(vocabulary[:_VOCAB_IN_PROMPT]) or "(empty library vocabulary)"
    return f"""You identify music artists for a personal music library. Search the web.

ARTIST: {evidence.get('artist')}
The user's files by this artist (use them to make sure you have the RIGHT artist — many acts share a name):
- Albums: {albums}
- Track titles: {tracks}
- Genre tags in the files: {tags}
- Already known: {'; '.join(known) or 'nothing'}

Find the artist's main genres and country of origin.
- genres: up to {_MAX_GENRES}, lower-case, most specific first. Prefer these words the library already uses: {vocab}. Use a different word only if none of those fit.
- country: ISO 3166-1 alpha-2 code of where the artist is from (e.g. GR, US, GB). For a group, where it formed.
- If the search results don't clearly describe THIS artist, set identified to false. A blank answer is far better than a guess: wrong tags corrupt the user's auto-play.

Reply with ONLY this JSON object, no other text:
{{"identified": true, "matched_as": "the artist as the sources describe them", "summary": "one short sentence, e.g. Greek rapper from Athens, active since 2015", "genres": [], "country": "", "confidence": "high|medium|low"}}"""


# ── call ──────────────────────────────────────────────────────────────────────

async def _generate(api_key: str, model: str, prompt: str, *, session=None,
                    timeout: int = 60) -> dict:
    """One grounded generateContent call → the raw response JSON. Raises
    ResearchUnavailable for key/quota/model problems, RuntimeError otherwise."""
    import aiohttp

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}],
        # No responseMimeType: JSON mode with a search tool isn't supported on
        # every model this key may select, and the parser below copes with
        # fences and stray prose.
        "generationConfig": {"temperature": 0.2},
    }
    own = session is None
    if own:
        session = aiohttp.ClientSession()
    try:
        async with session.post(
            _GENERATE_URL.format(model=model), json=payload,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                text = (await resp.text())[:300]
                logger.error("AI research HTTP %d for model %s: %s", resp.status, model, text)
                if resp.status in (400, 401, 403, 404, 429):
                    raise ResearchUnavailable(_http_reason(resp.status, text, model))
                raise RuntimeError(f"HTTP {resp.status}: {text}")
            return await resp.json()
    finally:
        if own:
            await session.close()


def _http_reason(status: int, text: str, model: str) -> str:
    low = text.lower()
    if status == 429:
        return "Gemini quota reached — try again later."
    if status in (401, 403) or "api key" in low:
        return "Gemini rejected the API key."
    if status == 404 or "not found" in low:
        return f"Model '{model}' isn't available for this key — pick another in Settings → AI Assistant."
    if "google_search" in low or "googlesearch" in low or "tool" in low:
        return f"Model '{model}' can't use Google Search — pick a Gemini 2.5+ model in Settings."
    return f"Gemini returned HTTP {status}."


# ── parsing + validation ─────────────────────────────────────────────────────

def _response_text(data: dict) -> str:
    cands = data.get("candidates") or []
    if not cands:
        return ""
    parts = ((cands[0].get("content") or {}).get("parts")) or []
    # Thinking models may interleave thought parts; keep the answer text only.
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def _grounding(data: dict) -> tuple[List[Dict[str, str]], List[str]]:
    cands = data.get("candidates") or []
    meta = (cands[0].get("groundingMetadata") if cands else None) or {}
    sources: List[Dict[str, str]] = []
    seen: set[str] = set()
    for ch in meta.get("groundingChunks") or []:
        web = ch.get("web") or {}
        uri, title = web.get("uri") or "", web.get("title") or ""
        if not uri or uri in seen:
            continue
        seen.add(uri)
        sources.append({"title": title or uri, "uri": uri})
    queries = [q for q in (meta.get("webSearchQueries") or []) if isinstance(q, str) and q]
    return sources[:8], queries[:4]


def parse_answer(text: str) -> Optional[dict]:
    """The model's JSON object, tolerating code fences and stray prose."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _clean_country(raw: Any) -> str:
    cc = str(raw or "").strip().upper()
    cc = _COUNTRY_ALIASES.get(cc, cc)
    return cc if cc in _ISO_ALPHA2 else ""


def _clean_genres(raw: Any, vocabulary: List[str]) -> tuple[List[str], List[str]]:
    """(genres, new_genres): every kept genre, and the subset the library has
    never used. New words are still offered — 'laiko' may be exactly right for
    a library that has no Greek folk yet — but the UI flags them."""
    from utils.db_manager import _clean_source_tags
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return [], []
    toks = _clean_source_tags(str(g) for g in raw if g)
    vocab = {v.lower() for v in vocabulary}
    genres: List[str] = []
    for t in toks:
        t = t.strip().lower()
        if t and len(t) <= 40 and t not in genres:
            genres.append(t)
    genres = genres[:_MAX_GENRES]
    return genres, [g for g in genres if g not in vocab]


def validate(answer: Optional[dict], sources: List[Dict[str, str]],
             vocabulary: List[str]) -> dict:
    """Reduce a raw model answer to what the workbench may offer."""
    if not answer:
        return {"status": "unparsed", "identified": False, "genres": [],
                "new_genres": [], "country": "", "confidence": "low",
                "summary": "", "matched_as": ""}
    identified = bool(answer.get("identified"))
    genres, new = _clean_genres(answer.get("genres"), vocabulary) if identified else ([], [])
    country = _clean_country(answer.get("country")) if identified else ""
    conf = str(answer.get("confidence") or "").lower()
    conf = conf if conf in _CONFIDENCE else "low"
    grounded = bool(sources)
    if not grounded:
        # Answered from memory. Genres are still offered (low confidence), but
        # never a country — that is the field the walk fences regions on.
        country, conf = "", "low"
    status = "ok" if (genres or country) else "unknown"
    return {
        "status": status,
        "identified": identified,
        "genres": genres,
        "new_genres": new,
        "country": country,
        "confidence": conf,
        "summary": str(answer.get("summary") or "").strip()[:240],
        "matched_as": str(answer.get("matched_as") or "").strip()[:120],
    }


# ── public API ────────────────────────────────────────────────────────────────

async def research_artist(db, artist_name: str, *, api_key: str = "", model: str = "",
                          session=None, force: bool = False,
                          vocabulary: Optional[List[str]] = None) -> dict:
    """Research one artist and cache the answer as a PENDING suggestion.

    Returns the payload: {'artist', 'status' ('ok'|'unknown'|'unparsed'),
    'genres', 'new_genres', 'country', 'confidence', 'summary', 'matched_as',
    'sources' [{title, uri}], 'queries', 'grounded', 'model', 'researched_at'}.
    A cached answer is returned without a call unless `force`. Raises
    ResearchUnavailable when research can't run at all."""
    if not force:
        cached = await db.get_ai_research(artist_name)
        if cached:
            return cached["payload"]
    if not api_key:
        api_key, cfg_model = load_research_settings()
        model = model or cfg_model
    if not api_key:
        raise ResearchUnavailable("Add a Gemini API key in Settings → AI Assistant to research artists.")
    if not model:
        from utils.llm_engine import DEFAULT_GEMINI_MODEL
        model = DEFAULT_GEMINI_MODEL
    if vocabulary is None:
        vocabulary = [v["name"] for v in await db.get_genre_vocabulary(limit=_VOCAB_IN_PROMPT)]

    evidence = await db.get_artist_research_evidence(artist_name)
    data = await _generate(api_key, model, build_prompt(evidence, vocabulary), session=session)
    sources, queries = _grounding(data)
    result = validate(parse_answer(_response_text(data)), sources, vocabulary)
    result.update({
        "artist": artist_name, "sources": sources, "queries": queries,
        "grounded": bool(sources), "model": model, "researched_at": time.time(),
    })
    logger.warning(
        "AI research %s → %s genres=%s country=%s conf=%s sources=%d",
        artist_name, result["status"], result["genres"], result["country"] or "-",
        result["confidence"], len(sources),
    )
    # Unknown answers are cached too (as dismissed): asking again costs a
    # search and will almost always say the same thing.
    await db.save_ai_research(artist_name, result,
                              state="pending" if result["status"] == "ok" else "dismissed")
    return result


async def research_artists(
    db, names: List[str], *, force: bool = False,
    progress: Optional[Callable[[int, int, str, dict], None]] = None,
    cancel_event: Optional[asyncio.Event] = None,
    spacing_s: float = _BATCH_SPACING_S,
) -> dict:
    """Research several artists in turn. Returns a summary:
    {'status': 'ok'|'cancelled'|'error: …', 'total', 'found', 'unknown',
    'skipped', 'failed'}. Stops at the first ResearchUnavailable — a bad key or
    an exhausted quota fails every artist the same way."""
    import aiohttp

    api_key, model = load_research_settings()
    summary = {"status": "ok", "total": len(names), "found": 0, "unknown": 0,
               "skipped": 0, "failed": 0}
    if not api_key:
        summary["status"] = "error: Add a Gemini API key in Settings → AI Assistant."
        return summary
    done_before = set() if force else await db.get_researched_artist_names()
    vocab = [v["name"] for v in await db.get_genre_vocabulary(limit=_VOCAB_IN_PROMPT)]
    called = False
    async with aiohttp.ClientSession() as session:
        for i, name in enumerate(names, 1):
            if cancel_event is not None and cancel_event.is_set():
                summary["status"] = "cancelled"
                break
            if name.lower() in done_before:
                summary["skipped"] += 1
                if progress:
                    progress(i, len(names), name, {"status": "skipped"})
                continue
            if called and spacing_s:
                await asyncio.sleep(spacing_s)
            called = True
            try:
                res = await research_artist(db, name, api_key=api_key, model=model,
                                            session=session, force=True,
                                            vocabulary=vocab)
            except ResearchUnavailable as exc:
                summary["status"] = f"error: {exc}"
                break
            except Exception as exc:
                logger.error("AI research failed for %s: %s", name, exc)
                summary["failed"] += 1
                res = {"status": "error"}
            else:
                summary["found" if res.get("status") == "ok" else "unknown"] += 1
            if progress:
                progress(i, len(names), name, res)
    return summary


async def accept_suggestion(db, artist_name: str, *, genres: Optional[List[str]] = None,
                            country: Optional[str] = None) -> None:
    """Write an accepted suggestion as the user's own answer (sticky, like any
    manual row), labelled 'ai' in provenance. Merges with what the artist
    already has rather than replacing it: a suggestion adds facts."""
    cached = await db.get_ai_research(artist_name)
    payload = (cached or {}).get("payload") or {}
    genres = list(genres if genres is not None else payload.get("genres") or [])
    country = (country if country is not None else payload.get("country")) or None
    cur = await db.get_artist_enrichment(artist_name) or {}
    have = [(g.get("name") if isinstance(g, dict) else str(g)) for g in (cur.get("genres") or [])]
    merged = list(have)
    for g in genres:
        if g and g.lower() not in {h.lower() for h in merged if h}:
            merged.append(g)
    final_country = country or cur.get("country") or None
    old_prov = cur.get("provenance") or {}
    await db.upsert_artist_enrichment(
        artist_name, mbid=cur.get("mbid"), country=final_country, area=cur.get("area"),
        genres=[{"name": g, "count": 1} for g in merged],
        source="manual", status="ok", score=100, force=True,
        # Each field keeps the origin it really has: an AI country only when
        # the AI supplied it; genres are 'ai' since the suggestion added some.
        provenance={
            "country": AI_ORIGIN if country else (old_prov.get("country") if final_country else None),
            "genres": AI_ORIGIN if genres else old_prov.get("genres"),
        },
    )
    if cached:
        await db.set_ai_research_state(artist_name, "accepted")


async def dismiss_suggestion(db, artist_name: str) -> None:
    await db.set_ai_research_state(artist_name, "dismissed")
