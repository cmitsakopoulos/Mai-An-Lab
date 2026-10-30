"""
Companion & Sync View / Subpage for Mai-An Lab.
Supports macOS Host controls (Server Toggle, PIN, Port) and
Android Client controls (Auto-Discovery, Pairing, Live DSP Offload, and Wireless Bundle Sync).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import subprocess
import sys
import time
from typing import Optional

import flet as ft

from ui.tokens import (
    BG, SURFACE, SURFACE2, SURFACE_ELEVATED, CYAN, AMBER, TEXT, DIM,
    BORDER, BORDER_SUBTLE, ACCENT_GREEN, ACCENT_RED, RADIUS_CARD, RADIUS_PILL,
    apply_opacity,
)
from utils.dsp import FEATURES_VERSION
from utils.sync.discovery import local_interfaces
from utils.sync.server import CompanionServer, DEFAULT_PORT, get_companion_server
from utils.sync.client import CompanionClient
from utils.sync.send import (
    SEND_FEATURES_PREF, SEND_QUALITY_OPTIONS, SEND_QUALITY_PREF, companion_target, send_defaults,
)

logger = logging.getLogger("sync.hub")


def is_android() -> bool:
    return hasattr(sys, "getandroidapilevel")


class SyncHubPane(ft.Container):
    """View component embedded inside Settings -> Companion & Sync."""

    def __init__(self, app: "StreamripFletApp"):
        super().__init__(expand=True)
        self.app = app
        self._is_android = is_android()

        # macOS Server State
        self._server: Optional[CompanionServer] = (
            None if self._is_android else get_companion_server(app))
        if self._server:
            # Idle auto-stop, PIN rotation and pairings happen off-screen; the
            # switch and PIN shown here must follow them.
            self._server.subscribe(self._on_server_state_changed)

        # Android Client State & Sync Manager
        from utils.sync.manager import CompanionSyncManager, get_sync_manager
        self._manager: Optional[CompanionSyncManager] = (
            get_sync_manager(app) if self._is_android else None)

        if self._manager:
            self._manager.subscribe(self._on_manager_state_changed)

        self._is_offloading = False
        self._cancel_offload = False
        self._was_connected = False
        self._finding = False

        self._build_ui()

    @property
    def _active_client(self) -> Optional[CompanionClient]:
        if self._manager and self._manager.client:
            return self._manager.client
        return getattr(self.app, "_companion_client", None)

    @property
    def _is_client_connected(self) -> bool:
        if self._manager:
            return self._manager.is_connected
        return self._active_client is not None

    def _on_manager_state_changed(self):
        self._update_android_ui_state()
        self._flush()

    def _flush(self):
        if self.page and not getattr(self.app, "is_background", False):
            try:
                self.page.update()
            except Exception:
                pass

    def _on_server_state_changed(self):
        self._refresh_macos_state()
        self._flush()

    @property
    def page(self):
        return getattr(self.app, "page", None)

    def _build_ui(self):
        if self._is_android:
            self.content = self._build_android_view()
        else:
            self.content = self._build_macos_view()

    # ═════════════════════════════════════════════════════════════════════════
    # macOS Host View (Manual Server Control, Zero-penalty when off)
    # ═════════════════════════════════════════════════════════════════════════
    def _build_macos_view(self) -> ft.Control:
        is_running = self._server.is_running if self._server else False
        local_ip = self._mac_lan_ip()
        bound_port = (self._server.bound_port if self._server and self._server.bound_port else DEFAULT_PORT)

        self._mac_status_dot = ft.Container(
            width=10, height=10, border_radius=5,
            bgcolor=ACCENT_GREEN if is_running else DIM,
        )
        self._mac_status_text = ft.Text(
            f"Online · {local_ip}:{bound_port}" if is_running else "Offline · Inactive",
            color=TEXT if is_running else DIM,
            size=13, weight=ft.FontWeight.W_500,
        )
        self._mac_switch = ft.Switch(
            value=is_running,
            active_color=CYAN,
            on_change=self._on_macos_server_toggle,
        )

        self._mac_client_text = ft.Text("", color=DIM, size=12, visible=False)
        # Network facts that explain a phone not finding this Mac: other
        # addresses, a VPN holding the default route, a large managed network.
        self._mac_net_text = ft.Text("", color=AMBER, size=12, visible=False, selectable=True)
        self._mac_pending_text = ft.Text("", color=DIM, size=12)
        self._mac_pending_row = ft.Row([
            ft.Container(content=self._mac_pending_text, expand=True),
            ft.TextButton("Clear", icon=ft.Icons.CLEAR_ALL_ROUNDED,
                          on_click=self._on_clear_pending_click),
        ], visible=False, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        pin_str = self._server.pin if self._server else "----"
        self._pin_display = ft.Text(
            pin_str,
            color=CYAN,
            size=20,
            weight=ft.FontWeight.BOLD,
            font_family="monospace",
            text_align=ft.TextAlign.CENTER,
        )
        self._endpoint_display = ft.Text(
            f"{local_ip}:{bound_port}",
            color=TEXT,
            size=14,
            weight=ft.FontWeight.W_600,
            font_family="monospace",
            text_align=ft.TextAlign.CENTER,
        )

        pin_box = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.KEY_ROUNDED, color=CYAN, size=13),
                    ft.Text("PAIRING PIN", color=DIM, size=11, weight=ft.FontWeight.BOLD),
                ], alignment=ft.MainAxisAlignment.CENTER, spacing=5),
                self._pin_display,
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER, spacing=6),
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER_SUBTLE),
            border_radius=10,
            padding=ft.Padding.symmetric(vertical=12, horizontal=16),
            alignment=ft.Alignment(0, 0),
            height=76,
            expand=True,
        )

        endpoint_box = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.ROUTER_ROUNDED, color=CYAN, size=13),
                    ft.Text("LOCAL ENDPOINT", color=DIM, size=11, weight=ft.FontWeight.BOLD),
                ], alignment=ft.MainAxisAlignment.CENTER, spacing=5),
                self._endpoint_display,
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER, spacing=6),
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER_SUBTLE),
            border_radius=10,
            padding=ft.Padding.symmetric(vertical=12, horizontal=16),
            alignment=ft.Alignment(0, 0),
            height=76,
            expand=True,
        )

        server_card = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.LAPTOP_MAC_ROUNDED, color=CYAN, size=22),
                    ft.Text("Companion Server", weight=ft.FontWeight.BOLD, color=TEXT, size=15, expand=True),
                    self._mac_switch,
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Row([
                    self._mac_status_dot,
                    ft.Container(content=self._mac_status_text, expand=True),
                ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text(
                    "Enables wireless track transfers and acoustic feature extraction from paired mobile devices. "
                    "Idles after 15 minutes of inactivity.",
                    color=DIM, size=12,
                ),
                self._mac_client_text,
                self._mac_pending_row,
                ft.Divider(color=BORDER_SUBTLE, height=10),
                ft.Row([
                    pin_box,
                    endpoint_box,
                ], spacing=12, alignment=ft.MainAxisAlignment.CENTER),
                self._mac_net_text,
                ft.Text(
                    "Phone can't find this Mac? Tap Find Mac on the phone, or type the endpoint "
                    "above under Manual IP. Office, campus and guest Wi-Fi often keep devices "
                    "apart; the phone's hotspot always works. USB: run "
                    "`adb reverse tcp:8945 tcp:8945` with the phone plugged in. Also allow Local "
                    "Network access for this app (System Settings → Privacy & Security → Local "
                    "Network) and accept the firewall prompt for incoming connections.",
                    color=DIM, size=11, selectable=True,
                ),
                ft.Row([
                    ft.TextButton(
                        "Reset Pairing",
                        icon=ft.Icons.LINK_OFF_ROUNDED,
                        on_click=self._on_reset_pairing_click,
                        tooltip="Unpair every phone and issue a new PIN",
                    ),
                ], wrap=True),
            ], spacing=10),
            padding=16,
            bgcolor=SURFACE2,
            border_radius=RADIUS_CARD,
            border=ft.Border.all(1, BORDER_SUBTLE),
        )

        tools_card = ft.Container(
            content=ft.Column([
                ft.Text("Backups & Archives", weight=ft.FontWeight.BOLD, color=TEXT, size=14),
                ft.Text(
                    "Database, playback history, and similarity graph backups received from mobile devices are saved to tools/state_backups/.",
                    color=DIM, size=12,
                ),
                ft.Row([
                    ft.TextButton(
                        "Open Backups Directory",
                        icon=ft.Icons.FOLDER_OPEN_ROUNDED,
                        on_click=self._on_open_backups_click,
                    ),
                ], wrap=True),
            ], spacing=10),
            padding=16,
            bgcolor=SURFACE2,
            border_radius=RADIUS_CARD,
            border=ft.Border.all(1, BORDER_SUBTLE),
        )

        # "Send to <phone>" applies these without asking each time.
        quality, features = send_defaults(self.app)
        self._send_quality_dd = ft.Dropdown(
            label="Audio quality",
            value=quality,
            options=[ft.DropdownOption(key=k, text=t) for k, t in SEND_QUALITY_OPTIONS],
            on_select=lambda e: self.app._save_pref(SEND_QUALITY_PREF, e.control.value),
            dense=True,
            border=ft.OutlineInputBorder(side=ft.BorderSide(1, BORDER), border_radius=8),
        )
        self._send_features_switch = ft.Switch(
            value=features,
            active_color=CYAN,
            on_change=lambda e: self.app._save_pref(SEND_FEATURES_PREF, bool(e.control.value)),
        )
        self._send_target_text = ft.Text("", color=DIM, size=12)
        self._send_progress_bar = ft.ProgressBar(value=0, color=ACCENT_GREEN, bgcolor=SURFACE, height=4, visible=False)
        self._send_progress_row = ft.Row([
            ft.Container(content=ft.Text("", color=DIM, size=12), expand=True),
            ft.TextButton("Cancel", icon=ft.Icons.CANCEL_ROUNDED,
                          on_click=lambda _: self._server and self._server.cancel_preparing()),
        ], visible=False, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        send_card = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.PHONE_IPHONE_ROUNDED, color=ACCENT_GREEN, size=22),
                    ft.Text("Sending to Phone", weight=ft.FontWeight.BOLD, color=TEXT, size=15, expand=True),
                ], spacing=8),
                self._send_target_text,
                self._send_progress_bar,
                self._send_progress_row,
                self._send_quality_dd,
                ft.Row([
                    ft.Column([
                        ft.Text("Include acoustic features", color=TEXT, size=13, weight=ft.FontWeight.W_600),
                        ft.Text("Analysed here so auto-play works on the phone straight away",
                                color=DIM, size=11),
                    ], spacing=1, expand=True),
                    self._send_features_switch,
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ], spacing=10),
            padding=16,
            bgcolor=SURFACE2,
            border_radius=RADIUS_CARD,
            border=ft.Border.all(1, BORDER_SUBTLE),
        )

        self._refresh_macos_state()
        return ft.ListView([
            server_card,
            send_card,
            tools_card,
        ], spacing=16, expand=True)

    @staticmethod
    def _mac_lan_ip() -> str:
        for iface in local_interfaces():
            if not iface["vpn"]:
                return iface["ip"]
        return "127.0.0.1"

    @staticmethod
    def _mac_network_notes() -> str:
        ifaces = local_interfaces()
        lan = [i for i in ifaces if not i["vpn"]]
        notes = []
        if ifaces and ifaces[0]["vpn"]:
            notes.append(f"A VPN ({ifaces[0]['name']}) carries this Mac's traffic. The phone "
                         "must use the address above, and some VPNs block local traffic "
                         "outright: pause it if the phone can't connect.")
        if len(lan) > 1:
            others = ", ".join(f"{i['ip']} ({i['name']})" for i in lan[1:])
            notes.append(f"Also reachable at {others}.")
        if lan and lan[0]["prefix"] < 24:
            notes.append(f"This is a large network (/{lan[0]['prefix']}), typical of office and "
                         "campus Wi-Fi. These usually block automatic discovery and often keep "
                         "devices apart: type the address above under Manual IP on the phone, "
                         "and if that fails too, use the phone's hotspot.")
        return "\n".join(notes)

    def _refresh_macos_state(self):
        if self._is_android or not self._server or not hasattr(self, "_mac_switch"):
            return
        srv = self._server
        running = srv.is_running
        ip = self._mac_lan_ip()
        port = srv.bound_port or DEFAULT_PORT
        self._mac_switch.value = running
        self._mac_status_dot.bgcolor = ACCENT_GREEN if running else DIM
        if running:
            self._mac_status_text.value = f"Online · {ip}:{port}"
        elif srv.stopped_reason == "idle":
            self._mac_status_text.value = "Offline · turned off after 15 min idle"
        else:
            self._mac_status_text.value = "Offline · Inactive"
        self._mac_status_text.color = TEXT if running else DIM
        self._pin_display.value = srv.pin
        self._endpoint_display.value = f"{ip}:{port}"
        notes = self._mac_network_notes()
        self._mac_net_text.value = notes
        self._mac_net_text.visible = bool(notes)
        if srv.last_client:
            name, cip, seen = srv.last_client
            ago = int(time.monotonic() - seen)
            when = "just now" if ago < 20 else (f"{ago // 60} min ago" if ago >= 60 else f"{ago}s ago")
            via = "USB" if cip in ("127.0.0.1", "::1") else cip
            self._mac_client_text.value = f"Last seen: {name} ({via}) · {when}"
            self._mac_client_text.visible = True
        n = len(srv._pending_transfers)
        self._mac_pending_text.value = (
            f"{n} track{'s' if n != 1 else ''} waiting for the phone to download."
            + ("" if running else " Offered again when the server is back on."))
        self._mac_pending_row.visible = n > 0

        target = companion_target(self.app)
        self._send_target_text.value = (
            f"Long-press a track or album and choose Send to {target.name}. These settings apply every time."
            if target.paired else
            "Pair a phone first: turn the server on and enter the PIN on the phone. "
            "Then long-press a track or album and choose Send to Phone.")
        prep = srv.preparing
        self._send_progress_bar.visible = self._send_progress_row.visible = prep is not None
        if prep:
            done, total = prep
            self._send_progress_bar.value = done / total if total else None
            self._send_progress_row.controls[0].content.value = f"Preparing {done} of {total} for the phone…"

    def _on_clear_pending_click(self, _e):
        if self._server:
            self._server.clear_pending_transfers()

    def _on_reset_pairing_click(self, _e):
        if not self._server:
            return
        self._server.reset_pairing()
        self.app.show_snackbar("Pairing reset. Phones must re-enter the new PIN.",
                               icon=ft.Icons.LINK_OFF_ROUNDED)

    def _on_macos_server_toggle(self, e):
        enabled = e.control.value
        self.page.run_task(self._do_macos_server_toggle, enabled)

    async def _do_macos_server_toggle(self, enabled: bool):
        if not self._server:
            return
        try:
            if enabled:
                port = await self._server.start()
                self.app.show_snackbar(f"Companion Server online on port {port}", icon=ft.Icons.CHECK_CIRCLE)
            else:
                await self._server.stop()
                self.app.show_snackbar("Companion Server stopped", icon=ft.Icons.STOP_CIRCLE_ROUNDED)
        except Exception as ex:
            self.app.show_snackbar(f"Server error: {ex}", color=ACCENT_RED)
        self._refresh_macos_state()
        self.page.update()

    def _on_open_backups_click(self, _e):
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        backup_dir = os.path.join(repo_root, "tools", "state_backups")
        os.makedirs(backup_dir, exist_ok=True)
        if platform.system() == "Darwin":
            subprocess.run(["open", backup_dir])

    # ═════════════════════════════════════════════════════════════════════════
    # Android Client View (Discovery, Pairing, DSP Offload & Bundle Sync)
    # ═════════════════════════════════════════════════════════════════════════
    def _build_android_view(self) -> ft.Control:
        # Connection Header with Switch (matches macOS design)
        self._android_switch = ft.Switch(
            value=self._manager.enabled if self._manager else False,
            active_color=CYAN,
            on_change=self._on_android_switch_toggle,
        )
        self._client_status_text = ft.Text("Offline · Inactive", color=DIM, size=13)
        self._client_status_dot = ft.Container(width=10, height=10, border_radius=5, bgcolor=DIM)

        self._unpair_btn = ft.TextButton(
            "Unpair Device",
            icon=ft.Icons.LINK_OFF_ROUNDED,
            on_click=self._on_unpair_click,
            visible=False,
        )

        self._server_ip_field = ft.TextField(
            label="Manual Mac IP",
            hint_text="e.g. 192.168.1.50 or 192.168.1.50:8946",
            dense=True,
            border=ft.OutlineInputBorder(side=ft.BorderSide(1, BORDER), border_radius=8),
            expand=True,
        )
        self._manual_ip_row = ft.Row([
            self._server_ip_field,
            ft.Button(
                "Connect",
                icon=ft.Icons.LINK_ROUNDED,
                on_click=self._on_manual_connect_click,
                style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
            ),
        ], spacing=8, visible=False)
        self._toggle_manual_btn = ft.TextButton(
            "Manual IP Connection…",
            icon=ft.Icons.ROUTER_ROUNDED,
            on_click=self._on_toggle_manual_click,
        )
        self._find_btn = ft.Button(
            "Find Mac",
            icon=ft.Icons.RADAR_ROUNDED,
            on_click=self._on_find_click,
            style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
        )
        # What the last Find Mac / manual connect learned, kept on screen:
        # a snackbar is gone before anyone reads a network diagnosis.
        self._diag_text = ft.Text("", color=DIM, size=12, visible=False, selectable=True)

        connection_card = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.PHONELINK_SETUP_ROUNDED, color=CYAN, size=22),
                    ft.Text("Companion Sync", weight=ft.FontWeight.BOLD, color=TEXT, size=15, expand=True),
                    self._android_switch,
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Row([
                    self._client_status_dot,
                    ft.Container(content=self._client_status_text, expand=True),
                ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text(
                    "Automatically discovers your Mac Companion over Wi-Fi, hotspot, or USB to "
                    "sync tracks and offload acoustic analysis. Pauses after 15 minutes without "
                    "use to save battery.",
                    color=DIM, size=12,
                ),
                ft.Row([
                    self._find_btn,
                    self._unpair_btn,
                    self._toggle_manual_btn,
                ], wrap=True, spacing=8),
                self._diag_text,
                self._manual_ip_row,
            ], spacing=10),
            padding=16,
            bgcolor=SURFACE2,
            border_radius=RADIUS_CARD,
            border=ft.Border.all(1, BORDER_SUBTLE),
        )

        # Pending Transfers Card (from Mac)
        self._transfers_stats_text = ft.Text("Connect to Mac to check pending transfers.", color=DIM, size=13)
        self._download_transfers_btn = ft.Button(
            "Download from Mac",
            icon=ft.Icons.DOWNLOAD_FOR_OFFLINE_ROUNDED,
            on_click=self._on_download_transfers_click,
            bgcolor=ACCENT_GREEN,
            color=BG,
            disabled=True,
        )
        self._check_transfers_btn = ft.TextButton(
            "Check Pending",
            icon=ft.Icons.REFRESH_ROUNDED,
            on_click=self._on_check_transfers_click,
            disabled=True,
        )

        transfers_card = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.PHONELINK_RING_ROUNDED, color=ACCENT_GREEN, size=22),
                    ft.Text("Transfers from Mac", weight=ft.FontWeight.BOLD, color=TEXT, size=15, expand=True),
                ], spacing=8),
                self._transfers_stats_text,
                ft.Row([
                    self._download_transfers_btn,
                    self._check_transfers_btn,
                ], wrap=True, spacing=8),
                ft.Row([
                    ft.Column([
                        ft.Text("Ask before downloading", color=TEXT, size=13, weight=ft.FontWeight.W_600),
                        ft.Text("Off: tracks sent from the Mac download on their own",
                                color=DIM, size=11),
                    ], spacing=1, expand=True),
                    ft.Switch(
                        value=self._manager.ask_before_download if self._manager else False,
                        active_color=CYAN,
                        on_change=lambda e: self._manager and self._manager.set_ask_before_download(
                            bool(e.control.value)),
                    ),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text(
                    "To send the other way, long-press a track or album and choose Send to your Mac.",
                    color=DIM, size=12,
                ),
            ], spacing=10),
            padding=16,
            bgcolor=SURFACE2,
            border_radius=RADIUS_CARD,
            border=ft.Border.all(1, BORDER_SUBTLE),
        )

        # DSP Offload Card
        self._dsp_stats_text = ft.Text("Checking library acoustic features...", color=TEXT, size=13)
        self._dsp_progress_bar = ft.ProgressBar(value=0, color=CYAN, bgcolor=SURFACE, height=4, visible=False)
        self._dsp_detail_text = ft.Text("", color=DIM, size=12, visible=False)
        self._offload_button = ft.Button(
            "Offload to Mac",
            icon=ft.Icons.BOLT_ROUNDED,
            on_click=self._on_start_offload_click,
            disabled=True,
        )
        self._cancel_offload_btn = ft.TextButton(
            "Cancel",
            icon=ft.Icons.CANCEL_ROUNDED,
            on_click=self._on_cancel_offload_click,
            visible=False,
        )

        offload_card = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.GRAPHIC_EQ_ROUNDED, color=AMBER, size=22),
                    ft.Text("DSP Acoustic Offload", weight=ft.FontWeight.BOLD, color=TEXT, size=15, expand=True),
                ], spacing=8),
                self._dsp_stats_text,
                self._dsp_progress_bar,
                self._dsp_detail_text,
                ft.Row([
                    self._offload_button,
                    self._cancel_offload_btn,
                ], wrap=True, spacing=8),
                ft.Text(
                    "Offload acoustic feature extraction (BPM, timbre, key) to your Mac for high-speed batch processing.",
                    color=DIM, size=12,
                ),
            ], spacing=10),
            padding=16,
            bgcolor=SURFACE2,
            border_radius=RADIUS_CARD,
            border=ft.Border.all(1, BORDER_SUBTLE),
        )

        # State Bundle Card
        self._upload_bundle_btn = ft.TextButton(
            "Send Backup to Mac",
            icon=ft.Icons.CLOUD_UPLOAD_ROUNDED,
            on_click=self._on_upload_bundle_click,
            disabled=not self._is_client_connected,
        )
        bundle_card = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.ARCHIVE_ROUNDED, color=CYAN, size=22),
                    ft.Text("Library State Backup", weight=ft.FontWeight.BOLD, color=TEXT, size=15, expand=True),
                ], spacing=8),
                ft.Text(
                    "Back up your library database, playback history, and similarity graph to your Mac.",
                    color=DIM, size=12,
                ),
                ft.Row([
                    self._upload_bundle_btn,
                ], wrap=True),
            ], spacing=10),
            padding=16,
            bgcolor=SURFACE2,
            border_radius=RADIUS_CARD,
            border=ft.Border.all(1, BORDER_SUBTLE),
        )

        self._update_android_ui_state()

        return ft.ListView([
            connection_card,
            transfers_card,
            offload_card,
            bundle_card,
        ], spacing=16, expand=True)

    def _update_android_ui_state(self):
        if not self._is_android or not hasattr(self, "_android_switch"):
            return

        is_enabled = self._manager.enabled if self._manager else False
        is_conn = self._manager.is_connected if self._manager else False
        is_search = self._manager.is_searching if self._manager else False
        is_asleep = self._manager.is_asleep if self._manager else False
        server = self._manager.discovered_server if self._manager else None

        self._android_switch.value = is_enabled
        if not self._finding:
            self._find_btn.content = "Test Connection" if is_conn else "Find Mac"
            self._find_btn.disabled = False

        if is_enabled and is_asleep:
            self._client_status_dot.bgcolor = DIM
            self._client_status_text.value = "Paused after 15 min idle · tap Find Mac to resume"
            self._unpair_btn.visible = False
            self._download_transfers_btn.disabled = True
            self._check_transfers_btn.disabled = True
            self._offload_button.disabled = True
            if hasattr(self, "_upload_bundle_btn"):
                self._upload_bundle_btn.disabled = True
        elif not is_enabled:
            self._client_status_dot.bgcolor = DIM
            self._client_status_text.value = "Offline · Inactive"
            self._unpair_btn.visible = False
            self._download_transfers_btn.disabled = True
            self._check_transfers_btn.disabled = True
            self._offload_button.disabled = True
            if hasattr(self, "_upload_bundle_btn"):
                self._upload_bundle_btn.disabled = True
        elif is_conn:
            self._client_status_dot.bgcolor = ACCENT_GREEN
            s_name = server.get("name", "Mac Companion") if server else "Mac Companion"
            s_ip = server.get("ip", "") if server else ""
            via = "USB" if s_ip in ("127.0.0.1", "::1") else s_ip
            self._client_status_text.value = f"Connected · {s_name} ({via})"
            mac_fv = server.get("features_version") if server else None
            if mac_fv and mac_fv != FEATURES_VERSION:
                self._client_status_text.value += (
                    f" · Mac app is on analysis v{mac_fv}, this phone v{FEATURES_VERSION}: update both")
            self._unpair_btn.visible = True
            self._check_transfers_btn.disabled = False
            # Never re-enabled mid-offload: a second run would race the first.
            self._offload_button.disabled = self._is_offloading
            if hasattr(self, "_upload_bundle_btn"):
                self._upload_bundle_btn.disabled = False
            if self.page:
                self.page.run_task(self._refresh_pending_transfers)
                # A library scan, not an HTTP call: on connect only, not on
                # every state change (each finished download notifies).
                if not self._was_connected:
                    self.page.run_task(self._refresh_dsp_stats)
        elif is_search:
            self._client_status_dot.bgcolor = AMBER
            err = self._manager.last_error if self._manager else ""
            self._client_status_text.value = (
                f"{err} · searching…" if err else "Searching for Mac on network or USB…")
            self._unpair_btn.visible = False
            self._download_transfers_btn.disabled = True
            self._check_transfers_btn.disabled = True
            self._offload_button.disabled = True
            if hasattr(self, "_upload_bundle_btn"):
                self._upload_bundle_btn.disabled = True
        else:
            self._client_status_dot.bgcolor = DIM
            self._client_status_text.value = "Offline · Inactive"
            self._unpair_btn.visible = False
            self._download_transfers_btn.disabled = True
            self._check_transfers_btn.disabled = True
            self._offload_button.disabled = True
            if hasattr(self, "_upload_bundle_btn"):
                self._upload_bundle_btn.disabled = True
        self._was_connected = is_conn and is_enabled and not is_asleep

    def _show_diagnosis(self, text: str, ok: bool = False):
        self._diag_text.value = text
        self._diag_text.color = ACCENT_GREEN if ok else DIM
        self._diag_text.visible = bool(text)

    def _on_find_click(self, _e):
        if self._manager and not self._finding:
            self.page.run_task(self._do_find)

    async def _do_find(self):
        self._finding = True
        self._find_btn.disabled = True
        self._find_btn.content = "Searching…"
        self._show_diagnosis("Trying USB, the last known address, a broadcast query, "
                             "then a scan of this network…")
        self._flush()
        try:
            result = await self._manager.find_mac()
        except Exception as ex:
            logger.exception("Find Mac failed")
            result = f"Search failed: {ex}"
        finally:
            self._finding = False
        found = self._manager.is_connected or self._manager._pairing_dialog_open
        self._show_diagnosis(result, ok=found)
        if not found and not self._manual_ip_row.visible:
            self._on_toggle_manual_click(None)
        self._update_android_ui_state()
        self._flush()

    def _on_android_switch_toggle(self, e):
        enabled = bool(e.control.value)
        self.page.run_task(self._do_toggle_android_switch, enabled)

    async def _do_toggle_android_switch(self, enabled: bool):
        if not self._manager:
            return
        if enabled:
            await self._manager.start()
            self.app.show_snackbar("Searching for Mac Companion…", icon=ft.Icons.RADAR_ROUNDED)
        else:
            await self._manager.stop()
            self.app.show_snackbar("Companion Sync disconnected", icon=ft.Icons.STOP_CIRCLE_ROUNDED)
        self._update_android_ui_state()
        if self.page:
            self.page.update()

    def _on_unpair_click(self, _e):
        if self._manager:
            self.page.run_task(self._manager.unpair)

    def _on_toggle_manual_click(self, _e):
        self._manual_ip_row.visible = not self._manual_ip_row.visible
        self._toggle_manual_btn.content = "Hide Manual IP" if self._manual_ip_row.visible else "Manual IP Connection…"
        if self.page:
            self.page.update()

    def _on_manual_connect_click(self, _e):
        ip = (self._server_ip_field.value or "").strip()
        if not ip:
            self.app.show_snackbar("Please enter an IP address", color=ACCENT_RED)
            return
        self.page.run_task(self._do_manual_connect, ip)

    async def _do_manual_connect(self, ip: str):
        host, _, port_s = ip.partition(":")
        try:
            port = int(port_s) if port_s else DEFAULT_PORT
        except ValueError:
            self.app.show_snackbar(f"Invalid port in {ip}", color=ACCENT_RED)
            return
        if not self._manager:
            return
        self._show_diagnosis(f"Connecting to {host}:{port}…")
        self._flush()
        result = await self._manager.connect_manual(host, port)
        self._show_diagnosis(result, ok=self._manager.is_connected or self._manager._pairing_dialog_open)
        self._update_android_ui_state()
        self._flush()

    async def _refresh_pending_transfers(self):
        client = self._active_client
        if not client or not self._is_client_connected:
            self._transfers_stats_text.value = "Connect to Mac to check pending transfers."
            self._download_transfers_btn.disabled = True
            self._check_transfers_btn.disabled = True
            if self.page:
                self.page.update()
            return

        try:
            transfers = await self._manager.check_pending_transfers() if self._manager else []
            count = len(transfers)
            if count == 0:
                self._transfers_stats_text.value = "No transfers waiting from Mac."
                self._download_transfers_btn.disabled = True
                self._download_transfers_btn.content = "Download from Mac"
            else:
                self._transfers_stats_text.value = f"{count} track{'s' if count != 1 else ''} ready to download from Mac."
                self._download_transfers_btn.disabled = False
                self._download_transfers_btn.content = f"Download ({count}) from Mac"
            self._check_transfers_btn.disabled = False
            if self.page:
                self.page.update()
        except Exception as ex:
            logger.debug("Failed to check pending transfers: %s", ex)

    def _on_check_transfers_click(self, _e):
        if self.page:
            self.page.run_task(self._refresh_pending_transfers)

    def _on_download_transfers_click(self, _e):
        if self.page:
            self.page.run_task(self._do_download_transfers)

    async def _do_download_transfers(self):
        if not self._manager or not self._is_client_connected:
            return
        try:
            transfers = await self._manager.check_pending_transfers()
            if not transfers:
                self.app.show_snackbar("No pending transfers found on Mac.")
                return
            n = self._manager.download_transfers(transfers)
            self.app.show_snackbar(f"Queued {n} track{'s' if n != 1 else ''} in Download Dock", icon=ft.Icons.CHECK_CIRCLE)
            self._transfers_stats_text.value = f"Queued {n} track{'s' if n != 1 else ''} to Download Dock."
            self._download_transfers_btn.disabled = True
            self._flush()
        except Exception as ex:
            self.app.show_snackbar(f"Failed to fetch transfers: {ex}", color=ACCENT_RED)

    async def _refresh_dsp_stats(self):
        try:
            if not getattr(self.app, "db_manager", None):
                return
            missing = await self.app.db_manager.get_tracks_missing_features(FEATURES_VERSION)
            count = len(missing)
            if count == 0:
                self._dsp_stats_text.value = "All tracks analyzed. Similarity walks and auto-play are ready."
                self._offload_button.disabled = True
            else:
                self._dsp_stats_text.value = f"{count} track{'s' if count != 1 else ''} require acoustic analysis."
                self._offload_button.disabled = self._is_offloading or not self._is_client_connected
            if self.page:
                self.page.update()
        except Exception as err:
            logger.warning("Failed to refresh DSP stats: %s", err)
            self._dsp_stats_text.value = f"Status check unavailable: {err}"
            if self.page:
                self.page.update()

    def _on_start_offload_click(self, _e):
        client = self._active_client
        if not client or not self._is_client_connected:
            self.app.show_snackbar("Connect and pair with your Mac first", color=AMBER)
            return
        self.page.run_task(self._do_start_offload)

    async def _do_start_offload(self):
        client = self._active_client
        if not client or not self._is_client_connected:
            self.app.show_snackbar("Connect and pair with your Mac first", color=AMBER)
            return

        missing = await self.app.db_manager.get_tracks_missing_features(FEATURES_VERSION)
        if not missing:
            self.app.show_snackbar("All tracks already analysed!", icon=ft.Icons.CHECK_CIRCLE)
            return

        from utils.progress_notify import ProgressNotifier
        self._is_offloading = True
        self._cancel_offload = False
        self._offload_button.disabled = True
        self._cancel_offload_btn.visible = True
        self._dsp_progress_bar.visible = True
        self._dsp_progress_bar.value = 0
        self._dsp_detail_text.visible = True
        self._dsp_detail_text.value = "Initiating offload..."
        self.page.update()

        def _cancel_from_notif():
            self._cancel_offload = True
            logger.info("DSP offload cancelled via system notification")

        notifier = ProgressNotifier("sync", "DSP Offload to Mac", on_cancel=_cancel_from_notif)
        summary = ""

        def _progress(done, total, current, rate):
            notifier.update(done, total, f"{done}/{total} tracks ({rate:.1f} t/s) · {current}")
            if getattr(self.app, "is_background", False):
                return  # the notification carries progress while hidden
            self._dsp_progress_bar.value = done / max(1, total)
            self._dsp_detail_text.value = f"Processed {done}/{total} ({rate:.1f} tracks/s) • {current}"
            self._flush()

        awake = self._manager.activity() if self._manager else contextlib.nullcontext()
        with awake:
            try:
                notifier.stage(f"Sending {len(missing)} tracks to Mac…")
                res = await client.offload_dsp(
                    missing_paths=missing,
                    db_manager=self.app.db_manager,
                    progress_cb=_progress,
                    cancel_check=lambda: self._cancel_offload,
                    features_version=FEATURES_VERSION,
                )
                if self._cancel_offload:
                    summary = f"DSP offload cancelled ({res.get('analysed', 0)} of {len(missing)} done)."
                elif res.get("failed", 0):
                    summary = f"Offload finished: {res['analysed']} analysed, {res['failed']} failed."
                else:
                    summary = f"Offload complete: {res['analysed']} tracks analysed."
                self.app.show_snackbar(summary, icon=ft.Icons.CHECK_CIRCLE, color=CYAN)

                if res.get("analysed"):
                    notifier.stage("Rebuilding similarity graph…")
                    from utils import track_graph as tg
                    await tg.build_metadata_edges(self.app.db_manager)
                    await tg.build_acoustic_edges(self.app.db_manager)
                    lib = getattr(self.app, "library_view", None)
                    if lib:
                        lib._cached_unanalysed = None
                        self.page.run_task(lib.load_library)
            except PermissionError:
                summary = "DSP offload stopped: the Mac no longer accepts this phone. Pair again."
                self.app.show_snackbar(summary, color=ACCENT_RED)
                if self._manager:
                    await self._manager.unpair()
            except ConnectionError:
                summary = "DSP offload stopped: lost connection to the Mac. Progress so far was saved."
                self.app.show_snackbar(summary, color=ACCENT_RED)
            except Exception as ex:
                logger.exception("DSP offload failed")
                summary = f"DSP offload failed: {ex}"
                self.app.show_snackbar(f"Offload error: {ex}", color=ACCENT_RED)
            finally:
                # Always finish: an unfinished notifier keeps the native task
                # service (and its wakelock) alive.
                notifier.finish(summary)
                self._is_offloading = False
                self._offload_button.disabled = False
                self._cancel_offload_btn.visible = False
                self._dsp_progress_bar.visible = False
                self._dsp_detail_text.visible = False
                await self._refresh_dsp_stats()

    def _on_cancel_offload_click(self, _e):
        self._cancel_offload = True
        self.app.show_snackbar("Cancelling offload...", icon=ft.Icons.CANCEL_ROUNDED)

    def _on_upload_bundle_click(self, _e):
        client = self._active_client
        if not client or not self._is_client_connected:
            self.app.show_snackbar("Connect to Mac first", color=AMBER)
            return
        self.page.run_task(self._do_upload_bundle)

    async def _do_upload_bundle(self):
        client = self._active_client
        if not client or not self._is_client_connected:
            self.app.show_snackbar("Connect to Mac first", color=AMBER)
            return

        from utils import state_export
        from utils.streamrip_api import get_config_path
        from utils.search_history import get_search_history_path
        from utils.progress_notify import ProgressNotifier

        notifier = ProgressNotifier("sync", "State Backup Upload")
        notifier.stage("Creating snapshot bundle…")
        self.app.show_snackbar("Creating snapshot bundle...", icon=ft.Icons.IOS_SHARE_ROUNDED)
        summary = ""
        bundle_path = None
        awake = self._manager.activity() if self._manager else contextlib.nullcontext()
        with awake:
            try:
                bundle_path = await asyncio.to_thread(
                    state_export.export_state,
                    self.app.db_manager.db_path,
                    get_config_path(),
                    get_search_history_path(),
                )
                notifier.stage("Transmitting bundle to Mac…")
                self.app.show_snackbar("Transmitting bundle to Mac...", icon=ft.Icons.CLOUD_UPLOAD_ROUNDED)
                res = await client.upload_state_bundle(bundle_path)
                saved_name = os.path.basename(res.get('saved_path', 'mai_an_lab_state.zip'))
                summary = f"Backup saved on Mac: {saved_name}"
                self.app.show_snackbar(summary, icon=ft.Icons.CHECK_CIRCLE)
                try:
                    os.remove(bundle_path)  # the Mac has it; don't pile zips up on the phone
                except OSError:
                    pass
            except Exception as ex:
                summary = f"Backup upload failed: {ex}"
                self.app.show_snackbar(f"Backup failed: {ex}", color=ACCENT_RED)
            finally:
                notifier.finish(summary)
