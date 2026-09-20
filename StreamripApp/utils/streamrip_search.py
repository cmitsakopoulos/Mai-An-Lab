"""Streamrip search across the supported backends (Qobuz, Deezer)."""

import logging
import threading
import asyncio
from .qobuz import QobuzClient
from .config import Config

logger = logging.getLogger(__name__)

# Mirrors ui.tokens.SOURCE_COLORS. Kept as a literal because utils/ must not
# import from ui/ (the UI layer imports utils, not the other way round), so any
# change here needs the same edit there.
_SOURCE_COLORS = {
    "qobuz": "#FF9F0A",   # Apple System Orange (gold)
    "deezer": "#BF5AF2",  # Apple System Purple
}
_DEFAULT_COLOR = "#FFFFFF"

SUPPORTED_SOURCES = ("qobuz", "deezer")

# Web URL templates, used to build a link the download path can parse back into
# (media_type, id, source) via streamrip_api._detect_type_and_id.
_SOURCE_URL_TEMPLATES = {
    "qobuz": "https://www.qobuz.com/{media_type}/{id}",
    "deezer": "https://www.deezer.com/{media_type}/{id}",
}


def _page_items(source: str, page: dict, media_type: str) -> list:
    """Items out of one raw search page.

    Qobuz paginates as {"tracks": {"items": [...]}}; Deezer returns a flat
    {"data": [...]} regardless of media type.
    """
    if source == "deezer":
        return page.get("data", []) or []
    return page.get(f"{media_type}s", {}).get("items", []) or []

class StreamripSearcher:
    _loop = None
    _thread = None
    # One cached, logged-in client per source.
    _clients: dict = {}
    _client_lock = None
    _last_activity = 0.0
    _cleanup_task = None
    _inactivity_timeout = 300.0  # 5 minutes inactivity timeout in seconds

    @classmethod
    def _get_loop(cls):
        if cls._loop is None:
            cls._loop = asyncio.new_event_loop()
            cls._thread = threading.Thread(
                target=cls._loop.run_forever,
                name="StreamripSearcherWorker",
                daemon=True,
            )
            cls._thread.start()
        cls._update_activity()
        return cls._loop

    @classmethod
    def _update_activity(cls):
        import time
        cls._last_activity = time.time()
        if cls._loop is not None:
            if cls._cleanup_task is None or cls._cleanup_task.done():
                cls._cleanup_task = asyncio.run_coroutine_threadsafe(
                    cls._inactivity_monitor(),
                    cls._loop
                )

    @classmethod
    async def _inactivity_monitor(cls):
        import time
        while True:
            await asyncio.sleep(15)  # Check every 15 seconds
            if cls._loop is None:
                break
            elapsed = time.time() - cls._last_activity
            if elapsed >= cls._inactivity_timeout:
                logger.info("StreamripSearcher: Inactivity timeout reached (%ds). Cleaning up network sessions...", cls._inactivity_timeout)
                
                # 1. Close every active client session
                if cls._client_lock is not None:
                    async with cls._client_lock:
                        for src, client in list(cls._clients.items()):
                            try:
                                if client.session and not client.session.closed:
                                    await client.session.close()
                            except Exception as e:
                                logger.error("Error closing %s client session: %s", src, e)
                        cls._clients.clear()
                
                # 2. Stop event loop and thread
                if cls._loop is not None:
                    cls._loop.stop()
                
                cls._loop = None
                cls._thread = None
                cls._client_lock = None
                cls._cleanup_task = None
                break

    def __init__(self, config_path=None):
        from .streamrip_api import get_config_path
        self.config_path = config_path or get_config_path()

    @staticmethod
    def _credentials_changed(source: str, old_cfg, new_cfg) -> bool:
        """Whether a cached client's stored credentials are now stale."""
        if source == "deezer":
            return old_cfg.session.deezer.arl != new_cfg.session.deezer.arl
        old_c = old_cfg.session.qobuz
        new_c = new_cfg.session.qobuz
        return (old_c.email_or_userid != new_c.email_or_userid or
                old_c.password_or_token != new_c.password_or_token or
                getattr(old_c, "app_id", None) != getattr(new_c, "app_id", None) or
                old_c.use_auth_token != new_c.use_auth_token)

    async def _get_client(self, source: str = "qobuz", progress_callback=None):
        source = (source or "qobuz").lower()
        if source not in SUPPORTED_SOURCES:
            raise Exception(f"Source '{source}' is not supported.")

        if StreamripSearcher._client_lock is None:
            StreamripSearcher._client_lock = asyncio.Lock()
            
        async with StreamripSearcher._client_lock:
            from .config import Config
            config = Config(self.config_path)
            cached = StreamripSearcher._clients.get(source)

            # Reset client if credentials changed in the configuration
            if cached is not None and self._credentials_changed(source, cached.config_root, config):
                logger.info("%s credentials changed, resetting client session.", source)
                try:
                    if hasattr(cached, "close"):
                        await cached.close()
                    elif cached.session and not cached.session.closed:
                        await cached.session.close()
                except Exception as e:
                    logger.error("Error closing %s client session: %s", source, e)
                cached = None
                StreamripSearcher._clients.pop(source, None)

            if cached is None or getattr(cached, "session", None) is None or cached.session.closed:
                if source == "deezer":
                    from .deezer import DeezerClient
                    client = DeezerClient(config)
                else:
                    from .qobuz import QobuzClient
                    client = QobuzClient(config)
                # The clients keep only their own config section, but the
                # staleness check above needs the whole tree.
                client.config_root = config
                if progress_callback:
                    progress_callback("Authenticating", "Signing in\u2026")
                await client.login()
                StreamripSearcher._clients[source] = client
            return StreamripSearcher._clients[source]

    async def get_track_stream_url(self, track_id: str, quality: int = 1, source: str = "qobuz") -> str:
        loop = self._get_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._get_track_stream_url_async(track_id, quality, source),
            loop
        )
        return await asyncio.wrap_future(future)

    async def _get_track_stream_url_async(self, track_id: str, quality: int, source: str = "qobuz") -> str:
        client = await self._get_client(source)
        downloadable = await client.get_downloadable(track_id, quality)
        return downloadable.url


    def get_artist_albums(self, artist_id: str, callback, limit: int = 30, offset: int = 0, source: str = "qobuz") -> None:
        loop = self._get_loop()
        asyncio.run_coroutine_threadsafe(
            self._run_artist_albums_wrapper(artist_id, callback, limit, offset, source),
            loop
        )

    async def _run_artist_albums_wrapper(self, artist_id, callback, limit, offset, source="qobuz"):
        try:
            results = await self._get_artist_albums_async(artist_id, limit, offset, source)
        except Exception as exc:
            logger.error("Get artist albums failed: %s", exc)
            results = []
        callback(results)

    async def _get_artist_albums_async(self, artist_id, limit, offset, source="qobuz"):
        source = (source or "qobuz").lower()
        client = await self._get_client(source)

        if source == "deezer":
            # The Deezer client returns every album in one flat list and takes
            # no paging arguments, so the window is applied here instead.
            resp = await client.get_metadata(artist_id, "artist")
            raw_albums = (resp.get("albums", []) or [])[offset:offset + limit]
        else:
            resp = await client.get_metadata(artist_id, "artist", limit=limit, offset=offset)
            albums_data = resp.get("albums", {})
            raw_albums = albums_data.get("items", [])[:limit]

        for a in raw_albums:
            a["_media_type"] = "album"
            
        parsed = self._parse_results(raw_albums, source)
        
        if len(raw_albums) > 0:
            # Always offer load more if we just got results (User preference for reliability)
            parsed.append({
                "media_type": "load_more_artist",
                "id": artist_id,
                "source": source,
                "offset": offset + limit,
                "limit": limit,
                "ui_title": "Load More",
                "name": "Load More"
            })
        elif offset > 0:
            # Only show exhausted if we actually tried to paginate and got nothing back
            parsed.append({
                "media_type": "search_exhausted",
                "ui_title": "All albums loaded",
                "name": "Exhausted"
            })
            
        return parsed

    def get_album_tracks(self, album_id: str, callback, source: str = "qobuz") -> None:
        loop = self._get_loop()
        asyncio.run_coroutine_threadsafe(
            self._run_album_tracks_wrapper(album_id, callback, source),
            loop
        )

    async def _run_album_tracks_wrapper(self, album_id, callback, source="qobuz"):
        try:
            results = await self._get_album_tracks_async(album_id, source)
        except Exception as exc:
            logger.error("Get album tracks failed: %s", exc)
            results = []
        callback(results)

    async def _get_album_tracks_async(self, album_id, source="qobuz"):
        source = (source or "qobuz").lower()
        client = await self._get_client(source)
        resp = await client.get_metadata(album_id, "album")
        raw = resp.get("tracks", [])
        # Qobuz nests its track list one level deeper than Deezer.
        raw_tracks = raw.get("items", []) if isinstance(raw, dict) else raw
        for t in raw_tracks:
            t["_media_type"] = "track"
            # Album-track payloads carry no album object of their own; the
            # tagging step needs one to name the album and find cover art.
            t.setdefault("album", resp)
        return self._parse_results(raw_tracks, source)

    def search(self, query: str, source: str, callback, media_types=None, limit: int = 50, offset: int = 0, progress_callback=None) -> None:
        source = (source or "qobuz").lower()
        if source not in SUPPORTED_SOURCES:
            callback({"error": f"Source '{source}' is not supported. Available: {', '.join(SUPPORTED_SOURCES)}."})
            return
        
        query = query.strip()
        loop = self._get_loop()
        asyncio.run_coroutine_threadsafe(
            self._run_search_wrapper(query, media_types or ["track", "album"], limit, offset, callback, progress_callback, source),
            loop
        )

    async def _run_search_wrapper(self, query, media_types, limit, offset, callback, progress_callback=None, source="qobuz"):
        try:
            results = await self._search_async(query, media_types, limit, offset, progress_callback, source)
        except Exception as exc:
            logger.error("Search failed: %s", exc, exc_info=True)
            results = {"error": str(exc)}
        callback(results)

    async def _search_async(self, query: str, media_types: list, limit: int = 50, offset: int = 0, progress_callback=None, source: str = "qobuz") -> list:
        from .exceptions import MissingCredentialsError, AuthenticationError
        source = (source or "qobuz").lower()
        label = source.title()
        missing_creds_hint = (
            "Deezer credentials are missing. Please enter your ARL cookie in the Settings tab."
            if source == "deezer" else
            "Qobuz credentials are missing. Please enter your User ID and Token in the Settings tab."
        )
        try:
            if progress_callback:
                progress_callback("Connecting", "Contacting API\u2026")
            client = await self._get_client(source, progress_callback)
        except MissingCredentialsError:
            raise Exception(missing_creds_hint)
        except AuthenticationError:
            raise Exception(f"{label} authentication failed. Please check your credentials in the Settings tab.")
        except Exception as exc:
            raise Exception(f"Connection failed: {exc}")

        async def _fetch_type(m_type: str) -> list:
            pages = await client.search(m_type, query, limit=limit, offset=offset)
            items_out = []
            for page in pages:
                for item in _page_items(source, page, m_type):
                    if isinstance(item, dict):
                        item["_media_type"] = m_type
                        items_out.append(item)
            return items_out

        # Fetch each media type independently. A failure on a single type must
        # not sink the whole search — but if EVERY type errors out we're looking
        # at a connection / auth / app-secret problem, not an empty result set.
        # In that case propagate a clear message so the UI can show an actionable
        # error instead of a misleading "No results found".
        results_per_type = []
        fetch_errors: list[Exception] = []
        any_ok = False
        for m in media_types:
            if progress_callback:
                progress_callback("Searching", f"Looking up {m}s\u2026")
            try:
                items = await _fetch_type(m)
            except Exception as exc:
                logger.warning("%s search %s failed: %s", label, m, exc, exc_info=True)
                fetch_errors.append(exc)
                continue
            any_ok = True
            results_per_type.append(items)

        if not any_ok and fetch_errors:
            raise self._humanize_search_error(fetch_errors[0])

        if progress_callback:
            progress_callback("Processing", "Formatting results\u2026")

        raw = []
        for items in results_per_type:
            raw.extend(items)

        return self._parse_results(raw, source)

    @staticmethod
    def _humanize_search_error(exc: Exception) -> Exception:
        """Translate a raw Qobuz/transport exception into a concise, actionable
        message for the search screen. The API surfaces auth / bad-app-secret
        failures as a non-200 status embedded in an AssertionError string
        ("Status: 401, Response: ..."), so we sniff that out first."""
        import re
        msg = str(exc).strip() or exc.__class__.__name__
        low = msg.lower()
        status_match = re.search(r"status[:=]\s*(\d{3})", msg, re.IGNORECASE)
        status = status_match.group(1) if status_match else None

        if type(exc).__name__ == "InvalidAppSecretError" or "app secret" in low or "appsecret" in low:
            return Exception("Qobuz rejected the App Secret. Check the App ID / Secret in the Settings tab.")
        if status in ("400", "401", "403"):
            return Exception(
                f"Qobuz rejected the request (HTTP {status}). Your App ID / Secret or "
                "credentials look invalid — verify them in the Settings tab."
            )
        if status == "429":
            return Exception("Qobuz is rate-limiting requests (HTTP 429). Wait a moment and try again.")
        if any(k in low for k in ("timeout", "timed out", "connect", "getaddrinfo",
                                  "resolve", "network", "ssl", "connection reset", "unreachable")):
            return Exception("Couldn't reach Qobuz — check your internet connection and try again.")
        return Exception(f"Qobuz search failed: {msg}")

    def _parse_results(self, raw_items, source):
        # We wrap the raw_items in a dict that mimics the expected page format
        pages = [{"items": raw_items}]
        from .search_results import SearchResults, TrackSummary, AlbumSummary
        # Our raw_items already have _media_type injected, so we group them
        tracks    = [i for i in raw_items if i.get("_media_type") == "track"]
        albums    = [i for i in raw_items if i.get("_media_type") == "album"]
        artists   = [i for i in raw_items if i.get("_media_type") == "artist"]
        playlists = [i for i in raw_items if i.get("_media_type") == "playlist"]
        
        results = []
        if tracks:
            sr_tracks = SearchResults.from_pages(source, "track", [{"tracks": {"items": tracks}}])
            results.extend(sr_tracks.results)
        if albums:
            sr_albums = SearchResults.from_pages(source, "album", [{"albums": {"items": albums}}])
            results.extend(sr_albums.results)
        if artists:
            sr_artists = SearchResults.from_pages(source, "artist", [{"artists": {"items": artists}}])
            results.extend(sr_artists.results)
        if playlists:
            sr_playlists = SearchResults.from_pages(source, "playlist", [{"playlists": {"items": playlists}}])
            results.extend(sr_playlists.results)
            
        parsed = []
        for r in results:
            m_type = r.media_type()
            date_released = getattr(r, "date_released", None)
            year = date_released.split("-")[0][:4] if date_released and date_released != "Unknown" else "N/A"
            image_url = getattr(r, "image_url", "")
            album_name = getattr(r, "album_name", "")
            
            detail_parts = []
            item_name = getattr(r, "name", "")
            if album_name and (m_type != "track" or album_name.strip().lower() != item_name.strip().lower()):
                detail_parts.append(album_name)
            if year != "N/A": detail_parts.append(year)
            detail_parts.append(m_type.upper())

            parsed.append({
                "id": r.id,
                "name": getattr(r, "name", "Unknown"),
                "artist": getattr(r, "artist", ""),
                "source": source,
                "media_type": m_type,
                "url": _SOURCE_URL_TEMPLATES.get(
                    source, _SOURCE_URL_TEMPLATES["qobuz"]
                ).format(media_type=m_type, id=r.id),
                "year": year,
                "album": album_name,
                "image": image_url,
                "ui_title": getattr(r, "name", "Unknown"),
                "ui_subtitle": getattr(r, "artist", f"{getattr(r, 'num_albums', '?')} Albums" if m_type == "artist" else ""),
                "ui_detail": "  •  ".join(detail_parts),
                "ui_source_color": _SOURCE_COLORS.get(source, _DEFAULT_COLOR),
            })

        # Qobuz returns the same recording wrapped in multiple search hits when
        # the track exists on a deluxe album, standard album, single, or compilation.
        # Deduplicate strictly by metadata (media_type, title, artist) for tracks/albums
        # and by (media_type, title) for other items, preserving order of first occurrence.
        seen: set = set()
        deduped = []
        for entry in parsed:
            m_type = entry["media_type"]
            title = str(entry.get("ui_title", entry.get("name", ""))).strip().lower()
            artist = str(entry.get("ui_subtitle", entry.get("artist", ""))).strip().lower()
            
            if m_type in ("track", "album"):
                key = (m_type, title, artist)
            else:
                key = (m_type, title)
                
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        return deduped
