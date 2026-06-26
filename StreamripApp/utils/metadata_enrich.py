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


def _name_close(a: str, b: str) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


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


def _best_match(name: str, artists: list[dict]) -> dict | None:
    """MusicBrainz returns artists score-sorted. Prefer the highest-scoring one
    whose name actually matches; otherwise fall back to the top result."""
    if not artists:
        return None
    for a in artists:
        if _name_close(name, a.get("name", "")):
            return a
    return artists[0]


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
        raises — failures come back as status='error'/'notfound'."""
        empty = {"status": "notfound", "mbid": None, "country": None,
                 "area": None, "genres": [], "score": 0}
        if not name or not name.strip():
            return empty

        q = _escape_lucene(name.strip())
        data, status = await self._get(
            "artist", {"query": f'artist:"{q}"', "fmt": "json", "limit": 5}
        )
        if data is None:
            return {**empty, "status": "error"}

        best = _best_match(name, data.get("artists") or [])
        if best is None:
            return empty

        try:
            score = int(best.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0
        mbid = best.get("id")
        country = best.get("country")
        area = (best.get("area") or {}).get("name")
        genres = _extract_genres(best)
        ok = score >= 90 and _name_close(name, best.get("name", ""))
        result = {
            "status": "ok" if ok else "lowconfidence",
            "mbid": mbid, "country": country, "area": area,
            "genres": genres, "score": score,
        }

        # The search payload often omits genres; a direct lookup is reliable.
        if with_genres and mbid:
            gdata, _ = await self._get(
                f"artist/{mbid}", {"inc": "genres+tags", "fmt": "json"}
            )
            if gdata:
                g2 = _extract_genres(gdata)
                if g2:
                    result["genres"] = g2
                result["country"] = result["country"] or gdata.get("country")
                result["area"] = result["area"] or (gdata.get("area") or {}).get("name")
        return result


async def enrich_library(
    db_manager, *, with_genres: bool = True, limit=None,
    contact: str = DEFAULT_CONTACT, include_failed: bool = False,
    max_consecutive_errors: int = 3, progress=None,
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

    summary = {"enriched": done, "model_pairs": model_pairs, **counts}
    logger.info("enrich_library: %s", summary)
    return summary
