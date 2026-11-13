"""General web lookups for Jarvis — the metadata researcher's web search, opened
up to any music question.

Two backends, the same two the artist researcher uses (utils/metadata_ai.py):

  • Google, through Gemini's googleSearch grounding — a written answer with the
    pages it came from. Used when Settings → AI Assistant → Research paradigm is
    'Google grounding', and as the fallback when the free search is unreachable.
  • DuckDuckGo Lite snippets (Wikipedia search if DDG is blocked) — free, no key.
    The agent model reads the snippets itself.

Gemini can't combine its googleSearch tool with function calling in one
request, so a web lookup is a separate request the agent calls as a tool.

Callers get one shape back:
    {"success", "backend": "google"|"web", "answer"?, "sources": [{title, uri,
     snippet?}], "query"} — or {"success": False, "error"}.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Optional

from utils import metadata_ai

logger = logging.getLogger(__name__)

_MAX_RESULTS = 6
_TIMEOUT_S = 4.0
_CACHE_TTL_S = 30 * 60
_cache: Dict[tuple, tuple[float, dict]] = {}
# Set once Google grounding fails for a reason that won't change this session
# (model can't search, quota): stop paying a round trip for it every call.
_google_off_reason: Optional[str] = None


def _cached(key: tuple) -> Optional[dict]:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    return None


def _store(key: tuple, value: dict) -> dict:
    if value.get("success"):
        _cache[key] = (time.monotonic(), value)
        if len(_cache) > 200:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
    return value


async def search_snippets(query: str, *, limit: int = _MAX_RESULTS,
                          session=None, timeout: float = _TIMEOUT_S) -> Optional[List[Dict[str, str]]]:
    """[{'title', 'uri', 'snippet'}] for any query: DuckDuckGo Lite, then
    Wikipedia search. [] = searched, nothing found; None = nothing reachable."""
    import aiohttp
    import urllib.parse

    own = session is None
    if own:
        session = aiohttp.ClientSession()
    reached = False
    out: List[Dict[str, str]] = []
    try:
        try:
            async with session.post(
                "https://lite.duckduckgo.com/lite/", data={"q": query},
                headers={"User-Agent": metadata_ai._BROWSER_UA},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 200:
                    reached = True
                    out = metadata_ai._parse_ddg_lite(await resp.text())[:limit]
                else:
                    logger.debug("DuckDuckGo HTTP %d for %r", resp.status, query)
        except Exception as exc:
            logger.debug("DuckDuckGo search failed for %r: %s", query, exc)

        if not out:
            try:
                url = ("https://en.wikipedia.org/w/api.php?action=query&list=search"
                       f"&srsearch={urllib.parse.quote(query)}&format=json&srlimit={limit}")
                async with session.get(url, headers={"User-Agent": metadata_ai._WIKI_UA},
                                       timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 200:
                        reached = True
                        data = await resp.json()
                        for item in (data.get("query", {}).get("search") or [])[:limit]:
                            snip = re.sub(r"<[^<]+?>", "", item.get("snippet", "")).strip()
                            title = item.get("title", "")
                            if snip:
                                out.append({
                                    "title": f"{title} - Wikipedia",
                                    "uri": "https://en.wikipedia.org/wiki/"
                                           + urllib.parse.quote(title.replace(" ", "_")),
                                    "snippet": snip,
                                })
            except Exception as exc:
                logger.debug("Wikipedia search failed for %r: %s", query, exc)
    finally:
        if own:
            await session.close()
    return out if reached else None


async def _ask_google(question: str, context: str) -> dict:
    """A grounded answer from Gemini + Google Search. Raises
    metadata_ai.ResearchUnavailable when Google can't be used."""
    global _google_off_reason
    key, model = metadata_ai.load_research_settings()
    if not key:
        raise metadata_ai.ResearchUnavailable("No Gemini API key set.")
    if _google_off_reason:
        raise metadata_ai.ResearchUnavailable(_google_off_reason)
    prompt = (
        "Search the web and answer for a music listener, in at most 6 short sentences. "
        "State facts plainly (names, years, places); say so if the sources disagree or "
        "you can't find it. No markdown headings.\n"
        + (f"Context: {context}\n" if context else "")
        + f"Question: {question}"
    )
    try:
        data = await metadata_ai._generate(key, model, prompt, use_grounding=True, timeout=45)
    except metadata_ai.ResearchUnavailable as exc:
        msg = str(exc)
        if "can't use Google Search" in msg or "quota" in msg.lower():
            _google_off_reason = msg
        raise
    answer = metadata_ai._response_text(data).strip()
    sources, queries = metadata_ai._grounding(data)
    if not answer:
        raise metadata_ai.ResearchUnavailable("Google returned no answer.")
    return {"success": True, "backend": "google", "answer": answer,
            "sources": sources, "searched": queries}


async def ask(question: str, *, context: str = "", search_query: str = "") -> dict:
    """Answer a question from the web. `search_query` is what the free search
    sends (defaults to the question); `context` steers the Google answer."""
    q = (question or "").strip()
    if not q:
        return {"success": False, "error": "Empty question."}
    sq = (search_query or q).strip()
    mode = metadata_ai.load_research_mode()
    key = ("ask", q.lower(), sq.lower(), context, mode)
    if (hit := _cached(key)) is not None:
        return hit

    want_google = mode == "google_grounding"
    if want_google:
        try:
            return _store(key, {**await _ask_google(q, context), "query": sq})
        except Exception as exc:
            logger.info("Google lookup unavailable (%s); using free web search.", exc)

    snippets = await search_snippets(sq)
    if snippets:
        return _store(key, {"success": True, "backend": "web", "query": sq,
                            "sources": snippets,
                            "note": "Search snippets — answer from them and name the site."})
    if not want_google and metadata_ai.research_available():
        # The free search is blocked or empty: try Google once before giving up.
        try:
            return _store(key, {**await _ask_google(q, context), "query": sq})
        except Exception as exc:
            logger.info("Google fallback unavailable: %s", exc)
    if snippets == []:
        return {"success": True, "backend": "web", "query": sq, "sources": [],
                "note": "The web search found nothing for this."}
    return {"success": False, "error": "The web isn't reachable right now."}


# ── related artists ──────────────────────────────────────────────────────────

def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).casefold()
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def names_mentioned(text: str, names: Iterable[str], *, exclude: Iterable[str] = ()) -> List[str]:
    """Which of `names` appear in `text` as whole words (case/accent-blind).
    Names shorter than 3 characters, or ones that are common words, are
    skipped — 'Air' or 'Yes' would match every page."""
    hay = " " + re.sub(r"[^\w&']+", " ", _fold(text)) + " "
    skip = {_fold(x) for x in exclude}
    found: Dict[str, int] = {}
    for name in names:
        n = _fold(name).strip()
        if len(n) < 3 or n in skip or n in _COMMON_WORDS or "," in name:
            continue
        needle = " " + re.sub(r"[^\w&']+", " ", n).strip() + " "
        at = hay.find(needle) if needle.strip() else -1
        if at >= 0 and name not in found:
            found[name] = at
    # In the order the page names them: the first named are the closest.
    return sorted(found, key=found.get)


_COMMON_WORDS = frozenset({
    "the", "and", "you", "air", "yes", "low", "live", "love", "music", "band", "rock",
    "pop", "jazz", "soul", "blues", "home", "light", "moon", "sun", "fire", "rain",
    "girls", "boys", "angel", "angels", "eden", "ghost", "heart", "hope", "kids",
    "lights", "free", "true", "best", "more", "like", "artists", "similar", "also",
    "fans", "songs", "album", "albums", "new", "one", "two", "three", "various artists",
})


async def related_artists(artist: str, library_artists: List[Dict[str, Any]]) -> dict:
    """Artists the web calls similar to `artist`, split into the ones this
    library holds (playable now) and the raw sources for the rest."""
    a = (artist or "").strip()
    if not a:
        return {"success": False, "error": "No artist given."}
    res = await ask(f"Which artists are similar to {a}? List names.",
                    search_query=f"artists similar to {a}",
                    context=f"The listener likes {a} and wants similar artists.")
    if not res.get("success"):
        return res
    text = " ".join([res.get("answer") or ""] + [
        f"{s.get('title', '')} {s.get('snippet', '')}" for s in res.get("sources") or []])
    by_name = {r["name"]: r for r in library_artists if r.get("name")}
    owned = names_mentioned(text, by_name, exclude=[a])
    return {
        **res,
        "artist": a,
        "in_your_library": [{"name": n, "track_count": by_name[n].get("track_count")}
                            for n in owned[:15]],
    }
