"""
"Send to <device>": the one library action for moving tracks between the Mac
and the phone, whichever side it is tapped on.

On the Mac it prepares the tracks (encode + acoustic features, per the saved
defaults) and publishes them for the phone, which downloads them on its own.
On the phone it pushes the files to the Mac, which files them into its
library. Menus ask `companion_target()` what to call the other device; the action is
always offered, and `send_tracks()` either sends without a dialog or says why
it can't (server off, not paired, not reachable).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
import flet as ft

from ui.tokens import ACCENT_RED
from utils.storage_paths import is_android

logger = logging.getLogger("sync.send")

# Mac → phone defaults, set in Settings → Companion & Sync.
SEND_QUALITY_PREF = "companion_send_quality"
SEND_FEATURES_PREF = "companion_send_features"
DEFAULT_SEND_QUALITY = "aac_vbr_max"
SEND_QUALITY_OPTIONS = [
    ("aac_vbr_max", "AAC (VBR Max)"),
    ("aac_320k", "AAC (320 kbps)"),
    ("aac_256k", "AAC (256 kbps)"),
    ("aac_192k", "AAC (192 kbps)"),
    ("original", "Original (No Transcode)"),
]


@dataclass(frozen=True)
class CompanionTarget:
    kind: str   # "phone" (we are the Mac) or "mac" (we are the phone)
    name: str
    paired: bool = True

    @property
    def label(self) -> str:
        return f"Send to {self.name}"

    @property
    def icon(self):
        return ft.Icons.PHONE_IPHONE_ROUNDED if self.kind == "phone" else ft.Icons.LAPTOP_MAC_ROUNDED


# A phone heartbeats every 15 s; one seen within this window is online.
_PHONE_ONLINE_S = 45.0


def companion_target(app) -> CompanionTarget:
    """The device a Send goes to, named as well as we know it. Always
    returned, connected or not: the action stays where people expect it,
    and tapping it explains what's missing instead of the row vanishing."""
    prefs = getattr(app, "_prefs", None) or {}
    if is_android():
        manager = getattr(app, "companion_manager", None)
        name = manager._mac_name("Mac") if manager else (prefs.get("companion_paired_name") or "Mac")
        return CompanionTarget("mac", name, bool(prefs.get("companion_paired_token")))
    from utils.sync.server import CLIENT_NAME_PREF
    name = prefs.get(CLIENT_NAME_PREF) or ""
    return CompanionTarget("phone", name or "Phone", bool(name))


def _phone_online(server) -> bool:
    seen = getattr(server, "last_client", None)
    return bool(seen) and time.monotonic() - seen[2] < _PHONE_ONLINE_S


async def _reach_mac(app, manager, target: CompanionTarget) -> bool:
    """The phone isn't linked: wake the link and try the saved address once.
    A failure says what to do next; tapping Send again retries."""
    if not target.paired:
        app.show_snackbar(
            "Not paired with a Mac yet. Turn on Companion Server on the Mac, then "
            "pair in Settings → Companion & Sync.",
            icon=ft.Icons.LINK_OFF_ROUNDED, color=ACCENT_RED)
        return False
    app.show_snackbar(f"Connecting to {target.name}…", icon=ft.Icons.SYNC_ROUNDED)
    try:
        if not manager.enabled or manager.is_asleep:
            await manager.start()
        manager.touch()
        await manager._probe_once()
    except Exception as ex:
        logger.info("reconnect before send failed: %s", ex)
    if manager.is_connected:
        return True
    app.show_snackbar(
        f"Can't reach {target.name}. Check Companion Server is on and both devices "
        "are on the same Wi-Fi, then tap Send again.",
        icon=ft.Icons.WIFI_OFF_ROUNDED, color=ACCENT_RED)
    return False


def send_defaults(app) -> tuple[str, bool]:
    prefs = getattr(app, "_prefs", None) or {}
    quality = prefs.get(SEND_QUALITY_PREF) or DEFAULT_SEND_QUALITY
    if quality not in dict(SEND_QUALITY_OPTIONS):
        quality = DEFAULT_SEND_QUALITY
    return quality, bool(prefs.get(SEND_FEATURES_PREF, True))


async def send_tracks(app, tracks: list[dict]) -> int:
    """Send library tracks to the other device. Feedback is a toast at each
    end; progress lives in the Sync screen (Mac) or a notification (phone).
    Returns how many tracks were handed over."""
    tracks = [t for t in tracks if t.get("path")]
    target = companion_target(app)
    if not tracks:
        return 0
    noun = f"{len(tracks)} track{'s' if len(tracks) != 1 else ''}"

    if target.kind == "mac":
        manager = app.companion_manager
        if not manager.is_connected and not await _reach_mac(app, manager, target):
            return 0
        target = companion_target(app)  # the reply may have renamed the Mac
        n = manager.send_tracks(tracks)
        if n:
            app.show_snackbar(f"Sending {noun} to {target.name}…", icon=ft.Icons.UPLOAD_ROUNDED)
        else:
            app.show_snackbar(f"Already sending to {target.name}.")
        return n

    from utils.sync.server import get_companion_server
    server = get_companion_server(app)
    quality, precompute = send_defaults(app)
    if not server.is_running:
        try:
            await server.start()
        except Exception as ex:
            app.show_snackbar(f"Couldn't start the Companion Server: {ex}", color=ACCENT_RED)
            return 0
    app.show_snackbar(f"Preparing {noun} for {target.name}…", icon=ft.Icons.PHONE_IPHONE_ROUNDED)
    try:
        queued = await server.queue_tracks_for_transfer(
            tracks=tracks, quality=quality, precompute_dsp=precompute)
    except Exception as ex:
        logger.exception("send to phone failed")
        app.show_snackbar(f"Couldn't prepare tracks: {ex}", color=ACCENT_RED)
        return 0
    if queued:
        n = len(queued)
        ready = f"{n} track{'s' if n != 1 else ''} ready"
        if _phone_online(server):
            app.show_snackbar(f"{ready} · {target.name} is downloading them",
                              icon=ft.Icons.CHECK_CIRCLE)
        elif not target.paired:
            app.show_snackbar(
                f"{ready}, but no phone is connected. Pair one in Companion & Sync; "
                "it collects them once it connects.", icon=ft.Icons.LINK_OFF_ROUNDED)
        else:
            app.show_snackbar(
                f"{ready}, but {target.name} isn't connected. Open the app on it, or "
                "tap Find Mac in Companion & Sync; it collects them then.",
                icon=ft.Icons.LINK_OFF_ROUNDED)
    else:
        app.show_snackbar("Nothing was sent: cancelled, or the files are no longer on disk.")
    return len(queued)
