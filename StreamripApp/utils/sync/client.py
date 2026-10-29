"""
Android / Desktop Companion Client for Mai-An Lab.
Connects to macOS CompanionServer for wireless DSP offload, track synchronization, and bundle backup.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Callable, Optional
from urllib.parse import quote

import aiohttp

logger = logging.getLogger("sync.client")

# Uploads of whole FLACs over Wi-Fi plus a server-side decode can take well
# over a minute; a `total` cap would kill exactly the big files. Bound the
# connect and each read stall instead.
_TRANSFER_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=180)
# Consecutive per-track network failures before an offload gives up: the Mac
# went away, and pushing on would just fail the rest of the library one by one.
_MAX_CONSECUTIVE_NET_FAILURES = 3


class CompanionClient:
    """Client for communicating with the macOS CompanionServer."""

    def __init__(self, host: str, port: int = 8945, auth_token: Optional[str] = None,
                 device_name: str = "Android Phone"):
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.device_name = device_name
        self.base_url = f"http://{host}:{port}"

    def _headers(self) -> dict[str, str]:
        h = {"X-Device-Name": quote(self.device_name)}
        if self.auth_token:
            h["Authorization"] = f"Bearer {self.auth_token}"
        return h

    @staticmethod
    def _check(resp: aiohttp.ClientResponse, what: str):
        if resp.status == 401:
            raise PermissionError(f"{what}: not paired (HTTP 401)")
        if resp.status != 200:
            raise RuntimeError(f"{what}: HTTP {resp.status}")

    def stream_url(self, transfer: dict) -> str:
        """Download URL for a pending transfer, addressed the way WE reach the
        Mac. The server's own stream_url names its LAN IP, which a phone on
        the USB tunnel (127.0.0.1) or another subnet cannot route to."""
        path = transfer.get("stream_path")
        if not path:
            url = transfer.get("stream_url") or ""
            i = url.find("/api/v1/")
            path = url[i:] if i >= 0 else ""
        url = f"{self.base_url}{path}"
        if self.auth_token:
            url += ("&" if "?" in url else "?") + f"token={quote(self.auth_token)}"
        return url

    async def ping(self, timeout: float = 1.5) -> dict:
        """Fast HTTP handshake probe (never uses ICMP ping). The reply's
        `authorized` field says whether our token is still accepted."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(f"{self.base_url}/api/v1/ping", headers=self._headers()) as resp:
                if resp.status == 200:
                    return await resp.json()
                raise RuntimeError(f"Server returned status {resp.status}")

    async def pair(self, pin: str, device_name: Optional[str] = None) -> str:
        """Pair with Mac using 4-digit PIN to obtain persistent auth token."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0)) as session:
            payload = {"pin": pin, "device_name": device_name or self.device_name}
            async with session.post(f"{self.base_url}/api/v1/pair", json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.auth_token = data.get("token")
                    return self.auth_token
                elif resp.status == 403:
                    raise PermissionError("Incorrect PIN")
                raise RuntimeError(f"Pairing failed: HTTP {resp.status}")

    async def offload_dsp(
        self,
        missing_paths: list[str],
        db_manager,
        progress_cb: Optional[Callable[[int, int, str, float], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        features_version: int = 5,
    ) -> dict:
        """End-to-end DSP Offload Pipeline:
        1. Probes Mac cache: any track already analyzed on Mac returns immediately (0 bytes audio sent!).
        2. Streams uncached tracks to Mac over HTTP for parallel feature extraction.
        3. Persists features in bulk into Android SQLite via update_tracks_features_batch.
        """
        total = len(missing_paths)
        if total == 0:
            return {"analysed": 0, "failed": 0, "total": 0}

        analysed = 0
        failed = 0
        start_time = time.perf_counter()

        def _notify(current_path: str):
            if progress_cb:
                elapsed = max(0.001, time.perf_counter() - start_time)
                rate = analysed / elapsed
                try:
                    res = progress_cb(analysed + failed, total, current_path, rate)
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(res)
                except Exception:
                    pass

        def _row(path: str, r: dict) -> tuple:
            t_blob = base64.b64decode(r["timbre_b64"]) if r.get("timbre_b64") else b""
            # The Mac's version, not ours: if the two builds differ, stamping
            # our newer version on older features would hide them from every
            # future re-analysis.
            return (
                path, r["bpm"], r["energy"], r["brightness"],
                r["rolloff"], r["beat_strength"],
                r["spectral_flatness"], r["spectral_contrast"],
                r["key_index"], t_blob, int(r.get("features_version") or features_version),
            )

        async with aiohttp.ClientSession(timeout=_TRANSFER_TIMEOUT, headers=self._headers()) as session:
            # ── 1. Cache Probe ──────────────────────────────────────────────
            probe_payload = {"tracks": [{"path": p} for p in missing_paths]}
            uncached_paths = list(missing_paths)
            try:
                async with session.post(f"{self.base_url}/api/v1/dsp/match", json=probe_payload) as resp:
                    self._check(resp, "Cache probe")
                    data = await resp.json()
                    cached_items = data.get("cached", [])
                    uncached_paths = data.get("missing", missing_paths)
                    if cached_items:
                        batch = [_row(c["path"], c) for c in cached_items]
                        await db_manager.update_tracks_features_batch(batch)
                        analysed += len(batch)
                        logger.info("Ingested %d tracks instantly from Mac cache!", len(batch))
                        _notify("Loaded from Mac cache")
            except PermissionError:
                raise
            except Exception as ex:
                logger.warning("Cache probe failed (%s), falling back to full extraction", ex)

            # ── 2. Stream & Extract Uncached Tracks ──────────────────────────
            chunk_batch = []
            net_failures = 0
            try:
                for path in uncached_paths:
                    if cancel_check and cancel_check():
                        logger.info("DSP offload cancelled by user")
                        break

                    if not os.path.isfile(path):
                        failed += 1
                        _notify(os.path.basename(path))
                        continue

                    try:
                        data = aiohttp.FormData()
                        # Path as a form field, not a header: header values
                        # must be latin-1, and Greek paths are not.
                        data.add_field("path", path)
                        with open(path, "rb") as audio_file:
                            data.add_field(
                                "file",
                                audio_file,
                                filename=os.path.basename(path),
                                content_type="application/octet-stream",
                            )
                            async with session.post(f"{self.base_url}/api/v1/dsp/extract", data=data) as resp:
                                if resp.status == 401:
                                    self._check(resp, "Offload")
                                if resp.status == 200:
                                    chunk_batch.append(_row(path, await resp.json()))
                                    analysed += 1
                                else:
                                    failed += 1
                                    logger.warning("Extract failed for %s: HTTP %d", path, resp.status)
                        net_failures = 0
                    except PermissionError:
                        raise
                    except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as ex:
                        failed += 1
                        net_failures += 1
                        logger.warning("Network error offloading %s: %s", path, ex)
                        if net_failures >= _MAX_CONSECUTIVE_NET_FAILURES:
                            raise ConnectionError("Lost connection to Mac") from ex
                    except Exception as ex:
                        failed += 1
                        logger.warning("Error offloading %s: %s", path, ex)

                    # Commit in small transactions of 10 to keep UI responsive
                    if len(chunk_batch) >= 10:
                        await db_manager.update_tracks_features_batch(chunk_batch)
                        chunk_batch.clear()

                    _notify(os.path.basename(path))
            finally:
                # Whatever was computed is kept, even when the run aborts.
                if chunk_batch:
                    await db_manager.update_tracks_features_batch(chunk_batch)
                    chunk_batch.clear()

        return {"analysed": analysed, "failed": failed, "total": total}

    async def upload_state_bundle(self, bundle_path: str) -> dict:
        """Upload an exported state .zip bundle to Mac."""
        if not os.path.isfile(bundle_path):
            raise FileNotFoundError(f"Bundle not found: {bundle_path}")

        async with aiohttp.ClientSession(timeout=_TRANSFER_TIMEOUT, headers=self._headers()) as session:
            data = aiohttp.FormData()
            with open(bundle_path, "rb") as f:
                data.add_field("file", f, filename=os.path.basename(bundle_path))
                async with session.post(f"{self.base_url}/api/v1/bundle/upload", data=data) as resp:
                    self._check(resp, "Upload")
                    return await resp.json()

    async def get_pending_transfers(self) -> list[dict]:
        """Fetch list of queued transfers ready for download from Mac.
        Raises on failure: an empty list must mean "nothing queued", not
        "the Mac stopped answering" or "we are no longer paired"."""
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5.0),
            headers=self._headers()
        ) as session:
            async with session.get(f"{self.base_url}/api/v1/transfers/pending") as resp:
                self._check(resp, "Pending transfers")
                data = await resp.json()
                return data.get("transfers", [])

    async def ack_transfers(self, ids: list[str]) -> bool:
        """Acknowledge receipt of transfers so Mac clears them from queue."""
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5.0),
            headers=self._headers()
        ) as session:
            payload = {"ids": ids}
            async with session.post(f"{self.base_url}/api/v1/transfers/ack", json=payload) as resp:
                return resp.status == 200
