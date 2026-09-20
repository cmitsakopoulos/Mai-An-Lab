"""
This file contains code from streamrip (https://github.com/nathom/streamrip).
Streamrip is the property of nathom and multiple other contributors in the streamrip community.
Big thanks to nathom and the streamrip community for their incredible work.
"""
import asyncio
import binascii
import hashlib
import logging

from .client import Client
from .config import Config
from .downloadable import DeezerDownloadable
from .exceptions import (
    AuthenticationError,
    MissingCredentialsError,
    NonStreamableError,
)

# Both of these are Deezer-only, optional, and historically awkward to bundle
# into this project's Android APK. Import defensively so that a build without
# them still starts and still does Qobuz; DeezerClient then fails loudly at
# construction instead of taking down the import of streamrip_api -> main.
try:
    import deezer  # provided by the `deezer-py` package

    _DEEZER_IMPORT_ERROR: Exception | None = None
except Exception as _e:  # pragma: no cover - depends on build environment
    deezer = None  # type: ignore[assignment]
    _DEEZER_IMPORT_ERROR = _e

try:
    from Cryptodome.Cipher import AES

    _AES_IMPORT_ERROR: Exception | None = None
except Exception as _e:  # pragma: no cover - depends on build environment
    AES = None  # type: ignore[assignment]
    _AES_IMPORT_ERROR = _e

logger = logging.getLogger("streamrip")


def deezer_is_available() -> tuple[bool, str]:
    """Whether this build can talk to Deezer at all, and why not if it cannot."""
    if deezer is None:
        return False, f"the 'deezer-py' package is missing ({_DEEZER_IMPORT_ERROR})"
    if AES is None:
        return False, f"the 'pycryptodomex' package is missing ({_AES_IMPORT_ERROR})"
    return True, ""


class DeezerClient(Client):
    """Client to handle the Deezer API. Does not do rate limiting.

    Every deezer-py call is synchronous and `requests`-backed, so each one is
    pushed onto a worker thread. Upstream streamrip leaves several of them
    (login, search, gw.get_track, get_track_url) on the calling thread; here
    that would block the Flet event loop and freeze the UI mid-download, so
    they are wrapped too.
    """

    source = "deezer"
    max_quality = 2

    # (gw format code, format string) indexed by config quality 0/1/2.
    QUALITY_MAP = [
        (9, "MP3_128"),  # quality 0
        (3, "MP3_320"),  # quality 1
        (1, "FLAC"),  # quality 2
    ]

    def __init__(self, config: Config):
        available, reason = deezer_is_available()
        if not available:
            raise NonStreamableError(f"Deezer support is unavailable: {reason}.")

        self.global_config = config
        self.client = deezer.Deezer()
        self.logged_in = False
        self.config = config.session.deezer

    async def login(self):
        # Used for track downloads; API requests go through deezer-py.
        self.session = await self.get_session(
            verify_ssl=self.global_config.session.downloads.verify_ssl
        )
        arl = self.config.arl
        if not arl:
            raise MissingCredentialsError
        success = await asyncio.to_thread(self.client.login_via_arl, arl)
        if not success:
            raise AuthenticationError
        self.logged_in = True

    async def get_metadata(self, item_id: str, media_type: str) -> dict:
        if media_type == "track":
            return await self.get_track(item_id)
        elif media_type == "album":
            return await self.get_album(item_id)
        elif media_type == "playlist":
            return await self.get_playlist(item_id)
        elif media_type == "artist":
            return await self.get_artist(item_id)
        else:
            raise NonStreamableError(f"Media type {media_type} not available on deezer")

    async def get_track(self, item_id: str) -> dict:
        try:
            item = await asyncio.to_thread(self.client.api.get_track, item_id)
        except Exception as e:
            raise NonStreamableError(e)

        album_id = item["album"]["id"]
        try:
            album_metadata, album_tracks = await asyncio.gather(
                asyncio.to_thread(self.client.api.get_album, album_id),
                asyncio.to_thread(self.client.api.get_album_tracks, album_id),
            )
        except Exception as e:
            logger.error(f"Error fetching album of track {item_id}: {e}")
            return item

        album_metadata["tracks"] = album_tracks["data"]
        album_metadata["track_total"] = len(album_tracks["data"])
        item["album"] = album_metadata

        return item

    async def get_album(self, item_id: str) -> dict:
        album_metadata, album_tracks = await asyncio.gather(
            asyncio.to_thread(self.client.api.get_album, item_id),
            asyncio.to_thread(self.client.api.get_album_tracks, item_id),
        )
        album_metadata["tracks"] = album_tracks["data"]
        album_metadata["track_total"] = len(album_tracks["data"])
        return album_metadata

    async def get_playlist(self, item_id: str) -> dict:
        pl_metadata, pl_tracks = await asyncio.gather(
            asyncio.to_thread(self.client.api.get_playlist, item_id),
            asyncio.to_thread(self.client.api.get_playlist_tracks, item_id),
        )
        pl_metadata["tracks"] = pl_tracks["data"]
        pl_metadata["track_total"] = len(pl_tracks["data"])
        return pl_metadata

    async def get_artist(self, item_id: str) -> dict:
        artist, albums = await asyncio.gather(
            asyncio.to_thread(self.client.api.get_artist, item_id),
            asyncio.to_thread(self.client.api.get_artist_albums, item_id),
        )
        artist["albums"] = albums["data"]
        return artist

    async def search(
        self, media_type: str, query: str, limit: int = 200, offset: int = 0
    ) -> list[dict]:
        if media_type == "featured":
            try:
                if query:
                    search_function = getattr(self.client.api, f"get_editorial_{query}")
                else:
                    search_function = self.client.api.get_editorial_releases
            except AttributeError:
                raise NonStreamableError(f'Invalid editorial selection "{query}"')
        else:
            try:
                search_function = getattr(self.client.api, f"search_{media_type}")
            except AttributeError:
                raise NonStreamableError(f"Invalid media type {media_type}")

        # deezer-py calls the offset "index"; the editorial endpoints take no
        # paging arguments at all.
        if media_type == "featured":
            response = await asyncio.to_thread(search_function, limit=limit)
        else:
            response = await asyncio.to_thread(
                search_function, query, limit=limit, index=offset
            )
        if response["total"] > 0:
            return [response]
        return []

    async def get_downloadable(
        self,
        item_id: str,
        quality: int = 2,
        is_retry: bool = False,
    ) -> DeezerDownloadable:
        if item_id is None:
            raise NonStreamableError(
                "No item id provided. This can happen when searching for fallback songs.",
            )
        # Config is documented as 0/1/2, but it is user-editable TOML and a
        # stray 3 would be an IndexError deep in the download path.
        quality = max(0, min(int(quality), self.max_quality))
        dl_info: dict = {"quality": quality, "id": item_id}

        track_info = await asyncio.to_thread(self.client.gw.get_track, item_id)

        fallback_id = track_info.get("FALLBACK", {}).get("SNG_ID")

        _, format_str = self.QUALITY_MAP[quality]

        dl_info["quality_to_size"] = [
            int(track_info.get(f"FILESIZE_{format}", 0))
            for _, format in self.QUALITY_MAP
        ]

        token = track_info["TRACK_TOKEN"]
        try:
            logger.debug("Fetching deezer url with token %s", token)
            url = await asyncio.to_thread(self.client.get_track_url, token, format_str)
        except deezer.WrongLicense:
            raise NonStreamableError(
                "The requested quality is not available with your subscription. "
                "Deezer HiFi is required for quality 2. Otherwise, the maximum "
                "quality allowed is 1.",
            )
        except deezer.WrongGeolocation:
            if not is_retry and fallback_id:
                return await self.get_downloadable(fallback_id, quality, is_retry=True)
            raise NonStreamableError(
                "The requested track is not available. This may be due to your country/location.",
            )

        if url is None:
            url = self._get_encrypted_file_url(
                item_id,
                track_info["MD5_ORIGIN"],
                track_info["MEDIA_VERSION"],
            )

        dl_info["url"] = url
        logger.debug("dz track info: %s", track_info)
        return DeezerDownloadable(self.session, dl_info)

    def _get_encrypted_file_url(
        self,
        meta_id: str,
        track_hash: str,
        media_version: str,
    ):
        logger.debug("Unable to fetch URL. Trying encryption method.")
        format_number = 1

        url_bytes = b"\xa4".join(
            (
                track_hash.encode(),
                str(format_number).encode(),
                str(meta_id).encode(),
                str(media_version).encode(),
            ),
        )
        url_hash = hashlib.md5(url_bytes).hexdigest()
        info_bytes = bytearray(url_hash.encode())
        info_bytes.extend(b"\xa4")
        info_bytes.extend(url_bytes)
        info_bytes.extend(b"\xa4")
        # Pad the bytes so that len(info_bytes) % 16 == 0
        padding_len = 16 - (len(info_bytes) % 16)
        info_bytes.extend(b"." * padding_len)

        path = binascii.hexlify(
            AES.new(b"jo6aey6haid2Teih", AES.MODE_ECB).encrypt(info_bytes),
        ).decode("utf-8")
        url = f"https://e-cdns-proxy-{track_hash[0]}.dzcdn.net/mobile/1/{path}"
        logger.debug("Encrypted file path %s", url)
        return url
