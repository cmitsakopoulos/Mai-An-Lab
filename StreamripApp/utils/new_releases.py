"""New releases by artists already in the library, for the Search landing page.

Neither source needs a sign-in, so the landing page never depends on the
user's Qobuz/Deezer tokens (which expire):

  * Deezer's public catalogue API (api.deezer.com): artist search plus
    discography, with cover art and exact release dates. The ARL cookie is only
    needed for streaming and downloads, never for these reads.
  * MusicBrainz release-groups, for artists Deezer can't identify but the
    metadata pass already pinned to an MBID. Covers come from the Cover Art
    Archive by URL, so they cost no request until shown.

Identity is the hard part: a name search can return "the other artist called
X". A Deezer artist is accepted only when its name matches exactly (ignoring
case and accents) and, when the library holds albums by that artist, at least
one of its Deezer titles matches one of them. Without owned albums to check
against, a single exact-name hit is required.

Results are cached per artist in `artist_releases` and refreshed weekly by a
background pass (`refresh_new_releases`); the landing page only ever reads the
cache (`get_new_releases`).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
import time
import unicodedata

try:
    import aiohttp
except Exception:  # pragma: no cover - import guarded so the app still loads
    aiohttp = None  # type: ignore

logger = logging.getLogger(__name__)

DEEZER_API = "https://api.deezer.com"
COVER_ART_ARCHIVE = "https://coverartarchive.org/release-group/{id}/front-250"

REFRESH_AFTER_S = 7 * 24 * 3600   # re-check each artist weekly
KEEP_DAYS = 730                    # what the cache stores per artist
SHOW_DAYS = 365                    # what the landing page calls "new"
SINGLE_DAYS = 45                   # ...and for singles, which come and go
_DEEZER_INTERVAL_S = 0.15          # Deezer allows 50 requests / 5 s
_CREDIT_SEPARATORS = re.compile(r",\s|\s&\s|\sfeat\.?\s|\sft\.?\s|\sx\s", re.I)
_VARIANT = re.compile(r"\b(remix|live|sped up|slowed|instrumental|acoustic version)\b", re.I)
_SKIP_ARTISTS = {"various artists", "various", "unknown", "unknown artist", ""}


def norm(text: str) -> str:
    """Comparable form of a title or name: no accents, case, bracketed
    edition notes ("(Deluxe)", "[Remastered]") or punctuation."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"\s-\s.*(edition|version|remaster).*$", " ", s)
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())


def is_lookup_candidate(name: str) -> bool:
    """Solo artist entries only. Credit strings ("21 Savage & Metro Boomin")
    match nobody exactly, and their members have entries of their own."""
    return norm(name) not in _SKIP_ARTISTS and not _CREDIT_SEPARATORS.search(name or "")


def _cutoff(days: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=days)).isoformat()


class ReleaseFetcher:
    """Discography lookups over one shared aiohttp session. Each lookup
    returns (status, source_id, releases) with status 'ok', 'notfound' or
    'error' (network/HTTP trouble: don't cache, try again next pass)."""

    def __init__(self, session, mb_client=None):
        self.session = session
        self.mb = mb_client
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def _deezer(self, path: str, params: dict | None = None):
        timeout = aiohttp.ClientTimeout(total=15)
        for attempt in range(2):
            async with self._lock:
                wait = _DEEZER_INTERVAL_S - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    async with self.session.get(f"{DEEZER_API}/{path}", params=params,
                                                timeout=timeout) as resp:
                        self._last = time.monotonic()
                        if resp.status != 200:
                            logger.warning("Deezer HTTP %s on %s", resp.status, path)
                            return None
                        data = await resp.json(content_type=None)
                except Exception as exc:
                    self._last = time.monotonic()
                    logger.warning("Deezer request failed on %s: %s: %s",
                                   path, type(exc).__name__, exc)
                    return None
            err = data.get("error") if isinstance(data, dict) else None
            if not err:
                return data
            if err.get("code") == 4 and attempt == 0:   # quota: back off once
                await asyncio.sleep(5)
                continue
            logger.warning("Deezer error on %s: %s", path, err)
            return None
        return None

    async def _deezer_albums(self, artist_id) -> list[dict] | None:
        data = await self._deezer(f"artist/{artist_id}/albums", {"limit": 200})
        if data is None:
            return None
        return [a for a in data.get("data") or [] if isinstance(a, dict)]

    async def deezer_releases(self, name: str, owned_titles: set[str],
                              known_id: str | None = None):
        if known_id:
            albums = await self._deezer_albums(known_id)
            if albums is None:
                return "error", None, None
            return "ok", str(known_id), _from_deezer(albums)

        found = await self._deezer("search/artist", {"q": name, "limit": 10})
        if found is None:
            return "error", None, None
        target = norm(name)
        candidates = [a for a in found.get("data") or []
                      if isinstance(a, dict) and norm(a.get("name")) == target][:3]
        if not candidates:
            return "notfound", None, []
        if not owned_titles and len(candidates) > 1:
            # Several artists share this name and nothing owned tells them apart.
            return "notfound", None, []
        for cand in candidates:
            albums = await self._deezer_albums(cand["id"])
            if albums is None:
                return "error", None, None
            if not owned_titles or owned_titles & {norm(a.get("title")) for a in albums}:
                return "ok", str(cand["id"]), _from_deezer(albums)
        return "notfound", None, []

    async def musicbrainz_releases(self, mbid: str):
        if self.mb is None or not mbid:
            return "notfound", None, []
        data, status = await self.mb._get(
            "release-group", {"artist": mbid, "type": "album|ep|single",
                              "limit": 100, "fmt": "json"})
        if data is None:
            return ("notfound", None, []) if status in (400, 404) else ("error", None, None)
        return "ok", mbid, _from_musicbrainz(data.get("release-groups") or [])


def _from_deezer(albums: list[dict]) -> list[dict]:
    cutoff = _cutoff(KEEP_DAYS)
    out = []
    for a in albums:
        date = str(a.get("release_date") or "")
        if date < cutoff or a.get("record_type") == "compile":
            continue
        out.append({
            "title": a.get("title") or "",
            "date": date,
            "type": a.get("record_type") or "album",
            "cover": a.get("cover_medium") or "",
            "source": "deezer",
        })
    return out


def _from_musicbrainz(groups: list[dict]) -> list[dict]:
    cutoff = _cutoff(KEEP_DAYS)
    out = []
    for g in groups:
        date = str(g.get("first-release-date") or "")
        # Live sets, compilations and remixes ride along in a browse; they
        # aren't what "a new release from this artist" means.
        if date < cutoff or g.get("secondary-types"):
            continue
        out.append({
            "title": g.get("title") or "",
            "date": date,
            "type": (g.get("primary-type") or "album").lower(),
            "cover": COVER_ART_ARCHIVE.format(id=g.get("id")),
            "source": "musicbrainz",
        })
    return out


async def refresh_new_releases(db, *, session=None, mb_client=None,
                               refresh_after_s: float = REFRESH_AFTER_S) -> int:
    """Re-check every library artist whose cache entry is missing or older
    than `refresh_after_s`. Returns how many artists were (re)written. Stops
    early when the network looks down, leaving the rest for the next pass."""
    own_session = session is None
    if own_session:
        if aiohttp is None:
            return 0
        session = aiohttp.ClientSession()
    try:
        if mb_client is None:
            from utils.metadata_enrich import MusicBrainzClient, DEFAULT_CONTACT
            mb_client = MusicBrainzClient(session, contact=DEFAULT_CONTACT)
        fetcher = ReleaseFetcher(session, mb_client)
        cache = await db.get_artist_release_cache()
        now = time.time()
        written = 0
        errors_in_a_row = 0
        for name in await db.get_release_lookup_artists():
            if not is_lookup_candidate(name):
                continue
            row = cache.get(name.lower())
            if row and now - row["fetched_at"] < refresh_after_s:
                continue
            albums, tracks = await db.get_artist_owned_titles(name)
            # Track titles count too: an owned single is an album on Deezer.
            owned = {norm(t) for t in (*albums, *tracks)} - {""}
            known = row["source_id"] if row and row["source"] == "deezer" else None
            status, source_id, releases = await fetcher.deezer_releases(name, owned, known)
            source = "deezer"
            if status == "notfound":
                enr = await db.get_artist_enrichment(name) or {}
                if enr.get("mbid"):
                    status, source_id, releases = await fetcher.musicbrainz_releases(enr["mbid"])
                    source = "musicbrainz"
            if status == "error":
                errors_in_a_row += 1
                if errors_in_a_row >= 3:
                    logger.warning("New releases: network looks down, stopping this pass")
                    break
                continue
            errors_in_a_row = 0
            if status == "notfound":
                source, source_id, releases = "none", None, []
            await db.save_artist_releases(name, source, source_id, releases)
            written += 1
        return written
    finally:
        if own_session:
            await session.close()


async def get_new_releases(db, *, days: int = SHOW_DAYS, limit: int = 12,
                           per_artist: int = 2) -> list[dict]:
    """Cached releases the library doesn't already hold, newest first.

    Albums and EPs count as new for `days`; singles only for SINGLE_DAYS and
    never as remixes, live cuts or sped-up edits, otherwise a busy artist's
    singles bury everyone's albums. At most `per_artist` per artist, and an
    album beats a single of the same name."""
    today = _dt.date.today().isoformat()
    cutoff = _cutoff(days)
    single_cutoff = _cutoff(min(days, SINGLE_DAYS))
    cache = await db.get_artist_release_cache()
    picked: dict[tuple[str, str], dict] = {}
    owned_cache: dict[str, set[str]] = {}
    for row in cache.values():
        artist = row["artist_name"]
        for rel in row["releases"]:
            date = rel.get("date") or ""
            single = rel.get("type") == "single"
            if not ((single_cutoff if single else cutoff) <= date <= today):
                continue
            if single and _VARIANT.search(rel.get("title") or ""):
                continue
            key = (artist.lower(), norm(rel.get("title")))
            if artist not in owned_cache:
                albums, tracks = await db.get_artist_owned_titles(artist)
                owned_cache[artist] = {norm(t) for t in (*albums, *tracks)}
            if key[1] in owned_cache[artist]:
                continue
            prev = picked.get(key)
            if prev is None or (prev["type"] != "album" and rel.get("type") == "album"):
                picked[key] = dict(rel, artist=artist)

    out, per = [], {}
    for rel in sorted(picked.values(), key=lambda r: r["date"], reverse=True):
        a = rel["artist"].lower()
        if per.get(a, 0) >= per_artist:
            continue
        per[a] = per.get(a, 0) + 1
        out.append(rel)
        if len(out) >= limit:
            break
    return out
