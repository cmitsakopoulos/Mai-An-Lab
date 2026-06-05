"""
This file contains code from streamrip (https://github.com/nathom/streamrip).
Streamrip is the property of nathom and multiple other contributors in the streamrip community.
Big thanks to nathom and the streamrip community for their incredible work.
"""
"""The clients that interact with the streaming service APIs."""


import asyncio
import contextlib
import logging
import time
from abc import ABC, abstractmethod

import aiohttp

from .ssl_utils import get_aiohttp_connector_kwargs
from .downloadable import Downloadable

logger = logging.getLogger("streamrip")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:83.0) Gecko/20100101 Firefox/83.0"
)

class SimpleLimiter:
    """A minimal sleep-based rate limiter to replace aiolimiter."""
    def __init__(self, requests_per_min: int):
        self.delay = 60.0 / requests_per_min if requests_per_min > 0 else 0
        self.last_request = 0.0
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        async with self.lock:
            elapsed = time.time() - self.last_request
            if elapsed < self.delay:
                await asyncio.sleep(self.delay - elapsed)
            self.last_request = time.time()
        return self

    async def __aexit__(self, *args):
        pass

class Client(ABC):
    source: str
    max_quality: int
    session: aiohttp.ClientSession
    logged_in: bool

    @abstractmethod
    async def login(self):
        raise NotImplementedError

    @abstractmethod
    async def get_metadata(self, item: str, media_type):
        raise NotImplementedError

    @abstractmethod
    async def search(self, media_type: str, query: str, limit: int = 500) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    async def get_downloadable(self, item: str, quality: int) -> Downloadable:
        raise NotImplementedError

    @staticmethod
    def get_rate_limiter(
        requests_per_min: int,
    ):
        return (
            SimpleLimiter(requests_per_min)
            if requests_per_min > 0
            else contextlib.nullcontext()
        )

    @staticmethod
    async def get_session(
        headers: dict | None = None, verify_ssl: bool = True, trace_configs: list | None = None
    ) -> aiohttp.ClientSession:
        if headers is None:
            headers = {}

        # Get connector kwargs based on SSL verification setting
        connector_kwargs = get_aiohttp_connector_kwargs(verify_ssl=verify_ssl)
        connector = aiohttp.TCPConnector(**connector_kwargs)

        return aiohttp.ClientSession(
            headers={"User-Agent": DEFAULT_USER_AGENT} | headers,
            connector=connector,
            trace_configs=trace_configs,
        )
