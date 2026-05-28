import asyncio
import sys
import logging
import flet as ft

from ui.tokens import BG, SURFACE, SURFACE2, CYAN, AMBER, TEXT, DIM, BORDER, apply_opacity
from ui.widgets import fmt_time

if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

logger = logging.getLogger(__name__)


class NowPlayingSheet:
    def __init__(self, app: "StreamripFletApp"):
        self.app  = app
        self.page = app.page
        self._initialized = False
        self.container = None

    def _ensure_initialized(self):
        if self._initialized:
            return

        self._title   = ft.Text("Unknown",  color=TEXT, size=18, weight=ft.FontWeight.W_700,
                                  text_align=ft.TextAlign.CENTER, max_lines=2,
                                  overflow=ft.TextOverflow.ELLIPSIS, expand=True)
        self._artist  = ft.Text("Unknown",  color=DIM,  size=13, text_align=ft.TextAlign.CENTER)
        self._album   = ft.Text("Unknown",  color=DIM + "88", size=11, text_align=ft.TextAlign.CENTER)
        
        self._artwork = ft.Image(
            src="", fit="cover",
            border_radius=ft.BorderRadius.all(20),
            visible=False,
            expand=True,
            scale=ft.Scale(1.0),
            animate_scale=ft.Animation(300, ft.AnimationCurve.EASE_OUT)
        )
        self._art_placeholder = ft.Container(
            bgcolor=SURFACE2,
            border_radius=20,
            expand=True,
            content=ft.Icon(ft.Icons.ALBUM, color=CYAN, size=96),
            alignment=ft.Alignment(0, 0),
            border=None,
        )
        self._artwork_container = ft.Container(
            content=self._artwork,
            shadow=ft.BoxShadow(blur_radius=30, color=CYAN+"33"),
            border_radius=20,
            border=None,
            expand=True,
        )
        self._overlay_icon = ft.Icon(ft.Icons.PLAY_ARROW, size=64, color=TEXT, opacity=0, animate_opacity=400)
        
        self._art_stack = ft.GestureDetector(
            content=ft.Stack([
                self._art_placeholder,
                self._artwork_container,
                ft.Container(self._overlay_icon, alignment=ft.Alignment(0, 0), expand=True)
            ], expand=True),
            on_tap=self._toggle_playback,
            on_horizontal_drag_end=self._handle_swipe
        )

        self._scrubber = ft.Slider(
            value=0, min=0, max=100,
            active_color=CYAN,
            inactive_color=SURFACE2,
            thumb_color=TEXT,
            expand=True,
            on_change_start=lambda e: setattr(self.app, "is_scrubbing", True),
            on_change_end=self._commit_scrub,
        )
        self._time_cur = ft.Text("0:00", color=DIM, size=12)
        self._time_tot = ft.Text("0:00", color=DIM, size=12)
        self._play_btn = ft.IconButton(
            icon=ft.Icons.PLAY_ARROW,
            icon_color=TEXT,
            icon_size=44,
            on_click=self._on_play_click,
        )
        self._shuffle_btn = ft.IconButton(
            icon=ft.Icons.SHUFFLE,
            icon_color=DIM,
            icon_size=20,
            on_click=lambda e: self.app.toggle_shuffle(),
        )
        self._play_similar_btn = ft.IconButton(
            icon=ft.Icons.LINK_ROUNDED if self.app.play_similar_mode else ft.Icons.LINK_OFF_ROUNDED,
            icon_color=CYAN if self.app.play_similar_mode else DIM,
            icon_size=20,
            tooltip="Play Similar (Dynamic Recommendation Walk)",
            on_click=self._toggle_play_similar,
        )
        self._auto_dj_btn = ft.IconButton(
            icon=ft.Icons.AUTO_AWESOME_ROUNDED if self.app.auto_dj_mode else ft.Icons.AUTO_AWESOME_OUTLINED,
            icon_color=AMBER if self.app.auto_dj_mode else DIM,
            icon_size=20,
            tooltip="Auto-DJ (Smart AI Curation)",
            visible=False,
            on_click=self._toggle_auto_dj,
        )
        self._repeat_btn = ft.IconButton(
            icon=ft.Icons.REPEAT,
            icon_color=DIM,
            icon_size=20,
            on_click=lambda e: self.app.cycle_repeat(),
        )
        
        lib = getattr(self.app, "library_view", None)
        in_mood_partition = (
            lib is not None
            and getattr(lib, "view_mode", "") == "partitions"
            and getattr(lib, "partition_sub_mode", "") == "moods"
        )
        show_feedback = (getattr(self.app, "auto_dj_mode", False) or in_mood_partition) and not getattr(self.app, "play_similar_mode", False)
        
        self._like_btn = ft.IconButton(
            icon=ft.Icons.THUMB_UP_OUTLINED,
            icon_color=DIM,
            icon_size=26,
            tooltip="Like this track",
            visible=show_feedback,
            on_click=lambda e: self.app._on_feedback_click(True),
        )
        self._dislike_btn = ft.IconButton(
            icon=ft.Icons.THUMB_DOWN_OUTLINED,
            icon_color=DIM,
            icon_size=26,
            tooltip="Dislike this track",
            visible=show_feedback,
            on_click=lambda e: self.app._on_feedback_click(False),
        )

        self._subtitle_text = ft.Text(
            f"{self._artist.value}  ·  {self._album.value}", 
            color=DIM, size=14,
            overflow=ft.TextOverflow.ELLIPSIS, max_lines=1,
            text_align=ft.TextAlign.CENTER
        )

        self._root_layout = ft.Column(
            [
                # Header
                ft.Container(
                    content=ft.Row(
                        [
                            ft.IconButton(
                                icon=ft.Icons.KEYBOARD_ARROW_DOWN,
                                icon_color=DIM, icon_size=32,
                                on_click=lambda e: self.collapse(),
                            ),
                            ft.Container(expand=True),
                            ft.IconButton(
                                icon=ft.Icons.PLAYLIST_PLAY,
                                icon_color=DIM, icon_size=26,
                                on_click=lambda e: self.app.queue_sheet.expand(),
                            ),
                        ]
                    ),
                    padding=ft.Padding.symmetric(horizontal=12),
                ),
                ft.Container(expand=True),
                # Artwork - Responsive Aspect Ratio Container
                ft.Container(
                    content=self._art_stack,
                    aspect_ratio=1.0,
                    margin=40,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    alignment=ft.Alignment(0, 0)
                ),
                ft.Container(expand=True),
                # Track info
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Text("NOW PLAYING", color=CYAN, size=10, weight=ft.FontWeight.W_700,
                                    opacity=0.65, text_align=ft.TextAlign.CENTER),
                            ft.Row(
                                [
                                    self._dislike_btn,
                                    self._title,
                                    self._like_btn,
                                ],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                spacing=12,
                            ),
                            self._subtitle_text,
                        ],
                        spacing=4,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding.symmetric(horizontal=24),
                    alignment=ft.Alignment(0, 0),
                ),
                ft.Container(height=16),
                # Scrubber
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Row([self._scrubber], spacing=0),
                            ft.Row(
                                [self._time_cur, ft.Container(expand=True), self._time_tot],
                                spacing=0,
                            ),
                        ],
                        spacing=0,
                    ),
                    padding=ft.Padding.symmetric(horizontal=24),
                ),
                # Shuffle / Repeat row
                ft.Container(
                    content=ft.Row(
                        [
                            self._shuffle_btn,
                            ft.Container(expand=True),
                            self._play_similar_btn,
                            ft.Container(expand=True),
                            self._repeat_btn,
                            self._auto_dj_btn,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding.symmetric(horizontal=24),
                ),
                ft.Container(height=8),
                # Main playback controls
                ft.Container(
                    content=ft.Row(
                        [
                            ft.IconButton(icon=ft.Icons.REPLAY_10, icon_color=CYAN, icon_size=26,
                                          on_click=lambda _: audio_engine.seek(audio_engine.position - 10)),
                            ft.IconButton(icon=ft.Icons.SKIP_PREVIOUS, icon_color=TEXT, icon_size=34,
                                          on_click=lambda e: audio_engine.previous()),
                            ft.Container(
                                content=self._play_btn,
                                bgcolor=SURFACE2,
                                border_radius=40,
                                width=72, height=72,
                                alignment=ft.Alignment(0, 0),
                            ),
                            ft.IconButton(icon=ft.Icons.SKIP_NEXT, icon_color=TEXT, icon_size=34,
                                          on_click=lambda e: audio_engine.next()),
                            ft.IconButton(icon=ft.Icons.FORWARD_10, icon_color=CYAN, icon_size=26,
                                          on_click=lambda _: audio_engine.seek(audio_engine.position + 10)),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_EVENLY,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding.only(bottom=36),
                ),
            ],
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            expand=True,
        )

        # 1. Update the initialization
        self.container = ft.BottomSheet(
            content=ft.Container(
                content=self._root_layout,
                bgcolor=BG,
                padding=ft.Padding.only(top=10, bottom=20),
                expand=True, # FIX: Let the container fill the strict fullscreen bounds
            ),
            fullscreen=True,       # CRITICAL FIX: Bypasses the 50% height restriction safely
            scrollable=False,      # CRITICAL FIX: Disable so expand=True works inside
            show_drag_handle=False,
            draggable=True,        # Native swipe-to-dismiss physics
            use_safe_area=True,    # Ensures content respects notch/gesture bar
            bgcolor=BG,
        )
        self._initialized = True

    def build(self) -> ft.Control:
        self._ensure_initialized()
        return self.container

    def expand(self):
        def _mutate():
            self._ensure_initialized()
            # FIX: Removed manual page height calculations. 
            # Flexbox handles resizing natively now.
            self.container.open = True
            self.update_play_similar(self.app.play_similar_mode)
        self.app.safe_update(_mutate)

    def collapse(self):
        def _mutate():
            self.container.open = False
        self.app.safe_update(_mutate)

    def _commit_scrub(self, e):
        self.app.is_scrubbing = False
        target = (e.control.value / 100.0) * audio_engine.duration
        audio_engine.seek(target)

    def _toggle_playback(self, e):
        audio_engine.toggle()
        def _mutate():
            self._overlay_icon.icon = ft.Icons.PAUSE if audio_engine.is_playing else ft.Icons.PLAY_ARROW
            self._overlay_icon.opacity = 1
        self.app.safe_update(_mutate)
        async def _fade():
            await asyncio.sleep(0.6)
            def _hide():
                self._overlay_icon.opacity = 0
            self.app.safe_update(_hide)
        asyncio.create_task(_fade())

    def _handle_swipe(self, e):
        # FIX: Protect against NoneType comparison crashes
        velocity = getattr(e, "primary_velocity", 0) or 0
        if velocity > 0: 
            audio_engine.previous()
        elif velocity < 0: 
            audio_engine.next()

    # ── state sync ──────────────────────────────────────────────────────────
    def update_meta(self, title: str, artist: str, album: str):
        self._ensure_initialized()
        self._title.value  = title  or "Unknown"
        self._artist.value = artist or "Unknown"
        self._album.value  = album  or "Unknown"
        self._subtitle_text.value = f"{self._artist.value}  ·  {self._album.value}"

    def update_artwork(self, src: str):
        self._ensure_initialized()
        if src:
            self._artwork.src        = src
            self._artwork.src_base64 = ""
            self._artwork.visible    = True
            self._artwork.scale      = ft.Scale(1.0)
        else:
            self._artwork.visible    = False

    def update_state(self, is_playing: bool):
        self._ensure_initialized()
        self._play_btn.icon = ft.Icons.PAUSE if is_playing else ft.Icons.PLAY_ARROW
        try:
            self._play_btn.update()
        except (RuntimeError, AssertionError):
            pass

    def update_progress(self, position: float, duration: float):
        self._ensure_initialized()
        if self.app.is_scrubbing:
            return
        pct = (position / duration * 100) if duration > 0 else 0
        self._scrubber.value = pct
        self._time_cur.value = fmt_time(position)

    def update_duration(self, duration: float):
        self._ensure_initialized()
        self._time_tot.value = fmt_time(duration)

    def update_shuffle(self, is_shuffle: bool):
        self._ensure_initialized()
        self._shuffle_btn.icon_color = CYAN if is_shuffle else DIM

    def update_repeat(self, mode: str):
        self._ensure_initialized()
        self._repeat_btn.icon_color = CYAN if mode != "none" else DIM
        self._repeat_btn.icon = ft.Icons.REPEAT_ONE if mode == "one" else ft.Icons.REPEAT

    async def _on_play_click(self, _e):
        await asyncio.sleep(0)
        audio_engine.toggle()
        is_playing = audio_engine.is_playing
        self.update_state(is_playing)
        self.app.mini_player.update_state(is_playing)
        self.page.update()

    def _toggle_play_similar(self, e):
        self.page.run_task(self._toggle_play_similar_async)

    async def _toggle_play_similar_async(self):
        target = not self.app.play_similar_mode
        if target:
            from utils import track_graph as tg
            try:
                missing = await self.app.db_manager.get_tracks_missing_features(tg.FEATURES_VERSION)
                if len(missing) > 0:
                    self.app.show_snackbar(
                        f"Play Similar is unavailable. {len(missing)} tracks lack DSP features. Run Jarvis analyzer first.",
                        color=AMBER,
                        icon=ft.Icons.WARNING_ROUNDED
                    )
                    return
            except Exception as exc:
                logger.exception("Play-Similar: Failed to verify missing features: %s", exc)

        self.app.set_play_similar_mode(target)

    def update_play_similar(self, enabled: bool):
        self._ensure_initialized()
        self._play_similar_btn.icon       = ft.Icons.LINK_ROUNDED if enabled else ft.Icons.LINK_OFF_ROUNDED
        self._play_similar_btn.icon_color = CYAN if enabled else DIM
        
        lib = getattr(self.app, "library_view", None)
        in_mood_partition = (
            lib is not None
            and getattr(lib, "view_mode", "") == "partitions"
            and getattr(lib, "partition_sub_mode", "") == "moods"
        )
        show_feedback = (in_mood_partition or getattr(self.app, "auto_dj_mode", False)) and not enabled
        self._like_btn.visible = show_feedback
        self._dislike_btn.visible = show_feedback
        
        self._artwork_container.border = ft.Border.all(3, CYAN) if enabled else (ft.Border.all(3, AMBER) if getattr(self.app, "auto_dj_mode", False) else None)
        self._art_placeholder.border   = ft.Border.all(3, CYAN) if enabled else (ft.Border.all(3, AMBER) if getattr(self.app, "auto_dj_mode", False) else None)

        try:
            self._play_similar_btn.update()
        except (RuntimeError, AssertionError):
            pass
        try:
            self._like_btn.update()
        except (RuntimeError, AssertionError):
            pass
        try:
            self._dislike_btn.update()
        except (RuntimeError, AssertionError):
            pass
        try:
            self._artwork_container.update()
        except (RuntimeError, AssertionError):
            pass
        try:
            self._art_placeholder.update()
        except (RuntimeError, AssertionError):
            pass

    def _toggle_auto_dj(self, e):
        self.app.set_auto_dj_mode(not self.app.auto_dj_mode)

    def update_auto_dj(self, enabled: bool):
        self._ensure_initialized()
        self._auto_dj_btn.icon       = ft.Icons.AUTO_AWESOME_ROUNDED if enabled else ft.Icons.AUTO_AWESOME_OUTLINED
        self._auto_dj_btn.icon_color = AMBER if enabled else DIM
        
        lib = getattr(self.app, "library_view", None)
        in_mood_partition = (
            lib is not None
            and getattr(lib, "view_mode", "") == "partitions"
            and getattr(lib, "partition_sub_mode", "") == "moods"
        )
        show_feedback = (enabled or in_mood_partition) and not getattr(self.app, "play_similar_mode", False)
        self._like_btn.visible = show_feedback
        self._dislike_btn.visible = show_feedback
        
        self._artwork_container.border = ft.Border.all(3, AMBER) if enabled else (ft.Border.all(3, CYAN) if self.app.play_similar_mode else None)
        self._art_placeholder.border   = ft.Border.all(3, AMBER) if enabled else (ft.Border.all(3, CYAN) if self.app.play_similar_mode else None)

        try:
            self._auto_dj_btn.update()
        except (RuntimeError, AssertionError):
            pass
        try:
            self._like_btn.update()
        except (RuntimeError, AssertionError):
            pass
        try:
            self._dislike_btn.update()
        except (RuntimeError, AssertionError):
            pass
        try:
            self._artwork_container.update()
        except (RuntimeError, AssertionError):
            pass
        try:
            self._art_placeholder.update()
        except (RuntimeError, AssertionError):
            pass
