import asyncio
import sys
import logging
import flet as ft

from ui.tokens import BG, SURFACE, SURFACE2, CYAN, AMBER, TEXT, DIM, BORDER, apply_opacity

if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

logger = logging.getLogger(__name__)


class MiniPlayerBar:
    def __init__(self, app: "StreamripFletApp"):
        self.app  = app
        self.page = app.page

        self._title     = ft.Text("Not Playing", color=TEXT, size=13, weight=ft.FontWeight.W_700,
                                   expand=True, overflow=ft.TextOverflow.ELLIPSIS, max_lines=1)
        self._artist    = ft.Text("", color=DIM, size=11,
                                   expand=True, overflow=ft.TextOverflow.ELLIPSIS, max_lines=1)
        self._play_icon = ft.Icons.PLAY_ARROW
        self._play_btn  = ft.IconButton(
            icon=ft.Icons.PLAY_ARROW,
            icon_color=CYAN,
            icon_size=28,
            on_click=self._on_play_click,
        )
        self._artwork = ft.Image(
            src="",
            width=44, height=44,
            fit="cover",
            border_radius=ft.BorderRadius.all(6),
            visible=False,
        )
        self._artwork_container = ft.Container(
            content=self._artwork,
            width=44, height=44,
            border_radius=6,
            border=None,
        )
        self._music_icon = ft.Icon(ft.Icons.MUSIC_NOTE, color=CYAN, size=24)
        self._music_icon_container = ft.Container(
            content=self._music_icon,
            width=44, height=44,
            bgcolor=SURFACE2,
            border_radius=6,
            alignment=ft.Alignment(0, 0),
            border=None,
        )
        self._progress   = ft.ProgressBar(value=0, color=CYAN, bgcolor=None, height=2)

        self._like_btn = ft.IconButton(
            icon=ft.Icons.THUMB_UP_OUTLINED,
            icon_color=DIM, icon_size=20,
            tooltip="Like this track",
            on_click=lambda e: self.app._on_feedback_click(True),
        )
        self._dislike_btn = ft.IconButton(
            icon=ft.Icons.THUMB_DOWN_OUTLINED,
            icon_color=DIM, icon_size=20,
            tooltip="Dislike this track",
            on_click=lambda e: self.app._on_feedback_click(False),
        )

        self._ever_shown  = False   # True once a title has been set at least once
        self._last_title  = ""
        self._last_artist = ""

        self.container = ft.Container(
            content=ft.Stack(
                [
                    # 1. Main interactive content (with padding applied here instead)
                    ft.Container(
                        content=ft.GestureDetector(
                            content=ft.Row(
                                [
                                    ft.Stack(
                                        [
                                            self._music_icon_container,
                                            self._artwork_container,
                                        ]
                                    ),
                                    ft.Column(
                                        [
                                            ft.Row([self._title], spacing=8, alignment=ft.MainAxisAlignment.START),
                                            self._artist
                                        ],
                                        spacing=2, expand=True,
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.SKIP_PREVIOUS,
                                        icon_color=DIM, icon_size=22,
                                        on_click=lambda e: audio_engine.previous(),
                                    ),
                                    self._play_btn,
                                    ft.IconButton(
                                        icon=ft.Icons.SKIP_NEXT,
                                        icon_color=DIM, icon_size=22,
                                        on_click=lambda e: audio_engine.next(),
                                    ),
                                ],
                                spacing=8,
                            ),
                            on_tap=lambda e: self.app.now_playing.expand(),
                            on_vertical_drag_end=lambda e: (
                                self.app.now_playing.expand() if (getattr(e, "primary_velocity", 0) or 0) < 0 else None
                            ),
                        ),
                        padding=ft.Padding.only(left=10, right=10, top=12, bottom=8),
                    ),

                    # 2. The Progress Bar positioned elegantly at the top
                    ft.Container(
                        content=self._progress,
                        top=4, left=12, right=12,
                    ),
                ]
            ),
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER),
            border_radius=12,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS, # Crucial: clips the progress bar to the rounded corners
            margin=ft.Margin.only(left=8, right=8, bottom=8),
            padding=0, # Crucial: Remove padding so the progress bar touches the edges
            visible=False,   # no layout space until first song; avoids the phantom gap
            opacity=0,
            animate_opacity=ft.Animation(200, ft.AnimationCurve.EASE_OUT),
        )

    def build(self) -> ft.Control:
        return self.container

    def update_meta(self, title: str, artist: str):
        # Called from within safe_update; mutate directly, rely on outer page.update()
        if title:
            self._last_title  = title
            self._last_artist = artist or ""
            self._title.value  = title
            self._artist.value = self._last_artist
            if not self._ever_shown:
                # First reveal: make it occupy layout space, then animate in
                self._ever_shown       = True
                self.container.visible = True
            self.container.opacity = 1.0
        elif self._ever_shown:
            # Playback stopped but we have history; show last track dimmed
            self._title.value  = self._last_title
            self._artist.value = self._last_artist
            self.container.opacity = 0.55
        # If never shown and title is empty, leave visible=False (no space taken)

    def update_artwork(self, src: str):
        if src:
            self._artwork.src        = src
            self._artwork.src_base64 = ""
            self._artwork.visible    = True
        else:
            self._artwork.visible    = False

    def update_play_similar(self, enabled: bool):
        self._artwork_container.border = ft.Border.all(2, CYAN) if enabled else None
        self._music_icon_container.border = ft.Border.all(2, CYAN) if enabled else None
        try:
            self._artwork_container.update()
        except (RuntimeError, AssertionError):
            pass
        try:
            self._music_icon_container.update()
        except (RuntimeError, AssertionError):
            pass

    def update_auto_dj(self, enabled: bool):
        self._artwork_container.border = ft.Border.all(2, AMBER) if enabled else None
        self._music_icon_container.border = ft.Border.all(2, AMBER) if enabled else None
        try:
            self._artwork_container.update()
        except (RuntimeError, AssertionError):
            pass
        try:
            self._music_icon_container.update()
        except (RuntimeError, AssertionError):
            pass

    def update_state(self, is_playing: bool):
        self._play_btn.icon = ft.Icons.PAUSE if is_playing else ft.Icons.PLAY_ARROW
        try:
            self._play_btn.update()
        except (RuntimeError, AssertionError):
            pass
        
        self.container.border = ft.Border.all(1, apply_opacity(0.7, "#FFFFFF")) if is_playing else ft.Border.all(1, BORDER)
        try:
            self.container.update()
        except (RuntimeError, AssertionError):
            pass

    def update_progress(self, pct: float):
        self._progress.value = pct / 100

    async def _on_play_click(self, _e):
        # Yield to ensure button animation starts immediately
        await asyncio.sleep(0)
        
        # Audio engine toggle can be blocking (I/O/Drivers), offload to thread
        await asyncio.to_thread(audio_engine.toggle)
        
        is_playing = audio_engine.is_playing
        self.update_state(is_playing)
        self.app.now_playing.update_state(is_playing)
        self.page.update()
