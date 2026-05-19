"""
This file contains code from streamrip (https://github.com/nathom/streamrip).
Streamrip is the property of nathom and multiple other contributors in the streamrip community.
Big thanks to nathom and the streamrip community for their incredible work.
"""
import asyncio
import base64
import hashlib
import logging
import re
import time
import json
from collections import OrderedDict
from typing import List, Optional

import aiohttp

from .config import Config
from .exceptions import (
    AuthenticationError,
    IneligibleError,
    InvalidAppIdError,
    InvalidAppSecretError,
    MissingCredentialsError,
    NonStreamableError,
)
from .client import Client
from .downloadable import BasicDownloadable, Downloadable
from .ssl_utils import get_aiohttp_connector_kwargs

logger = logging.getLogger("streamrip")

QOBUZ_BASE_URL = "https://www.qobuz.com/api.json/0.2"

QOBUZ_FEATURED_KEYS = {
    "most-streamed",
    "recent-releases",
    "best-sellers",
    "press-awards",
    "ideal-discography",
    "editor-picks",
    "most-featured",
    "qobuzissims",
    "new-releases",
    "new-releases-full",
    "harmonia-mundi",
    "universal-classic",
    "universal-jazz",
    "universal-jeunesse",
    "universal-chanson",
}


class QobuzSpoofer:
    """Spoofs the information required to stream tracks from Qobuz."""

    def __init__(self, verify_ssl: bool = True):
        """Create a Spoofer."""
        self.seed_timezone_regex = (
            r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.ut'
            r"imezone\.(?P<timezone>[a-z]+)\)"
        )
        self.info_extras_regex = (
            r'name:"\w+/(?P<timezone>{timezones})",info:"'
            r'(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"'
        )
        self.app_id_regex = (
            r'production:{api:{appId:"(?P<app_id>\d{9})",appSecret:"(\w{32})'
        )
        self.verify_ssl = verify_ssl

    async def get_app_id_and_secrets(self) -> tuple[str, list[str]]:
        # Android Python environments often lack up-to-date CA certificates.
        # Since this is just fetching public JS files to scrape the app secret,
        # it's safe to disable SSL verification to prevent crashes on Android.
        connector = aiohttp.TCPConnector(verify_ssl=False)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:83.0) Gecko/20100101 Firefox/83.0"}
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            async with session.get("https://play.qobuz.com/login", timeout=30) as req:
                chunks = []
                try:
                    async for chunk in req.content.iter_chunked(8192):
                        chunks.append(chunk)
                except aiohttp.ClientPayloadError:
                    pass
                login_page = b"".join(chunks).decode("utf-8", errors="ignore")

            bundle_url_match = re.search(
                r'<script src="(/resources/\d+\.\d+\.\d+-[a-z]\d{3}/bundle\.js)"></script>',
                login_page,
            )
            if bundle_url_match is None:
                raise Exception("Could not find bundle.js url in login page.")
                
            bundle_url = bundle_url_match.group(1)

            async with session.get("https://play.qobuz.com" + bundle_url, timeout=60) as req:
                chunks = []
                try:
                    async for chunk in req.content.iter_chunked(8192):
                        chunks.append(chunk)
                except aiohttp.ClientPayloadError:
                    pass
                self.bundle = b"".join(chunks).decode("utf-8", errors="ignore")

        match = re.search(self.app_id_regex, self.bundle)
        if match is None:
            raise Exception(f"Could not find app id in bundle (length: {len(self.bundle)}).")

        app_id = str(match.group("app_id"))

        # get secrets
        seed_matches = re.finditer(self.seed_timezone_regex, self.bundle)
        secrets = OrderedDict()
        for match in seed_matches:
            seed, timezone = match.group("seed", "timezone")
            secrets[timezone] = [seed]

        keypairs = list(secrets.items())
        if len(keypairs) > 1:
            secrets.move_to_end(keypairs[1][0], last=False)

        info_extras_regex = self.info_extras_regex.format(
            timezones="|".join(timezone.capitalize() for timezone in secrets),
        )
        info_extras_matches = re.finditer(info_extras_regex, self.bundle)
        for match in info_extras_matches:
            timezone, info, extras = match.group("timezone", "info", "extras")
            secrets[timezone.lower()] += [info, extras]

        for secret_pair in secrets:
            secrets[secret_pair] = base64.standard_b64decode(
                "".join(secrets[secret_pair])[:-44],
            ).decode("utf-8")

        vals: List[str] = list(secrets.values())
        if "" in vals:
            vals.remove("")

        return app_id, vals


class QobuzClient(Client):
    source = "qobuz"
    max_quality = 4

    def __init__(self, config: Config):
        self.logged_in = False
        self.config = config
        self.rate_limiter = self.get_rate_limiter(
            config.session.downloads.requests_per_minute,
        )
        self.secret: Optional[str] = None

    async def login(self):
        self.session = await self.get_session(
            verify_ssl=self.config.session.downloads.verify_ssl
        )
        """User credentials require either a user token OR a user email & password.

        A hash of the password is stored in self.config.qobuz.password_or_token.
        This data as well as the app_id is passed to self._get_user_auth_token() to get
        the actual credentials for the user.
        """
        c = self.config.session.qobuz
        if not c.email_or_userid or not c.password_or_token:
            raise MissingCredentialsError

        assert not self.logged_in, "Already logged in"

        async def _attempt_login(force_refresh=False):
            # Check if we have cached app_id and secrets
            if force_refresh or not c.app_id or not c.secrets:
                logger.info("App id/secrets not found or stale, fetching from Qobuz bundle")
                c.app_id, c.secrets = await self._get_app_id_and_secrets()
                
                # Persist to the config file so we don't have to scrape every time
                self.config.file.qobuz.app_id = c.app_id
                self.config.file.qobuz.secrets = c.secrets
                self.config.file.set_modified()
                # This will write to the toml file on disk
                self.config.save_file()
            else:
                logger.info("Using cached app_id and secrets")
                
            self.app_id = c.app_id
            self.session.headers.update({"X-App-Id": str(c.app_id)})

            if c.use_auth_token:
                params = {
                    "user_id": c.email_or_userid,
                    "user_auth_token": c.password_or_token,
                    "app_id": str(c.app_id),
                }
            else:
                params = {
                    "email": c.email_or_userid,
                    "password": c.password_or_token,
                    "app_id": str(c.app_id),
                }

            logger.debug("Request params %s", params)
            status, resp = await self._api_request("user/login", params)
            logger.debug("Login resp: %s", resp)

            if status == 401:
                raise AuthenticationError(f"Invalid credentials from params {params}")
            elif status == 400:
                if not force_refresh:
                    logger.warning("Cached App ID seems invalid, forcing refresh...")
                    return False, None
                raise InvalidAppIdError(f"Invalid app id from params {params}")
            elif status != 200 or not resp.get("user"):
                raise Exception(f"Login failed with status {status}. Response: {resp}")

            if not resp["user"]["credential"]["parameters"]:
                raise IneligibleError("Free accounts are not eligible to download tracks.")

            uat = resp["user_auth_token"]
            self.session.headers.update({"X-User-Auth-Token": uat})

            try:
                self.secret = await self._get_valid_secret(c.secrets)
            except InvalidAppSecretError:
                if not force_refresh:
                    logger.warning("Cached secrets seem invalid, forcing refresh...")
                    return False, None
                raise
                
            return True, resp

        success, resp = await _attempt_login(force_refresh=False)
        if not success:
            success, resp = await _attempt_login(force_refresh=True)

        logger.debug("Logged in to Qobuz")
        self.logged_in = True

    async def get_metadata(self, item: str, media_type: str, limit: int = 500, offset: int = 0):
        if media_type == "label":
            return await self.get_label(item)

        c = self.config.session.qobuz
        params = {
            "app_id": str(c.app_id),
            f"{media_type}_id": item,
            "limit": limit,
            "offset": offset,
        }

        extras = {
            "artist": "albums",
            "playlist": "tracks",
            "label": "albums",
        }

        if media_type in extras:
            params.update({"extra": extras[media_type]})

        logger.debug("request params: %s", params)

        epoint = f"{media_type}/get"

        status, resp = await self._api_request(epoint, params)

        if status != 200:
            raise NonStreamableError(
                f'Error fetching metadata. Message: "{resp["message"]}"',
            )

        return resp

    async def get_label(self, label_id: str) -> dict:
        c = self.config.session.qobuz
        page_limit = 500
        params = {
            "app_id": str(c.app_id),
            "label_id": label_id,
            "limit": page_limit,
            "offset": 0,
            "extra": "albums",
        }
        epoint = "label/get"
        status, label_resp = await self._api_request(epoint, params)
        assert status == 200
        albums_count = label_resp["albums_count"]

        if albums_count <= page_limit:
            return label_resp

        requests = [
            self._api_request(
                epoint,
                {
                    "app_id": str(c.app_id),
                    "label_id": label_id,
                    "limit": page_limit,
                    "offset": offset,
                    "extra": "albums",
                },
            )
            for offset in range(page_limit, albums_count, page_limit)
        ]

        results = await asyncio.gather(*requests)
        items = label_resp["albums"]["items"]
        for status, resp in results:
            assert status == 200
            items.extend(resp["albums"]["items"])

        return label_resp

    async def search(self, media_type: str, query: str, limit: int = 500, offset: int = 0) -> list[dict]:
        if media_type not in ("artist", "album", "track", "playlist"):
            raise Exception(f"{media_type} not available for search on qobuz")

        params = {
            "query": query,
        }
        epoint = f"{media_type}/search"

        return await self._paginate(epoint, params, limit=limit, offset=offset)

    async def get_featured(self, query, limit: int = 500) -> list[dict]:
        params = {
            "type": query,
        }
        assert query in QOBUZ_FEATURED_KEYS, f'query "{query}" is invalid.'
        epoint = "album/getFeatured"
        return await self._paginate(epoint, params, limit=limit)

    async def get_user_favorites(self, media_type: str, limit: int = 500) -> list[dict]:
        assert media_type in ("track", "artist", "album")
        params = {"type": f"{media_type}s"}
        epoint = "favorite/getUserFavorites"

        return await self._paginate(epoint, params, limit=limit)

    async def get_user_playlists(self, limit: int = 500) -> list[dict]:
        epoint = "playlist/getUserPlaylists"
        return await self._paginate(epoint, {}, limit=limit)

    async def get_downloadable(self, item: str, quality: int) -> Downloadable:
        assert self.secret is not None and self.logged_in and 1 <= quality <= 4
        status, resp_json = await self._request_file_url(item, quality, self.secret)
        assert status == 200
        stream_url = resp_json.get("url")

        if stream_url is None:
            restrictions = resp_json["restrictions"]
            if restrictions:
                # Turn CamelCase code into a readable sentence
                words = re.findall(r"([A-Z][a-z]+)", restrictions[0]["code"])
                raise NonStreamableError(
                    words[0] + " " + " ".join(map(str.lower, words[1:])) + ".",
                )
            raise NonStreamableError

        # Qobuz silently downgrades streams in regions where the requested
        # tier isn't licensed (e.g. ask for Hi-Res FLAC, get MP3). The actual
        # format is reported in the `mime_type` field; trust that, not the
        # quality the caller asked for. Saving an MP3 with a `.flac`
        # extension causes downstream mime/format mismatches in mutagen,
        # tinytag, and ExoPlayer.
        mime_type = (resp_json.get("mime_type") or "").lower()
        if "flac" in mime_type:
            extension = "flac"
        elif "mpeg" in mime_type or "mp3" in mime_type:
            extension = "mp3"
        elif "mp4" in mime_type or "aac" in mime_type:
            extension = "m4a"
        else:
            fallback = "flac" if quality > 1 else "mp3"
            logger.warning(
                "Qobuz returned unrecognised mime_type %r for track %s; "
                "falling back to %s based on requested quality",
                mime_type, item, fallback,
            )
            extension = fallback

        return BasicDownloadable(
            self.session, stream_url, extension, source="qobuz"
        )

    async def _paginate(
        self,
        epoint: str,
        params: dict,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        """Paginate search results.

        params:
            limit: If None, all the results are yielded. Otherwise a maximum
            of `limit` results are yielded.

        Returns
        -------
            Generator that yields (status code, response) tuples
        """
        params.update({"limit": limit, "offset": offset})
        status, page = await self._api_request(epoint, params)
        assert status == 200, status
        logger.debug("paginate: initial request made with status %d", status)
        # albums, tracks, etc.
        key = epoint.split("/")[0] + "s"
        items = page.get(key, {})
        total = items.get("total", 0)
        if limit is not None and limit < total:
            total = limit

        logger.debug("paginate: %d total items requested", total)

        if total == 0:
            logger.debug("Nothing found from %s epoint", epoint)
            return []

        limit = int(page.get(key, {}).get("limit", 500))
        offset = int(page.get(key, {}).get("offset", 0))

        logger.debug("paginate: from response: limit=%d, offset=%d", limit, offset)
        params.update({"limit": limit})

        pages = []
        requests = []
        assert status == 200, status
        pages.append(page)
        while (offset + limit) < total:
            offset += limit
            params.update({"offset": offset})
            requests.append(self._api_request(epoint, params.copy()))

        for status, resp in await asyncio.gather(*requests):
            assert status == 200
            pages.append(resp)

        return pages

    async def _get_app_id_and_secrets(self) -> tuple[str, list[str]]:
        spoofer = QobuzSpoofer(verify_ssl=self.config.session.downloads.verify_ssl)
        return await spoofer.get_app_id_and_secrets()

    async def _test_secret(self, secret: str) -> Optional[str]:
        status, _ = await self._request_file_url("19512574", 4, secret)
        if status == 400:
            return None
        if status == 200 or status == 401:
            return secret
        logger.warning("Got status %d when testing secret", status)
        return None

    async def _get_valid_secret(self, secrets: list[str]) -> str:
        results = await asyncio.gather(
            *[self._test_secret(secret) for secret in secrets],
        )
        working_secrets = [r for r in results if r is not None]
        if len(working_secrets) == 0:
            raise InvalidAppSecretError(secrets)

        return working_secrets[0]

    async def _request_file_url(
        self,
        track_id: str,
        quality: int,
        secret: str,
    ) -> tuple[int, dict]:
        quality = self.get_quality(quality)
        unix_ts = time.time()
        r_sig = f"trackgetFileUrlformat_id{quality}intentstreamtrack_id{track_id}{unix_ts}{secret}"
        logger.debug("Raw request signature: %s", r_sig)
        r_sig_hashed = hashlib.md5(r_sig.encode("utf-8")).hexdigest()
        logger.debug("Hashed request signature: %s", r_sig_hashed)
        params = {
            "app_id": str(self.app_id),
            "request_ts": unix_ts,
            "request_sig": r_sig_hashed,
            "track_id": track_id,
            "format_id": quality,
            "intent": "stream",
        }
        return await self._api_request("track/getFileUrl", params)

    async def _api_request(self, epoint: str, params: dict, retries: int = 3) -> tuple[int, dict]:
        """Make a request to the API.
        returns: status code, json parsed response
        """
        url = f"{QOBUZ_BASE_URL}/{epoint}"
        logger.debug("api_request: endpoint=%s, params=%s", epoint, params)
        
        for attempt in range(retries):
            try:
                async with self.rate_limiter:
                    async with self.session.get(url, params=params) as response:
                        try:
                            resp_json = await response.json()
                            return response.status, resp_json
                        except aiohttp.ClientPayloadError as e:
                            logger.error(f"Payload error. Status {response.status}. Returning empty dict.")
                            return response.status, {}
                        except aiohttp.ContentTypeError as e:
                            logger.error(f"Content type error (likely HTML error page). Status {response.status}. Returning empty dict.")
                            return response.status, {}
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"Network error on attempt {attempt + 1}/{retries} for {url}: {e}")
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))

    @staticmethod
    def get_quality(quality: int):
        quality_map = (5, 6, 7, 27)
        return quality_map[quality - 1]
