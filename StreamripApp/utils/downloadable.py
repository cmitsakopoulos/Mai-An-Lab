"""
This file contains code from streamrip (https://github.com/nathom/streamrip).
Streamrip is the property of nathom and multiple other contributors in the streamrip community.
Big thanks to nathom and the streamrip community for their incredible work.
"""
import functools
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

import aiohttp

from .exceptions import NonStreamableError

# pycryptodomex is only needed for Deezer's encrypted CDN streams. It is an
# optional/native dependency, and this fork has a history of dependencies that
# fail to bundle into the Android APK. A hard import at module scope would take
# the whole app down (this module is imported by streamrip_api -> main) and
# break *Qobuz* — which needs no crypto at all — over a Deezer-only dep. So we
# degrade: import defensively here and only fail at the moment decryption is
# actually required, with an actionable message.
try:
    from Cryptodome.Cipher import Blowfish

    _CRYPTO_IMPORT_ERROR: Exception | None = None
except Exception as _e:  # pragma: no cover - depends on build environment
    Blowfish = None  # type: ignore[assignment]
    _CRYPTO_IMPORT_ERROR = _e

logger = logging.getLogger("streamrip")

# Deezer's per-track Blowfish key is derived from this constant and the track id.
BLOWFISH_SECRET = "g4el58wc0zvf9na1"


def generate_temp_path(url: str):
    return os.path.join(
        tempfile.gettempdir(),
        f"__streamrip_{hash(url)}_{time.time()}.download",
    )


async def fast_async_download(session: aiohttp.ClientSession, path: str, url: str, callback: Callable[[int], None]):
    """Asynchronous download using aiohttp with efficient chunking.

    We use a large chunk size (1MB) to ensure that the event loop is not 
    saturated with too many yields, which was a performance bottleneck 
    in previous implementations.
    """
    chunk_size: int = 1024 * 1024  # 1 MB
    async with session.get(url) as resp:
        resp.raise_for_status()
        with open(path, "wb") as file:
            while True:
                chunk = await resp.content.read(chunk_size)
                if not chunk:
                    break
                file.write(chunk)
                callback(len(chunk))



@dataclass(slots=True)
class Downloadable(ABC):
    session: aiohttp.ClientSession
    url: str
    extension: str
    source: str = "Unknown"
    _size_base: Optional[int] = None

    async def download(self, path: str, callback: Callable[[int], Any]):
        await self._download(path, callback)

    async def size(self) -> int:
        if hasattr(self, "_size") and self._size is not None:
            return self._size

        async with self.session.head(self.url) as response:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length", 0)
            self._size = int(content_length)
            return self._size

    @property
    def _size(self):
        return self._size_base

    @_size.setter
    def _size(self, v):
        self._size_base = v

    @abstractmethod
    async def _download(self, path: str, callback: Callable[[int], None]):
        raise NotImplementedError


class BasicDownloadable(Downloadable):
    """Just downloads a URL."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        extension: str,
        source: str | None = None,
    ):
        self.session = session
        self.url = url
        self.extension = extension
        self._size = None
        self.source: str = source or "Unknown"

    async def _download(self, path: str, callback):
        await fast_async_download(self.session, path, self.url, callback)


class DeezerDownloadable(Downloadable):
    """A Deezer CDN stream, transparently decrypted on the way to disk.

    Deezer serves most tracks from an encrypted CDN path (``/media/`` or
    ``/mobile/``). Every 6144-byte block of such a stream has only its first
    2048 bytes enciphered with Blowfish-CBC under a per-track key; the
    remaining 4096 bytes are plaintext. Unencrypted URLs are served as-is and
    take the ordinary download path.
    """

    is_encrypted = re.compile("/m(?:obile|edia)/")

    # One Blowfish block-triplet: 2048 enciphered bytes + 4096 plaintext bytes.
    ENCRYPT_CHUNK_SIZE = 3 * 2048

    def __init__(self, session: aiohttp.ClientSession, info: dict):
        logger.debug("Deezer info for downloadable: %s", info)
        self.session = session
        self.url = info["url"]
        self.source: str = "deezer"
        qualities_available = [
            i for i, size in enumerate(info["quality_to_size"]) if size > 0
        ]
        if len(qualities_available) == 0:
            raise NonStreamableError(
                "Missing download info. Skipping.",
            )
        max_quality_available = max(qualities_available)
        self.quality = min(info["quality"], max_quality_available)
        self._size = info["quality_to_size"][self.quality]
        if self.quality <= 1:
            self.extension = "mp3"
        else:
            self.extension = "flac"
        self.id = str(info["id"])

    async def _download(self, path: str, callback):
        async with self.session.get(self.url, allow_redirects=True) as resp:
            resp.raise_for_status()
            self._size = int(resp.headers.get("Content-Length", 0))
            if self._size < 20000 and not self.url.endswith(".jpg"):
                # Deezer signals most failures with a short JSON body and a 200,
                # so a tiny response is an error payload, not a tiny song.
                try:
                    info = await resp.json(content_type=None)
                except (json.JSONDecodeError, aiohttp.ContentTypeError, ValueError):
                    raise NonStreamableError("File not found.")

                if isinstance(info, dict) and "error" in info:
                    raise NonStreamableError(
                        f"{info['error']} - {info.get('message', '')}".strip(" -")
                    )
                raise NonStreamableError(info)

            if self.is_encrypted.search(self.url) is None:
                logger.debug("Deezer file at %s not encrypted.", self.url)
                # NOTE: this fork's fast_async_download takes the session as its
                # first argument and derives headers from it; upstream streamrip
                # passes (path, url, headers, callback) instead. Keep this call
                # matched to the local signature.
                await fast_async_download(self.session, path, self.url, callback)
                return

            blowfish_key = self._generate_blowfish_key(self.id)
            logger.debug(
                "Deezer file (id %s) at %s is encrypted. Decrypting.",
                self.id,
                self.url,
            )
            self._assert_crypto_available()

            # Decrypt as the bytes arrive rather than buffering the whole track
            # first (upstream builds a full in-memory bytearray). A hi-res FLAC
            # is ~40-80 MB, which is a real risk of being OOM-killed on the
            # Android target. We keep a carry buffer and flush every complete
            # 6144-byte block, so peak memory is one block regardless of size.
            carry = bytearray()
            chunk_size = self.ENCRYPT_CHUNK_SIZE
            with open(path, "wb") as audio:
                async for data, _ in resp.content.iter_chunks():
                    if not data:
                        continue
                    carry += data
                    callback(len(data))
                    while len(carry) >= chunk_size:
                        block = carry[:chunk_size]
                        del carry[:chunk_size]
                        audio.write(
                            self._decrypt_chunk(blowfish_key, bytes(block[:2048]))
                            + bytes(block[2048:])
                        )

                # Trailing partial block: it is only enciphered if it still
                # carries a whole 2048-byte Blowfish run.
                if carry:
                    if len(carry) >= 2048:
                        audio.write(
                            self._decrypt_chunk(blowfish_key, bytes(carry[:2048]))
                            + bytes(carry[2048:])
                        )
                    else:
                        audio.write(bytes(carry))

    @staticmethod
    def _assert_crypto_available():
        if Blowfish is None:
            raise NonStreamableError(
                "Deezer downloads need the 'pycryptodomex' package, which is not "
                f"available in this build ({_CRYPTO_IMPORT_ERROR}). Qobuz is "
                "unaffected."
            )

    @staticmethod
    def _decrypt_chunk(key, data):
        """Decrypt one 2048-byte Blowfish-CBC run of a Deezer stream."""
        return Blowfish.new(
            key,
            Blowfish.MODE_CBC,
            b"\x00\x01\x02\x03\x04\x05\x06\x07",
        ).decrypt(data)

    @staticmethod
    def _generate_blowfish_key(track_id: str) -> bytes:
        """Derive the per-track Blowfish key from the track id."""
        md5_hash = hashlib.md5(track_id.encode()).hexdigest()
        return "".join(
            chr(functools.reduce(lambda x, y: x ^ y, map(ord, t)))
            for t in zip(md5_hash[:16], md5_hash[16:], BLOWFISH_SECRET)
        ).encode()
