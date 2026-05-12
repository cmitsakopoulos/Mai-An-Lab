"""
This file contains code from streamrip (https://github.com/nathom/streamrip).
Streamrip is the property of nathom and multiple other contributors in the streamrip community.
Big thanks to nathom and the streamrip community for their incredible work.
"""
import asyncio

import base64
import functools
import hashlib
import itertools
import json
import logging
import os
import re
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

import aiohttp

from .exceptions import NonStreamableError

logger = logging.getLogger("streamrip")


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
