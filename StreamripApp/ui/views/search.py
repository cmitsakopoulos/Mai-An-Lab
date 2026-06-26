import os
import re
import sys
import shutil
import math
import logging
import asyncio
import flet as ft
from ui.tokens import (
    BG, SURFACE, SURFACE2, CYAN, AMBER, TEXT, DIM, BORDER, 
    SOURCE_COLORS, LIB_ARTIST_COLOR, LIB_ALBUM_COLOR, LIB_TRACK_COLOR, 
    apply_opacity
)
from ui.widgets import AnimatedEntry, SkeletonRow, src_color, strip_markup, build_page_ghost_top, build_page_ghost_bottom

if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

from utils.filepath_utils import get_app_dir

logger = logging.getLogger(__name__)


class ConnectionSignal(ft.Row):
    def __init__(self):
        # 4 bars with heights: 4, 7, 10, 13
        # No animation to conserve battery consumption
        self.bars = [
            ft.Container(width=3, height=4, bgcolor=DIM, border_radius=1),
            ft.Container(width=3, height=7, bgcolor=DIM, border_radius=1),
            ft.Container(width=3, height=10, bgcolor=DIM, border_radius=1),
            ft.Container(width=3, height=13, bgcolor=DIM, border_radius=1),
        ]
        super().__init__(
            controls=self.bars,
            spacing=1.5,
            alignment=ft.MainAxisAlignment.START,
            vertical_alignment=ft.CrossAxisAlignment.END,
            visible=False,
        )

    def set_level(self, level: int, connected: bool = False):
        # level: 0 (all dimmed) to 4 (all lit)
        # connected: turns bars green when connection is established
        color = "#00E676" if connected else CYAN
        inactive_color = apply_opacity(0.2, color)
        for i, bar in enumerate(self.bars):
            if i < level:
                bar.bgcolor = color
            else:
                bar.bgcolor = inactive_color


class SearchView:
    def __init__(self, app: "StreamripFletApp"):
        from utils.streamrip_search import StreamripSearcher
        self.app             = app
        self.page            = app.page
        self.searcher        = StreamripSearcher()
        self._connection_signal = ConnectionSignal()
        self.current_search_id = 0
        self.selected_source = "qobuz"
        # Unified pre-fetch cache: all three types are fetched in one search call.
        # Keyed by media_type singular ("track", "album", "artist").
        self.cached_results: dict[str, list[dict]] = {"track": [], "album": [], "artist": []}
        self._active_preview_data: dict | None = None
        self._active_preview_task: asyncio.Task | None = None
        self._active_preview_stop_event: asyncio.Event | None = None
        self.expanded_nodes: set[str] = set() # Track IDs/Artist IDs of expanded items
        self.node_cache: dict[str, list[dict]] = {} # Cache for expanded node children
        self.view_mode = "tracks" # artist, album, track (plural, matches tab labels)
        self._hide_card_task: asyncio.Task | None = None
        self._hide_search_card_task: asyncio.Task | None = None
        self._hide_preview_card_task: asyncio.Task | None = None

        self.current_offset = 0
        self._is_loading_more = False

        # ── Search Bar & Sources ───────────────────────────────────────────
        self._search_field = ft.TextField(
            hint_text="Artist, album or track…",
            hint_style=ft.TextStyle(color=DIM, size=15),
            bgcolor="transparent",
            border=ft.InputBorder.NONE,
            text_style=ft.TextStyle(color=TEXT, size=16),
            content_padding=ft.Padding.only(left=4, right=4, top=18, bottom=18),
            on_submit=lambda e: asyncio.create_task(self.start_search()),
            on_change=self._on_input_change,
            on_focus=self._on_search_focus,
            on_blur=self._on_search_blur,
            expand=True,
            cursor_color=CYAN,
        )

        self._clear_btn = ft.IconButton(
            icon=ft.Icons.CLOSE,
            icon_color=DIM,
            icon_size=18,
            visible=False,
            on_click=self._clear_search,
        )

        self._search_go_btn = ft.Container(
            content=ft.Icon(ft.Icons.ARROW_FORWARD_ROUNDED, color=BG, size=20),
            bgcolor=CYAN,
            width=44, height=44,
            border_radius=12,
            alignment=ft.Alignment(0, 0),
            on_click=lambda e: asyncio.create_task(self.start_search()),
            animate=ft.Animation(120, ft.AnimationCurve.EASE_OUT),
        )

        self._mic_btn = None # Moved to AssistantView

        self._search_bar_container = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.SEARCH_ROUNDED, color=CYAN, size=20),
                    self._search_field,
                    self._clear_btn,
                    self._search_go_btn,
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=SURFACE2,
            border=ft.Border.all(1.5, BORDER),
            border_radius=16,
            padding=ft.Padding.only(left=14, right=8, top=0, bottom=0),
            expand=True,
        )

        self._search_row = ft.Row(
            [self._search_bar_container],
            spacing=0,
        )


        self._search_progress = ft.ProgressRing(
            width=20, height=20,
            stroke_width=2,
            color=CYAN,
            visible=False,
        )

        # Pagination variables
        self.current_page = 0
        self.total_pages = 1
        self.items_per_page = 35
        self._is_changing_page = False
        self._is_programmatic_scroll = False
        self._last_scroll_pixels = 0

        # results list
        self._results_list = ft.ListView(
            expand=True,
            spacing=8,
            padding=ft.Padding.only(left=12, right=12, top=4, bottom=20),
            on_scroll=self._on_list_scroll,
        )

        self._animated_results_wrapper = ft.Container(
            content=self._results_list,
            expand=True,
            offset=ft.Offset(0, 0),
            opacity=1.0,
            animate_offset=ft.Animation(100, ft.AnimationCurve.EASE_OUT_QUAD),
            animate_opacity=ft.Animation(100, ft.AnimationCurve.EASE_OUT_QUAD),
        )

        # Pagination bar styled and behaving identically to LibraryView:
        # explicit arrow taps OR horizontal swipe on the bar — no scroll-to-
        # boundary auto-advance. Buttons start disabled until results land.
        self._prev_page_btn = ft.IconButton(
            icon=ft.Icons.CHEVRON_LEFT_ROUNDED,
            icon_color=DIM,
            icon_size=20,
            disabled=True,
            tooltip="Previous Page",
            on_click=lambda e: self.page.run_task(self.change_page, self.current_page - 1, scroll_to_bottom=True),
        )
        self._next_page_btn = ft.IconButton(
            icon=ft.Icons.CHEVRON_RIGHT_ROUNDED,
            icon_color=DIM,
            icon_size=20,
            disabled=True,
            tooltip="Next Page",
            on_click=lambda e: self.page.run_task(self.change_page, self.current_page + 1, scroll_to_bottom=False),
        )
        self._page_label = ft.Text(
            "Page 1 of 1",
            color=TEXT,
            size=12,
            weight=ft.FontWeight.W_700,
        )
        self._pagination_bar = ft.Container(
            content=ft.Row(
                [
                    self._prev_page_btn,
                    self._page_label,
                    self._next_page_btn,
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=20,
            ),
            bgcolor=apply_opacity(0.1, SURFACE),
            border_radius=12,
            padding=ft.Padding.symmetric(vertical=4, horizontal=16),
            margin=ft.Margin.only(left=14, right=14, bottom=6),
            border=ft.Border.all(1, apply_opacity(0.1, CYAN)),
            visible=False,
        )

        # setup prompt (shown when credentials missing)
        self._setup_prompt = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.LOCK_OUTLINE_ROUNDED, color=CYAN, size=48),
                    ft.Text("Setup Required", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text("Please enter your Qobuz credentials in Settings to enable search.", 
                            color=DIM, size=13, text_align=ft.TextAlign.CENTER),
                    ft.Container(height=12),
                    ft.Row([
                        ft.Button(
                            "Go to Settings",
                            icon=ft.Icons.SETTINGS_ROUNDED,
                            on_click=lambda _: self.app._switch_tab(3),
                            style=ft.ButtonStyle(color=BG, bgcolor=CYAN)
                        ),
                        ft.TextButton(
                            "Refresh",
                            icon=ft.Icons.REFRESH_ROUNDED,
                            on_click=lambda _: self.refresh_setup_state(),
                        ),
                    ], alignment=ft.MainAxisAlignment.CENTER),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
            alignment=ft.Alignment(0, 0),
            visible=False,
            expand=True,
            padding=30,
        )

        # empty state
        self._empty_label = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.SEARCH_OFF, color=DIM, size=48),
                    ft.Text("No results found", color=DIM, size=14),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
            alignment=ft.Alignment(0, 0),
            visible=False,
            expand=True,
        )


        # download progress card
        self._progress_status  = ft.Text("Ready", color=TEXT, size=13, weight=ft.FontWeight.W_700)
        self._progress_pct     = ft.Text("", color=CYAN, size=12)
        self._progress_detail  = ft.Text("", color=DIM,  size=11, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
        self._progress_meta    = ft.Text("", color=TEXT, size=13, weight=ft.FontWeight.W_600,
                                         max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self._progress_bar     = ft.ProgressBar(value=0, color=CYAN, bgcolor=SURFACE2, expand=True)
        self._progress_spinner = ft.ProgressRing(width=18, height=18, stroke_width=2, color=CYAN, visible=False)
        self._queue_chips_row  = ft.Row(spacing=6, wrap=True)
        self._progress_up_next = ft.Text("", color=DIM, size=11, weight=ft.FontWeight.W_500, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, visible=False)

        self._cancel_btn = ft.TextButton(
            "Cancel",
            style=ft.ButtonStyle(color={"": "#FF4444"}),
            on_click=lambda e: self.app.queue.cancel_current(),
        )
        self._clear_queue_btn = ft.TextButton(
            "Clear All",
            style=ft.ButtonStyle(color={"": DIM}),
            on_click=lambda e: self.app.queue.clear(),
        )

        self._progress_card = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._progress_spinner,
                            ft.Column(
                                [
                                    ft.Row([self._progress_status, self._progress_pct], spacing=8),
                                    self._progress_meta,
                                    self._progress_detail,
                                ],
                                spacing=2,
                                expand=True,
                            ),
                            ft.Column(
                                [self._cancel_btn, self._clear_queue_btn],
                                spacing=0,
                            ),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self._progress_bar,
                    self._queue_chips_row,
                    self._progress_up_next,
                ],
                spacing=8,
            ),
            bgcolor=SURFACE,
            border=ft.Border.all(1, CYAN + "55"),
            border_radius=12,
            padding=14,
            margin=ft.Margin.symmetric(horizontal=12),
            visible=False,
            offset=ft.Offset(0, 0.4),
            animate_offset=ft.Animation(300, ft.AnimationCurve.EASE_OUT_BACK),
            opacity=0,
            animate_opacity=ft.Animation(250, ft.AnimationCurve.EASE_OUT),
        )

        # Search connection progress card
        self._search_progress_status = ft.Text("Searching Qobuz...", color=TEXT, size=13, weight=ft.FontWeight.W_700)
        self._search_progress_detail = ft.Text("Connecting to Qobuz API...", color=DIM, size=11, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
        self._search_progress_spinner = ft.ProgressRing(width=18, height=18, stroke_width=2, color=CYAN)

        self._search_progress_card = ft.Container(
            content=ft.Row(
                [
                    self._search_progress_spinner,
                    ft.Column(
                        [
                            self._search_progress_status,
                            self._search_progress_detail,
                        ],
                        spacing=2,
                        expand=True,
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=SURFACE,
            border=ft.Border.all(1, CYAN + "55"),
            border_radius=12,
            padding=14,
            margin=ft.Margin.symmetric(horizontal=12),
            visible=False,
            offset=ft.Offset(0, 0.4),
            animate_offset=ft.Animation(300, ft.AnimationCurve.EASE_OUT_BACK),
            opacity=0,
            animate_opacity=ft.Animation(250, ft.AnimationCurve.EASE_OUT),
        )

        # Preview progress card
        self._preview_progress_status = ft.Text("Loading Preview...", color=TEXT, size=13, weight=ft.FontWeight.W_700)
        self._preview_progress_detail = ft.Text("Initializing...", color=DIM, size=11, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
        self._preview_progress_spinner = ft.ProgressRing(width=18, height=18, stroke_width=2, color=CYAN)
        self._preview_cancel_btn = ft.IconButton(
            icon=ft.Icons.CLOSE,
            icon_color=DIM,
            icon_size=16,
            tooltip="Cancel Preview",
            on_click=self._cancel_preview_click,
        )

        self._preview_progress_card = ft.Container(
            content=ft.Row(
                [
                    self._preview_progress_spinner,
                    ft.Column(
                        [
                            self._preview_progress_status,
                            self._preview_progress_detail,
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    self._preview_cancel_btn,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=SURFACE,
            border=ft.Border.all(1, CYAN + "55"),
            border_radius=12,
            padding=14,
            margin=ft.Margin.symmetric(horizontal=12),
            visible=False,
            offset=ft.Offset(0, 0.4),
            animate_offset=ft.Animation(300, ft.AnimationCurve.EASE_OUT_BACK),
            opacity=0,
            animate_opacity=ft.Animation(250, ft.AnimationCurve.EASE_OUT),
        )

        # history list
        self._history_list = ft.ListView(spacing=8, padding=ft.Padding.symmetric(horizontal=12))

        # Recent searches sheet (instantiated once)
        self._history_sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column([], tight=True, spacing=0),
                padding=20,
                bgcolor=SURFACE,
            ),
        )
        self.page.overlay.append(self._history_sheet)

        self._history_expanded = False
        self._history_section = ft.Container()

        self._search_indicator = ft.ProgressRing(width=16, height=16, stroke_width=2, color=CYAN, visible=False)
        self._view_tabs_row = ft.Row(spacing=8)
        self._update_view_tabs()

        # ── Landing Page Container ──
        self._landing_container = ft.ListView(
            expand=True,
            spacing=0,
            padding=ft.Padding.only(left=16, right=16, top=10, bottom=100),
            visible=True,
        )

        # ── Root container ─────────────────────────────────────────────────
        self._root = ft.Column(
            [
                ft.Container(
                    content=ft.Column(
                        [
                            # Title + settings shortcut
                            ft.Row(
                                [
                                    ft.Column(
                                        [
                                            ft.Text("Streamrip", size=26, weight=ft.FontWeight.W_800, color=TEXT),
                                            ft.Row(
                                                [
                                                    ft.Text("Qobuz", size=14, color=CYAN, weight=ft.FontWeight.W_500),
                                                    self._connection_signal,
                                                ],
                                                spacing=8,
                                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                            ),
                                        ],
                                        spacing=2,
                                        expand=True,
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.HISTORY,
                                        icon_color=DIM, icon_size=22,
                                        on_click=self._show_recent_searches,
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.SETTINGS_OUTLINED,
                                        icon_color=DIM, icon_size=22,
                                        on_click=lambda e: self.app._switch_tab(3),
                                    ),
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            # Search bar
                            ft.Container(
                                content=ft.Row([
                                    self._search_field,
                                    self._search_indicator,
                                    self._clear_btn,
                                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                bgcolor=SURFACE2,
                                border_radius=16,
                                padding=ft.Padding.only(left=14, right=8),
                                border=ft.Border.all(1.5, BORDER),
                            ),
                            self._view_tabs_row,
                        ],
                        spacing=14,
                    ),
                    padding=ft.Padding.only(left=16, right=16, top=20, bottom=8),
                ),

                self._progress_card,
                self._preview_progress_card,
                self._search_progress_card,
                
                # Main Results Area
                ft.Stack(
                    [
                        self._animated_results_wrapper,
                        self._empty_label,
                        self._setup_prompt,
                        self._landing_container,
                    ],
                    expand=True,
                ),
                
                self._pagination_bar,
            ],
            expand=True,
            spacing=0,
        )


    def try_update(self, *controls):
        for c in controls:
            try: c.update()
            except: pass

    def _build_top_ghost(self) -> ft.Control:
        return build_page_ghost_top(
            lambda e: self.page.run_task(self.change_page, self.current_page - 1, scroll_to_bottom=True)
        )

    def _build_bottom_ghost(self) -> ft.Control:
        return build_page_ghost_bottom(
            lambda e: self.page.run_task(self.change_page, self.current_page + 1, scroll_to_bottom=False)
        )

    def _update_pagination_ui(self):
        total = max(1, self.total_pages)
        self._page_label.value = f"Page {self.current_page + 1} of {total}"
        self._prev_page_btn.disabled = self.current_page <= 0
        self._prev_page_btn.icon_color = DIM if self.current_page <= 0 else CYAN
        self._next_page_btn.disabled = self.current_page >= self.total_pages - 1
        self._pagination_bar.visible = self.total_pages > 1
        self.try_update(self._pagination_bar)

    def _on_list_scroll(self, e: ft.OnScrollEvent):
        # Position-tracking only; no auto-advance on boundary. Pagination is
        # explicit via the pagination bar (tap arrows or swipe horizontally),
        # mirroring LibraryView.
        if self._is_changing_page or getattr(self, "_is_programmatic_scroll", False):
            return
        self._last_scroll_pixels = e.pixels

    def _on_pagination_swipe(self, e):
        """Switch pages on horizontal swipe of the pagination bar. Same
        velocity threshold and direction mapping as LibraryView so the two
        pages feel identical."""
        if self._is_changing_page or getattr(self, "_is_programmatic_scroll", False):
            return
        vx = getattr(e, "primary_velocity", 0) or 0
        if abs(vx) < 300:
            return
        if vx < 0:  # swipe left → next
            if self.current_page < self.total_pages - 1:
                self.page.run_task(self.change_page, self.current_page + 1)
        else:       # swipe right → previous
            if self.current_page > 0:
                self.page.run_task(self.change_page, self.current_page - 1, scroll_to_bottom=True)

    async def change_page(self, new_page: int, scroll_to_bottom: bool = False):
        if self._is_changing_page or new_page < 0 or new_page >= self.total_pages:
            return
        
        self._is_changing_page = True
        try:
            # 1. Slide Out active view (left if going forward, right if going backward)
            is_forward = new_page > self.current_page
            exit_offset = ft.Offset(-0.15, 0) if is_forward else ft.Offset(0.15, 0)
            entry_offset = ft.Offset(0.15, 0) if is_forward else ft.Offset(-0.15, 0)
            
            self._animated_results_wrapper.offset = exit_offset
            self._animated_results_wrapper.opacity = 0.0
            self.try_update(self._animated_results_wrapper)
            
            # Wait for transition animation to finish (snappy 80ms)
            await asyncio.sleep(0.08)
            
            # 2. Update page index and instantiate new controls
            self.current_page = new_page
            
            active_type = self.view_mode[:-1]  # "tracks" -> "track"
            source = self.cached_results.get(active_type, [])
            
            # Re-slice items
            start_idx = self.current_page * self.items_per_page
            end_idx = start_idx + self.items_per_page
            page_items = source[start_idx:end_idx]
            
            controls = []
            
            if self.current_page > 0:
                controls.append(self._build_top_ghost())
                
            for i, r in enumerate(page_items):
                card = self._result_card(start_idx + i, r, depth=0)
                controls.append(card)
                
            if self.current_page < self.total_pages - 1:
                controls.append(self._build_bottom_ghost())
                    
            # Update controls
            self._results_list.controls = controls
            self._update_pagination_ui()
            
            # 3. Teleport off-screen to the other side instantly
            self._animated_results_wrapper.offset = entry_offset
            self.try_update(self._animated_results_wrapper, self._results_list)
            
            # Wait a tiny tick for layout (snappy 40ms)
            await asyncio.sleep(0.04)
            
            # 4. Scroll to target offset safely
            self._is_programmatic_scroll = True
            try:
                if scroll_to_bottom:
                    target_offset = 3250
                else:
                    target_offset = 45 if self.current_page > 0 else 0
                
                await self._results_list.scroll_to(offset=target_offset, duration=0)
                self._last_scroll_pixels = target_offset
            except Exception:
                pass
            finally:
                await asyncio.sleep(0.03)
                self._is_programmatic_scroll = False
            
            # 5. Slide In the new view from the other side
            self._animated_results_wrapper.offset = ft.Offset(0, 0)
            self._animated_results_wrapper.opacity = 1.0
            self.try_update(self._animated_results_wrapper)
            
        except Exception as ex:
            logger.error(f"Error in SearchView.change_page: {ex}")
        finally:
            # Cooldown to let scroll physics settle fully (snappy 150ms)
            await asyncio.sleep(0.15)
            self._is_changing_page = False


    # ── public build ────────────────────────────────────────────────────────
    def build(self) -> ft.Control:
        self.refresh_setup_state(update=False)
        return self._root

    def refresh_setup_state(self, update=True):
        from utils.streamrip_api import load_config
        cfg = load_config()
        q = cfg.get("qobuz", {})
        has_creds = bool(q.get("email_or_userid") and q.get("password_or_token"))
        
        landing_cfg = cfg.get("landing", {})
        show_most_listened = bool(landing_cfg.get("show_search_history", True))
        show_stats   = bool(landing_cfg.get("show_library_stats", True))

        def _apply():
            self._setup_prompt.visible = not has_creds
            if not has_creds:
                self._results_list.controls.clear()
                self._empty_label.visible = False
                self._landing_container.visible = False
            else:
                # If no search query, show landing page
                is_empty = not bool(self._search_field.value)
                self._landing_container.visible = is_empty
                if is_empty:
                    self.page.run_task(self._refresh_landing_page, show_most_listened, show_stats)
        
        if update:
            self.app.safe_update(_apply)
        else:
            _apply()

    async def _refresh_landing_page(self, show_most_listened, show_stats):
        if getattr(self.app, "_is_restarting", False):
            return
            
        self._landing_container.controls.clear()
        
        try:
            stats_content = await self._get_library_stats_content()
            self._landing_container.controls.append(
                self._build_landing_card("Library at a Glance", ft.Icons.INSERT_CHART_OUTLINED_ROUNDED, stats_content)
            )
            
            if show_most_listened:
                most_played_tracks = await self.app.db_manager.get_most_played(limit=5)
                if most_played_tracks:
                    most_played_content = self._build_most_played_content(most_played_tracks)
                    self._landing_container.controls.append(
                        self._build_landing_card("Most Listened Tracks", ft.Icons.REPLAY_ROUNDED, most_played_content)
                    )

        except Exception as e:
            logger.warning(f"Failed to refresh landing page during transition: {e}")
            return

        self.app.safe_update(lambda: None)

    def _build_most_played_content(self, tracks):
        items = []
        for t in tracks:
            items.append(
                ft.Container(
                    content=ft.Row([
                        ft.Icon(ft.Icons.MUSIC_NOTE_ROUNDED, color=CYAN, size=16),
                        ft.Column([
                            ft.Text(t['title'], color=TEXT, size=13, weight="bold", no_wrap=True),
                            ft.Text(f"{t['artist']} • {t['count']} plays", color=DIM, size=11),
                        ], spacing=1, expand=True),
                    ], spacing=12),
                    padding=ft.Padding.symmetric(vertical=8),
                    on_click=lambda e, p=t['path']: self.app.page.run_task(self.app.play_track, p),
                )
            )
        return ft.Column(items, spacing=0)

    def _build_landing_card(self, title: str, icon: str, content: ft.Control):
        return ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(icon, color=CYAN, size=18),
                    ft.Text(title, color=TEXT, size=14, weight=ft.FontWeight.W_700),
                ], spacing=8),
                ft.Divider(color=BORDER, height=16),
                content
            ], spacing=0),
            bgcolor=SURFACE2,
            border_radius=12,
            padding=14,
            margin=ft.Margin.only(bottom=16),
        )

    async def _get_library_stats_content(self):
        total_tracks = await self.app.db_manager.get_total_tracks()
        artists = await self.app.db_manager.get_all_artists()
        albums = await self.app.db_manager.get_all_albums()
        playlists = await self.app.db_manager.get_all_playlists()
        
        return ft.Row([
            self._build_stat_item("Tracks", str(total_tracks)),
            self._build_stat_item("Albums", str(len(albums))),
            self._build_stat_item("Artists", str(len(artists))),
            self._build_stat_item("Playlists", str(len(playlists))),
        ], alignment=ft.MainAxisAlignment.SPACE_AROUND)

    def _build_stat_item(self, label, value):
        return ft.Column([
            ft.Text(value, color=TEXT, size=18, weight=ft.FontWeight.BOLD),
            ft.Text(label.upper(), color=DIM, size=9, weight=ft.FontWeight.W_600),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2)

    def _on_input_change(self, e):
        def _mutate():
            has_val = bool(e.control.value)
            self._clear_btn.visible = has_val
            # Hide landing page when we have content
            self._landing_container.visible = not has_val
            if not has_val:
                self._connection_signal.visible = False
        self.app.safe_update(_mutate)

    def _on_search_focus(self, _e):
        def _mutate():
            self._search_bar_container.border = ft.Border.all(1.5, CYAN + "99")
            self._search_bar_container.bgcolor = SURFACE
            self._search_go_btn.bgcolor = CYAN
        self.app.safe_update(_mutate)

    def _on_search_blur(self, _e):
        def _mutate():
            self._search_bar_container.border = ft.Border.all(1.5, BORDER)
            self._search_bar_container.bgcolor = SURFACE2
        self.app.safe_update(_mutate)

    def _show_recent_searches(self, e):
        from utils.search_history import load_searches
        searches = load_searches()
        if not searches: return
        
        def _mutate():
            self._history_sheet.content.content.controls = [
                ft.Text("Recent Searches", color=TEXT, size=15, weight=ft.FontWeight.W_700),
                ft.Divider(color=BORDER),
                *[
                    ft.ListTile(
                        title=ft.Text(s, color=TEXT),
                        on_click=lambda _, q=s: self._recent_clicked(q),
                    ) for s in searches
                ]
            ]
            self._history_sheet.open = True
        self.app.safe_update(_mutate)

    def _recent_clicked(self, query):
        if hasattr(self, "_history_sheet"):
            def _mutate():
                self._history_sheet.open = False
            self.app.safe_update(_mutate)
        self._do_recent(query)

    def _do_recent(self, query):
        self._search_field.value = query
        asyncio.create_task(self.start_search())

    # ── search logic ────────────────────────────────────────────────────────
    def _clear_search(self, _e=None):
        # Bump current_search_id so any in-flight searcher.search callback
        # fails its id-equality guard in _on_results and gets dropped.
        self.current_search_id += 1
        self.hide_search_progress(success=False)

        def _mutate():
            self._stop_skeleton_pulse()
            self._search_field.value = ""
            self._clear_btn.visible  = False
            self._search_indicator.visible = False
            self._results_list.controls.clear()
            self._empty_label.visible = False
            self.cached_results = {"track": [], "album": [], "artist": []}
            self.expanded_nodes.clear()
            self.node_cache.clear()
            self.current_page = 0
            self.total_pages = 1
            self._pagination_bar.visible = False
            self._landing_container.visible = True
        self.app.safe_update(_mutate)
        self.refresh_setup_state()

    async def start_search(self, _e=None):
        await self.app.error_boundary.capture(self._start_search_core)(_e)

    async def _start_search_core(self, _e=None):
        await asyncio.sleep(0)
        query = (self._search_field.value or "").strip()
        if not query:
            self.app.show_snackbar("Please enter a search term.")
            return

        from utils.search_history import add_search
        add_search(query)
        preview_dir = os.path.join(get_app_dir(), "previews")
        if await asyncio.to_thread(os.path.exists, preview_dir):
            await asyncio.to_thread(shutil.rmtree, preview_dir, ignore_errors=True)

        self.current_search_id += 1
        search_id = self.current_search_id
        self._active_preview_data = None
        self.expanded_nodes.clear()
        self.node_cache.clear()
        self.current_offset = 0
        self._is_loading_more = False
        self._empty_label.visible = False
        self._landing_container.visible = False
        
        # Proactive check for credentials
        from utils.streamrip_api import load_config
        cfg = load_config()
        q = cfg.get("qobuz", {})
        if not q.get("email_or_userid") or not q.get("password_or_token"):
            self.app.show_snackbar("Qobuz credentials not set. Search disabled.", icon=ft.Icons.LOCK_OUTLINE_ROUNDED, color="#FFA500")
            self.app._switch_tab(3)
            return

        self._search_indicator.visible = True
        self._clear_btn.visible = True

        # Show connection stages progress card
        self.show_search_progress("Initializing...", "Starting search query...")

        # Skeleton rows
        cards = [SkeletonRow(delay=i * 0.08) for i in range(8)]
        def _mutate_start():
            self._results_list.controls = cards
            self._results_list.opacity = 1.0
            self._results_list.offset = ft.Offset(0, 0)
        self.app.safe_update(_mutate_start)

        def search_progress_callback(status, detail):
            if self.current_search_id == search_id:
                self.update_search_progress(status, detail)

        def results_callback(results):
            if self.current_search_id == search_id:
                success = results is not None and not (isinstance(results, dict) and "error" in results)
                self.hide_search_progress(success=success)
                self._on_results(results)

        asyncio.create_task(asyncio.to_thread(
            self.searcher.search,
            query, self.selected_source, results_callback,
            media_types=["track", "album", "artist"],
            limit=250, offset=0,
            progress_callback=search_progress_callback
        ))

    def _on_results(self, results, *args, **kwargs):
        self._is_loading_more = False
        async def _update_ui():
            self._stop_skeleton_pulse()
            self._search_indicator.visible = False
            
            if results is None:
                self.app.show_snackbar("Search failed: Network error.")
                return

            if isinstance(results, dict) and "error" in results:
                self.app._show_error(results.get("error", "Unknown error"))
                return

            if not results:
                self._empty_label.visible = True
                self._results_list.controls.clear()
                self._update_pagination_ui()
                self.page.update()
                return

            async def _check_library(r):
                m_type = r.get("media_type", "track")
                title = strip_markup(r.get("ui_title", r.get("name", "")))
                artist = strip_markup(r.get("ui_subtitle", r.get("artist", "")))
                if m_type == "track":
                    exists = await self.app.db_manager.get_track_by_meta(title, artist)
                else:
                    exists = await self.app.db_manager.get_album_by_meta(title, artist)
                r["is_in_library"] = bool(exists)

            await asyncio.gather(*[_check_library(r) for r in results])

            # Full search: route every result into its typed bucket
            self.cached_results = {"track": [], "album": [], "artist": []}
            for r in results:
                m_type = r.get("media_type", "track")
                if m_type in self.cached_results:
                    self.cached_results[m_type].append(r)
            
            self.current_page = 0
            self._rebuild_results()

        self.page.run_task(_update_ui)

    def _rebuild_results(self):
        active_type = self.view_mode[:-1]  # "tracks" -> "track"
        source = self.cached_results.get(active_type, [])

        self._results_list.controls.clear()

        self.total_pages = math.ceil(len(source) / self.items_per_page)
        
        # Sliced items for current page
        start_idx = self.current_page * self.items_per_page
        end_idx = start_idx + self.items_per_page
        page_items = source[start_idx:end_idx]

        self._empty_label.visible = not source

        first_chunk = []
        if self.current_page > 0:
            first_chunk.append(self._build_top_ghost())

        for i, r in enumerate(page_items):
            card = self._result_card(start_idx + i, r, depth=0)
            first_chunk.append(card)

        if self.current_page < self.total_pages - 1:
            first_chunk.append(self._build_bottom_ghost())

        self._results_list.controls.extend(first_chunk)
        self._update_pagination_ui()
        self.app.safe_update(lambda: None)

    def _set_view_mode(self, mode: str):
        self.view_mode = mode
        self.expanded_nodes.clear()
        self.current_page = 0
        self._update_view_tabs()
        query = (self._search_field.value or "").strip()
        if query:
            cache_populated = any(v for v in self.cached_results.values())
            if cache_populated:
                self._rebuild_results()
            else:
                asyncio.create_task(self.start_search())
        else:
            self.app.page.update()

    def _update_view_tabs(self):
        icons = {
            "artists": ft.Icons.PERSON_ROUNDED,
            "albums": ft.Icons.ALBUM_ROUNDED,
            "tracks": ft.Icons.MUSIC_NOTE_ROUNDED,
        }
        accents = {
            "artists": LIB_ARTIST_COLOR,
            "albums": LIB_ALBUM_COLOR,
            "tracks": LIB_TRACK_COLOR,
        }
        tabs = []
        for mode in ["artists", "albums", "tracks"]:
            is_active = (self.view_mode == mode)
            col = accents[mode]
            label = mode.capitalize()
            tabs.append(
                ft.GestureDetector(
                    content=ft.Container(
                        content=ft.Column(
                            [
                                ft.Icon(icons[mode], color=BG if is_active else col, size=18),
                                ft.Text(label, size=10, weight=ft.FontWeight.W_700, color=BG if is_active else TEXT),
                            ],
                            spacing=2,
                            alignment=ft.MainAxisAlignment.CENTER,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        bgcolor=col if is_active else apply_opacity(0.08, col),
                        border=ft.Border.all(1, col if is_active else apply_opacity(0.2, col)),
                        width=88,
                        height=52,
                        border_radius=12,
                    ),
                    on_tap=lambda e, m=mode: self._set_view_mode(m)
                )
            )
        self._view_tabs_row.controls = tabs

    def _result_card(self, index: int, r: dict, depth: int = 0) -> ft.Control:
        m_type = r.get("media_type", "track")
        
        if m_type == "load_more_artist":
            return AnimatedEntry(self._build_load_more_button(r, depth), target_height=64, data=r, depth=depth)
        
        if m_type == "search_exhausted":
            card = ft.Container(
                content=ft.Text("; End of Discography ;", color=DIM, size=11, weight=ft.FontWeight.W_500),
                alignment=ft.Alignment(0, 0),
                padding=ft.Padding.only(left=20 * depth, top=16, bottom=16),
            )
            return AnimatedEntry(card, target_height=48, data=r, depth=depth)
            
        node_id = f"{m_type}_{r.get('id')}"
        is_expanded = node_id in self.expanded_nodes
        
        accent = {
            "artist": LIB_ARTIST_COLOR,
            "album": LIB_ALBUM_COLOR,
            "track": LIB_TRACK_COLOR,
        }.get(m_type, CYAN)
        
        icon_map = {
            "artist": ft.Icons.PERSON_ROUNDED,
            "album": ft.Icons.ALBUM_ROUNDED,
            "track": ft.Icons.MUSIC_NOTE_ROUNDED,
        }
        
        title    = strip_markup(r.get("ui_title",    r.get("name",   "Unknown")))
        subtitle = strip_markup(r.get("ui_subtitle", r.get("artist", "")))
        detail   = strip_markup(r.get("ui_detail",   ""))
        
        expected_preview_title = f"(Preview) {title}"
        is_playing = (
            (audio_engine.current_track == title or audio_engine.current_track == expected_preview_title)
            and audio_engine.current_artist == subtitle
        )

        is_in_library = r.get("is_in_library", False)
        download_icon = ft.Icons.CHECK_CIRCLE if is_in_library else ft.Icons.DOWNLOAD_OUTLINED
        download_color = CYAN if is_in_library else DIM
             
        expand_icon = ft.Icon(
            ft.Icons.KEYBOARD_ARROW_DOWN if is_expanded else ft.Icons.KEYBOARD_ARROW_RIGHT,
            color=accent if is_expanded else DIM,
            size=20,
        ) if m_type in ("artist", "album") else None

        _prev_state = r.get("preview_state", "idle")
        _prev_icon  = ft.Icons.PAUSE_CIRCLE if _prev_state == "playing" else (
                      ft.Icons.SYNC if _prev_state == "loading" else ft.Icons.PLAY_CIRCLE_OUTLINE)
        
        def on_download(_e, data=r):
            self.app.quality_selector_sheet.show(data)

        async def preview_click(e):
            if self._active_preview_data and self._active_preview_data != r:
                self._active_preview_data["preview_state"] = "idle"
            
            self._active_preview_data = r
            self.refresh_results_only()
            await asyncio.sleep(0)
            
            if r.get("preview_state") == "playing":
                audio_engine.stop()
                r.update({"preview_state": "idle"})
                self._active_preview_data = None
            else:
                r.update({"preview_state": "loading"})
                self._start_preview(index, r, preview_btn, tile)
            
            self.refresh_results_only()

        async def toggle_node(_e):
            await self._toggle_search_node(r, tile)

        preview_btn = ft.Container(
            content=ft.Icon(_prev_icon, color=CYAN if _prev_state != "idle" else DIM, size=20) if _prev_state != "loading" else 
                    ft.ProgressRing(width=16, height=16, stroke_width=2, color=CYAN),
            width=40, height=40, alignment=ft.Alignment(0, 0),
            on_click=preview_click,
            tooltip="Preview",
            border_radius=20,
        )


        tile = ft.ListTile(
            leading=ft.Row([
                ft.Container(width=depth * 20, visible=depth > 0),
                ft.Icon(icon_map.get(m_type, ft.Icons.MUSIC_NOTE), color=accent),
            ], tight=True),
            title=ft.Text(title, color=TEXT, size=13, weight=ft.FontWeight.W_600, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Text(f"{subtitle}{'  ·  ' + detail if detail else ''}", color=DIM, size=12, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            trailing=ft.Row([
                preview_btn if m_type == "track" else ft.Container(),
                ft.IconButton(
                    icon=download_icon,
                    icon_color=download_color,
                    icon_size=20,
                    tooltip="Redownload" if is_in_library else "Download",
                    on_click=on_download,
                ) if m_type in ("track", "album") else ft.Container(),
                expand_icon if expand_icon else ft.Container(),
            ], tight=True, spacing=0),
            bgcolor=apply_opacity(0.12, accent) if is_playing or _prev_state != "idle" else "transparent",
            on_click=preview_click if m_type == "track" else toggle_node,
        )

        return AnimatedEntry(tile, target_height=64, data=r, depth=depth)

    def _build_load_more_button(self, r: dict, depth: int) -> ft.Control:
        artist_id = r.get("id")
        offset = r.get("offset", 30)
        limit = r.get("limit", 30)
        
        btn = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.ADD_CIRCLE_OUTLINE, color=CYAN, size=20),
                ft.Text("Load More Albums", color=CYAN, size=13, weight=ft.FontWeight.W_600),
            ], alignment=ft.MainAxisAlignment.CENTER, spacing=10),
            bgcolor=apply_opacity(0.1, CYAN),
            border=ft.Border.all(1, apply_opacity(0.3, CYAN)),
            border_radius=12,
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            on_click=lambda e: on_click_handler(e),
        )
        
        def on_click_handler(e):
            btn.content = ft.Row([
                ft.ProgressRing(width=16, height=16, color=CYAN, stroke_width=2),
                ft.Text("Loading...", color=CYAN, size=13)
            ], alignment=ft.MainAxisAlignment.CENTER, spacing=10)
            btn.update()
            
            def callback(results):
                def _insert():
                    try:
                        parent_list = self._results_list.controls
                        curr_idx = -1
                        for i, entry in enumerate(parent_list):
                            if getattr(entry, "data", None) == r:
                                curr_idx = i
                                break
                        if curr_idx == -1: return
                        
                        parent_list.pop(curr_idx)
                        
                        if results and isinstance(results, list) and len(results) > 0:
                            for k, child in enumerate(results):
                                card = self._result_card(k, child, depth=depth)
                                parent_list.insert(curr_idx + k, card)
                                
                        self._results_list.update()
                    except:
                        pass
                self.app.safe_update(_insert)
                
            self.searcher.get_artist_albums(str(artist_id), callback, limit=limit, offset=offset)
        
        container = ft.Container(
            content=btn,
            padding=ft.Padding.only(left=20 * depth + 16, right=16, top=8, bottom=8),
        )
        
        return container 

    async def _toggle_search_node(self, node_data: dict, tile_ctrl: ft.ListTile):
        m_type = node_data.get("media_type")
        node_id = f"{m_type}_{node_data.get('id')}"
        is_expanding = node_id not in self.expanded_nodes
        
        parent_list = self._results_list.controls
        node_idx = -1
        for i, entry in enumerate(parent_list):
            if getattr(entry, "content", None) == tile_ctrl:
                node_idx = i
                break
        if node_idx == -1: return

        if is_expanding:
            self.expanded_nodes.add(node_id)
            accent = { "artist": LIB_ARTIST_COLOR, "album": LIB_ALBUM_COLOR }.get(m_type, CYAN)
            tile_ctrl.bgcolor = apply_opacity(0.12, accent)
            if isinstance(tile_ctrl.trailing, ft.Row):
                tile_ctrl.trailing.controls[2] = ft.Container(
                    content=ft.ProgressRing(width=16, height=16, stroke_width=2, color=accent),
                    width=20, height=20, alignment=ft.Alignment(0, 0)
                )
            self.app.page.update()

            def children_callback(children):
                self.node_cache[node_id] = children
                if node_id not in self.expanded_nodes: return
                async def _process():
                    async def _check_child(c):
                        title = strip_markup(c.get("ui_title", c.get("name", "")))
                        artist = strip_markup(c.get("ui_subtitle", c.get("artist", "")))
                        if c.get("media_type") == "track":
                            exists = await self.app.db_manager.get_track_by_meta(title, artist)
                        else: # album
                            exists = await self.app.db_manager.get_album_by_meta(title, artist)
                        c["is_in_library"] = bool(exists)

                    await asyncio.gather(*[_check_child(c) for c in children])
                    
                    def _insert():
                        try:
                            if isinstance(tile_ctrl.trailing, ft.Row):
                                tile_ctrl.trailing.controls[2] = ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN, color=accent, size=20)
                            
                            curr_idx = -1
                            for j, entry in enumerate(parent_list):
                                if getattr(entry, "content", None) == tile_ctrl:
                                    curr_idx = j; break
                            if curr_idx == -1: return

                            depth = 0
                            if isinstance(tile_ctrl.leading, ft.Row):
                                depth = int(tile_ctrl.leading.controls[0].width / 20)

                            for k, child in enumerate(children):
                                card = self._result_card(k, child, depth=depth+1)
                                parent_list.insert(curr_idx + 1 + k, card)
                            self._results_list.update()
                        except: pass
                    self.app.safe_update(_insert)
                self.page.run_task(_process)

            if node_id in self.node_cache:
                children_callback(self.node_cache[node_id])
                return

            if m_type == "artist":
                self.searcher.get_artist_albums(str(node_data.get("id")), children_callback)
            elif m_type == "album":
                self.searcher.get_album_tracks(str(node_data.get("id")), children_callback)

        else:
            self.expanded_nodes.discard(node_id)
            tile_ctrl.bgcolor = "transparent"
            if isinstance(tile_ctrl.trailing, ft.Row):
                tile_ctrl.trailing.controls[2] = ft.Icon(ft.Icons.KEYBOARD_ARROW_RIGHT, color=DIM, size=20)
            
            depth = 0
            if isinstance(tile_ctrl.leading, ft.Row):
                depth = int(tile_ctrl.leading.controls[0].width / 20)
            
            idx = node_idx + 1
            while idx < len(parent_list):
                child_entry = parent_list[idx]
                if not isinstance(child_entry, AnimatedEntry): break
                child_depth = getattr(child_entry, "depth", 0)
                if child_depth > depth:
                    if child_entry.data and isinstance(child_entry.data, dict):
                        c_id = f"{child_entry.data.get('media_type')}_{child_entry.data.get('id')}"
                        self.expanded_nodes.discard(c_id)
                    parent_list.pop(idx)
                    continue
                break
            self._results_list.update()

    def _stop_skeleton_pulse(self):
        pass

    def refresh_results_only(self):
        """Re-evaluates icons and backgrounds for all search result cards."""
        for entry in self._results_list.controls:
            if not isinstance(entry, AnimatedEntry):
                continue
            
            card = entry.content # ft.ListTile
            r = entry.data
            if not r: continue
            
            state = r.get("preview_state", "idle")
            ui_title  = strip_markup(r.get("ui_title", r.get("name", "Unknown")))
            ui_artist = strip_markup(r.get("ui_subtitle", r.get("artist", "")))
            expected_preview_title = f"(Preview) {ui_title}"
            
            is_playing = (
                (audio_engine.current_track == ui_title or audio_engine.current_track == expected_preview_title) 
                and audio_engine.current_artist == ui_artist
            )
            is_loading = (state == "loading")

            m_type = r.get("media_type", "track")
            accent = {
                "artist": LIB_ARTIST_COLOR,
                "album": LIB_ALBUM_COLOR,
                "track": LIB_TRACK_COLOR,
            }.get(m_type, CYAN)

            node_id = f"{m_type}_{r.get('id')}"
            is_expanded = node_id in self.expanded_nodes

            if is_playing or is_loading or is_expanded:
                card.bgcolor = apply_opacity(0.12, accent)
            else:
                card.bgcolor = "transparent"
            
            _prev_icon = ft.Icons.PAUSE_CIRCLE if state == "playing" else (
                         ft.Icons.SYNC if state == "loading" else ft.Icons.PLAY_CIRCLE_OUTLINE)
            
            if isinstance(card.trailing, ft.Row) and card.trailing.controls:
                if m_type == "track":
                    p_btn = card.trailing.controls[0]
                    if isinstance(p_btn, ft.Container):
                        if state == "loading":
                            p_btn.content = ft.ProgressRing(width=16, height=16, stroke_width=2, color=CYAN)
                        else:
                            p_btn.content = ft.Icon(_prev_icon, color=CYAN if state != "idle" else DIM, size=20)
                
                if m_type in ("artist", "album"):
                    e_icon = card.trailing.controls[-1]
                    if isinstance(e_icon, ft.Icon):
                        e_icon.name = ft.Icons.KEYBOARD_ARROW_DOWN if is_expanded else ft.Icons.KEYBOARD_ARROW_RIGHT
                        e_icon.color = accent if is_expanded else DIM

            card.update()

    def update_chips(self, chips):
        def _mutate():
            self._queue_chips_row.controls = chips
        self.app.safe_update(_mutate)

    def _start_preview(self, index: int, data: dict, icon_ctrl: ft.Icon, container_ctrl: ft.Container):
        if self._active_preview_task and not self._active_preview_task.done():
            self._active_preview_task.cancel()
        if hasattr(self, "_active_preview_stop_event") and self._active_preview_stop_event:
            self._active_preview_stop_event.set()

        self._active_preview_stop_event = asyncio.Event()

        async def _worker():
            try:
                track_id = data.get("id")
                title = re.sub(r"\[.*?\]", "", data.get("ui_title", data.get("name", ""))).strip()
                artist = data.get("ui_subtitle", data.get("artist", ""))
                
                self.show_preview_progress("Connecting...", f"Resolving stream URL for '{title}'...")
                
                stream_url = None
                if track_id:
                    try:
                        # Attempt to resolve direct stream URL
                        stream_url = await self.searcher.get_track_stream_url(str(track_id), quality=1)
                        logger.info("Direct preview stream URL resolved: %s", stream_url)
                    except Exception as stream_exc:
                        logger.warning("Streaming URL resolution failed, falling back to download: %s", stream_exc)
                        self.update_preview_progress(
                            "Streaming Unavailable", 
                            "Falling back to preview download..."
                        )
                        await asyncio.sleep(1.2) # Let the user read the status message
                
                # If we successfully resolved the stream URL, play it
                if stream_url:
                    meta = {
                        "path":         stream_url,
                        "track_title":  f"(Preview) {title}",
                        "artist_name":  artist,
                        "album_title":  "Streamrip Search",
                        "image_url":    data.get("image_url", data.get("image", "")),
                    }
                    
                    # Play the stream via audio engine
                    audio_engine.jarvis_controlled = False
                    audio_engine.set_queue([meta], start_index=0)
                    
                    # Monitor streaming playback for start success or error failure
                    playback_failed = False
                    error_signal = asyncio.Event()
                    
                    def on_err(_inst, _msg):
                        error_signal.set()
                        
                    audio_engine.bind(on_playback_error=on_err)
                    try:
                        # Wait up to 6.0 seconds (60 * 0.1s) to see if it starts playing or fails
                        for _ in range(60):
                            if error_signal.is_set():
                                playback_failed = True
                                break
                            if audio_engine.is_playing and (audio_engine.duration > 0.0 or audio_engine.position > 0.0):
                                break
                            await asyncio.sleep(0.1)
                    finally:
                        audio_engine.unbind(on_playback_error=on_err)
                        
                    if not playback_failed:
                        def _play_success():
                            data["preview_state"] = "playing"
                            icon_ctrl.content = ft.Icon(ft.Icons.STOP_CIRCLE_OUTLINED, color=CYAN, size=20)
                            container_ctrl.shadow = ft.BoxShadow(blur_radius=8, color=apply_opacity(0.15, CYAN))
                            self.app.show_snackbar(f"Streaming preview: {title}")
                            icon_ctrl.update()
                            container_ctrl.update()
                            self.hide_preview_progress()
                        self.app.safe_update(_play_success)
                        return
                    else:
                        logger.warning("Stream URL resolved but playback failed. Falling back to preview download...")
                        self.update_preview_progress(
                            "Stream Playback Failed", 
                            "Falling back to preview download..."
                        )
                        await asyncio.sleep(1.2)
                
                # FALLBACK LOGIC: Download preview file
                from utils.streamrip_api import download as _do_download
                url       = data.get("url", "")
                safe_name = "".join(c if c.isalnum() else "_" for c in title[:20])
                pdir      = os.path.join(get_app_dir(), "previews", f"{index}_{safe_name}")
                await asyncio.to_thread(os.makedirs, pdir, exist_ok=True)

                audio_file = await self._find_audio(pdir)
                if not audio_file and url:
                    self.update_preview_progress("Downloading...", f"Fetching preview for '{title}'...")
                    
                    # Define progress callback for download
                    def dl_progress(status_data):
                        pct = status_data.get("percent", 0)
                        msg = status_data.get("message", "")
                        self.update_preview_progress(
                            f"Downloading ({pct}%)...", 
                            msg
                        )

                    if self._active_preview_stop_event.is_set():
                        raise asyncio.CancelledError()

                    if asyncio.iscoroutinefunction(_do_download):
                        await _do_download(url, pdir, progress_callback=dl_progress, quality=1, stop_event=self._active_preview_stop_event) 
                    else:
                        await asyncio.to_thread(_do_download, url, pdir, progress_callback=dl_progress, quality=1, stop_event=self._active_preview_stop_event)
                    
                    if self._active_preview_stop_event.is_set():
                        raise asyncio.CancelledError()
                        
                    audio_file = await self._find_audio(pdir)

                if audio_file:
                    meta = {
                        "path":         audio_file,
                        "track_title":  f"(Preview) {title}",
                        "artist_name":  artist,
                        "album_title":  "Streamrip Search",
                        "image_url":    data.get("image_url", data.get("image", "")),
                    }
                    def _play_success_downloaded():
                        data["preview_state"] = "playing"
                        icon_ctrl.content = ft.Icon(ft.Icons.STOP_CIRCLE_OUTLINED, color=CYAN, size=20)
                        container_ctrl.shadow = ft.BoxShadow(blur_radius=8, color=apply_opacity(0.15, CYAN))
                        audio_engine.jarvis_controlled = False
                        audio_engine.set_queue([meta], start_index=0)
                        self.app.show_snackbar(f"Playing preview (downloaded): {title}")
                        icon_ctrl.update()
                        container_ctrl.update()
                        self.hide_preview_progress()
                        
                    self.app.safe_update(_play_success_downloaded)
                else:
                    files_found = os.listdir(pdir) if os.path.exists(pdir) else "Directory Missing"
                    logger.error("Audio not found in %s. Found instead: %s", pdir, files_found)
                    raise Exception(f"Audio not found. Content: {files_found}")

            except asyncio.CancelledError:
                logger.info("Preview task cancelled.")
                def _play_cancelled():
                    data["preview_state"] = "idle"
                    icon_ctrl.content = ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINE, color=DIM, size=20)
                    container_ctrl.shadow = ft.BoxShadow(blur_radius=0, color=ft.Colors.TRANSPARENT, spread_radius=0)
                    icon_ctrl.update()
                    container_ctrl.update()
                    self.hide_preview_progress()
                self.app.safe_update(_play_cancelled)

            except Exception as exc:
                logger.error("Preview failed: %s", exc)
                _exc = exc
                def _play_fail():
                    data["preview_state"] = "idle"
                    icon_ctrl.content = ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINE, color=DIM, size=20)
                    container_ctrl.shadow = ft.BoxShadow(blur_radius=0, color=ft.Colors.TRANSPARENT, spread_radius=0)
                    self.app.show_snackbar("Preview failed.")
                    self.app._show_error(_exc)
                    icon_ctrl.update()
                    container_ctrl.update()
                    self.hide_preview_progress()
                self.app.safe_update(_play_fail)

        self._active_preview_task = asyncio.create_task(_worker())

    async def _find_audio(self, directory: str, retries: int = 5) -> str | None:
        """Scan directory for audio files, with retries to handle filesystem sync latency."""
        def _scan():
            found = []
            for root, _, files in os.walk(directory):
                for f in files:
                    full_path = os.path.join(root, f)
                    if f.lower().endswith((".mp3", ".m4a", ".flac", ".wav")):
                        return full_path
                    if f.lower().endswith((".tmp", ".part", ".download")):
                        found.append(full_path)
            
            if found:
                logger.warning("Found incomplete downloads in %s: %s", directory, found)
            return None
        
        for i in range(retries):
            res = await asyncio.to_thread(_scan)
            if res: return res
            if i < retries - 1:
                await asyncio.sleep(0.5)
        return None

    def show_progress_card(self):
        if self._hide_card_task:
            self._hide_card_task.cancel()
            self._hide_card_task = None
        def _mutate():
            self._progress_status.value    = "Connecting…"
            self._progress_pct.value       = ""
            self._progress_detail.value    = ""
            self._progress_bar.value       = None
            self._progress_spinner.visible = True
            self._progress_card.visible    = True
            self._progress_card.opacity    = 1
            self._progress_card.offset     = ft.Offset(0, 0)
        self.app.safe_update(_mutate)

    def hide_progress_card(self):
        if self._hide_card_task:
            self._hide_card_task.cancel()
        def _mutate():
            self._progress_spinner.visible = False
            self._progress_card.opacity    = 0
            self._progress_card.offset     = ft.Offset(0, 0.4)
        self.app.safe_update(_mutate)
        async def _delayed_hide():
            try:
                await asyncio.sleep(0.3)
                self._hide_card_done()
            except asyncio.CancelledError:
                pass
        self._hide_card_task = asyncio.create_task(_delayed_hide())

    def _hide_card_done(self):
        def _mutate():
            self._progress_card.visible = False
            self._progress_bar.value    = 0
        self.app.safe_update(_mutate)
        self._hide_card_task = None

    def show_search_progress(self, status: str, detail: str = ""):
        if self._hide_search_card_task:
            self._hide_search_card_task.cancel()
            self._hide_search_card_task = None
        def _mutate():
            self._search_progress_status.value = status
            self._search_progress_detail.value = detail
            self._search_progress_card.visible = True
            self._search_progress_card.opacity = 1
            self._search_progress_card.offset = ft.Offset(0, 0)
            self._connection_signal.visible = True
            self._connection_signal.set_level(1, connected=False)
        self.app.safe_update(_mutate)

    def update_search_progress(self, status: str, detail: str = ""):
        if self._hide_search_card_task:
            self._hide_search_card_task.cancel()
            self._hide_search_card_task = None
            def _mutate_show():
                self._search_progress_card.visible = True
                self._search_progress_card.opacity = 1
                self._search_progress_card.offset = ft.Offset(0, 0)
                self._connection_signal.visible = True
            self.app.safe_update(_mutate_show)
        
        # Map statuses/details to cellular signal strength (1 to 4)
        level = 1
        if "DNS" in status:
            level = 1
        elif "TCP" in status:
            level = 2
        elif "TLS" in status or "SSL" in status or "Handshake" in status:
            level = 3
        elif "HTTP" in status or "Request" in status or "Data" in status or "Streaming" in status:
            level = 4

        def _mutate():
            self._search_progress_status.value = status
            self._search_progress_detail.value = detail
            self._connection_signal.set_level(level, connected=False)
        self.app.safe_update(_mutate)

    def hide_search_progress(self, success: bool = True):
        if self._hide_search_card_task:
            self._hide_search_card_task.cancel()
            self._hide_search_card_task = None
        def _mutate_signal():
            if success:
                self._connection_signal.visible = True
                self._connection_signal.set_level(4, connected=True)
            else:
                self._connection_signal.visible = False
        self.app.safe_update(_mutate_signal)

        def _mutate_card():
            self._search_progress_card.opacity = 0
            self._search_progress_card.offset = ft.Offset(0, 0.4)
        self.app.safe_update(_mutate_card)

        async def _delayed_hide_card():
            try:
                await asyncio.sleep(0.3)
                def _mutate_done():
                    self._search_progress_card.visible = False
                self.app.safe_update(_mutate_done)
            except asyncio.CancelledError:
                pass
        self._hide_search_card_task = asyncio.create_task(_delayed_hide_card())

    def _cancel_preview_click(self, e):
        if hasattr(self, "_active_preview_stop_event") and self._active_preview_stop_event:
            self._active_preview_stop_event.set()
        if self._active_preview_task and not self._active_preview_task.done():
            self._active_preview_task.cancel()
        audio_engine.stop()
        if self._active_preview_data:
            self._active_preview_data["preview_state"] = "idle"
            self._active_preview_data = None
        self.hide_preview_progress()
        self.refresh_results_only()

    def show_preview_progress(self, status: str, detail: str = ""):
        if self._hide_preview_card_task:
            self._hide_preview_card_task.cancel()
            self._hide_preview_card_task = None
        def _mutate():
            self._preview_progress_status.value = status
            self._preview_progress_detail.value = detail
            self._preview_progress_card.visible = True
            self._preview_progress_card.opacity = 1
            self._preview_progress_card.offset = ft.Offset(0, 0)
        self.app.safe_update(_mutate)

    def update_preview_progress(self, status: str, detail: str = ""):
        if self._hide_preview_card_task:
            self._hide_preview_card_task.cancel()
            self._hide_preview_card_task = None
            def _mutate_show():
                self._preview_progress_card.visible = True
                self._preview_progress_card.opacity = 1
                self._preview_progress_card.offset = ft.Offset(0, 0)
            self.app.safe_update(_mutate_show)
        def _mutate():
            self._preview_progress_status.value = status
            self._preview_progress_detail.value = detail
        self.app.safe_update(_mutate)

    def hide_preview_progress(self):
        if self._hide_preview_card_task:
            self._hide_preview_card_task.cancel()
        def _mutate():
            self._preview_progress_card.opacity = 0
            self._preview_progress_card.offset = ft.Offset(0, 0.4)
        self.app.safe_update(_mutate)
        async def _delayed_hide():
            try:
                await asyncio.sleep(0.3)
                self._hide_preview_card_done()
            except asyncio.CancelledError:
                pass
        self._hide_preview_card_task = asyncio.create_task(_delayed_hide())

    def _hide_preview_card_done(self):
        def _mutate():
            self._preview_progress_card.visible = False
        self.app.safe_update(_mutate)
        self._hide_preview_card_task = None


    def update_progress(self, status: str, pct: float | None, detail: str = ""):
        if self._hide_card_task:
            self._hide_card_task.cancel()
            self._hide_card_task = None
            def _mutate_show():
                self._progress_card.visible = True
                self._progress_card.opacity = 1
                self._progress_card.offset  = ft.Offset(0, 0)
            self.app.safe_update(_mutate_show)

        self._progress_status.value = status
        self._progress_status.color = CYAN if status not in ("Finished", "Error") else TEXT
        
        is_indeterminate = pct is None or pct < 0
        if pct is not None and pct >= 0:
            self._progress_pct.value   = f"{int(pct)}%"
            self._progress_bar.value   = pct / 100
        else:
            self._progress_pct.value   = ""
            self._progress_bar.value   = None
            
        self._progress_spinner.visible = is_indeterminate and status not in ("Finished", "Error", "Failed", "Cancelled")
        
        if detail:
            self._progress_detail.value = detail
            
        if self.app.queue.current_job:
            meta = self.app.queue.current_job.get("metadata", {})
            name   = meta.get("name", "")
            artist = meta.get("artist", "")
            self._progress_meta.value = f"{name}{'  •  ' + artist if artist else ''}"
        
        self.app.page.update()

    def refresh_queue_ui(self, queue: list[dict]):
        def _mutate():
            if queue:
                next_item = queue[0]
                meta = next_item.get("metadata", {})
                title = meta.get("name", "Unknown")
                artist = meta.get("artist", "Unknown Artist")
                self._progress_up_next.value = f"Up Next: {title} — {artist}"
                self._progress_up_next.visible = True
            else:
                self._progress_up_next.value = ""
                self._progress_up_next.visible = False
        self.app.safe_update(_mutate)

    def refresh_now_playing(self):
        """Update shadows on all visible cards to reflect the currently playing track."""
        self.refresh_results_only()

    def _remove_history_item(self, item: dict):
        pass
