"""Streamrip search; Minimal Qobuz-only implementation."""

import logging
import threading
import asyncio
from .qobuz import QobuzClient
from .config import Config

logger = logging.getLogger(__name__)

_SOURCE_COLORS = {
    "qobuz": "#D4AF37",
}
_DEFAULT_COLOR = "#FFFFFF"

class StreamripSearcher:
    _loop = None
    _thread = None
    _client = None
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
                
                # 1. Close active Qobuz client session
                if cls._client_lock is not None:
                    async with cls._client_lock:
                        if cls._client is not None:
                            try:
                                if cls._client.session and not cls._client.session.closed:
                                    await cls._client.session.close()
                            except Exception as e:
                                logger.error("Error closing client session: %s", e)
                            cls._client = None
                
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

    async def _get_client(self):
        if StreamripSearcher._client_lock is None:
            StreamripSearcher._client_lock = asyncio.Lock()
            
        async with StreamripSearcher._client_lock:
            from .config import Config
            config = Config(self.config_path)
            
            # Reset client if credentials changed in the configuration
            if StreamripSearcher._client is not None:
                old_c = StreamripSearcher._client.config.session.qobuz
                new_c = config.session.qobuz
                if (old_c.email_or_userid != new_c.email_or_userid or 
                    old_c.password_or_token != new_c.password_or_token):
                    logger.info("Qobuz credentials changed, resetting client session.")
                    if StreamripSearcher._client.session and not StreamripSearcher._client.session.closed:
                        await StreamripSearcher._client.session.close()
                    StreamripSearcher._client = None

            if StreamripSearcher._client is None or getattr(StreamripSearcher._client, "session", None) is None or StreamripSearcher._client.session.closed:
                from .qobuz import QobuzClient
                StreamripSearcher._client = QobuzClient(config)
                await StreamripSearcher._client.login()
            return StreamripSearcher._client

    def get_artist_albums(self, artist_id: str, callback, limit: int = 30, offset: int = 0) -> None:
        loop = self._get_loop()
        asyncio.run_coroutine_threadsafe(
            self._run_artist_albums_wrapper(artist_id, callback, limit, offset),
            loop
        )

    async def _run_artist_albums_wrapper(self, artist_id, callback, limit, offset):
        try:
            results = await self._get_artist_albums_async(artist_id, limit, offset)
        except Exception as exc:
            logger.error("Get artist albums failed: %s", exc)
            results = []
        callback(results)

    async def _get_artist_albums_async(self, artist_id, limit, offset):
        client = await self._get_client()
        resp = await client.get_metadata(artist_id, "artist", limit=limit, offset=offset)
        
        albums_data = resp.get("albums", {})
        raw_albums = albums_data.get("items", [])
        
        raw_albums = raw_albums[:limit]
        
        for a in raw_albums:
            a["_media_type"] = "album"
            
        parsed = self._parse_results(raw_albums, "qobuz")
        
        if len(raw_albums) > 0:
            # Always offer load more if we just got results (User preference for reliability)
            parsed.append({
                "media_type": "load_more_artist",
                "id": artist_id,
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

    def get_album_tracks(self, album_id: str, callback) -> None:
        loop = self._get_loop()
        asyncio.run_coroutine_threadsafe(
            self._run_album_tracks_wrapper(album_id, callback),
            loop
        )

    async def _run_album_tracks_wrapper(self, album_id, callback):
        try:
            results = await self._get_album_tracks_async(album_id)
        except Exception as exc:
            logger.error("Get album tracks failed: %s", exc)
            results = []
        callback(results)

    async def _get_album_tracks_async(self, album_id):
        client = await self._get_client()
        resp = await client.get_metadata(album_id, "album")
        raw_tracks = resp.get("tracks", {}).get("items", [])
        for t in raw_tracks:
            t["_media_type"] = "track"
        return self._parse_results(raw_tracks, "qobuz")

    def search(self, query: str, source: str, callback, media_types=None, limit: int = 50, offset: int = 0) -> None:
        if source.lower() != "qobuz":
            callback({"error": f"Source '{source}' is not supported in this minimal build."})
            return
        
        query = query.strip()
        loop = self._get_loop()
        asyncio.run_coroutine_threadsafe(
            self._run_search_wrapper(query, media_types or ["track", "album"], limit, offset, callback),
            loop
        )

    async def _run_search_wrapper(self, query, media_types, limit, offset, callback):
        try:
            results = await self._search_async(query, media_types, limit, offset)
        except Exception as exc:
            logger.error("Search failed: %s", exc, exc_info=True)
            results = {"error": str(exc)}
        callback(results)

    async def _search_async(self, query: str, media_types: list, limit: int = 50, offset: int = 0) -> list:
        from .exceptions import MissingCredentialsError, AuthenticationError
        try:
            client = await self._get_client()
        except MissingCredentialsError:
            raise Exception("Qobuz credentials are missing. Please enter your User ID and Token in the Settings tab.")
        except AuthenticationError:
            raise Exception("Qobuz authentication failed. Please check your credentials in the Settings tab.")
        except Exception as exc:
            raise Exception(f"Connection failed: {exc}")

        async def _fetch_type(m_type: str) -> list:
            try:
                pages = await client.search(m_type, query, limit=limit, offset=offset)
                items_out = []
                for page in pages:
                    for item in page.get(f"{m_type}s", {}).get("items", []):
                        if isinstance(item, dict):
                            item["_media_type"] = m_type
                            items_out.append(item)
                return items_out
            except Exception as exc:
                logger.warning("Qobuz search %s: %s", m_type, exc)
                return []

        results_per_type = await asyncio.gather(*[_fetch_type(m) for m in media_types])
        raw = []
        for items in results_per_type:
            raw.extend(items)

        return self._parse_results(raw, "qobuz")

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
            if album_name: detail_parts.append(album_name)
            if year != "N/A": detail_parts.append(year)
            detail_parts.append(m_type.upper())

            parsed.append({
                "id": r.id,
                "name": getattr(r, "name", "Unknown"),
                "artist": getattr(r, "artist", ""),
                "source": source,
                "media_type": m_type,
                "url": f"https://www.qobuz.com/{m_type}/{r.id}",
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
