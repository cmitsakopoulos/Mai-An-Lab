"""Artist metadata enrichment via the MusicBrainz web service (aiohttp).

Why MusicBrainz: it's open (CC0), free, and — crucially for the Greek-vs-US
hip-hop problem — exposes artist *area/country*, an objective provenance signal
that no acoustic feature can recover. We also pull community genre/tag labels.

Design notes:
  • Pure aiohttp + stdlib, so it compiles for Android exactly like the rest of
    the networking layer; nothing here imports numpy / the audio stack.
  • MusicBrainz asks for ≤ 1 request/second and a descriptive User-Agent with a
    contact. `MusicBrainzClient` self-throttles and sets that header, and backs
    off on HTTP 503 (their rate-limit response).
  • Enrichment is keyed/cached per ARTIST (stable, ~hundreds per library), never
    per track, so the whole library is a few hundred polite requests.
"""

from __future__ import annotations

import asyncio
import logging
import time
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


def _escape_lucene(s: str) -> str:
    return "".join("\\" + c if c in _LUCENE_SPECIAL else c for c in (s or ""))


def _norm_name(s: str) -> str:
    """Loose key for comparing artist names: lowercase alphanumerics only."""
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _norm_tokens(s: str) -> set[str]:
    """Extract lowercased alphanumeric word tokens."""
    return set("".join(c if c.isalnum() else " " for c in (s or "").lower()).split())


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
        """Throttled GET → (json|None, http_status). Honours 503 with backoff."""
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        timeout = aiohttp.ClientTimeout(total=20)
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
                        else:
                            return None, resp.status
                except Exception as exc:
                    self._last = time.monotonic()
                    logger.debug("MB request error (%s): %s", path, exc)
                    status = -1
            # backoff happens outside the lock so we don't pin it while sleeping
            await asyncio.sleep(1.5 * (attempt + 1))
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
                 "area": None, "genres": [], "score": 0}
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
        #     non-junk hit (so multi-artist strings like 'Digga D, Sav'O' still
        #     get a best-effort genre), but never to a tribute/karaoke entity.
        # Combined Lucene query to search name, sortname, alias, and unquoted terms
        # in a single request (5x faster than 5 sequential HTTP calls).
        combined_q = f'artist:"{q}" OR alias:"{q}" OR artist:{q} OR {q}'
        if sort_name_q:
            combined_q = f'artist:"{q}" OR sortname:"{sort_name_q}" OR alias:"{q}" OR artist:{q} OR {q}'

        best = None
        matched = False       # True = genuine name/alias match; False = weak fallback
        weak = None           # first non-junk hit, used only if nothing matches
        saw_error = False

        data, status = await self._get(
            "artist", {"query": combined_q, "fmt": "json", "limit": 10}
        )
        if data is None:
            saw_error = True
        else:
            arts = data.get("artists") or []
            m = _closest_match(raw_name, arts)
            if m is not None:
                best, matched = m, True
            else:
                weak = next((a for a in arts if not _looks_like_junk(a)), None)

        # ── No genuine match: try DECOMPOSING a multi-artist credit ───────────
        if not matched:
            members = split_artist_credits(raw_name)
            if members:
                merged = await self._lookup_credit_members(
                    members, with_genres=with_genres,
                )
                if merged is not None:
                    return merged

        if best is None:
            best = weak
        if best is None:
            # Nothing usable (all junk, or requests failed). notfound → retried.
            return {**empty, "status": "error" if saw_error else "notfound"}

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
                "score": score,
            }

        country = _extract_country(best)
        area = (best.get("area") or {}).get("name")
        genres = _extract_genres(best)

        # 'ok' requires a genuine name/alias match AND a high MB score.
        result = {
            "status": "ok" if score >= 90 else "lowconfidence",
            "mbid": mbid, "country": country, "area": area,
            "genres": genres, "score": score,
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
            if sub.get("status") not in ("ok", "lowconfidence"):
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
        return {
            "status": "ok" if resolved == len(members[:4]) else "lowconfidence",
            "mbid": mbids[0] if mbids else None,
            "country": country,
            "area": None,
            "genres": [
                {"name": n, "count": c} for n, c in genre_counts.most_common(8)
            ],
            "score": 100 if resolved == len(members[:4]) else 50,
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


async def enrich_library(
    db_manager, *, with_genres: bool = True, limit=None,
    contact: str = DEFAULT_CONTACT, include_failed: bool = False,
    max_consecutive_errors: int = 3, progress=None,
    cancel_event: asyncio.Event | None = None,
) -> dict:
    """Incrementally enrich artists that have no cached metadata yet.

    Only artists without an enrichment row are fetched, so calling this after
    each library index enriches just the *new* artists. Transient request
    failures (offline / rate cap) are NOT persisted — the artist stays
    'needing' and is retried on the next index — and the pass aborts after a few
    consecutive errors (almost certainly offline). On success it refreshes the
    NPMI genre model so the walk's metadata gate stays current.

    Returns a summary dict. Never raises; safe to fire-and-forget."""
    if aiohttp is None:
        return {"enriched": 0, "status": "no_aiohttp"}
    try:
        artists = await db_manager.get_artists_needing_enrichment(
            limit=limit, include_failed=include_failed,
        )
    except Exception as exc:
        return {"enriched": 0, "status": f"error: {exc}"}
    if not artists:
        return {"enriched": 0, "status": "uptodate"}

    counts: Counter = Counter()
    done = 0
    consecutive_errors = 0
    async with aiohttp.ClientSession() as session:
        client = MusicBrainzClient(session, contact=contact)
        for i, name in enumerate(artists, 1):
            if cancel_event and cancel_event.is_set():
                counts["cancelled"] = 1
                break
            try:
                res = await client.lookup_artist(name, with_genres=with_genres)
            except Exception:
                res = {"status": "error", "mbid": None, "country": None,
                       "area": None, "genres": [], "score": 0}
            status = res["status"]
            if status == "error":
                consecutive_errors += 1
                counts["error"] += 1
                if progress:
                    progress(i, len(artists), name, res)
                if consecutive_errors >= max_consecutive_errors:
                    counts["aborted"] = 1
                    break
                continue
            consecutive_errors = 0
            await db_manager.upsert_artist_enrichment(
                name, mbid=res.get("mbid"), country=res.get("country"),
                area=res.get("area"), genres=res.get("genres"),
                score=res.get("score"), status=status,
            )
            counts[status] += 1
            done += 1
            if progress:
                progress(i, len(artists), name, res)

    model_pairs = None
    if done:
        # Refresh the NPMI genre model from the updated enrichment so the walk
        # gate is current without waiting for a full graph rebuild.
        try:
            from utils.track_graph import build_genre_affinity
            model_pairs = await build_genre_affinity(db_manager)
        except Exception as exc:
            logger.debug("genre model refresh after enrichment failed: %s", exc)

    # Automatically fix and normalize track genres in the database using new API metadata
    norm_summary = None
    try:
        norm_summary = await db_manager.fix_and_normalize_track_genres()
    except Exception as exc:
        logger.debug("Automatic track genre normalization after enrichment failed: %s", exc)

    summary = {"enriched": done, "model_pairs": model_pairs, "genre_fix": norm_summary, **counts}
    logger.info("enrich_library: %s", summary)
    return summary
