"""Artist metadata enrichment — a cascade of sources, feeding Auto-Play.

The walk reads exactly two fields per artist: `country` and `genres`.
`_pool_foreign` gates the candidate pool on them and `genre_graph.node_label`
places the artist in the journey graph. This module fills them in, and
`refresh_walk_models` is what makes an edit actually reach the walk.

Why MusicBrainz first: it's open (CC0), free, and — crucially for the Greek-vs-US
hip-hop problem — exposes artist *area/country*, an objective provenance signal
that no acoustic feature can recover. We also pull community genre/tag labels.

Why it is not enough on its own: measured on the real library, a quarter of
artists match MusicBrainz perfectly (status='ok', score=100) and come back with
an EMPTY genre list, and that set is not a random sample — it is concentrated in
the Greek scene, which is exactly where the pool gate most needs tags. So
`enrich_library` falls through to the artist's own file tags and then to Qobuz
(`utils.metadata_qobuz`), recording per-field provenance so the workbench can
show which source supplied what.

Design notes:
  • Pure aiohttp + stdlib, so it compiles for Android exactly like the rest of
    the networking layer; nothing here imports numpy / the audio stack.
  • MusicBrainz asks for ≤ 1 request/second and a descriptive User-Agent with a
    contact. `MusicBrainzClient` self-throttles and sets that header, and backs
    off on HTTP 503 (their rate-limit response).
  • Enrichment is keyed/cached per ARTIST (stable, ~hundreds per library), never
    per track, so the whole library is a few hundred polite requests.
  • Failures are logged at `warning`/`error`, never `debug`: on a release
    Android build nothing below `error` reaches logcat, so a silently-failing
    pass used to be indistinguishable from a library with no metadata.
"""

from __future__ import annotations

import asyncio
import logging
import time
import unicodedata
from collections import Counter

try:
    import aiohttp
except Exception:  # pragma: no cover - import guarded so the app still loads
    aiohttp = None  # type: ignore

logger = logging.getLogger(__name__)

# Contact for the MusicBrainz User-Agent (they ask for one). Personal app.
DEFAULT_CONTACT = "mitsacopoulos@gmail.com"

_BASE = "https://musicbrainz.org/ws/2"
# Lucene special characters that must be escaped inside an artist:"..." query.
_LUCENE_SPECIAL = set('+-&|!(){}[]^"~*?:\\/')

# MusicBrainz search score below which even a genuine name/alias match is held
# back for review. Deliberately low: `_closest_match` has already proved the
# name matches, so this only catches a pathological fuzzy hit.
_GENUINE_SCORE_FLOOR = 70


def _escape_lucene(s: str) -> str:
    return "".join("\\" + c if c in _LUCENE_SPECIAL else c for c in (s or ""))


def _fold(s: str) -> str:
    """Lowercase and strip diacritics, so an accent can never split a name.

    `str.isalnum()` is True for 'é', so the old alnum-only key kept accents and
    made 'Cesária Evora' (as the file credits her) a non-match for MusicBrainz's
    'Cesária Évora' — a score-100 hit with a country, rejected on one mark and
    stored as an empty 'lowconfidence' row. NFKD splits the letter from its
    combining mark; dropping category Mn leaves the base letter."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", (s or "").lower())
        if not unicodedata.combining(c)
    )


def _norm_name(s: str) -> str:
    """Loose key for comparing artist names: folded, alphanumerics only."""
    return "".join(c for c in _fold(s) if c.isalnum())


def _norm_tokens(s: str) -> set[str]:
    """Folded alphanumeric word tokens."""
    return set("".join(c if c.isalnum() else " " for c in _fold(s)).split())


def _name_close(a: str, b: str) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    ta = _norm_tokens(a)
    tb = _norm_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True

    # Exact token match or tight subset match
    inter = len(ta & tb)
    if inter > 0:
        if inter == len(ta) and len(ta) == len(tb):
            return True
        if inter >= min(len(ta), len(tb)) and abs(len(ta) - len(tb)) <= 1:
            return True
    return False


# Separators that join several credited artists into one string. Streaming
# sources emit these freely ('Travis Scott/Metro Boomin/21 Savage',
# '163Margs, Digga D', 'Russ Millions, YV, BUNI'), and each distinct string
# becomes its own `artists` row with its own MusicBrainz lookup.
_CREDIT_SEPARATORS = (
    "/", ",", "&", " x ", " X ", " vs ", " vs. ", " feat ", " feat. ",
    " ft ", " ft. ", " featuring ", " with ", " + ", " · ",
)


def split_artist_credits(name: str) -> list[str]:
    """Decompose a multi-artist credit string into its member names.

    Returns [] when the string carries no separator (the overwhelmingly common
    single-artist case), so callers can cheaply test "is this a collab credit?".

    IMPORTANT — this is only ever a FALLBACK, used after a whole-string lookup
    has already failed to match a real entity. Bands whose names contain
    separators ('Earth, Wind & Fire', 'Crosby, Stills & Nash') resolve on the
    whole string and therefore never reach decomposition."""
    raw = (name or "").strip()
    if not raw:
        return []
    parts = [raw]
    for sep in _CREDIT_SEPARATORS:
        nxt: list[str] = []
        for p in parts:
            nxt.extend(p.split(sep))
        parts = nxt
    members = []
    seen = set()
    for p in parts:
        p = p.strip(" \t-–—")
        if len(p) < 2:
            continue
        key = _norm_name(p)
        if not key or key in seen:
            continue
        seen.add(key)
        members.append(p)
    return members if len(members) > 1 else []


def credit_keys(name: str) -> frozenset:
    """Normalised member keys for an artist credit string — {whole string} for a
    solo artist, {member, member, …} for a collab. Two credit strings that share
    a key name the same performer, which is what the walk's same-artist guards
    need instead of raw string equality ('21 Savage' vs
    '21 Savage & Metro Boomin')."""
    members = split_artist_credits(name)
    if not members:
        k = _norm_name(name)
        return frozenset({k}) if k else frozenset()
    return frozenset(_norm_name(m) for m in members if _norm_name(m))


def _extract_genres(obj: dict, top: int = 8) -> list[dict]:
    """Genre/tag list from an artist object → [{'name', 'count'}], count-desc.
    Prefers curated `genres`; falls back to free `tags`."""
    raw = obj.get("genres") or obj.get("tags") or []
    out = []
    for g in raw:
        name = (g or {}).get("name")
        if not name:
            continue
        try:
            count = int(g.get("count", 0) or 0)
        except (TypeError, ValueError):
            count = 0
        out.append({"name": name, "count": count})
    out.sort(key=lambda d: -d["count"])
    return out[:top]


def _closest_match(name: str, artists: list[dict]) -> dict | None:
    """First artist whose name, sort-name, OR an alias genuinely matches `name`
    (see `_name_close`), else None. Unlike `_best_match` this does NOT fall back
    to the top search hit, so a tribute band / mashup that merely contains the
    query string (e.g. 'Kanye West Tribute Band') is rejected instead of accepted
    as truth. Alias matching is what recovers renamed artists — MusicBrainz
    renamed 'Kanye West' → 'Ye', keeping 'Kanye West' only as an alias."""
    for a in artists or []:
        if _name_close(name, a.get("name", "")):
            return a
        if _name_close(name, a.get("sort-name", "")):
            return a
        for al in a.get("aliases") or []:
            if isinstance(al, dict):
                if al.get("name") and _name_close(name, al.get("name")):
                    return a
                if al.get("sort-name") and _name_close(name, al.get("sort-name")):
                    return a
            elif isinstance(al, str) and _name_close(name, al):
                return a
    return None


_JUNK_ENTITY_MARKERS = ("tribute", "karaoke", "cover band", "covers band")


def _looks_like_junk(a: dict) -> bool:
    """A search hit that is clearly not the real artist — a tribute/karaoke/cover
    act. These outrank real artists on literal-string searches (a 'Kanye West
    Tribute Band' scores 100 for `artist:\"Kanye West\"`) and must never be the
    fallback we enrich from."""
    txt = ((a.get("name") or "") + " " + (a.get("disambiguation") or "")).lower()
    return any(m in txt for m in _JUNK_ENTITY_MARKERS)


def _best_match(name: str, artists: list[dict]) -> dict | None:
    """Highest-scoring genuinely-matching artist, else the top non-junk hit (kept
    for callers that want a best-effort guess)."""
    if not artists:
        return None
    m = _closest_match(name, artists)
    if m is not None:
        return m
    for a in artists:
        if not _looks_like_junk(a):
            return a
    return None


_COUNTRY_NAME_TO_ISO = {
    # North America & Caribbean
    "united states": "US", "usa": "US", "canada": "CA", "mexico": "MX", "puerto rico": "PR",
    "jamaica": "JM", "cuba": "CU", "trinidad": "TT", "dominican republic": "DO",
    
    # Europe
    "united kingdom": "GB", "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "greece": "GR", "ελλάδα": "GR", "france": "FR", "germany": "DE", "deutschland": "DE",
    "sweden": "SE", "norway": "NO", "finland": "FI", "denmark": "DK", "iceland": "IS",
    "netherlands": "NL", "holland": "NL", "belgium": "BE", "switzerland": "CH", "austria": "AT",
    "italy": "IT", "spain": "ES", "portugal": "PT", "ireland": "IE", "poland": "PL",
    "czech": "CZ", "czechia": "CZ", "slovakia": "SK", "hungary": "HU", "romania": "RO",
    "bulgaria": "BG", "serbia": "RS", "croatia": "HR", "slovenia": "SI", "bosnia": "BA",
    "ukraine": "UA", "russia": "RU", "belarus": "BY", "georgia": "GE", "armenia": "AM",
    "turkey": "TR", "türkiye": "TR", "cyprus": "CY", "albania": "AL", "north macedonia": "MK",
    
    # South & Central America
    "brazil": "BR", "brasil": "BR", "argentina": "AR", "chile": "CL", "colombia": "CO",
    "peru": "PE", "venezuela": "VE", "uruguay": "UY",
    
    # Asia & Middle East
    "japan": "JP", "nippon": "JP", "south korea": "KR", "korea": "KR", "china": "CN",
    "taiwan": "TW", "hong kong": "HK", "india": "IN", "indonesia": "ID", "philippines": "PH",
    "thailand": "TH", "vietnam": "VN", "singapore": "SG", "malaysia": "MY",
    "israel": "IL", "palestine": "PS", "lebanon": "LB", "egypt": "EG", "iran": "IR",
    
    # Oceania & Africa
    "australia": "AU", "new zealand": "NZ", "south africa": "ZA", "nigeria": "NG",
    "ghana": "GH", "kenya": "KE", "ethiopia": "ET", "morocco": "MA", "senegal": "SN",
}


def _extract_country(obj: dict) -> str | None:
    """Extract 2-letter ISO country code from a MusicBrainz artist dict.
    Checks top-level `country`, `area.iso-3166-1-codes`, `begin-area.iso-3166-1-codes`,
    and `area.name` fallback."""
    if not obj or not isinstance(obj, dict):
        return None

    c = obj.get("country")
    if c and isinstance(c, str) and len(c.strip()) == 2:
        return c.strip().upper()

    area = obj.get("area") or {}
    codes = area.get("iso-3166-1-codes") or []
    if codes and isinstance(codes, list) and len(codes[0]) == 2:
        return str(codes[0]).upper()

    begin_area = obj.get("begin-area") or {}
    begin_codes = begin_area.get("iso-3166-1-codes") or []
    if begin_codes and isinstance(begin_codes, list) and len(begin_codes[0]) == 2:
        return str(begin_codes[0]).upper()

    aname = (area.get("name") or begin_area.get("name") or "").lower().strip()
    for k, iso in _COUNTRY_NAME_TO_ISO.items():
        if k in aname:
            return iso

    return None


def _copy_result(res: dict) -> dict:
    """Copy of a lookup result that shares no mutable state with the original.
    Only `genres` is mutable, so a shallow dict copy plus a fresh genre list is
    enough — and it keeps the memo immune to a caller editing what it got back."""
    out = dict(res)
    out["genres"] = [dict(g) for g in (res.get("genres") or [])]
    return out


class MusicBrainzClient:
    """Rate-limited MusicBrainz lookups over a shared aiohttp session."""

    def __init__(self, session, contact: str = "anonymous@example.com",
                 min_interval: float = 1.1, app_name: str = "MaiAnLab"):
        if aiohttp is None:
            raise RuntimeError("aiohttp is required for MusicBrainzClient")
        self.session = session
        self.user_agent = f"{app_name}/1.0 ( {contact} )"
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()  # serialise + throttle all requests
        # Per-pass memo of resolved artists. Credit decomposition re-resolves
        # each member, and members recur heavily across a library ('21 Savage'
        # appears in four different credit strings here) — without this, one
        # enrichment pass re-pays the full 5-tier cascade at 1.1 s/request for
        # every repeat. Bounded by the artist count of a single pass.
        self._artist_memo: dict = {}

    async def _get(self, path: str, params: dict, retries: int = 3):
        """Throttled GET → (json|None, http_status). Honours 503 with backoff.

        Failures are LOUD on purpose. These used to be `logger.debug`, which on a
        release Android build means they do not reach logcat at all — a whole
        sync could fail end to end and the only visible trace was a workbench
        that cheerfully reported "0 matched". The final give-up is `error` so it
        surfaces as I/serious_python on device; the intermediate retries stay at
        `warning` so a single flaky request doesn't shout.

        `status` distinguishes the cases the UI has to tell apart: 503 is
        MusicBrainz rate-limiting us (back off, try later), -1 is the network
        being down (offline), and anything else is a real HTTP answer."""
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        timeout = aiohttp.ClientTimeout(total=20)
        status = -1
        for attempt in range(retries):
            async with self._lock:
                wait = self.min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    async with self.session.get(
                        f"{_BASE}/{path}", params=params,
                        headers=headers, timeout=timeout,
                    ) as resp:
                        self._last = time.monotonic()
                        if resp.status == 200:
                            return await resp.json(), 200
                        if resp.status == 503:
                            status = 503  # rate-limited → back off below
                            logger.warning(
                                "MusicBrainz rate-limited (503) on %s, attempt %d/%d",
                                path, attempt + 1, retries,
                            )
                        else:
                            logger.warning(
                                "MusicBrainz HTTP %s on %s", resp.status, path,
                            )
                            return None, resp.status
                except Exception as exc:
                    self._last = time.monotonic()
                    status = -1
                    logger.warning(
                        "MusicBrainz request failed on %s (attempt %d/%d): %s: %s",
                        path, attempt + 1, retries, type(exc).__name__, exc,
                    )
            # backoff happens outside the lock so we don't pin it while sleeping
            await asyncio.sleep(1.5 * (attempt + 1))
        logger.error(
            "MusicBrainz unreachable after %d attempts on %s (last status %s) — "
            "enrichment cannot proceed", retries, path, status,
        )
        return None, status

    async def lookup_artist(self, name: str, *, with_genres: bool = False) -> dict:
        """Resolve one artist name → provenance + genres.

        Returns a dict with keys: status ('ok'|'lowconfidence'|'notfound'|
        'error'), mbid, country, area, genres (list), score (0-100). Never
        raises — failures come back as status='error'/'notfound'.

        Memoised per client (i.e. per enrichment pass); 'error' results are not
        cached so a transient network failure is still retried."""
        raw_name = (name or "").strip()
        memo_key = (raw_name.lower(), bool(with_genres))
        cached = self._artist_memo.get(memo_key)
        if cached is not None:
            return _copy_result(cached)
        result = await self._lookup_artist_uncached(raw_name, with_genres=with_genres)
        if result.get("status") != "error":
            self._artist_memo[memo_key] = _copy_result(result)
        return result

    async def _lookup_artist_uncached(
        self, name: str, *, with_genres: bool = False,
    ) -> dict:
        """The real resolution cascade. See `lookup_artist` for the contract."""
        empty = {"status": "notfound", "mbid": None, "country": None,
                 "area": None, "genres": [], "score": 0, "reason": None}
        raw_name = (name or "").strip()
        if not raw_name:
            return empty

        q = _escape_lucene(raw_name)
        parts = raw_name.split()
        sort_name_q = None
        if len(parts) >= 2:
            sort_str = f"{parts[-1]}, {' '.join(parts[:-1])}"
            sort_name_q = _escape_lucene(sort_str)

        # Query cascade. Each tier is tried in turn; we keep going until a tier
        # yields a GENUINE name/alias match (`_closest_match`), not merely a
        # non-empty result. Key changes vs "stop on first non-empty tier + accept
        # artists[0]":
        #   • the `alias:` tier recovers renamed artists — MusicBrainz renamed
        #     'Kanye West' → 'Ye', so name/sortname searches only surface a
        #     'Kanye West Tribute Band'; the real entity is reachable only by
        #     alias;
        #   • a non-matching tier no longer short-circuits the cascade;
        #   • if no tier produces a genuine match we fall back to the first
        #     non-junk hit, but never to a tribute/karaoke entity — and never at
        #     all for a multi-artist credit string (see `_credit_fallback_ok`).
        # Combined Lucene query to search name, sortname, alias, and unquoted terms
        # in a single request (5x faster than 5 sequential HTTP calls).
        combined_q = f'artist:"{q}" OR alias:"{q}" OR artist:{q} OR {q}'
        if sort_name_q:
            combined_q = f'artist:"{q}" OR sortname:"{sort_name_q}" OR alias:"{q}" OR artist:{q} OR {q}'

        best = None
        matched = False       # True = genuine name/alias match; False = weak fallback
        weak = None           # first non-junk hit, used only if nothing matches
        saw_error = False
        reason = None

        data, status = await self._get(
            "artist", {"query": combined_q, "fmt": "json", "limit": 10}
        )
        if data is None:
            saw_error = True
            # Carried through to the caller so the UI can say "offline" or
            # "rate-limited" instead of lumping a network failure in with
            # "MusicBrainz has no tags for this artist".
            reason = ("rate_limited" if status == 503
                      else "offline" if status == -1
                      else f"http_{status}")
        else:
            arts = data.get("artists") or []
            m = _closest_match(raw_name, arts)
            if m is not None:
                best, matched = m, True
            else:
                weak = next((a for a in arts if not _looks_like_junk(a)), None)

        # ── No genuine match: try DECOMPOSING a multi-artist credit ───────────
        is_credit = False
        if not matched:
            members = split_artist_credits(raw_name)
            if members:
                is_credit = True
                merged = await self._lookup_credit_members(
                    members, with_genres=with_genres,
                )
                if merged is not None:
                    return merged

        if best is None and not is_credit:
            best = weak
        if best is None:
            # Nothing usable: all junk, the requests failed, or this is a credit
            # string whose members didn't resolve.
            #
            # The weak fallback is DELIBERATELY not offered to a credit string.
            # 'Toquel, Fly Lo, Beyond' has no legitimate whole-string entity, so
            # MusicBrainz's fuzzy match returns whatever scores highest on the
            # literal text — measured live, that Greek-rap credit came back
            # 'rock, cantopop, chinese, cantonese' (the Hong Kong band Beyond)
            # and 'Toquel, Light' came back British progressive rock. Those rows
            # store as 'lowconfidence', which `get_all_artist_genre_sets` feeds
            # into the NPMI corpus and `get_artist_meta_for_paths` hands to the
            # walk's pool gate regardless of status — so a guess here does not
            # merely look wrong in the workbench, it re-fences the queue.
            # Better a blank the user can fill than fiction they can't see.
            return {
                **empty,
                "status": "error" if saw_error else "notfound",
                "reason": reason if saw_error else (
                    "credit_unresolved" if is_credit else "no_match"
                ),
            }

        try:
            score = int(best.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0
        mbid = best.get("id")

        # ── A weak fallback provides candidate tags for review ──────────────
        if not matched:
            return {
                "status": "lowconfidence", "mbid": mbid,
                "country": _extract_country(best),
                "area": (best.get("area") or {}).get("name"),
                "genres": _extract_genres(best),
                "score": score, "reason": "weak_match",
            }

        country = _extract_country(best)
        area = (best.get("area") or {}).get("name")
        genres = _extract_genres(best)

        # `matched` already means a genuine name / sort-name / alias hit, so the
        # old `score >= 90` gate on top of it was redundant belt-and-braces that
        # parked real artists in the review queue forever (four rows on this
        # library sat at 81-87 with correct tags). A low floor still catches the
        # pathological fuzzy hit.
        status = "ok" if score >= _GENUINE_SCORE_FLOOR else "lowconfidence"

        # A one-word name is where a genuine-looking match is most often the
        # wrong act: 'Light' (a Greek rapper) resolves to a Dutch band at score
        # 100, and 'Beyond' to a Cantopop group. Demand a corroborating field
        # before calling a bare single token confirmed.
        if status == "ok" and len(_norm_tokens(raw_name)) < 2 and not (genres or country):
            status = "lowconfidence"

        result = {
            "status": status,
            "mbid": mbid, "country": country, "area": area,
            "genres": genres, "score": score,
            # 'no_tags' is the single most common outcome on this library and is
            # NOT a failure: a quarter of artists match perfectly and simply have
            # no MusicBrainz tags. Naming it lets the UI stop painting them red
            # and lets the cascade know to try another source.
            "reason": None if genres else "no_tags",
        }

        # The search payload often omits genres; fetch direct lookup only if needed.
        if with_genres and mbid and not result["genres"]:
            gdata, _ = await self._get(
                f"artist/{mbid}", {"inc": "genres+tags", "fmt": "json"}
            )
            if gdata:
                g2 = _extract_genres(gdata)
                if g2:
                    result["genres"] = g2
                    result["reason"] = None
                result["country"] = _extract_country(gdata) or result["country"]
                result["area"] = result["area"] or (gdata.get("area") or {}).get("name")
        return result

    async def _lookup_credit_members(
        self, members: list[str], *, with_genres: bool = False,
    ) -> dict | None:
        """Resolve each member of a multi-artist credit and fuse the results.

        Genres are unioned (summing tag counts, so a genre both members carry
        outranks one either carries alone); country is taken only when the
        resolved members AGREE, since a cross-border collab has no single
        provenance and guessing one would mis-fire the regional-scene pool rule.

        Only members that come back status='ok' — a GENUINE name/alias match —
        contribute. Accepting 'lowconfidence' members too is what produced the
        fabrications this fusion was supposed to prevent: a credit's members are
        often short generic words, and MusicBrainz happily returns a famous act
        for one ('Beyond' → the Hong Kong band, so 'Toquel, Fly Lo, Beyond'
        fused to cantopop/chinese; 'Light' → British progressive rock). A weak
        member is no evidence at all, so it is skipped rather than averaged in.

        Returns None when no member resolves, so the caller can fall through to
        its own handling. Status is 'ok' only if every member matched genuinely —
        a partially-resolved credit stays 'lowconfidence' and so remains eligible
        for a later re-sync."""
        genre_counts: Counter = Counter()
        countries: list[str] = []
        mbids: list[str] = []
        resolved = 0
        for m in members[:4]:   # cap the fan-out; credits beyond 4 are noise
            sub = await self.lookup_artist(m, with_genres=with_genres)
            if sub.get("status") != "ok":
                continue
            if not sub.get("genres") and not sub.get("country"):
                continue
            resolved += 1
            for g in sub.get("genres") or []:
                nm = (g or {}).get("name")
                if nm:
                    genre_counts[nm] += max(int(g.get("count", 1) or 1), 1)
            if sub.get("country"):
                countries.append(sub["country"])
            if sub.get("mbid"):
                mbids.append(sub["mbid"])

        if not resolved:
            return None

        country = countries[0] if countries and len(set(countries)) == 1 else None
        genres = [{"name": n, "count": c} for n, c in genre_counts.most_common(8)]
        full = resolved == len(members[:4])
        return {
            "status": "ok" if full else "lowconfidence",
            "mbid": mbids[0] if mbids else None,
            "country": country,
            "area": None,
            "genres": genres,
            "score": 100 if full else 50,
            "reason": None if genres else ("no_tags" if full else "partial_credit"),
        }

    async def search_candidates(self, name: str, limit: int = 10) -> list[dict]:
        """Perform a direct MusicBrainz search and return candidate dicts."""
        raw_name = (name or "").strip()
        if not raw_name:
            return []
        q = _escape_lucene(raw_name)
        data, status = await self._get(
            "artist", {"query": f'artist:"{q}" OR alias:"{q}" OR {q}', "fmt": "json", "limit": limit}
        )
        if not data:
            return []
        arts = data.get("artists") or []
        results = []
        for a in arts:
            mbid = a.get("id")
            g = _extract_genres(a)
            c = _extract_country(a)
            try:
                sc = int(a.get("score", 0) or 0)
            except (TypeError, ValueError):
                sc = 0
            results.append({
                "name": a.get("name") or raw_name,
                "mbid": mbid,
                "disambiguation": a.get("disambiguation", ""),
                "country": c,
                "area": (a.get("area") or {}).get("name"),
                "genres": g,
                "score": sc,
                "is_junk": _looks_like_junk(a),
            })
        return results


async def search_musicbrainz_artists_candidates(
    name: str, contact: str = DEFAULT_CONTACT, limit: int = 10
) -> list[dict]:
    """Standalone helper to query MusicBrainz candidate entities for an artist name."""
    if aiohttp is None:
        return []
    async with aiohttp.ClientSession() as session:
        client = MusicBrainzClient(session, contact=contact)
        return await client.search_candidates(name, limit=limit)


async def musicbrainz_artist_details(
    mbid: str, contact: str = DEFAULT_CONTACT
) -> dict:
    """Full genres/country/area for a KNOWN MBID via a direct entity lookup.

    The `/artist?query=` search payload the candidate picker uses often omits
    genres (and sometimes country), so a candidate chosen there would be
    committed with empty tags — leaving the artist an unresolved gap. This does
    the reliable `/artist/{mbid}?inc=genres+tags` follow-up so a picked match
    actually populates the fields the walk reads. Returns {} on any failure
    (offline / bad MBID), so callers fall back to the shallow search data."""
    if aiohttp is None or not mbid:
        return {}
    try:
        async with aiohttp.ClientSession() as session:
            client = MusicBrainzClient(session, contact=contact)
            gdata, _ = await client._get(
                f"artist/{mbid}", {"inc": "genres+tags", "fmt": "json"}
            )
    except Exception as exc:
        logger.debug("MB detail lookup failed for %s: %s", mbid, exc)
        return {}
    if not gdata:
        return {}
    return {
        "genres": _extract_genres(gdata),
        "country": _extract_country(gdata),
        "area": (gdata.get("area") or {}).get("name"),
    }


# ── Propagating metadata edits into Auto-Play ────────────────────────────────
# Every write to artist_enrichment has to reach THREE derived artefacts before
# the walk sees it, and until now only some callers rebuilt some of them:
#
#   1. albums.genre / genre_bucket  — display + grouping only.
#   2. genre_affinity (NPMI)        — the walk's genre-similarity metric.
#   3. the JOURNEY GRAPH            — the scaffolding `walk()` actually traverses
#                                     first, and the node fence the radius
#                                     fallback uses when it doesn't.
#
# (3) was the gap. `build_journey_graph` was called only from
# `build_acoustic_edges` (a full library rebuild) and from the bulk override
# path — so a sync, a single-artist Save, an Accept and a Use-This-Match all
# rebuilt the NPMI model and left the journey graph stale. Worse, the payload is
# memory-cached in `db_manager._genre_graph_cache`, so the staleness persisted
# for the whole process: you could tag an artist, see "Saved", and get a queue
# built from the pre-edit nodes until the next app restart. That is precisely
# the "metadata doesn't sync to auto-play" symptom.
#
# Rebuilds are COALESCED rather than run per save: tagging six artists in a row
# should cost one rebuild, not six full-library passes on the UI's event loop.
_REFRESH_DEBOUNCE_S = 1.5
_refresh_pending: dict[int, asyncio.Task] = {}


async def _do_refresh_walk_models(db_manager) -> dict:
    """Rebuild every artefact derived from artist_enrichment. Never raises."""
    out: dict = {}
    try:
        out["genre_fix"] = await db_manager.fix_and_normalize_track_genres()
    except Exception as exc:
        logger.warning("track genre normalization failed: %s", exc)
    try:
        from utils.track_graph import build_genre_affinity, build_journey_graph
        out["model_pairs"] = await build_genre_affinity(db_manager)
        # The journey graph splits nodes by family AND country, so a country
        # edit moves the artist even when its genres are unchanged.
        out["journey_nodes"] = await build_journey_graph(db_manager)
    except Exception as exc:
        logger.warning("walk model refresh failed: %s", exc)
    logger.info("refresh_walk_models: %s", out)
    return out


def refresh_walk_models(db_manager, *, on_done=None) -> None:
    """Schedule a coalesced rebuild of the walk's derived models.

    Call this after ANY write to artist_enrichment. Repeated calls inside the
    debounce window collapse into one rebuild, so a burst of single-artist saves
    (or a drag of twenty artists onto one genre) costs a single pass.

    `on_done(summary)` fires when the rebuild lands, so the UI can confirm
    "Auto-Play updated" at the moment it is actually true rather than optimistically
    on save. Fire-and-forget: failures are logged, never raised."""
    key = id(db_manager)
    existing = _refresh_pending.get(key)
    if existing is not None and not existing.done():
        existing.cancel()

    async def _runner():
        try:
            await asyncio.sleep(_REFRESH_DEBOUNCE_S)
        except asyncio.CancelledError:
            return          # superseded by a later save; that one will rebuild
        summary = await _do_refresh_walk_models(db_manager)
        _refresh_pending.pop(key, None)
        if on_done:
            try:
                res = on_done(summary)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.debug("refresh_walk_models callback failed: %s", exc)

    try:
        _refresh_pending[key] = asyncio.get_running_loop().create_task(_runner())
    except RuntimeError:
        # No loop (sync context / tests) — nothing to schedule against.
        logger.debug("refresh_walk_models called with no running loop; skipped")


async def enrich_library(
    db_manager, *, with_genres: bool = True, limit=None,
    contact: str = DEFAULT_CONTACT, include_failed: bool = False,
    max_consecutive_errors: int = 3, progress=None,
    cancel_event: asyncio.Event | None = None,
    use_qobuz: bool = True, source_genres=None,
    retry_incomplete: bool = False,
) -> dict:
    """Incrementally enrich artists that have no cached metadata yet, from a
    CASCADE of sources.

    Only artists without an enrichment row are fetched, so calling this after
    each library index enriches just the *new* artists. Transient request
    failures (offline / rate cap) are NOT persisted — the artist stays
    'needing' and is retried on the next index — and the pass aborts after a few
    consecutive errors (almost certainly offline).

    ── The cascade ─────────────────────────────────────────────────────────────
    MusicBrainz is the authority and always runs first, but on this library a
    quarter of artists match it perfectly and come back with NO genres at all —
    concentrated in the Greek scene, which is exactly where the walk's pool gate
    most needs them. So when MusicBrainz yields no genres we fall through:

      1. MusicBrainz   country (authoritative) + curated genres
      2. your own FILES  `albums.genre`, already on disk from the download source
      3. Qobuz         the store this library came from — see `utils.metadata_qobuz`

    A Deezer tier sat at the end of this cascade briefly and was removed: it
    contributed ZERO across the whole 252-artist library, because tier 2 already
    carries whatever the download source wrote at download time (Deezer's own
    genre included, for a Deezer-sourced album) and Qobuz covers the rest.

    Country is never taken from tiers 2-3: it is the field the walk's regional
    fence reads, and neither source carries a trustworthy one. Each field records
    where it came from in `provenance`, so the workbench can show the origin and
    the user can tell a hand-checkable guess from an authority.

    A tier-2/3 genre is only ever written when MusicBrainz supplied NONE, so an
    authority answer is never overwritten by a weaker one.

    Returns a summary dict — including `aborted`, `cancelled` and `reasons`, all
    of which the caller is expected to SHOW rather than discard. Never raises."""
    if aiohttp is None:
        logger.error("enrich_library: aiohttp unavailable, cannot enrich")
        return {"enriched": 0, "status": "no_aiohttp"}
    try:
        try:
            artists = await db_manager.get_artists_needing_enrichment(
                limit=limit, include_failed=include_failed,
                retry_incomplete=retry_incomplete,
            )
        except TypeError:
            # Backend predates the retry_incomplete scope (test fakes).
            artists = await db_manager.get_artists_needing_enrichment(
                limit=limit, include_failed=include_failed,
            )
    except Exception as exc:
        logger.error("enrich_library: cannot list artists needing enrichment: %s", exc)
        return {"enriched": 0, "status": f"error: {exc}"}
    if not artists:
        return {"enriched": 0, "status": "uptodate"}

    # File tags for the whole library in one query, rather than a lookup per
    # artist inside the loop.
    if source_genres is None:
        source_genres = {}
        if hasattr(db_manager, "get_all_artist_source_genres"):
            try:
                source_genres = await db_manager.get_all_artist_source_genres()
            except Exception as exc:
                logger.warning("source-tag preload failed: %s", exc)

    counts: Counter = Counter()
    reasons: Counter = Counter()
    done = 0
    consecutive_errors = 0
    status = "completed"
    # Opened on first use and shared for the pass — logging in per artist would
    # cost a round trip each. `qobuz_tried` stops a failed login retrying for
    # every remaining artist.
    qobuz_client = None
    qobuz_tried = False
    async with aiohttp.ClientSession() as session:
        client = MusicBrainzClient(session, contact=contact)
        for i, name in enumerate(artists, 1):
            if cancel_event and cancel_event.is_set():
                counts["cancelled"] = 1
                status = "cancelled"
                break
            try:
                res = await client.lookup_artist(name, with_genres=with_genres)
            except Exception as exc:
                # Previously swallowed with no log at all, which made a
                # systematic failure look like a library full of untagged artists.
                logger.warning("lookup_artist raised for %r: %s: %s",
                               name, type(exc).__name__, exc)
                res = {"status": "error", "mbid": None, "country": None,
                       "area": None, "genres": [], "score": 0,
                       "reason": type(exc).__name__}
            st = res["status"]
            if st == "error":
                consecutive_errors += 1
                counts["error"] += 1
                reasons[res.get("reason") or "unknown"] += 1
                if progress:
                    progress(i, len(artists), name, res)
                if consecutive_errors >= max_consecutive_errors:
                    counts["aborted"] = 1
                    status = "aborted"
                    logger.error(
                        "enrich_library: aborting after %d consecutive failures "
                        "(last reason: %s) — %d/%d artists processed",
                        consecutive_errors, res.get("reason"), i, len(artists),
                    )
                    break
                continue
            consecutive_errors = 0

            genres = res.get("genres") or []
            provenance = {"country": "musicbrainz" if res.get("country") else None,
                          "genres": "musicbrainz" if genres else None}

            # ── Tier 2: the artist's own files ──────────────────────────────
            if not genres:
                file_tags = source_genres.get(name) or []
                if file_tags:
                    genres = [{"name": t, "count": 1} for t in file_tags[:8]]
                    provenance["genres"] = "files"
                    counts["from_files"] += 1

            # ── Tier 3: Qobuz — the store this library was bought from ──────
            # Ahead of Deezer deliberately. On the nine artists that survived
            # MusicBrainz + file tags, Qobuz resolved seven and Deezer two: the
            # artist strings ARE Qobuz's own spellings (most of this library was
            # downloaded from it), and it returns 35-158 albums per artist so a
            # genre consensus is real evidence rather than one record's label.
            if not genres and use_qobuz:
                if qobuz_client is None and not qobuz_tried:
                    qobuz_tried = True
                    from utils.metadata_qobuz import open_client
                    qobuz_client = await open_client()
                if qobuz_client is not None:
                    try:
                        from utils.metadata_qobuz import lookup_artist_genres as qz
                        qres = await qz(qobuz_client, name)
                    except Exception as exc:
                        logger.warning("Qobuz tier raised for %r: %s", name, exc)
                        qres = {"genres": []}
                    if qres.get("genres"):
                        genres = qres["genres"]
                        provenance["genres"] = "qobuz"
                        counts["from_qobuz"] += 1

            if not genres:
                reasons[res.get("reason") or "no_tags"] += 1

            # A supplemented artist is resolved, not 'lowconfidence on tags'.
            if genres and st == "lowconfidence" and res.get("mbid") is None:
                st = "ok"

            # Report the tier that actually supplied the genres, so the UI's
            # live counters can separate "MusicBrainz knew this" from "we had
            # to fall back" from "still nothing".
            res = {**res, "genres": genres,
                   "reason": provenance["genres"] if provenance["genres"] in
                   ("files", "qobuz") else res.get("reason")}
            await db_manager.upsert_artist_enrichment(
                name, mbid=res.get("mbid"), country=res.get("country"),
                area=res.get("area"), genres=genres,
                score=res.get("score"), status=st, provenance=provenance,
            )
            counts[st] += 1
            done += 1
            if progress:
                progress(i, len(artists), name, res)

    if qobuz_client is not None:
        from utils.metadata_qobuz import close_client
        await close_client(qobuz_client)

    refresh = await _do_refresh_walk_models(db_manager) if done else {}

    summary = {
        "enriched": done,
        "status": status,
        "total": len(artists),
        "reasons": dict(reasons),
        **refresh,
        **counts,
    }
    log = logger.error if status in ("aborted",) else logger.info
    log("enrich_library: %s", summary)
    return summary
