"""Auto-play settings sheet: on/off, Follow / Stay, and what the station is
playing from. Opened by tapping the chain icon in Now Playing (long-press there
is the instant on/off shortcut) and from the queue sheet's Auto-play header.

All behaviour lives in utils/autoplay.py; this only presents it.
"""
import os
import sys

import flet as ft

from ui.tokens import (
    BORDER, BORDER_SUBTLE, CYAN, DIM, RADIUS_CARD, SURFACE, SURFACE2, TEXT, apply_opacity,
)
from ui.widgets import CupertinoSegmentedBar
from utils.autoplay import FOLLOW, STAY

if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

MODE_COPY = {
    FOLLOW: "Each song you pick restarts the mix, and it drifts with the songs "
            "you listen through.",
    STAY:   "The mix stays anchored to the song it started from, whatever you "
            "play in between.",
}
MODE_SHORT = {FOLLOW: "Follow", STAY: "Stay"}


def anchor_label(app) -> str:
    """"Title — Artist" of the track the station is playing from, or ""."""
    path = getattr(app.autoplay, "anchor", "") or ""
    if not path:
        return ""
    for t in audio_engine.queue:
        if t.get("path") == path:
            title = t.get("track_title") or os.path.basename(path)
            artist = t.get("artist_name") or ""
            return f"{title} — {artist}" if artist else title
    return os.path.splitext(os.path.basename(path))[0]


def _caption(text: str) -> ft.Text:
    return ft.Text(text, color=DIM, size=11, weight=ft.FontWeight.W_600)


class AutoPlaySheet:
    """Built fresh on every open (cheap, and never shows stale state)."""

    def __init__(self, app):
        self.app = app
        self.page = app.page
        self._sheet: ft.BottomSheet | None = None

    # ── presentation ────────────────────────────────────────────────────────
    def open(self):
        self._sheet = self._build()
        self.page.show_dialog(self._sheet)

    def _build(self) -> ft.BottomSheet:
        ap = self.app.autoplay
        enabled = bool(self.app.play_similar_mode)

        self._switch = ft.CupertinoSwitch(
            value=enabled,
            active_track_color=CYAN,
            on_change=self._on_toggle,
        )
        self._mode_bar = CupertinoSegmentedBar(
            segments=[
                (FOLLOW, "Follow", ft.Icons.EXPLORE_ROUNDED, None),
                (STAY,   "Stay",   ft.Icons.ANCHOR_ROUNDED, None),
            ],
            selected_key=ap.anchor_mode,
            on_change=self._on_mode,
            height=38,
        )
        self._mode_copy = ft.Text(MODE_COPY[ap.anchor_mode], color=DIM, size=12)
        self._anchor_text = ft.Text(
            "", color=TEXT, size=14, weight=ft.FontWeight.W_500,
            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
        )
        self._buffer_text = ft.Text("", color=DIM, size=12)
        self._anchor_card = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.ALL_INCLUSIVE_ROUNDED, color=CYAN, size=18),
                    ft.Column([self._anchor_text, self._buffer_text], spacing=2,
                              expand=True, tight=True),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=SURFACE2,
            border=ft.Border.all(1, BORDER_SUBTLE),
            border_radius=RADIUS_CARD,
            padding=ft.Padding.symmetric(horizontal=14, vertical=12),
        )
        self._anchor_section = ft.Column(
            [_caption("PLAYING FROM"), self._anchor_card], spacing=8, tight=True,
        )
        self._fill_anchor()

        header = ft.Row(
            [
                ft.Container(
                    content=ft.Icon(ft.Icons.ALL_INCLUSIVE_ROUNDED, color=CYAN, size=20),
                    bgcolor=apply_opacity(0.14, CYAN),
                    border_radius=12,
                    width=40, height=40,
                    alignment=ft.Alignment(0, 0),
                ),
                ft.Column(
                    [
                        ft.Text("Auto-play", color=TEXT, size=17, weight=ft.FontWeight.W_600),
                        ft.Text("Similar songs keep playing after your queue.",
                                color=DIM, size=12),
                    ],
                    spacing=2, expand=True, tight=True,
                ),
                self._switch,
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        grab = ft.Container(
            content=ft.Row([ft.Container(width=36, height=5, bgcolor=BORDER, border_radius=3)],
                           alignment=ft.MainAxisAlignment.CENTER),
            padding=ft.Padding.only(top=8, bottom=14),
        )

        return ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        grab,
                        header,
                        ft.Container(height=18),
                        _caption("MODE"),
                        ft.Container(height=8),
                        self._mode_bar,
                        ft.Container(content=self._mode_copy, padding=ft.Padding.only(top=8, left=4)),
                        ft.Container(height=18),
                        self._anchor_section,
                    ],
                    spacing=0,
                    tight=True,
                ),
                bgcolor=SURFACE,
                padding=ft.Padding.only(left=20, right=20, bottom=28),
            ),
            bgcolor=SURFACE,
            # Swipe-down to dismiss, like the Now Playing / Queue sheets.
            # Flet's BottomSheet defaults to draggable=False.
            draggable=True,
        )

    def _fill_anchor(self):
        """The 'Playing from' card: only meaningful while auto-play is on."""
        enabled = bool(self.app.play_similar_mode)
        label = anchor_label(self.app)
        self._anchor_section.visible = enabled and bool(label)
        self._anchor_text.value = label
        n = len(self.app.autoplay.buffer_paths())
        self._buffer_text.value = (
            f"{n} song{'s' if n != 1 else ''} lined up" if n else "Finding similar songs…"
        )

    # ── events ──────────────────────────────────────────────────────────────
    def _on_toggle(self, e):
        self.app.set_play_similar_mode(bool(e.control.value))
        self._fill_anchor()
        self._safe_update(self._anchor_section)

    def _on_mode(self, key: str):
        self.app.set_autoplay_anchor_mode(key)
        self._mode_copy.value = MODE_COPY[key]
        self._safe_update(self._mode_copy)

    @staticmethod
    def _safe_update(ctrl):
        try:
            ctrl.update()
        except Exception:
            pass
