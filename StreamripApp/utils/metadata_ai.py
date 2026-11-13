"""AI metadata research — a grounded web lookup for artists the cascade can't place.

MusicBrainz → files → Qobuz leaves a residue of artists nobody catalogues: the
Greek laiko singer with country but no tags, the drill MC with neither. People
on the web DO know them — Wikipedia, Discogs, label pages, Reddit threads — so
this asks Gemini to search and answer from what it finds.

── Rules that make it safe to put next to the walk ───────────────────────────
  • Suggestions only. Answers land in `artist_ai_research`, never in
    `artist_enrichment`; nothing reaches Auto-Play until the user accepts.
  • Country needs backing. Country decides an artist's culture in auto-play,
    so the model's own confidence isn't enough: the web snippets it was shown
    must name that country, or the user's titles must be in a script only
    that country writes (Greek → GR/CY). Snippets naming only OTHER countries
    veto it. Memory alone keeps a country only in memory modes at high
    confidence. Google-grounded answers (paid tier) are trusted as before.
  • Search is ours, not Gemini's. The free tier can't use the googleSearch
    tool, so DuckDuckGo Lite (then Wikipedia) is queried here and the
    snippets go into the prompt. A snippet only counts as grounding when it
    names one of the user's album or track titles.
  • Constrained. Genres are steered to the library's own vocabulary (the NPMI
    model only understands tokens it has seen); country must be a real ISO
    3166 code; "not identified" is always a valid answer and the prompt says
    a blank beats a guess.
  • The library tells the model WHICH artist this is. Several acts share most
    names, so album and track titles go in with the question.
  • Researched once. The cache is consulted before every call; a re-run is an
    explicit choice. The exception: an empty answer given while search was
    unreachable is retried, since it never saw the web.
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

# DuckDuckGo Lite answers 403 to aiohttp's default User-Agent, so every request
# names a browser — including on a session the caller passed in.
_BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
# Wikimedia asks API clients to identify themselves.
_WIKI_UA = "StreamripApp/2.0 (personal music library metadata)"
_MAX_SNIPPETS = 4

# Titles too generic to prove a web page is about THIS artist.
_GENERIC_TITLES = frozenset({
    "intro", "outro", "interlude", "skit", "live", "remix", "remixes", "untitled",
    "greatest hits", "best of", "the best of", "singles", "demo", "acoustic",
})

# Country names and demonyms as they appear in English bios, normalised the same
# way as snippet text (see _norm). Language words that name many countries'
# artists ("english", "spanish", "arabic") and genre prefixes ("uk drill") are
# left out: they'd contradict correct answers.
_COUNTRY_WORDS = {
    "GR": ("greek", "greece", "hellenic", "ελληνας", "ελληνιδα", "ελληνικ", "ελλαδα"),
    "CY": ("cypriot", "cyprus", "κυπρ"),
    "US": ("american", "united states", "usa"),
    "GB": ("british", "united kingdom", "england", "scottish", "scotland", "welsh", "wales"),
    "IE": ("irish", "ireland"), "CA": ("canadian", "canada"),
    "AU": ("australian", "australia"), "NZ": ("new zealand",),
    "FR": ("france",), "DE": ("german", "germany"), "IT": ("italian", "italy"),
    "ES": ("spain", "spaniard"), "PT": ("portugal",), "NL": ("dutch", "netherlands"),
    "BE": ("belgian", "belgium"), "AT": ("austrian", "austria"), "CH": ("swiss", "switzerland"),
    "SE": ("swedish", "sweden"), "NO": ("norwegian", "norway"), "DK": ("danish", "denmark"),
    "FI": ("finnish", "finland"), "IS": ("icelandic", "iceland"), "PL": ("polish", "poland"),
    "CZ": ("czech",), "HU": ("hungarian", "hungary"), "RO": ("romanian", "romania"),
    "BG": ("bulgarian", "bulgaria"), "RS": ("serbian", "serbia"), "HR": ("croatian", "croatia"),
    "AL": ("albanian", "albania"), "MK": ("north macedonia",), "TR": ("turkish", "turkey"),
    "RU": ("russian", "russia"), "UA": ("ukrainian", "ukraine"), "IL": ("israeli", "israel"),
    "LB": ("lebanese", "lebanon"), "EG": ("egyptian", "egypt"), "MA": ("moroccan", "morocco"),
    "DZ": ("algerian", "algeria"), "NG": ("nigerian", "nigeria"), "GH": ("ghanaian", "ghana"),
    "ZA": ("south african",), "JM": ("jamaican", "jamaica"), "BR": ("brazilian", "brazil"),
    "AR": ("argentine", "argentinian", "argentina"), "MX": ("mexican", "mexico"),
    "CO": ("colombian", "colombia"), "PR": ("puerto rican", "puerto rico"),
    "CU": ("cuban", "cuba"), "JP": ("japanese", "japan"), "KR": ("korean", "korea"),
    "CN": ("chinese", "china"), "IN": ("india",),
}
# Language words: they back a claim only when no country above is named, since
# "Spanish-language Mexican singer" is Mexican.
_COUNTRY_LANGUAGE_WORDS = {
    "ES": ("spanish", "espanol", "espanola"), "GB": ("english",),
    "FR": ("french", "francais", "francaise"), "PT": ("portuguese",),
}
# A title match only counts on a page that's about music — an album called
# "Αθήνα" also matches every Athens hotel listing.
_MUSIC_CUES = ("album", "song", "single", "track", "singer", "rapper", "band", "music",
               "musician", "record", "lyrics", "discography", "ep", "mixtape", "spotify",
               "τραγουδ", "αλμπουμ", "δισκ", "ραπερ", "μουσικ", "cancion", "chanson")
_MUSIC_CUE_RE = re.compile(r"(?<!\w)(" + "|".join(_MUSIC_CUES) + ")")

# Phrases that contain a demonym without naming the artist's country.
_COUNTRY_NOISE = ("latin american", "african american", "north american", "south american",
                  "greek mythology", "greek god")

# A script in the user's own titles that only a few countries write in.
_SCRIPT_COUNTRIES = (
    (re.compile(r"[Ͱ-Ͽἀ-῿]"), {"GR", "CY"}),
    (re.compile(r"[가-힯]"), {"KR"}),
    (re.compile(r"[぀-ヿ]"), {"JP"}),
    (re.compile(r"[֐-׿]"), {"IL"}),
    (re.compile(r"[฀-๿]"), {"TH"}),
    (re.compile(r"[Ⴀ-ჿ]"), {"GE"}),
    (re.compile(r"[԰-֏]"), {"AM"}),
)


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


def load_research_mode() -> str:
    """Active research paradigm: 'hybrid' (default), 'weights', 'web_snippets', or 'google_grounding'."""
    try:
        from utils.streamrip_api import load_config
        cfg = load_config() or {}
        acfg = dict(cfg.get("assistant") or {})
    except Exception as exc:
        logger.warning("AI research: couldn't read config for mode: %s", exc)
        acfg = {}
    return str(acfg.get("research_mode") or "hybrid").strip().lower()


def research_available() -> bool:
    return bool(load_research_settings()[0])


# ── web snippet searcher (free tier open grounding) ───────────────────────────

def _search_queries(artist: str, evidence: Optional[dict]) -> List[str]:
    """The artist plus their biggest album first — a bare name finds whichever
    act of that name is most famous — then the bare name as a fallback."""
    queries = []
    own = _norm(artist)
    for a in (evidence or {}).get("albums") or []:
        title = str(a.get("title") or "").strip()
        t = _norm(title)
        if len(t) >= 3 and t != own and t not in _GENERIC_TITLES:
            queries.append(f"{artist} {title} album")
            break
    queries.append(f"{artist} music artist")
    return queries


def _parse_ddg_lite(html: str) -> List[Dict[str, str]]:
    """Results from a DuckDuckGo Lite page: each `result-link` anchor paired
    with the `result-snippet` cell that follows it. Ads are skipped. Standard
    library only — bs4 isn't bundled in the phone build."""
    import urllib.parse
    from html.parser import HTMLParser

    class _Lite(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.out: List[Dict[str, str]] = []
            self.current: Optional[Dict[str, str]] = None
            self.capture: Optional[str] = None   # "title" | "snippet"
            self.buf: List[str] = []

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            classes = (a.get("class") or "").split()
            if tag == "a" and "result-link" in classes:
                href = a.get("href") or ""
                if href.startswith("//"):
                    href = "https:" + href
                parsed = urllib.parse.urlparse(href)
                if "duckduckgo.com" in parsed.netloc:
                    target = urllib.parse.parse_qs(parsed.query).get("uddg")
                    href = target[0] if target else ""   # /y.js etc. are ads
                self.current = {"uri": href} if href else None
                self.capture, self.buf = "title", []
            elif tag == "td" and "result-snippet" in classes and self.current:
                self.capture, self.buf = "snippet", []

        def handle_endtag(self, tag):
            if (self.capture == "title" and tag == "a") or (self.capture == "snippet" and tag == "td"):
                text = " ".join("".join(self.buf).split())
                if self.current is not None:
                    self.current[self.capture] = text
                    if self.capture == "snippet":
                        if text:
                            self.out.append(self.current)
                        self.current = None
                self.capture = None

        def handle_data(self, data):
            if self.capture:
                self.buf.append(data)

    parser = _Lite()
    try:
        parser.feed(html or "")
    except Exception as exc:
        logger.debug("DuckDuckGo page didn't parse: %s", exc)
    return [{"title": r.get("title", ""), "uri": r["uri"], "snippet": r["snippet"]}
            for r in parser.out]


async def _fetch_web_snippets(
    artist: str,
    evidence: Optional[dict] = None,
    session=None,
    timeout: float = 3.5,
) -> Optional[List[Dict[str, str]]]:
    """Web search snippets for an artist from DuckDuckGo Lite, falling back to
    Wikipedia search. Returns [{'title', 'uri', 'snippet'}] — [] when the
    searches ran and found nothing, None when no search could be reached
    (blocked, offline), so the caller can tell "nobody knows them" from
    "we never looked"."""
    import aiohttp
    import urllib.parse

    snippets: List[Dict[str, str]] = []
    reached = False
    own = session is None
    if own:
        session = aiohttp.ClientSession()
    try:
        for q in _search_queries(artist, evidence):
            try:
                async with session.post(
                    "https://lite.duckduckgo.com/lite/", data={"q": q},
                    headers={"User-Agent": _BROWSER_UA},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status != 200:
                        logger.debug("DuckDuckGo HTTP %d for %r", resp.status, q)
                        break   # blocked: a second query only digs deeper
                    reached = True
                    snippets = _parse_ddg_lite(await resp.text())[:_MAX_SNIPPETS]
            except Exception as exc:
                logger.debug("DuckDuckGo snippet lookup failed for %s: %s", artist, exc)
                break
            if snippets:
                break

        if not snippets:
            try:
                wiki_url = ("https://en.wikipedia.org/w/api.php?action=query&list=search"
                            f"&srsearch={urllib.parse.quote(artist + ' music')}&format=json")
                async with session.get(wiki_url, headers={"User-Agent": _WIKI_UA},
                                       timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 200:
                        reached = True
                        data = await resp.json()
                        for item in (data.get("query", {}).get("search") or [])[:3]:
                            clean_s = re.sub(r"<[^<]+?>", "", item.get("snippet", "")).strip()
                            title = item.get("title", "")
                            if clean_s:
                                snippets.append({
                                    "title": f"{title} - Wikipedia",
                                    "uri": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title)}",
                                    "snippet": clean_s,
                                })
            except Exception as exc:
                logger.debug("Wikipedia snippet fallback failed for %s: %s", artist, exc)
    finally:
        if own:
            await session.close()

    return snippets if reached else None


# ── corroboration ─────────────────────────────────────────────────────────────
# The model's own confidence is a poor gate for country — the field Auto-Play's
# regional fence acts on. These checks ask whether the text the model was shown,
# or the user's own titles, actually back its answer.

def _norm(s: Any) -> str:
    """Lower-case, accent-free, punctuation-free: 'Έλληνας' → 'ελληνας'."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^\w]+", " ", s).split())


def _domain(uri: str) -> str:
    import urllib.parse
    host = urllib.parse.urlparse(uri or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def on_target_snippets(snippets: List[Dict[str, str]], evidence: dict) -> List[Dict[str, str]]:
    """Snippets that name one of the user's album or track titles on a page
    about music — evidence it's THIS artist, not another act (or a hotel)
    with the same name."""
    own = _norm(evidence.get("artist"))
    titles = set()
    for t in [a.get("title") for a in evidence.get("albums") or []] + list(evidence.get("tracks") or []):
        n = _norm(t)
        if len(n) >= 4 and n != own and n not in _GENERIC_TITLES:
            titles.add(n)
    hits = []
    for s in snippets:
        text = f" {_norm(s.get('title', '') + ' ' + s.get('snippet', ''))} "
        if any(f" {t} " in text for t in titles) and _MUSIC_CUE_RE.search(text):
            hits.append(s)
    return hits


def _words_found(text: str, table: Dict[str, tuple]) -> set[str]:
    found = set()
    for cc, words in table.items():
        for w in words:
            # Normalised like the text: casefold turns a final ς into σ.
            pat = r"(?<!\w)" + re.escape(_norm(w)) + (r"(?!\w)" if w == "usa" else "")
            if re.search(pat, text):
                found.add(cc)
                break
    return found


def _countries_named(text: str) -> set[str]:
    """Countries a text names outright; failing those, the ones its language
    words point to."""
    text = _norm(text)
    for phrase in _COUNTRY_NOISE:
        text = text.replace(phrase, " ")
    return _words_found(text, _COUNTRY_WORDS) or _words_found(text, _COUNTRY_LANGUAGE_WORDS)


def country_basis(country: str, snippets: List[Dict[str, str]], evidence: dict) -> str:
    """Why a claimed country can be trusted: 'web' (the snippets name it),
    'script' (the user's titles are in a script only that country writes),
    'contradicted' (the snippets name other countries and not this one), or ''
    (nothing either way)."""
    if not country:
        return ""
    on_target = on_target_snippets(snippets, evidence)
    pool = on_target or snippets
    named = set()
    for s in pool:
        named |= _countries_named(f"{s.get('title', '')} {s.get('snippet', '')}")
    if country in named:
        return "web"
    titles = " ".join([str(a.get("title") or "") for a in evidence.get("albums") or []]
                      + [str(t) for t in evidence.get("tracks") or []])
    for pattern, ccs in _SCRIPT_COUNTRIES:
        if country in ccs and pattern.search(titles):
            return "script"
    return "contradicted" if named else ""


# ── prompt ────────────────────────────────────────────────────────────────────

def _artist_block(i: int, evidence: dict, snippets: List[Dict[str, str]],
                  show_snippets: bool) -> str:
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
    lines = [
        f"ARTIST {i}: {evidence.get('artist')}",
        f"- Albums: {albums}",
        f"- Track titles: {tracks}",
        f"- Genre tags in the files: {tags}",
        f"- Already known: {'; '.join(known) or 'nothing'}",
    ]
    if evidence.get("disputed"):
        lines.append(f"- The library's current answer may be wrong: {evidence['disputed']}. "
                     "Check it rather than repeating it.")
    if show_snippets:
        if snippets:
            lines.append("- Live web search snippets:")
            lines += [f"  - [{_domain(s.get('uri', '')) or 'web'}] {s.get('title', '')}: "
                      f"{s.get('snippet', '')}" for s in snippets]
        else:
            lines.append("- Live web search snippets: none found")
    return "\n".join(lines)


def build_prompt(items: List[tuple], vocabulary: List[str], mode: str = "hybrid") -> str:
    """One prompt for one or more artists. `items` is [(evidence, snippets)];
    artists are numbered from 1 and answered by that number."""
    vocab = ", ".join(vocabulary[:_VOCAB_IN_PROMPT]) or "(empty library vocabulary)"
    any_snippets = any(sn for _ev, sn in items)
    if mode == "google_grounding":
        instruction = "Search the web."
    elif any_snippets:
        instruction = ("Use the live web search snippets given with each artist and your "
                       "music knowledge to identify them.")
    else:
        instruction = ("Use your internal training knowledge (global/regional music scenes, "
                       "forums, and discographies) to identify each artist. Match strictly "
                       "against the user's files.")
    show_snippets = mode in ("hybrid", "web_snippets")
    blocks = "\n\n".join(_artist_block(i, ev, sn, show_snippets)
                         for i, (ev, sn) in enumerate(items, 1))
    return f"""You identify music artists for a personal music library. {instruction}

Each artist comes with the user's own files. Use them to make sure you have the RIGHT artist — many acts share a name.

{blocks}

For each artist, find their main genres and country of origin.
- genres: up to {_MAX_GENRES}, lower-case, most specific first. Prefer these words the library already uses: {vocab}. Use a different word only if none of those fit.
- country: ISO 3166-1 alpha-2 code of where the artist is from (e.g. GR, US, GB). For a group, where it formed.
- If an artist cannot be identified or does not match the user's tracks/albums, set identified to false for them. A blank answer is far better than a guess: wrong tags corrupt the user's auto-play.

Reply with ONLY this JSON object, one entry per artist, "id" being the artist's number:
{{"artists": [{{"id": 1, "identified": true, "matched_as": "the artist as the sources describe them", "summary": "one short sentence, e.g. Greek rapper from Athens, active since 2015", "genres": [], "country": "", "confidence": "high|medium|low"}}]}}"""


# Structured output: the model must return exactly this shape, so nothing is
# lost to stray prose. Not sent with the googleSearch tool (not every model
# accepts both). propertyOrdering puts the identification before the answer.
_ANSWER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "artists": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "INTEGER"},
                    "identified": {"type": "BOOLEAN"},
                    "matched_as": {"type": "STRING"},
                    "summary": {"type": "STRING"},
                    "genres": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "country": {"type": "STRING"},
                    "confidence": {"type": "STRING", "enum": list(_CONFIDENCE)},
                },
                "required": ["id", "identified", "genres", "country", "confidence"],
                "propertyOrdering": ["id", "identified", "matched_as", "summary",
                                     "genres", "country", "confidence"],
            },
        },
    },
    "required": ["artists"],
}


# ── call ──────────────────────────────────────────────────────────────────────

_MAX_RETRY_DELAY_S = 60.0


def _retry_delay(text: str) -> Optional[float]:
    """Seconds a 429 asks us to wait (RetryInfo 'retryDelay': '23s'), if any."""
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', text or "")
    return float(m.group(1)) if m else None


def _schema_rejected(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in ("schema", "propertyordering", "response_mime_type",
                                  "responsemimetype"))


async def _generate(api_key: str, model: str, prompt: str, *, session=None,
                    timeout: int = 90, use_grounding: bool = False,
                    structured: bool = False) -> dict:
    """One generateContent call → raw response JSON. When use_grounding is True,
    attaches the googleSearch tool (paid tier). `structured` asks for JSON in
    _ANSWER_SCHEMA's shape. One wait-and-retry each for a per-minute 429 that
    names its delay, a structured-output request the model rejects (sent again
    as plain text) and a dropped connection. Raises ResearchUnavailable for
    key/quota/model problems, RuntimeError otherwise."""
    import aiohttp

    gen_cfg: Dict[str, Any] = {"temperature": 0.2}
    if structured and not use_grounding:
        gen_cfg.update(responseMimeType="application/json", responseSchema=_ANSWER_SCHEMA)
    payload: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    if use_grounding:
        payload["tools"] = [{"googleSearch": {}}]

    own = session is None
    if own:
        session = aiohttp.ClientSession()
    retried = set()
    try:
        while True:
            try:
                async with session.post(
                    _GENERATE_URL.format(model=model), json=payload,
                    headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=timeout, connect=10),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    full = await resp.text()
                    text = full[:300]
                    logger.error("AI research HTTP %d for model %s: %s", resp.status, model, text)
                    if resp.status == 429 and "quota" not in retried:
                        delay = _retry_delay(full)
                        if delay is not None and delay <= _MAX_RETRY_DELAY_S:
                            retried.add("quota")
                            logger.warning("Gemini asked to wait %.0fs; waiting once.", delay)
                            await asyncio.sleep(delay + 1)
                            continue
                    if (resp.status == 400 and "responseSchema" in gen_cfg
                            and _schema_rejected(full)):
                        logger.warning("Model %s rejected structured output; asking for "
                                       "plain JSON instead.", model)
                        gen_cfg.pop("responseMimeType", None)
                        gen_cfg.pop("responseSchema", None)
                        continue
                    if resp.status in (400, 401, 403, 404, 429):
                        raise ResearchUnavailable(_http_reason(resp.status, text, model))
                    raise RuntimeError(f"HTTP {resp.status}: {text}")
            except aiohttp.ClientConnectorError as exc:
                if "network" not in retried:
                    retried.add("network")
                    logger.warning("DNS/Connection hiccup during AI generation (%s), retrying in 1.5s...", exc)
                    await asyncio.sleep(1.5)
                    continue
                raise ResearchUnavailable(f"Network error contacting Gemini: {exc}")
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
        return f"Model '{model}' can't use Google Search — switch Research Paradigm to 'Smart Hybrid' or 'Training Weights' in Settings → AI Assistant."
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


def parse_answers(text: str, n: int) -> Dict[int, dict]:
    """{artist number: answer} from a reply to an n-artist prompt. Takes the
    {"artists": [...]} object, a bare list, or — for a single artist — the
    one answer object an older prompt shape returned. Entries without a
    usable id are numbered by position."""
    if not text:
        return {}
    entries: Any = None
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    if stripped.startswith("["):
        try:
            entries = json.loads(stripped)
        except Exception:
            entries = None
    if entries is None:
        obj = parse_answer(text)
        if obj is None:
            return {}
        if isinstance(obj.get("artists"), list):
            entries = obj["artists"]
        elif n == 1 and "identified" in obj:
            entries = [obj]
        else:
            return {}
    out: Dict[int, dict] = {}
    for pos, a in enumerate(entries if isinstance(entries, list) else [], 1):
        if not isinstance(a, dict):
            continue
        try:
            i = int(a.get("id"))
        except (TypeError, ValueError):
            i = pos
        if 1 <= i <= n and i not in out:
            out[i] = a
    return out


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
             vocabulary: List[str], allow_memory_country: bool = False,
             basis: Optional[str] = None) -> dict:
    """Reduce a raw model answer to what the workbench may offer.

    `basis` is why the claimed country can be trusted (see country_basis).
    Left as None, any source counts as backing — right only for answers
    Google Search grounded. Without backing, a country survives only from
    memory mode at high confidence."""
    if not answer:
        return {"status": "unparsed", "identified": False, "genres": [],
                "new_genres": [], "country": "", "confidence": "low",
                "summary": "", "matched_as": "", "country_basis": "",
                "country_withheld": ""}
    identified = bool(answer.get("identified"))
    genres, new = _clean_genres(answer.get("genres"), vocabulary) if identified else ([], [])
    country = _clean_country(answer.get("country")) if identified else ""
    conf = str(answer.get("confidence") or "").lower()
    conf = conf if conf in _CONFIDENCE else "low"
    if basis is None:
        basis = "grounding" if sources else ""
    withheld = ""
    if country:
        if basis == "contradicted":
            withheld = "contradicted"
        elif basis in ("grounding", "web", "script"):
            pass
        elif allow_memory_country and conf == "high":
            basis = "memory"
        else:
            withheld = "unsupported"
        if withheld:
            country = ""
    if not sources and not allow_memory_country:
        conf = "low"
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
        "country_basis": basis if country else "",
        "country_withheld": withheld,
    }


# ── public API ────────────────────────────────────────────────────────────────

# Artists per Gemini call. The free tier's binding limit is requests per day,
# not tokens, so one call for a dozen artists stretches it about twelvefold.
# Twelve keeps the prompt (evidence + snippets) well under 10k tokens.
_BATCH_SIZE = 12
# Between artists' web searches inside a batch: DuckDuckGo blocks bursts.
_SEARCH_SPACING_S = 1.0


def _worth_retrying(payload: dict) -> bool:
    """An answer worth asking for again: the reply didn't parse, or it came
    back empty while web search was unreachable (the model answered from
    memory alone, so a later run with search may do better)."""
    status = payload.get("status")
    return status == "unparsed" or (status != "ok" and bool(payload.get("search_failed")))


def _finish(evidence: dict, answer: Optional[dict], snippets: List[Dict[str, str]],
            search_failed: bool, vocabulary: List[str], mode: str, model: str,
            grounding: tuple = ((), ())) -> dict:
    """Corroborate and validate one artist's answer."""
    artist = str(evidence.get("artist") or "")
    g_sources, g_queries = grounding
    if g_sources:
        sources, queries, basis, grounded = list(g_sources), list(g_queries), None, True
    else:
        sources = [{"title": s.get("title") or artist, "uri": s["uri"]}
                   for s in snippets if s.get("uri")]
        queries = _search_queries(artist, evidence)[:1] if snippets else []
        claimed = _clean_country((answer or {}).get("country"))
        basis = country_basis(claimed, snippets, evidence)
        grounded = bool(on_target_snippets(snippets, evidence))
    if mode == "google_grounding":
        origin_mode = "google_grounding"
    else:
        origin_mode = "web_snippets" if snippets else "weights"
    result = validate(answer, sources, vocabulary,
                      allow_memory_country=mode in ("hybrid", "weights"), basis=basis)
    result.update({
        "artist": artist, "sources": sources, "queries": queries,
        "grounded": grounded, "model": model, "researched_at": time.time(),
        "origin_mode": origin_mode, "search_failed": search_failed,
    })
    return result


async def research_evidence_batch(
    evidences: List[dict], vocabulary: List[str], *, api_key: str, model: str,
    mode: str = "hybrid", session=None, search_spacing_s: float = _SEARCH_SPACING_S,
    on_search: Optional[Callable[[str], None]] = None,
) -> List[dict]:
    """Research several artists from prepared evidence: web-search each, then
    ONE model call for all of them, then corroborate and validate each answer.
    Results come back in input order. No cache and no DB, so the blind eval
    runs exactly what ships.

    Google grounding can't be batched — its sources aren't attributed per
    artist — so that mode makes one call per artist."""
    if mode == "google_grounding" and len(evidences) > 1:
        out: List[dict] = []
        for ev in evidences:
            out += await research_evidence_batch([ev], vocabulary, api_key=api_key,
                                                 model=model, mode=mode, session=session)
        return out

    gathered: List[tuple] = []
    for i, ev in enumerate(evidences):
        artist = str(ev.get("artist") or "")
        snippets: List[Dict[str, str]] = []
        failed = False
        if mode in ("hybrid", "web_snippets"):
            if on_search:
                on_search(artist)
            if i and search_spacing_s:
                await asyncio.sleep(search_spacing_s)
            found = await _fetch_web_snippets(artist, evidence=ev, session=session)
            failed = found is None
            snippets = found or []
            if not snippets:
                logger.info("No web snippets for %s (%s); answering from model memory.",
                            artist, "search unreachable" if failed else "no results")
        gathered.append((ev, snippets, failed))

    prompt = build_prompt([(ev, sn) for ev, sn, _f in gathered], vocabulary, mode=mode)
    grounding = mode == "google_grounding"
    data = await _generate(api_key, model, prompt, session=session,
                           use_grounding=grounding, structured=not grounding)
    answers = parse_answers(_response_text(data), len(gathered))
    g = _grounding(data) if len(gathered) == 1 else ((), ())
    return [_finish(ev, answers.get(i), sn, failed, vocabulary, mode, model, grounding=g)
            for i, (ev, sn, failed) in enumerate(gathered, 1)]


async def research_evidence(evidence: dict, vocabulary: List[str], *, api_key: str,
                            model: str, mode: str = "hybrid", session=None) -> dict:
    """One artist through research_evidence_batch."""
    return (await research_evidence_batch([evidence], vocabulary, api_key=api_key,
                                          model=model, mode=mode, session=session))[0]


def _log_result(res: dict) -> None:
    logger.warning(
        "AI research %s (%s) → %s genres=%s country=%s (%s) conf=%s sources=%d",
        res.get("artist"), res.get("origin_mode"), res.get("status"), res.get("genres"),
        res.get("country") or "-", res.get("country_basis") or res.get("country_withheld") or "-",
        res.get("confidence"), len(res.get("sources") or []),
    )


async def _save(db, artist_name: str, result: dict) -> None:
    # Unknown answers are cached too (as dismissed): asking again costs a
    # call and will almost always say the same thing — unless the reply was
    # garbled or search was down, which _worth_retrying lets through next time.
    await db.save_ai_research(artist_name, result,
                              state="pending" if result["status"] == "ok" else "dismissed")


async def research_artist(db, artist_name: str, *, api_key: str = "", model: str = "",
                          session=None, force: bool = False,
                          vocabulary: Optional[List[str]] = None,
                          mode: str = "", dispute: str = "") -> dict:
    """Research one artist and cache the answer as a PENDING suggestion.

    Returns the payload: {'artist', 'status' ('ok'|'unknown'|'unparsed'),
    'genres', 'new_genres', 'country', 'country_basis', 'country_withheld',
    'confidence', 'summary', 'matched_as', 'sources' [{title, uri}], 'queries',
    'grounded', 'model', 'researched_at', 'origin_mode', 'search_failed'}.
    `dispute` tells the model the library's current answer looks wrong and
    why (from the metadata audit). A cached answer is returned without a call
    unless `force` (or _worth_retrying). Raises ResearchUnavailable when
    research can't run at all."""
    if not force:
        cached = await db.get_ai_research(artist_name)
        if cached and not _worth_retrying(cached["payload"]):
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

    effective_mode = (mode or load_research_mode() or "hybrid").lower()
    evidence = await db.get_artist_research_evidence(artist_name)
    if dispute:
        evidence["disputed"] = dispute
    result = await research_evidence(evidence, vocabulary, api_key=api_key, model=model,
                                     mode=effective_mode, session=session)
    result["artist"] = artist_name
    _log_result(result)
    await _save(db, artist_name, result)
    return result


async def research_artists(
    db, names: List[str], *, force: bool = False,
    progress: Optional[Callable[[int, int, str, dict], None]] = None,
    cancel_event: Optional[asyncio.Event] = None,
    spacing_s: float = _BATCH_SPACING_S,
    mode: str = "",
    batch_size: int = _BATCH_SIZE,
) -> dict:
    """Research several artists, `batch_size` per model call. Returns a summary:
    {'status': 'ok'|'cancelled'|'error: …', 'total', 'found', 'unknown',
    'skipped', 'failed'}. Stops at the first ResearchUnavailable — a bad key or
    an exhausted quota fails every artist the same way.

    `progress(i, total, name, result)` fires once per artist when its answer is
    in (result {'status': 'skipped'} for a cached one), and with
    {'status': 'searching'} — i unchanged — while each artist's web search runs."""
    import aiohttp

    api_key, model = load_research_settings()
    summary = {"status": "ok", "total": len(names), "found": 0, "unknown": 0,
               "skipped": 0, "failed": 0}
    if not api_key:
        summary["status"] = "error: Add a Gemini API key in Settings → AI Assistant."
        return summary
    effective_mode = (mode or load_research_mode() or "hybrid").lower()
    done_before = set() if force else await db.get_researched_artist_names()
    vocab = [v["name"] for v in await db.get_genre_vocabulary(limit=_VOCAB_IN_PROMPT)]

    done = 0
    todo: List[str] = []
    for name in names:
        if name.lower() in done_before and not _worth_retrying(
                ((await db.get_ai_research(name)) or {}).get("payload") or {}):
            summary["skipped"] += 1
            done += 1
            if progress:
                progress(done, len(names), name, {"status": "skipped"})
        else:
            todo.append(name)

    async with aiohttp.ClientSession() as session:
        for start in range(0, len(todo), max(1, batch_size)):
            if cancel_event is not None and cancel_event.is_set():
                summary["status"] = "cancelled"
                break
            if start and spacing_s:
                await asyncio.sleep(spacing_s)
            chunk = todo[start:start + max(1, batch_size)]
            evidences = [await db.get_artist_research_evidence(n) for n in chunk]

            def _searching(artist, _done=done):
                if progress:
                    progress(_done, len(names), artist, {"status": "searching"})
            try:
                results = await research_evidence_batch(
                    evidences, vocab, api_key=api_key, model=model, mode=effective_mode,
                    session=session, search_spacing_s=min(spacing_s, _SEARCH_SPACING_S),
                    on_search=_searching)
            except ResearchUnavailable as exc:
                summary["status"] = f"error: {exc}"
                break
            except Exception as exc:
                logger.error("AI research batch failed (%s…): %s", chunk[0], exc)
                summary["failed"] += len(chunk)
                for name in chunk:
                    done += 1
                    if progress:
                        progress(done, len(names), name, {"status": "error"})
                continue
            for name, res in zip(chunk, results):
                res["artist"] = name
                _log_result(res)
                await _save(db, name, res)
                summary["found" if res.get("status") == "ok" else "unknown"] += 1
                done += 1
                if progress:
                    progress(done, len(names), name, res)
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
