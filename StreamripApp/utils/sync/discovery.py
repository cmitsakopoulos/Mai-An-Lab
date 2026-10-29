"""
UDP Discovery Beacon & Listener for Mai-An Lab Companion Sync.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import socket
import time
from typing import Callable, Optional

logger = logging.getLogger("sync.discovery")

UDP_PORT = 54545
BEACON_MAGIC = "mai_an_sync"
# A listener re-reports a server it already knows at most this often, so a
# consumer that ignored one beacon (a dismissed PIN prompt) hears it again.
_REPORT_INTERVAL_S = 10.0
# How often the listener asks out loud. Android drops inbound broadcast
# datagrams unless the app holds a WifiManager.MulticastLock, which needs
# pyjnius — absent from the serious_python build. Sending a broadcast needs no
# lock, and the Mac answers the query by UNICAST, which is always delivered.
_QUERY_INTERVAL_S = 2.0


def resolve_local_ip() -> str:
    """Determine the primary local IPv4 address by querying the OS routing table."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Does not actually transmit packets over the wire, but causes the OS
        # to select the egress interface toward the Internet or router.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


def _bind_udp(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        # Reuseport supported on Darwin/Linux/Android
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", port))
    sock.setblocking(False)
    return sock


def _acquire_multicast_lock():
    """Acquires Android WifiManager.MulticastLock to permit reception of UDP broadcasts.
    No-op on non-Android platforms.
    """
    try:
        from jnius import autoclass
        ActivityThread = autoclass("android.app.ActivityThread")
        Context = autoclass("android.content.Context")
        app = ActivityThread.currentApplication()
        if app:
            ctx = app.getApplicationContext()
            wifi = ctx.getSystemService(Context.WIFI_SERVICE)
            if wifi:
                lock = wifi.createMulticastLock("MaiAnLab:Discovery")
                lock.setReferenceCounted(True)
                lock.acquire()
                logger.info("Acquired Android WifiManager.MulticastLock")
                return lock
    except Exception as e:
        logger.debug("MulticastLock not available or failed: %s", e)
    return None


def _beacon_payload(device_name: str, port: int) -> bytes:
    return json.dumps({
        "service": BEACON_MAGIC,
        "version": 1,
        "type": "beacon",
        "name": device_name,
        "ip": resolve_local_ip(),
        "port": port,
    }).encode("utf-8")


def _broadcast_targets(local_ip: str) -> list[str]:
    targets = ["255.255.255.255"]
    parts = local_ip.split(".")
    if len(parts) == 4 and parts[0] not in ("127", "0"):
        targets.append(f"{parts[0]}.{parts[1]}.{parts[2]}.255")
    return targets


class BeaconBroadcaster:
    """Periodically emits UDP broadcast packets declaring the Mac Companion Server.
    Strictly active ONLY while CompanionServer is enabled.
    """

    def __init__(self, port: int, device_name: Optional[str] = None, interval: float = 2.0):
        self.port = port
        self.device_name = device_name or platform.node() or "Mac Companion"
        self.interval = interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._sock: Optional[socket.socket] = None
        self._resp_task: Optional[asyncio.Task] = None
        self._resp_sock: Optional[socket.socket] = None

    def start(self):
        if self._running:
            return
        self._running = True
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._sock.setblocking(False)
            self._task = asyncio.create_task(self._broadcast_loop())
            logger.info("BeaconBroadcaster started on port %d (announcing port %d)", UDP_PORT, self.port)
        except Exception as ex:
            logger.warning("Failed to start BeaconBroadcaster: %s", ex)
            self.stop()
            return
        # The responder is best-effort: without it, listeners that can receive
        # broadcasts (desktop, USB-tunnelled phones) still find us.
        try:
            self._resp_sock = _bind_udp(UDP_PORT)
            self._resp_task = asyncio.create_task(self._respond_loop())
        except Exception as ex:
            logger.warning("Discovery responder unavailable on UDP %d: %s", UDP_PORT, ex)
            self._resp_sock = None

    def stop(self):
        self._running = False
        for task in (self._task, self._resp_task):
            if task and not task.done():
                task.cancel()
        self._task = self._resp_task = None
        for sock in (self._sock, self._resp_sock):
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
        self._sock = self._resp_sock = None
        logger.info("BeaconBroadcaster stopped")

    async def _broadcast_loop(self):
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                payload = _beacon_payload(self.device_name, self.port)
                for target in _broadcast_targets(resolve_local_ip()):
                    try:
                        await loop.sock_sendto(self._sock, payload, (target, UDP_PORT))
                    except Exception as err:
                        # EHOSTUNREACH here on macOS 15+ usually means Local
                        # Network access was denied for this app/terminal.
                        logger.debug("Broadcast sendto %s error: %s", target, err)
            except asyncio.CancelledError:
                break
            except Exception as ex:
                logger.debug("Beacon broadcast error: %s", ex)

            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break

    async def _respond_loop(self):
        """Answer listener queries by unicast to the asking socket."""
        loop = asyncio.get_running_loop()
        while self._running and self._resp_sock:
            try:
                data, addr = await loop.sock_recvfrom(self._resp_sock, 2048)
                msg = json.loads(data.decode("utf-8"))
                if msg.get("service") != BEACON_MAGIC or msg.get("type") != "query":
                    continue  # our own beacons echo back here; ignore them
                await loop.sock_sendto(
                    self._resp_sock, _beacon_payload(self.device_name, self.port), addr)
            except asyncio.CancelledError:
                break
            except Exception as ex:
                logger.debug("Discovery responder error: %s", ex)
                await asyncio.sleep(0.2)


class BeaconListener:
    """Finds macOS Companion Servers on the LAN.

    Two paths into one socket bound to UDP_PORT: the Mac's periodic broadcast
    beacons (only delivered on Android while a MulticastLock is held), and
    unicast replies to the query this listener broadcasts every couple of
    seconds (always delivered). Run on-demand; stop it once connected.
    """

    def __init__(self, on_device_found: Optional[Callable[[dict], None]] = None):
        self.on_device_found = on_device_found
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._query_task: Optional[asyncio.Task] = None
        self._sock: Optional[socket.socket] = None
        self._multicast_lock = None
        self.discovered: dict[str, dict] = {}
        self._last_reported: dict[str, float] = {}

    def start(self):
        if self._running:
            return
        self._running = True
        self.discovered.clear()
        self._last_reported.clear()
        self._multicast_lock = _acquire_multicast_lock()
        try:
            self._sock = _bind_udp(UDP_PORT)
        except Exception as ex:
            # The port is taken (a second app instance): an ephemeral port
            # still receives unicast query replies.
            logger.info("UDP %d unavailable (%s); listening on an ephemeral port", UDP_PORT, ex)
            try:
                self._sock = _bind_udp(0)
            except Exception as ex2:
                logger.warning("Failed to start BeaconListener: %s", ex2)
                self.stop()
                return
        self._task = asyncio.create_task(self._listen_loop())
        self._query_task = asyncio.create_task(self._query_loop())
        logger.info("BeaconListener listening on UDP port %d", self._sock.getsockname()[1])

    def stop(self):
        self._running = False
        for task in (self._task, self._query_task):
            if task and not task.done():
                task.cancel()
        self._task = self._query_task = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._multicast_lock:
            try:
                if hasattr(self._multicast_lock, "isHeld") and self._multicast_lock.isHeld():
                    self._multicast_lock.release()
                    logger.info("Released Android WifiManager.MulticastLock")
            except Exception as e:
                logger.debug("Failed releasing MulticastLock: %s", e)
            self._multicast_lock = None
        logger.info("BeaconListener stopped")

    async def _query_loop(self):
        loop = asyncio.get_running_loop()
        query = json.dumps({"service": BEACON_MAGIC, "version": 1, "type": "query"}).encode("utf-8")
        while self._running and self._sock:
            for target in _broadcast_targets(resolve_local_ip()):
                try:
                    await loop.sock_sendto(self._sock, query, (target, UDP_PORT))
                except asyncio.CancelledError:
                    return
                except Exception as err:
                    logger.debug("Discovery query to %s failed: %s", target, err)
            try:
                await asyncio.sleep(_QUERY_INTERVAL_S)
            except asyncio.CancelledError:
                return

    async def _listen_loop(self):
        loop = asyncio.get_running_loop()
        while self._running and self._sock:
            try:
                data, addr = await loop.sock_recvfrom(self._sock, 2048)
                parsed = json.loads(data.decode("utf-8"))
                if parsed.get("service") != BEACON_MAGIC or parsed.get("type") == "query":
                    continue
                # The sender's address is the one that provably routes back to
                # us; the advertised IP can name a different interface (VPN,
                # second NIC) on a multi-homed Mac.
                host = addr[0] or parsed.get("ip")
                port = int(parsed.get("port", 8945))
                key = f"{host}:{port}"
                parsed["ip"] = host
                parsed["port"] = port
                if key not in self.discovered:
                    logger.info("Discovered Companion Server at %s (%s)", key, parsed.get("name"))
                self.discovered[key] = parsed
                now = time.monotonic()
                if now - self._last_reported.get(key, -1e9) < _REPORT_INTERVAL_S:
                    continue
                self._last_reported[key] = now
                if self.on_device_found:
                    try:
                        res = self.on_device_found(parsed)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as ex:
                        logger.warning("on_device_found callback raised %s", ex)
            except asyncio.CancelledError:
                break
            except Exception as ex:
                logger.debug("BeaconListener receive error: %s", ex)
                await asyncio.sleep(0.5)

    @classmethod
    async def scan_once(cls, timeout: float = 4.5) -> list[dict]:
        """Convenience method to scan for servers for `timeout` seconds and return results."""
        listener = cls()
        listener.start()
        try:
            await asyncio.sleep(timeout)
            return list(listener.discovered.values())
        finally:
            listener.stop()

