"""Streamrip search — Minimal Qobuz-only implementation."""

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
    def __init__(self, config_path=None):
        from .streamrip_api import get_config_path
        self.config_path = config_path or get_config_path()

    def get_artist_albums(self, artist_id: str, callback) -> None:
        threading.Thread(
            target=self._run_artist_albums,
            args=(artist_id, callback),
            daemon=True,
        ).start()

    def _run_artist_albums(self, artist_id, callback):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            results = loop.run_until_complete(self._get_artist_albums_async(artist_id))
            loop.close()
        except Exception as exc:
            logger.error("Get artist albums failed: %s", exc)
            results = []
        callback(results)

    async def _get_artist_albums_async(self, artist_id):
        from .config import Config
        from .qobuz import QobuzClient
        config = Config(self.config_path)
        client = QobuzClient(config)
        try:
            await client.login()
            resp = await client.get_metadata(artist_id, "artist")
            # Qobuz returns albums in the 'albums' key when 'extra=albums' is used in get_metadata
            raw_albums = resp.get("albums", {}).get("items", [])
            for a in raw_albums:
                a["_media_type"] = "album"
            return self._parse_results(raw_albums, "qobuz")
        finally:
            if hasattr(client, "session") and client.session:
                await client.session.close()

    def get_album_tracks(self, album_id: str, callback) -> None:
        threading.Thread(
            target=self._run_album_tracks,
            args=(album_id, callback),
            daemon=True,
        ).start()

    def _run_album_tracks(self, album_id, callback):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            results = loop.run_until_complete(self._get_album_tracks_async(album_id))
            loop.close()
        except Exception as exc:
            logger.error("Get album tracks failed: %s", exc)
            results = []
        callback(results)

    async def _get_album_tracks_async(self, album_id):
        from .config import Config
        from .qobuz import QobuzClient
        config = Config(self.config_path)
        client = QobuzClient(config)
        try:
            await client.login()
            resp = await client.get_metadata(album_id, "album")
            raw_tracks = resp.get("tracks", {}).get("items", [])
            for t in raw_tracks:
                t["_media_type"] = "track"
            return self._parse_results(raw_tracks, "qobuz")
        finally:
            if hasattr(client, "session") and client.session:
                await client.session.close()

    def search(self, query: str, source: str, callback, media_types=None, limit: int = 50, offset: int = 0) -> None:
        if source.lower() != "qobuz":
            callback({"error": f"Source '{source}' is not supported in this minimal build."})
            return
        
        threading.Thread(
            target=self._run_search,
            args=(query, source, callback, media_types or ["track", "album"], limit, offset),
            daemon=True,
        ).start()

    def _run_search(self, query, source, callback, media_types, limit=50, offset=0):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            results = loop.run_until_complete(self._search_async(query, media_types, limit, offset))
            loop.close()
        except Exception as exc:
            logger.error("Search failed: %s", exc, exc_info=True)
            results = {"error": str(exc)}
        callback(results)

    async def _search_async(self, query: str, media_types: list, limit: int = 50, offset: int = 0) -> list:
        from .exceptions import MissingCredentialsError, AuthenticationError
        config = Config(self.config_path)
        client = QobuzClient(config)
        try:
            await client.login()
        except MissingCredentialsError:
            raise Exception("Qobuz credentials are missing. Please enter your User ID and Token in the Settings tab.")
        except AuthenticationError:
            raise Exception("Qobuz authentication failed. Please check your credentials in the Settings tab.")
        except Exception as exc:
            raise Exception(f"Connection failed: {exc}")

        raw = []
        try:
            for m_type in media_types:
                try:
                    pages = await client.search(m_type, query, limit=limit, offset=offset)
                    for page in pages:
                        items = page.get(f"{m_type}s", {}).get("items", [])
                        for item in items:
                            if isinstance(item, dict):
                                item["_media_type"] = m_type
                                raw.append(item)
                        # We don't break here if offset is used, because _paginate 
                        # already handles the requested limit/offset across pages.
                        # However, for simplicity with Qobuz API, one call to search 
                        # with limit=50 and offset=X returns exactly what we want.
                except Exception as exc:
                    logger.warning("Qobuz search %s: %s", m_type, exc)
        finally:
            if hasattr(client, "session") and client.session:
                await client.session.close()

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
        # the track exists on a single, an album, and a compilation. They share
        # the same track ID and therefore the same download URL — clicking
        # "download" on one would queue identical bytes from each duplicate.
        # Dedupe by (media_type, id), preserving order of first occurrence.
        seen: set = set()
        deduped = []
        for entry in parsed:
            key = (entry["media_type"], str(entry["id"]))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        return deduped
