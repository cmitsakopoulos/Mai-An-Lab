"""
Companion Sync Manager for Android and Desktop clients.
Orchestrates automatic discovery via UDP beacons and USB probes,
handles seamless PIN pairing via Pop-Up Dialogs, token persistence,
and incoming transfer prompts.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import ipaddress
import logging
import time
from typing import Callable, Optional

import flet as ft

from ui.tokens import BG, CYAN, DIM, BORDER, ACCENT_GREEN, ACCENT_RED
from utils.sync.discovery import BeaconListener, resolve_local_ip, sweep_subnet
from utils.sync.client import CompanionClient
from utils.sync.server import DEFAULT_PORT

logger = logging.getLogger("sync.manager")

_PROBE_INTERVAL_S = 4.0
_HEARTBEAT_INTERVAL_S = 15.0
# In the background the heartbeat stands down (battery), but a transfer sent
# from the Mac right after the phone is pocketed should still be noticed: one
# cheap pending-transfers poll this often, until the idle auto-off sleeps the
# link (background polls don't count as use, so they never extend it).
_BG_TRANSFER_POLL_S = 60.0
# Missed heartbeats before the link is declared down. One miss is common on
# Wi-Fi power-save wakeups; two in a row (~30 s) is a gone Mac.
_MISSES_BEFORE_DROP = 2
# After the user cancels a PIN prompt (or unpairs), the same Mac is not
# prompted for again for this long: the 4 s USB probe and the beacon would
# otherwise re-open the dialog the moment it closed.
_PIN_SNOOZE_S = 120.0
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
# Idle auto-off: no pairing, transfer, offload or user tap for this long and
# discovery, probing and heartbeats stop until asked again.
IDLE_TIMEOUT_S = 15 * 60
# Downloads handed to the queue keep the link up, but a job the queue dropped
# without reporting must not keep it up forever.
_IN_FLIGHT_GRACE_S = 60 * 60
# How long "Find Mac" waits for a broadcast-query reply before sweeping.
_FIND_QUERY_WAIT_S = 3.0


_QUALITY_LABELS = {
    "aac_vbr_max": "AAC VBR",
    "aac_320k": "AAC 320",
    "aac_256k": "AAC 256",
    "aac_192k": "AAC 192",
}


def transfer_quality_label(t: dict) -> str:
    """What the Mac actually sends: the AAC profile, or for "original" the
    source file's format (FLAC, MP3…)."""
    q = str(t.get("quality") or "original")
    if q in _QUALITY_LABELS:
        return _QUALITY_LABELS[q]
    return (str(t.get("extension") or "").upper() or "ORIGINAL")


def _explain_connect_error(ex: BaseException, host: str, port: int) -> str:
    """One sentence on why host:port didn't answer, in terms the user can act
    on: refused means the Mac is reachable and the server is off; silence
    means the network (or the Mac being away) is in the way."""
    os_err = getattr(ex, "os_error", None) or ex
    if isinstance(os_err, ConnectionRefusedError):
        return (f"{host} is reachable, but nothing answers on port {port}. Turn on "
                "Companion Server on the Mac, or check the port it shows.")
    if (isinstance(ex, (asyncio.TimeoutError, TimeoutError))
            or getattr(os_err, "errno", None) in (errno.EHOSTUNREACH, errno.ENETUNREACH)):
        return (f"No answer from {host}. Either the Mac is asleep or on another network, "
                "or this network keeps devices apart (office, campus, hotel and guest "
                "Wi-Fi often do). Then only your phone's hotspot or a USB cable will work.")
    if isinstance(ex, RuntimeError):
        return f"Something answered at {host}:{port}, but it isn't the Mai-An Companion Server."
    return f"Could not reach {host}:{port} ({ex})."

_INSTANCE: Optional["CompanionSyncManager"] = None


def get_sync_manager(app) -> "CompanionSyncManager":
    """The process's single manager, re-bound to the newest app object. A UI
    restart or new Flet session builds a new app; a second manager would run
    its own probe/heartbeat loops and fight the first for the UDP port."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = CompanionSyncManager(app)
    else:
        _INSTANCE.rebind(app)
    app.companion_manager = app._companion_manager = _INSTANCE
    return _INSTANCE


class CompanionSyncManager:
    """Singleton-style coordinator attached to StreamripFletApp.
    Manages client-side auto-discovery, PIN popup pairing, and transfer alerts.
    """

    def __init__(self, app: "StreamripFletApp"):
        self.app = app
        self._enabled = bool(self._pref("companion_sync_enabled", False))
        self.client: Optional[CompanionClient] = getattr(app, "_companion_client", None)
        self.listener: Optional[BeaconListener] = None
        self.discovered_server: Optional[dict] = None
        self.is_connected = False
        self.is_searching = False
        self.last_error: str = ""

        self._probe_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._connect_lock = asyncio.Lock()
        self._misses = 0
        self._pairing_dialog: Optional[ft.AlertDialog] = None
        self._transfer_dialog: Optional[ft.AlertDialog] = None
        self._pin_snoozed_until: dict[str, float] = {}
        self._prompted_transfer_ids: set[str] = set()
        # Transfers already announced by a background notification.
        self._notified_transfer_ids: set[str] = set()
        self._bg_polled_at = 0.0
        # Handed to the download queue and not yet finished. Acked to the Mac
        # only when the file is on disk, so a failed download stays queued
        # there instead of vanishing.
        self._in_flight_ids: set[str] = set()
        self._unacked_ids: set[str] = set()
        self._status_listeners: list[Callable[[], None]] = []
        # Idle auto-off. `enabled` (the user's preference) survives it; a
        # sleeping manager wakes on Find Mac, the switch, or the next launch.
        self.is_asleep = False
        self._last_activity = time.monotonic()
        self._busy = 0

    def rebind(self, app):
        if app is self.app:
            return
        self.app = app
        app._companion_client = self.client
        # Dialogs of the old session's page are gone with it.
        self._pairing_dialog = self._transfer_dialog = None
        self._status_listeners.clear()

    # ─── Prefs ─────────────────────────────────────────────────────────────────

    def _pref(self, key: str, default=None):
        return (getattr(self.app, "_prefs", None) or {}).get(key, default)

    def _save_pref(self, key: str, value):
        try:
            self.app._save_pref(key, value)
        except Exception as ex:
            logger.warning("failed to save %s: %s", key, ex)

    def _in_background(self) -> bool:
        return bool(getattr(self.app, "is_background", False))

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ─── Idle auto-off ─────────────────────────────────────────────────────────

    def touch(self):
        """Mark real use: pairing, transfers, offload, a user tap. Heartbeats
        and probes don't count, or the link could never idle out."""
        self._last_activity = time.monotonic()

    @contextlib.contextmanager
    def activity(self):
        """Hold the link awake for a long job (offload, backup upload)."""
        self._busy += 1
        self.touch()
        try:
            yield
        finally:
            self._busy -= 1
            self.touch()

    def _idle_expired(self) -> bool:
        if not self._enabled or self.is_asleep or self._busy:
            return False
        idle = time.monotonic() - self._last_activity
        if self._in_flight_ids and idle < _IN_FLIGHT_GRACE_S:
            return False
        return idle >= IDLE_TIMEOUT_S

    async def _maybe_sleep(self) -> bool:
        if not self._idle_expired():
            return False
        await self._flush_acks()
        self._sleep()
        return True

    def _sleep(self):
        """Stop discovery, probing and heartbeats. Called from inside the
        probe or heartbeat loop too; both exit on is_asleep."""
        self.is_asleep = True
        self._stop_listener()
        current = asyncio.current_task()
        for task in (self._probe_task, self._heartbeat_task):
            if task and not task.done() and task is not current:
                task.cancel()
        self._probe_task = self._heartbeat_task = None
        self.is_connected = False
        self.is_searching = False
        self.client = None
        self.app._companion_client = None
        self.last_error = ""
        self._misses = 0
        logger.info("Companion Sync idle for %d min; paused", IDLE_TIMEOUT_S // 60)
        self._notify()

    @property
    def _pairing_dialog_open(self) -> bool:
        return bool(self._pairing_dialog and self._pairing_dialog.open)

    @property
    def _transfer_dialog_open(self) -> bool:
        return bool(self._transfer_dialog and self._transfer_dialog.open)

    def subscribe(self, callback: Callable[[], None]):
        """Subscribe UI components (e.g. SyncHubPane) to state changes."""
        if callback not in self._status_listeners:
            self._status_listeners.append(callback)

    def unsubscribe(self, callback: Callable[[], None]):
        """Unsubscribe UI components."""
        if callback in self._status_listeners:
            self._status_listeners.remove(callback)

    def _notify(self):
        """Notify all subscribed UI listeners of a state change."""
        for cb in list(self._status_listeners):
            try:
                cb()
            except Exception as ex:
                logger.debug("Error in sync manager listener: %s", ex)

    # ─── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        """Enable Companion Sync and start background discovery."""
        self._enabled = True
        self.is_asleep = False
        self.touch()
        self._save_pref("companion_sync_enabled", True)
        self.is_searching = not self.is_connected
        self.last_error = ""
        self._notify()

        if not self.is_connected:
            self._start_listener()
        if not self._probe_task or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_loop())
        if not self._heartbeat_task or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self):
        """Disable Companion Sync, stop discovery, and disconnect."""
        self._enabled = False
        self.is_asleep = False
        self._save_pref("companion_sync_enabled", False)
        self.is_searching = False
        self.is_connected = False
        self._stop_listener()

        for task in (self._probe_task, self._heartbeat_task):
            if task and not task.done():
                task.cancel()
        self._probe_task = self._heartbeat_task = None

        for dlg in (self._pairing_dialog, self._transfer_dialog):
            self.app.dismiss_dialog(dlg)

        self.client = None
        self.app._companion_client = None
        self._notify()

    def handle_app_background(self):
        # The UDP query loop broadcasts every 2 s; never let that run with the
        # screen off. Probe/heartbeat loops idle themselves while backgrounded.
        self._stop_listener()

    def handle_app_resume(self):
        if not self._enabled or self.is_asleep:
            return
        if self._idle_expired():
            # The loops idle in the background and may not have noticed yet.
            asyncio.create_task(self._maybe_sleep())
            return
        if self.is_connected:
            asyncio.create_task(self._heartbeat_once())
        else:
            self._start_listener()
            asyncio.create_task(self._probe_once())

    def _start_listener(self):
        if self.listener or self._in_background():
            return
        self.listener = BeaconListener(on_device_found=self._on_beacon_received)
        self.listener.start()

    def _stop_listener(self):
        if self.listener:
            self.listener.stop()
            self.listener = None

    async def unpair(self):
        """Unpair currently connected device, clear saved credentials, and resume searching."""
        host = self.client.host if self.client else self._pref("companion_paired_host", "")
        self._save_pref("companion_paired_token", "")
        self._save_pref("companion_paired_host", "")
        self._save_pref("companion_paired_port", DEFAULT_PORT)
        self._save_pref("companion_paired_name", "")
        if host:
            self._pin_snoozed_until[host] = time.monotonic() + _PIN_SNOOZE_S
        self._prompted_transfer_ids.clear()
        self._set_disconnected()
        self.discovered_server = None
        self._notify()
        self.app.show_snackbar("Unpaired from Mac Companion.", icon=ft.Icons.LINK_OFF_ROUNDED)

    # ─── Connection state ──────────────────────────────────────────────────────

    def _set_connected(self, client: CompanionClient, info: dict):
        self.client = client
        self.app._companion_client = client
        self.is_connected = True
        self.is_searching = False
        self.last_error = ""
        self._misses = 0
        name = info.get("name") or self._pref("companion_paired_name", "") or "Mac Companion"
        self.discovered_server = {
            "name": name,
            "ip": client.host,
            "port": client.port,
            "features_version": info.get("features_version"),
        }
        # A USB session's 127.0.0.1 is useless for reconnecting over Wi-Fi
        # later; keep the last LAN address.
        if client.host not in _LOOPBACK:
            self._save_pref("companion_paired_host", client.host)
            self._save_pref("companion_paired_port", client.port)
        self._save_pref("companion_paired_name", name)
        self._stop_listener()
        logger.info("Connected to %s at %s:%d", name, client.host, client.port)
        self._notify()
        asyncio.create_task(self.check_pending_transfers(prompt_user=True))

    def _set_disconnected(self, reason: str = ""):
        was = self.is_connected
        self.is_connected = False
        self.client = None
        self.app._companion_client = None
        self.is_searching = self._enabled and not self.is_asleep
        self.last_error = reason
        self._misses = 0
        if self.is_searching:
            self._start_listener()
        if was:
            logger.info("Companion link down: %s", reason or "disconnected")
        self._notify()

    # ─── Discovery & Connection ────────────────────────────────────────────────

    def _on_beacon_received(self, server_info: dict):
        """Callback invoked when UDP beacon from Mac is heard."""
        if not self._enabled or self.is_asleep or self.is_connected:
            return
        asyncio.create_task(self.handle_discovered_server(server_info))

    def _candidates(self) -> list[tuple[str, int]]:
        # 127.0.0.1 first: with `adb reverse tcp:8945 tcp:8945` the USB cable
        # is the most reliable path and needs no pairing.
        out = [("127.0.0.1", DEFAULT_PORT)]
        host = self._pref("companion_paired_host", "")
        if host:
            port = int(self._pref("companion_paired_port", DEFAULT_PORT) or DEFAULT_PORT)
            if (host, port) not in out:
                out.append((host, port))
        return out

    async def _probe_once(self):
        if self.is_connected or self._pairing_dialog_open or self._in_background():
            return
        for host, port in self._candidates():
            await self.handle_discovered_server({"ip": host, "port": port})
            if self.is_connected or self._pairing_dialog_open:
                return

    async def _probe_loop(self):
        """Periodically probes local loopback (USB adb reverse tunnel) and cached IP."""
        while self._enabled and not self.is_asleep:
            try:
                if await self._maybe_sleep():
                    break
                await self._probe_once()
            except Exception as ex:
                logger.debug("probe failed: %s", ex)
            try:
                await asyncio.sleep(_PROBE_INTERVAL_S)
            except asyncio.CancelledError:
                break

    async def _try_cached_connection(self):
        """Attempt immediate connection using cached host & token."""
        await self._probe_once()

    async def handle_discovered_server(self, server_info: dict, interactive: bool = False):
        """Connect to a Mac found via beacon, probe, or manual entry.

        Pings with the saved token; the server says whether it accepts it. An
        unauthorized reply leads to the PIN prompt. `interactive` (a user tap)
        waits its turn instead of yielding to a background attempt, allows a
        slower answer, re-raises network errors and ignores the PIN snooze.
        """
        host = server_info.get("ip")
        port = int(server_info.get("port") or DEFAULT_PORT)
        if not host:
            return
        if self._connect_lock.locked() and not interactive:
            return  # a beacon and a probe raced; one attempt is enough
        async with self._connect_lock:
            if self.is_connected and self.client and (self.client.host, self.client.port) == (host, port):
                return
            token = self._pref("companion_paired_token", "") or None
            client = CompanionClient(host, port, auth_token=token)
            try:
                info = await client.ping(timeout=4.0 if interactive else 1.5)
            except Exception as ex:
                if interactive:
                    raise
                logger.debug("ping %s:%d failed: %s", host, port, ex)
                return
            if info.get("status") != "ok":
                if interactive:
                    raise RuntimeError("not a companion server")
                return
            if info.get("authorized"):
                self._set_connected(client, info)
                return

        name = info.get("name") or server_info.get("name") or "Mac Companion"
        if not interactive and time.monotonic() < self._pin_snoozed_until.get(host, 0.0):
            return
        if self._in_background():
            return  # prompt when the user is looking; resume re-probes
        self.discovered_server = {"name": name, "ip": host, "port": port}
        self._notify()
        await self.prompt_pin_dialog({"name": name, "ip": host, "port": port})

    # ─── Find Mac / manual connect (user-triggered) ────────────────────────────

    def _outcome(self, how: str = "") -> Optional[str]:
        if self.is_connected and self.discovered_server:
            s = self.discovered_server
            return f"Connected to {s.get('name')} ({s.get('ip')}){how}."
        if self._pairing_dialog_open and self.discovered_server:
            s = self.discovered_server
            return f"Found {s.get('name')} ({s.get('ip')}){how}. Enter the PIN shown on the Mac."
        return None

    async def find_mac(self) -> str:
        """The "Find Mac" / "Test connection" button. Wakes the link, then
        tries each path in turn and says what it learned:
        USB tunnel and last address → broadcast query → /24 sweep."""
        if not self._enabled or self.is_asleep:
            await self.start()
        self.touch()
        self._pin_snoozed_until.clear()

        client = self.client
        if self.is_connected and client:
            t0 = time.perf_counter()
            try:
                await client.ping(timeout=3.0)
                ms = (time.perf_counter() - t0) * 1000
                return f"{self._outcome()[:-1]} · answered in {ms:.0f} ms."
            except Exception as ex:
                self._set_disconnected("Mac stopped responding")
                logger.info("test ping failed: %s", ex)

        await self._probe_once()
        found = self._outcome()
        if found:
            return found

        # A fresh listener re-reports at once, even a Mac it heard recently.
        self._stop_listener()
        self._start_listener()
        deadline = time.monotonic() + _FIND_QUERY_WAIT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            found = self._outcome()
            if found:
                return found

        my_ip = resolve_local_ip()
        ports = [DEFAULT_PORT]
        cached_port = int(self._pref("companion_paired_port", DEFAULT_PORT) or DEFAULT_PORT)
        if cached_port not in ports:
            ports.append(cached_port)
        for port in ports:
            for host in await sweep_subnet(my_ip, port):
                try:
                    await self.handle_discovered_server({"ip": host, "port": port}, interactive=True)
                except Exception:
                    continue
                found = self._outcome(" by scanning: this network blocks discovery "
                                      "broadcasts, so the address is saved for next time")
                if found:
                    return found

        try:
            on_lan = ipaddress.ip_address(my_ip).is_private and not my_ip.startswith("127.")
        except ValueError:
            on_lan = False
        if not on_lan:
            return (f"This phone isn't on a local network (its address is {my_ip}). "
                    "Join the Mac's Wi-Fi, or use your phone's hotspot or a USB cable.")
        return (f"No Mac answered. This phone is {my_ip}. Check Companion Server is on; "
                "if the address the Mac shows starts differently, the two aren't on the "
                "same network. If it matches, type it under Manual IP: when that fails "
                "too, this network keeps devices apart, and only your phone's hotspot or "
                "a USB cable will work.")

    async def connect_manual(self, host: str, port: int) -> str:
        if not self._enabled or self.is_asleep:
            await self.start()
        self.touch()
        self._pin_snoozed_until.pop(host, None)
        try:
            await self.handle_discovered_server({"ip": host, "port": port}, interactive=True)
        except Exception as ex:
            return _explain_connect_error(ex, host, port)
        return self._outcome() or f"{host}:{port} answered, but the connection didn't complete."

    # ─── PIN Pairing Pop-Up Dialog ─────────────────────────────────────────────

    async def prompt_pin_dialog(self, server_info: dict):
        """Displays the modal pop-up on Android requesting the Mac pairing PIN."""
        if self._pairing_dialog_open:
            return

        host = server_info.get("ip", "127.0.0.1")
        port = int(server_info.get("port", DEFAULT_PORT))
        name = server_info.get("name", "Mac Companion")

        pin_input = ft.TextField(
            label="4-Digit PIN",
            hint_text="0000",
            autofocus=True,
            password=True,
            can_reveal_password=True,
            keyboard_type=ft.KeyboardType.NUMBER,
            text_align=ft.TextAlign.CENTER,
            text_size=20,
            border=ft.OutlineInputBorder(side=ft.BorderSide(1, BORDER), border_radius=8),
            max_length=4,
        )

        error_text = ft.Text("", color=ACCENT_RED, size=12, visible=False)
        pair_btn = ft.Button("Connect", bgcolor=CYAN, color=BG)
        paired = False

        def _snooze():
            if not paired:
                self._pin_snoozed_until[host] = time.monotonic() + _PIN_SNOOZE_S

        def _close_dialog():
            _snooze()
            self.app.dismiss_dialog(dlg)

        def _refresh():
            try:
                dlg.update()
            except Exception:
                pass

        async def _do_pair(_e=None):
            nonlocal paired
            entered_pin = (pin_input.value or "").strip()
            if len(entered_pin) != 4 or not entered_pin.isdigit():
                error_text.value = "Please enter the 4-digit PIN."
                error_text.visible = True
                _refresh()
                return

            error_text.visible = False
            pair_btn.disabled = True
            pair_btn.content = "Pairing…"
            _refresh()

            client = CompanionClient(host=host, port=port)
            try:
                token = await client.pair(entered_pin)
                self._save_pref("companion_paired_token", token)
                info = await client.ping(timeout=3.0)
                paired = True
                self.touch()
                self.app.dismiss_dialog(dlg)
                self._set_connected(client, info)
                self.app.show_snackbar(f"Connected to {name}", icon=ft.Icons.CHECK_CIRCLE)
            except PermissionError:
                error_text.value = ("Incorrect PIN. Check the code on your Mac "
                                    "(it changes after 5 wrong tries).")
                error_text.visible = True
                pair_btn.disabled = False
                pair_btn.content = "Connect"
                _refresh()
            except Exception as ex:
                error_text.value = f"Connection failed: {ex}"
                error_text.visible = True
                pair_btn.disabled = False
                pair_btn.content = "Connect"
                _refresh()

        pair_btn.on_click = lambda e: self.app.page.run_task(_do_pair)
        pin_input.on_submit = lambda e: self.app.page.run_task(_do_pair)

        page_w = self.app.page.width if (self.app.page and self.app.page.width) else 400
        dialog_w = min(380, max(280, page_w - 48))

        via = "USB" if host in _LOOPBACK else host
        dlg = ft.AlertDialog(
            title=ft.Row([
                ft.Icon(ft.Icons.PHONELINK_SETUP_ROUNDED, color=CYAN, size=24),
                ft.Text(f"Pair with {name}", weight=ft.FontWeight.BOLD, size=16, expand=True),
            ], spacing=8),
            content=ft.Container(
                content=ft.Column([
                    ft.Text(
                        f"Found {name} ({via}). Enter the 4-digit PIN shown in "
                        "Settings → Companion & Sync on your Mac.",
                        color=DIM, size=13,
                    ),
                    pin_input,
                    error_text,
                ], spacing=12, tight=True),
                width=dialog_w,
            ),
            actions=[
                ft.TextButton("Not now", on_click=lambda _: _close_dialog()),
                pair_btn,
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=lambda _: _snooze(),
        )
        self._pairing_dialog = dlg
        self.app.page.show_dialog(dlg)

    # ─── Heartbeat, Transfer Polling & Alert Pop-Up ───────────────────────────

    async def _heartbeat_once(self):
        client = self.client
        if not self.is_connected or not client:
            return
        try:
            info = await client.ping(timeout=3.0)
        except Exception as ex:
            self._misses += 1
            logger.debug("heartbeat miss %d: %s", self._misses, ex)
            if self._misses >= _MISSES_BEFORE_DROP:
                self._set_disconnected("Mac stopped responding")
            return
        self._misses = 0
        if not info.get("authorized"):
            # The Mac reset pairing (or lost its token): ours is dead.
            self._save_pref("companion_paired_token", "")
            self._set_disconnected("Pairing was revoked on the Mac")
            return
        await self._flush_acks()
        await self.check_pending_transfers(prompt_user=True)

    async def _heartbeat_loop(self):
        """Liveness check + transfer poll. Without it a Mac that quit or
        slept stayed "Connected" forever and every action failed."""
        while self._enabled and not self.is_asleep:
            try:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            except asyncio.CancelledError:
                break
            # Checked before the background skip, so a phone left in a
            # pocket stops its loops too.
            if await self._maybe_sleep():
                break
            if self._in_background():
                now = time.monotonic()
                if now - self._bg_polled_at >= _BG_TRANSFER_POLL_S:
                    self._bg_polled_at = now
                    try:
                        await self._background_transfer_check()
                    except Exception as ex:
                        logger.debug("background transfer check failed: %s", ex)
                continue
            try:
                await self._heartbeat_once()
            except Exception as ex:
                logger.debug("heartbeat failed: %s", ex)

    async def check_pending_transfers(self, prompt_user: bool = False) -> list[dict]:
        """Fetches pending transfers from Mac; optionally surfaces download pop-up.
        Excludes transfers already handed to the download queue."""
        client = self.client
        if not client or not self.is_connected:
            return []
        try:
            transfers = await client.get_pending_transfers()
        except PermissionError:
            self._save_pref("companion_paired_token", "")
            self._set_disconnected("Pairing was revoked on the Mac")
            return []
        except Exception as ex:
            logger.debug("Failed checking pending transfers: %s", ex)
            return []

        transfers = [t for t in transfers if str(t.get("id")) not in self._in_flight_ids]
        if transfers and prompt_user and not self._transfer_dialog_open and not self._in_background():
            ids = {str(t.get("id")) for t in transfers if t.get("id")}
            if ids - self._prompted_transfer_ids:
                self.touch()  # the Mac sent something new: that's use
                self._prompted_transfer_ids.update(ids)
                await self.prompt_transfer_dialog(transfers)
        return transfers

    async def _background_transfer_check(self):
        """Backgrounded: no dialog can show, so announce new transfers with a
        notification that opens the Sync screen. Not counted as a miss on
        failure (the foreground heartbeat owns liveness) and not as use."""
        transfers = await self.check_pending_transfers(prompt_user=False)
        ids = {str(t.get("id")) for t in transfers if t.get("id")}
        fresh = ids - self._prompted_transfer_ids - self._notified_transfer_ids
        if not fresh:
            return
        self._notified_transfer_ids.update(fresh)
        from utils.progress_notify import post_alert
        n = len(ids)
        mac = (self.discovered_server or {}).get("name") or "Your Mac"
        post_alert(
            "transfers",
            f"{mac} has {n} track{'s' if n != 1 else ''} for you",
            "Tap to review and download them.",
            navigate_to="sync:transfers", urgent=True, icon="sync",
        )

    def download_transfers(self, transfers: list[dict]) -> int:
        """Hand transfers to the download queue. Returns how many were queued."""
        client = self.client
        # The app holds its QueueController as `app.queue`; this used to ask
        # for `app.queue_controller`, which doesn't exist, so "Download" on the
        # transfer dialog silently queued nothing.
        qc = getattr(self.app, "queue", None)
        if not client or not qc:
            return 0
        items = []
        for t in transfers:
            tid = str(t.get("id") or "")
            if not tid or tid in self._in_flight_ids:
                continue
            self._in_flight_ids.add(tid)
            items.append({
                "source": "companion",
                "media_type": "track",
                "id": tid,
                "companion_transfer_id": tid,
                "url": client.stream_url(t),
                "ui_title": t.get("title"),
                "ui_subtitle": t.get("artist"),
                "title": t.get("title"),
                "artist": t.get("artist"),
                "album": t.get("album"),
                "dsp_features": t.get("dsp_features"),
                "quality_label": transfer_quality_label(t),
            })
        if items:
            self.touch()
            qc.enqueue_many(items, quality_tier="original")
        self._notify()
        return len(items)

    def on_transfer_finished(self, transfer_id: str, ok: bool):
        """Called by the download queue when a companion job ends."""
        self._in_flight_ids.discard(transfer_id)
        self.touch()
        if ok:
            self._unacked_ids.add(transfer_id)
            asyncio.create_task(self._flush_acks())
        else:
            # Let the next poll offer it again.
            self._prompted_transfer_ids.discard(transfer_id)
        self._notify()

    async def _flush_acks(self):
        client = self.client
        if not client or not self._unacked_ids:
            return
        ids = list(self._unacked_ids)
        try:
            if await client.ack_transfers(ids):
                self._unacked_ids.difference_update(ids)
        except Exception as ex:
            logger.debug("ack failed (will retry): %s", ex)

    async def prompt_transfer_dialog(self, transfers: list[dict]):
        """Presents a pop-up dialog alerting user that tracks are ready to download."""
        if self._transfer_dialog_open:
            return
        # The dialog supersedes the background notification about them.
        from utils.progress_notify import cancel_alert
        cancel_alert("transfers")

        count = len(transfers)
        mac_name = (self.discovered_server.get("name") if self.discovered_server else "Mac Companion")

        def _close_dialog():
            self.app.dismiss_dialog(dlg)

        def _do_download(_e):
            _close_dialog()
            n = self.download_transfers(transfers)
            self.app.show_snackbar(
                f"Queued {n} track{'s' if n != 1 else ''} in Download Dock",
                icon=ft.Icons.CHECK_CIRCLE,
            )

        page_w = self.app.page.width if (self.app.page and self.app.page.width) else 400
        dialog_w = min(380, max(280, page_w - 48))

        labels = sorted({transfer_quality_label(t) for t in transfers})
        with_dsp = sum(1 for t in transfers if t.get("dsp_features"))
        summary = f"{count} track{'s' if count != 1 else ''} · {', '.join(labels)}"
        if with_dsp:
            summary += " · acoustic features included" if with_dsp == count else f" · features for {with_dsp}"

        track_rows = [
            ft.Column([
                ft.Text(t.get("title") or "Unknown", size=13, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Text(t.get("artist") or "", size=11, color=DIM, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ], spacing=0, tight=True)
            for t in transfers
        ]
        track_list = ft.Container(
            content=ft.ListView(track_rows, spacing=8, scroll=ft.ScrollMode.AUTO),
            height=min(260, 44 * len(track_rows) + 16),
            border=ft.Border.all(1, BORDER),
            border_radius=10,
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        )

        dlg = ft.AlertDialog(
            title=ft.Row([
                ft.Icon(ft.Icons.PHONELINK_RING_ROUNDED, color=ACCENT_GREEN, size=24),
                ft.Text(f"Transfers from {mac_name}", weight=ft.FontWeight.BOLD, size=16, expand=True),
            ], spacing=8),
            content=ft.Container(
                content=ft.Column([
                    ft.Text(summary, color=DIM, size=13),
                    track_list,
                ], spacing=10, tight=True),
                width=dialog_w,
            ),
            actions=[
                ft.TextButton("Later", on_click=lambda _: _close_dialog()),
                ft.Button(
                    f"Download ({count})",
                    bgcolor=ACCENT_GREEN,
                    color=BG,
                    on_click=_do_download,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._transfer_dialog = dlg
        self.app.page.show_dialog(dlg)
