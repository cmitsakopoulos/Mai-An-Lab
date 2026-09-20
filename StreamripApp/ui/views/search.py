import os
import re
import sys
import shutil
import math
import logging
import asyncio
import flet as ft
from ui.tokens import (
    BG, SURFACE, SURFACE2, SURFACE_ELEVATED, CYAN, AMBER, TEXT, DIM, TEXT_TERTIARY, BORDER, BORDER_SUBTLE, 
    RADIUS_CARD, RADIUS_PILL, RADIUS_THUMB,
    SOURCE_COLORS, LIB_ARTIST_COLOR, LIB_ALBUM_COLOR, LIB_TRACK_COLOR, 
    apply_opacity
)
from ui.widgets import AnimatedEntry, SkeletonRow, src_color, strip_markup, build_page_ghost_top, build_page_ghost_bottom, CupertinoSegmentedBar

if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

from utils.filepath_utils import get_app_dir

logger = logging.getLogger(__name__)

# ── Source-agnostic status vocabulary ────────────────────────────────────────
# Single home for every user-facing progress string in the search/download path.
# These must never interpolate a backend name: a source is identified visually
# (source pill, per-card badge, SOURCE_COLORS) rather than in prose, so adding a
# backend costs zero strings and no message can go stale against its source.
# Errors are the deliberate exception — there, naming the service is what makes
# the message actionable, because the remedy differs per source.
PROGRESS_SEARCHING  = "Searching\u2026"
PROGRESS_CONTACTING = "Contacting API\u2026"
PROGRESS_CONNECTING = "Connecting\u2026"


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
        prefs = getattr(self.app, "_prefs", None) or {}
        saved_source = prefs.get("search_source")
        if saved_source in ("qobuz", "deezer"):
            self.selected_source = saved_source
        else:
            self.selected_source = "qobuz"
        # Replaces the old static "Qobuz" subtitle. Deliberately narrower and
        # shorter than the artists/albums/tracks bar further down the page, and
        # colour-coded per source, so two stacked capsules never read as one
        # control: this one says WHERE we search, that one says WHAT we show.
        self._source_bar = CupertinoSegmentedBar(
            segments=[
                ("qobuz", "Qobuz", None, src_color("qobuz")),
                ("deezer", "Deezer", None, src_color("deezer")),
            ],
            selected_key=self.selected_source,
            on_change=lambda k: self._set_source(k),
            height=36,
            width=184,
        )
        # Unified pre-fetch cache: all three types are fetched in one search
        # call, keyed by media_type singular, and then keyed AGAIN by source.
        # Switching Qobuz<->Deezer used to discard the previous source's results
        # outright, so toggling the pill back and forth on one query refetched
        # every time. Each source keeps its own bucket plus the query it was
        # fetched for, so a return trip is instant and a stale cache is
        # recognisable rather than silently served.
        self._source_cache: dict[str, dict[str, list[dict]]] = {}
        self._cache_query: dict[str, str] = {}
        self._active_preview_data: dict | None = None
        self._active_preview_task: asyncio.Task | None = None
        self._active_preview_stop_event: asyncio.Event | None = None
        self.expanded_nodes: set[str] = set() # Track IDs/Artist IDs of expanded items
        self.node_cache: dict[str, list[dict]] = {} # Cache for expanded node children
        self.view_mode = "tracks" # artist, album, track (plural, matches tab labels)

        # ── multi-select ───────────────────────────────────────────────────
        # Selection is keyed "source:media_type:id" and the RECORD is retained
        # alongside it, not just the key. Paging and the artists/albums/tracks
        # toggle both rebuild the visible rows, so a selection that only held
        # row references would silently shrink the moment the user turned a page
        # — the batch would quietly download less than the count promised.
        self.selection_mode = False
        self.selected_keys: set[str] = set()
        self._selected_records: dict[str, dict] = {}
        self._hide_search_card_task: asyncio.Task | None = None
        self._hide_preview_card_task: asyncio.Task | None = None


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
        self._saved_scroll = 0.0

        # results list
        self._results_list = ft.ListView(
            expand=True,
            spacing=8,
            padding=ft.Padding.only(left=12, right=12, top=4, bottom=20),
            on_scroll=self._on_list_scroll,
            # Android shows no scrollbar unless `scroll` is set (mobile
            # ScrollBehavior adds none); the page ScrollbarTheme styles it.
            scroll=ft.ScrollMode.ALWAYS,
            # Flet's own docs: scroll_to "is ineffective for controls that
            # build items dynamically", and build_controls_on_demand defaults
            # to True. Every scroll_to in this view was therefore calling a
            # documented no-op and only appeared to work when the target
            # happened to be inside the already-built window — which is what
            # made page-change scrolling unreliable. Affordable here precisely
            # because results are paginated at items_per_page, not 250 deep.
            build_controls_on_demand=False,
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
                    ft.Text("Please enter your Qobuz or Deezer credentials in Settings to enable search.", 
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
            bgcolor=BG,
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

        # error state — shown INSTEAD of the empty "No results" label when the
        # search itself failed to reach Qobuz (connection down, bad App ID /
        # Secret, rejected credentials). Gives the user the specific reason plus
        # a way to act on it, rather than silently pretending nothing matched.
        self._error_detail = ft.Text(
            "", color=DIM, size=12, text_align=ft.TextAlign.CENTER,
            selectable=True, max_lines=4, overflow=ft.TextOverflow.ELLIPSIS,
        )
        self._error_label = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.CLOUD_OFF_ROUNDED, color="#FF6B6B", size=48),
                    ft.Text("Search couldn't connect", size=18, weight=ft.FontWeight.BOLD, color=TEXT),
                    self._error_detail,
                    ft.Container(height=12),
                    ft.Row([
                        ft.Button(
                            "Check Settings",
                            icon=ft.Icons.SETTINGS_ROUNDED,
                            on_click=lambda _: self.app._switch_tab(3),
                            style=ft.ButtonStyle(color=BG, bgcolor=CYAN),
                        ),
                        ft.TextButton(
                            "Retry",
                            icon=ft.Icons.REFRESH_ROUNDED,
                            on_click=lambda _: self.page.run_task(self.start_search),
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


        # Search connection progress card
        # Progress prose is deliberately source-agnostic. Naming the backend in
        # a status string means every new source needs a new string (and every
        # missed one lies to the user — "Connecting to Qobuz API" fired for
        # Deezer jobs). WHICH source is in play is communicated as data instead:
        # the source pill above, and the per-card source badge.
        self._search_progress_status = ft.Text(PROGRESS_SEARCHING, color=TEXT, size=13, weight=ft.FontWeight.W_700)
        self._search_progress_detail = ft.Text(PROGRESS_CONTACTING, color=DIM, size=11, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
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
            border=ft.Border.all(1, apply_opacity(0.35, CYAN)),
            border_radius=12,
            padding=12,
            margin=ft.Margin.only(left=12, right=12, bottom=8),
            shadow=ft.BoxShadow(blur_radius=16, spread_radius=-4, color="#66000000"),
            visible=False,
            # Slides down from above the list rather than up from below it:
            # these now overlay the results instead of displacing them.
            offset=ft.Offset(0, -0.4),
            animate_offset=ft.Animation(220, ft.AnimationCurve.EASE_OUT_CUBIC),
            opacity=0,
            animate_opacity=ft.Animation(180, ft.AnimationCurve.EASE_OUT),
        )

        # Preview progress card
        self._preview_progress_status = ft.Text("Loading Preview...", color=TEXT, size=13, weight=ft.FontWeight.W_700)
        self._preview_progress_detail = ft.Text(PROGRESS_CONNECTING, color=DIM, size=11, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
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
            border=ft.Border.all(1, apply_opacity(0.35, CYAN)),
            border_radius=12,
            padding=12,
            margin=ft.Margin.only(left=12, right=12, bottom=8),
            shadow=ft.BoxShadow(blur_radius=16, spread_radius=-4, color="#66000000"),
            visible=False,
            # Slides down from above the list rather than up from below it:
            # these now overlay the results instead of displacing them.
            offset=ft.Offset(0, -0.4),
            animate_offset=ft.Animation(220, ft.AnimationCurve.EASE_OUT_CUBIC),
            opacity=0,
            animate_opacity=ft.Animation(180, ft.AnimationCurve.EASE_OUT),
        )

        self._search_indicator = ft.ProgressRing(width=16, height=16, stroke_width=2, color=CYAN, visible=False)

        # The search bar that is ACTUALLY mounted in _root. It used to be built
        # inline and anonymously, while _on_search_focus/_blur styled a second,
        # detached container that was never added to the tree — so the focus ring
        # never rendered. One named control, one owner.
        self._search_shell = ft.Container(
            content=ft.Row([
                self._search_field,
                self._search_indicator,
                self._clear_btn,
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=SURFACE2,
            border_radius=16,
            padding=ft.Padding.only(left=14, right=8),
            border=ft.Border.all(1.5, BORDER),
            animate=ft.Animation(140, ft.AnimationCurve.EASE_OUT),
        )
        self._view_tabs_row = ft.Row(spacing=8)
        self._update_view_tabs()

        # Selection action bar. Occupies the same slot as the type tabs and
        # cross-fades with them, so entering selection mode never reflows the
        # header or shifts the results list under the user's finger.
        self._selection_count = ft.Text(
            "", color=TEXT, size=13, weight=ft.FontWeight.W_700,
        )
        self._select_all_btn = ft.TextButton(
            content=ft.Text("Select all", color=CYAN, size=12, weight=ft.FontWeight.W_600),
            on_click=lambda e: self._select_all_on_page(),
        )
        self._selection_download_btn = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.DOWNLOAD_ROUNDED, color=BG, size=16),
                    ft.Text("Download", color=BG, size=12, weight=ft.FontWeight.W_700),
                ],
                spacing=5, tight=True,
            ),
            bgcolor=CYAN,
            border_radius=RADIUS_PILL,
            padding=ft.Padding.symmetric(horizontal=12, vertical=7),
            on_click=lambda e: self._download_selection(),
        )
        self._selection_bar = ft.Container(
            content=ft.Row(
                [
                    ft.IconButton(
                        icon=ft.Icons.CLOSE_ROUNDED, icon_color=DIM, icon_size=18,
                        tooltip="Exit selection",
                        on_click=lambda e: self.exit_selection(),
                    ),
                    self._selection_count,
                    ft.Container(expand=True),
                    self._select_all_btn,
                    self._selection_download_btn,
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=SURFACE,
            border=ft.Border.all(1, apply_opacity(0.35, CYAN)),
            border_radius=RADIUS_PILL,
            padding=ft.Padding.only(left=4, right=6, top=2, bottom=2),
            height=40,
        )
        self._header_slot = ft.AnimatedSwitcher(
            content=self._view_tabs_row,
            duration=160,
            reverse_duration=120,
            transition=ft.AnimatedSwitcherTransition.FADE,
            switch_in_curve=ft.AnimationCurve.EASE_OUT,
            switch_out_curve=ft.AnimationCurve.EASE_IN,
        )

        # ── Landing Page Container ──
        self._landing_container = ft.ListView(
            expand=True,
            spacing=0,
            padding=ft.Padding.only(left=16, right=16, top=10, bottom=100),
            visible=True,
            # Android shows no scrollbar unless `scroll` is set (mobile
            # ScrollBehavior adds none); the page ScrollbarTheme styles it.
            scroll=ft.ScrollMode.ALWAYS,
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
                                                    self._source_bar,
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
                            self._search_shell,
                            self._header_slot,
                        ],
                        spacing=14,
                    ),
                    padding=ft.Padding.only(left=16, right=16, top=20, bottom=8),
                ),

                # Main Results Area. The status cards live INSIDE this stack,
                # pinned to its top edge: as column children they occupied
                # layout space, so the whole result list jumped down the moment
                # a search or preview started and jumped back when it ended.
                ft.Stack(
                    [
                        self._animated_results_wrapper,
                        self._empty_label,
                        self._error_label,
                        self._setup_prompt,
                        self._landing_container,
                        ft.Container(
                            content=ft.Column(
                                [
                                    self._search_progress_card,
                                    self._preview_progress_card,
                                ],
                                spacing=0,
                                tight=True,
                            ),
                            top=0, left=0, right=0,
                        ),
                    ],
                    expand=True,
                ),
                
                self._pagination_bar,
            ],
            expand=True,
            spacing=0,
        )


    @property
    def cached_results(self) -> dict[str, list[dict]]:
        """The selected source's buckets. Every existing call site reads and
        writes this name; the per-source indirection lives here alone."""
        return self._source_cache.setdefault(
            self.selected_source, {"track": [], "album": [], "artist": []}
        )

    @cached_results.setter
    def cached_results(self, value: dict[str, list[dict]]):
        self._source_cache[self.selected_source] = value

    def _cache_is_fresh(self, source: str, query: str) -> bool:
        """True when `source` already holds results for exactly this query."""
        if self._cache_query.get(source) != query:
            return False
        return any(self._source_cache.get(source, {}).values())

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
                card = self._result_card(start_idx + i, r, depth=0, stagger_index=i)
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
                # offset=-1 is Flet's documented "jump to the end"; the old
                # literal 3250 was an overshoot that relied on clamping and on
                # every row being exactly 64px tall.
                target_offset = -1 if scroll_to_bottom else (45 if self.current_page > 0 else 0)

                await self._results_list.scroll_to(offset=target_offset, duration=0)
                self._last_scroll_pixels = max(0, target_offset)
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


    # ── tab lifecycle ───────────────────────────────────────────────────────
    # _switch_tab reassigns _tab_content.content, which remounts this whole
    # subtree. The Python controls survive in _view_cache, so results, query
    # and expansion state were never actually lost — but Flutter disposes the
    # ListView and its scroll controller, so the user was silently returned to
    # the top of a 35-row page every time they glanced at Library.

    def on_hide(self):
        """Called as the Search tab is switched away from."""
        self._saved_scroll = max(0.0, float(self._last_scroll_pixels or 0.0))

    def on_show(self):
        """Called as the Search tab is switched back to."""
        target = getattr(self, "_saved_scroll", 0.0)
        if target <= 0:
            return
        self.page.run_task(self._restore_scroll, target)

    async def _restore_scroll(self, target: float):
        # One frame for the remounted ListView to lay out before it can be
        # positioned; without this the scroll lands on a zero-height viewport.
        await asyncio.sleep(0.05)
        self._is_programmatic_scroll = True
        try:
            await self._results_list.scroll_to(offset=target, duration=0)
            self._last_scroll_pixels = target
        except Exception:
            # A restore is a convenience; never let it break tab switching.
            logger.debug("scroll restore failed", exc_info=True)
        finally:
            await asyncio.sleep(0.03)
            self._is_programmatic_scroll = False

    # ── public build ────────────────────────────────────────────────────────
    def build(self) -> ft.Control:
        self.refresh_setup_state(update=False)
        return self._root

    def refresh_setup_state(self, update=True):
        from utils.streamrip_api import load_config
        cfg = load_config()
        # The lock screen used to read ONLY the Qobuz section, so a user who had
        # configured Deezer and nothing else was held behind "Setup Required"
        # forever with their results cleared. The gate is whether ANY source is
        # usable; which one is selected is _credentials_missing's job, and that
        # is checked per-search with an actionable, source-named message.
        configured = self._configured_sources(cfg)
        has_creds = bool(configured)
        if configured and self.selected_source not in configured:
            # Don't strand the user on a source they can't search when another
            # one is ready. Assignment only — no re-query, no cache invalidation,
            # since there is nothing cached for an unconfigured source anyway.
            self.selected_source = configured[0]
            if hasattr(self.app, "_save_pref"):
                self.app._save_pref("search_source", self.selected_source)
            try:
                self._source_bar.set_selected(self.selected_source)
            except Exception:
                pass
        
        landing_cfg = cfg.get("landing", {})
        show_most_listened = bool(landing_cfg.get("show_search_history", True))
        show_stats   = bool(landing_cfg.get("show_library_stats", True))

        def _apply():
            search_field = getattr(self, "_search_field", None)
            has_query = bool((getattr(search_field, "value", None) or "").strip())
            results_list = getattr(self, "_results_list", None)
            controls = getattr(results_list, "controls", None) if results_list else None
            source_cache = getattr(self, "_source_cache", None) or {}
            cached = any(source_cache.get(getattr(self, "selected_source", None), {}).values())
            has_results = bool(controls) or cached
            if has_query or has_results:
                self._setup_prompt.visible = False
            else:
                self._setup_prompt.visible = not has_creds

            if not has_creds:
                if not has_query and not has_results:
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
                    ft.Text(title, color=TEXT, size=14, weight=ft.FontWeight.W_600),
                ], spacing=8),
                ft.Divider(color=BORDER_SUBTLE, height=16),
                content
            ], spacing=0),
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER_SUBTLE),
            border_radius=RADIUS_CARD,
            padding=16,
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
            # Hide landing page and setup prompt when we have content
            self._landing_container.visible = not has_val
            if has_val:
                self._setup_prompt.visible = False
            if not has_val:
                self._connection_signal.visible = False
        self.app.safe_update(_mutate)

    def _on_search_focus(self, _e):
        def _mutate():
            self._search_shell.border = ft.Border.all(1.5, apply_opacity(0.6, CYAN))
            self._search_shell.bgcolor = SURFACE
        self.app.safe_update(_mutate)

    def _on_search_blur(self, _e):
        def _mutate():
            self._search_shell.border = ft.Border.all(1.5, BORDER)
            self._search_shell.bgcolor = SURFACE2
        self.app.safe_update(_mutate)

    def _show_recent_searches(self, e):
        # Presented through the Flet 0.86 dialog stack (show_dialog)
        # rather than the legacy page.overlay + `.open = True` path. The legacy
        # path never installs the dismiss lifecycle, so a scrim tap closed the
        # sheet in Flutter while leaving `.open` True on the Python side — and
        # the next open, setting an already-true flag, was a silent no-op.
        from utils.search_history import load_searches
        searches = load_searches()
        if not searches: return

        # Built fresh per invocation rather than held as a long-lived field, so
        # the control is never re-parented into a second sheet. `scroll` is
        # mandatory here: Android's mobile ScrollBehavior renders no scrollbar
        # without it, which is what made the list look uncollapsibly clipped.
        self._history_list = ft.ListView(
            spacing=2,
            height=300,
            padding=ft.Padding.symmetric(horizontal=12),
            scroll=ft.ScrollMode.ALWAYS,
            controls=[
                ft.ListTile(
                    leading=ft.Icon(ft.Icons.HISTORY, color=DIM, size=18),
                    title=ft.Text(s, color=TEXT, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    on_click=lambda _, q=s: self._recent_clicked(q),
                ) for s in searches
            ],
        )

        self._history_sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text("Recent Searches", color=TEXT, size=15, weight=ft.FontWeight.W_700),
                        ft.Divider(color=BORDER),
                        self._history_list,
                    ],
                    tight=True,
                    spacing=0,
                ),
                padding=16,
                bgcolor=SURFACE,
            ),
            bgcolor=SURFACE,
        )
        self.page.show_dialog(self._history_sheet)

    def _recent_clicked(self, query):
        # Name the sheet: pop_dialog() would close whichever dialog is topmost,
        # and a toast from background work counts as one.
        self.app.dismiss_dialog(self._history_sheet)
        self._do_recent(query)

    def _do_recent(self, query):
        self._search_field.value = query
        asyncio.create_task(self.start_search())

    # ── search logic ────────────────────────────────────────────────────────
    def _clear_search(self, _e=None):
        # Bump current_search_id so any in-flight searcher.search callback
        # fails its id-equality guard in _on_results and gets dropped.
        self.current_search_id += 1
        self.exit_selection()
        self.hide_search_progress(success=False)

        def _mutate():
            self._search_field.value = ""
            self._clear_btn.visible  = False
            self._search_indicator.visible = False
            self._results_list.controls.clear()
            self._empty_label.visible = False
            self._error_label.visible = False
            self._source_cache.clear()
            self._cache_query.clear()
            self.expanded_nodes.clear()
            self.node_cache.clear()
            self.current_page = 0
            self.total_pages = 1
            self._pagination_bar.visible = False
            self._landing_container.visible = True
        self.app.safe_update(_mutate)
        self.refresh_setup_state()

    def _configured_sources(self, cfg: dict | None = None) -> list[str]:
        """Sources with usable credentials, in pill order.

        Single table so a new backend is one entry here rather than a new branch
        in every gate. `_credentials_missing` reads the same table.
        """
        if cfg is None:
            from utils.streamrip_api import load_config
            cfg = load_config()
        q = cfg.get("qobuz", {}) or {}
        d = cfg.get("deezer", {}) or {}
        configured = []
        if q.get("email_or_userid") and q.get("password_or_token"):
            configured.append("qobuz")
        if d.get("arl"):
            configured.append("deezer")
        return configured

    def _source_label(self) -> str:
        return (self.selected_source or "qobuz").title()

    # Per-source remedy text. Errors DO name the backend — unlike progress
    # prose — because the fix differs per source and an unnamed error is not
    # actionable. One table, so a new source is one entry, not a new branch.
    _MISSING_CREDS_HINT = {
        "qobuz":  "Qobuz credentials not set. Add your User ID and token in Settings.",
        "deezer": "Deezer ARL cookie not set. Add it in Settings.",
    }

    def _credentials_missing(self, source: str) -> str | None:
        """Human-readable reason this source can't be searched yet, or None."""
        if source in self._configured_sources():
            return None
        return self._MISSING_CREDS_HINT.get(source, f"{source.title()} is not configured.")

    def _set_source(self, source: str):
        """Switch the backend the search bar queries.

        Results, caches and expansion state are all keyed to the previous
        source's ids, so they are dropped rather than re-labelled. If a query is
        already in the box we re-run it immediately, which makes the pill read as
        "search this again over there" instead of merely a filter.
        """
        source = (source or "qobuz").lower()
        if source == self.selected_source:
            return
        self.selected_source = source
        if hasattr(self.app, "_save_pref"):
            self.app._save_pref("search_source", source)

        # Invalidate everything tied to the old source's ids — the selection
        # keys are source-scoped, so they cannot survive the switch either.
        self.exit_selection()
        self.current_search_id += 1
        self.expanded_nodes.clear()
        self.node_cache.clear()
        self._active_preview_data = None

        query = (self._search_field.value or "").strip()
        if query and self._cache_is_fresh(source, query):
            # Already fetched for this exact query: render from cache rather
            # than making the user wait through an identical round trip.
            self.current_page = 0
            self._landing_container.visible = False
            self._rebuild_results()
        elif query:
            self.page.run_task(self.start_search)
        else:
            def _reset():
                self._results_list.controls = []
                self._empty_label.visible = False
                self._error_label.visible = False
                self._landing_container.visible = True
            self.app.safe_update(_reset)

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
        self.exit_selection()
        self._active_preview_data = None
        self.expanded_nodes.clear()
        self.node_cache.clear()
        self._setup_prompt.visible = False
        self._empty_label.visible = False
        self._error_label.visible = False
        self._landing_container.visible = False

        # Proactive check for credentials (per selected source)
        missing = self._credentials_missing(self.selected_source)
        if missing:
            self.app.show_snackbar(missing, icon=ft.Icons.LOCK_OUTLINE_ROUNDED, color=src_color(self.selected_source))
            self.app._switch_tab(3)
            return

        self._search_indicator.visible = True
        self._clear_btn.visible = True

        # Show connection stages progress card
        self.show_search_progress(PROGRESS_SEARCHING, PROGRESS_CONNECTING)

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
        async def _update_ui():
            self._search_indicator.visible = False
            
            if results is None:
                self._show_search_error(
                    f"Couldn't reach {self._source_label()} — check your internet connection and try again."
                )
                self.page.update()
                return

            if isinstance(results, dict) and "error" in results:
                self._show_search_error(results.get("error", "Unknown error"))
                self.page.update()
                return

            if not results:
                self._error_label.visible = False
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

            # Full search: route every result into its typed bucket, and stamp
            # the cache with the query it answers so a later source switch can
            # tell a usable cache from a stale one.
            self._cache_query[self.selected_source] = (self._search_field.value or "").strip()
            self.cached_results = {"track": [], "album": [], "artist": []}
            for r in results:
                m_type = r.get("media_type", "track")
                if m_type in self.cached_results:
                    self.cached_results[m_type].append(r)
            
            self.current_page = 0
            self._rebuild_results()

        self.page.run_task(_update_ui)

    def _show_search_error(self, message: str):
        """Surface a search connection/auth failure inline in the results area
        instead of the misleading empty state or the full-screen crash boundary.
        Caller is responsible for the surrounding page.update()."""
        self._setup_prompt.visible = False
        self._error_detail.value = message or "An unknown error occurred while searching."
        self._error_label.visible = True
        self._empty_label.visible = False
        self._landing_container.visible = False
        self._results_list.controls.clear()
        self._pagination_bar.visible = False

    def _rebuild_results(self):
        active_type = self.view_mode[:-1]  # "tracks" -> "track"
        source = self.cached_results.get(active_type, [])

        self._results_list.controls.clear()

        self.total_pages = math.ceil(len(source) / self.items_per_page)
        
        # Sliced items for current page
        start_idx = self.current_page * self.items_per_page
        end_idx = start_idx + self.items_per_page
        page_items = source[start_idx:end_idx]

        self._setup_prompt.visible = False
        self._error_label.visible = False
        self._empty_label.visible = not source

        first_chunk = []
        if self.current_page > 0:
            first_chunk.append(self._build_top_ghost())

        for i, r in enumerate(page_items):
            card = self._result_card(start_idx + i, r, depth=0, stagger_index=i)
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
        from utils.streamrip_api import load_config
        try:
            cfg = load_config()
            appearance = cfg.get("appearance", {})
        except Exception:
            appearance = {}
        pill_style = str(appearance.get("pill_style", "category")).lower()
        icons = {
            "artists": ft.Icons.PERSON_ROUNDED,
            "albums": ft.Icons.ALBUM_ROUNDED,
            "tracks": ft.Icons.MUSIC_NOTE_ROUNDED,
        }
        accents = {
            "artists": CYAN if pill_style == "unified" else LIB_ARTIST_COLOR,
            "albums":  CYAN if pill_style == "unified" else LIB_ALBUM_COLOR,
            "tracks":  CYAN if pill_style == "unified" else LIB_TRACK_COLOR,
        }
        segments = [
            (mode, mode.capitalize(), icons[mode], accents[mode])
            for mode in ["artists", "albums", "tracks"]
        ]
        if (
            self._view_tabs_row.controls
            and isinstance(self._view_tabs_row.controls[0], CupertinoSegmentedBar)
            and [s[0] for s in self._view_tabs_row.controls[0].segments] == [s[0] for s in segments]
            and [s[3] for s in self._view_tabs_row.controls[0].segments] == [s[3] for s in segments]
        ):
            self._view_tabs_row.controls[0].set_selected(self.view_mode)
            return

        self._view_tabs_row.controls = [
            CupertinoSegmentedBar(
                segments=segments,
                selected_key=self.view_mode,
                on_change=lambda m: self._set_view_mode(m),
                height=40,
                expand=True,
            )
        ]
        self.try_update(self._view_tabs_row)

    # ── multi-select ────────────────────────────────────────────────────────
    # Downloading ten tracks used to cost twenty taps and ten identical quality
    # decisions: every row had its own download button, and each one reopened
    # the quality sheet. Long-press (the idiom LibraryView already uses) puts
    # the list into selection mode instead.

    SELECTABLE_TYPES = ("track", "album")

    def _result_key(self, r: dict) -> str:
        """Stable identity for a result across pages, tabs and sources."""
        return f"{r.get('source') or self.selected_source}:{r.get('media_type', 'track')}:{r.get('id')}"

    def _is_selectable(self, r: dict) -> bool:
        return r.get("media_type") in self.SELECTABLE_TYPES

    def _is_selected(self, r: dict) -> bool:
        return self._result_key(r) in self.selected_keys

    def enter_selection(self, r: dict | None = None):
        """Long-press entry point."""
        if r is not None and not self._is_selectable(r):
            # Artists are not downloadable, so they cannot seed a selection.
            return
        if not self.selection_mode:
            self.selection_mode = True
            self.app.trigger_haptic("long_press")
        if r is not None:
            self._toggle_selection(r, refresh=False)
        self._sync_selection_ui()

    def exit_selection(self):
        self.selection_mode = False
        self.selected_keys.clear()
        self._selected_records.clear()
        self._sync_selection_ui()

    def _toggle_selection(self, r: dict, refresh: bool = True):
        key = self._result_key(r)
        if key in self.selected_keys:
            self.selected_keys.discard(key)
            self._selected_records.pop(key, None)
        else:
            self.selected_keys.add(key)
            # Retain the record itself: the row it came from may be gone by the
            # time the batch is submitted.
            self._selected_records[key] = r
        if refresh:
            self._sync_selection_ui()

    def _select_all_on_page(self):
        """Select every selectable row currently rendered. Deliberately scoped
        to the page rather than all 250 cached results — a one-tap 250-item
        download queue is not something a user can undo comfortably."""
        added = 0
        for entry in self._results_list.controls:
            r = getattr(entry, "data", None)
            if not isinstance(r, dict) or not self._is_selectable(r):
                continue
            key = self._result_key(r)
            if key not in self.selected_keys:
                self.selected_keys.add(key)
                self._selected_records[key] = r
                added += 1
        if added:
            self.app.trigger_haptic("selection")
        self._sync_selection_ui()

    def _sync_selection_ui(self):
        """Repaint every selection-dependent surface from one place."""
        count = len(self.selected_keys)
        self._selection_count.value = f"{count} selected" if count else "Select items"
        self._selection_download_btn.visible = count > 0
        self._header_slot.content = (
            self._selection_bar if self.selection_mode else self._view_tabs_row
        )
        self.refresh_results_only()
        self.app.safe_update(lambda: None)

    def _download_selection(self):
        records = list(self._selected_records.values())
        if not records:
            return
        self.app.quality_selector_sheet.show_batch(
            records, on_done=lambda: self.exit_selection()
        )

    # ── row visuals: ONE source of truth ────────────────────────────────────
    # _result_card (build) and refresh_results_only (in-place mutate) used to
    # derive a row's icons independently and had drifted: build drew
    # PLAY_CIRCLE_FILLED_ROUNDED at 22 and CHEVRON_RIGHT_ROUNDED, refresh redrew
    # the same slots as PLAY_CIRCLE_OUTLINE at 20 and KEYBOARD_ARROW_RIGHT, so
    # every playback event silently restyled the list. Both paths now build
    # their visuals here, which makes that class of drift unrepresentable.

    _ROW_ACCENTS = {
        "artist": LIB_ARTIST_COLOR,
        "album":  LIB_ALBUM_COLOR,
        "track":  LIB_TRACK_COLOR,
    }

    def _row_accent(self, m_type: str) -> str:
        return self._ROW_ACCENTS.get(m_type, CYAN)

    def _is_row_playing(self, r: dict) -> bool:
        title  = strip_markup(r.get("ui_title", r.get("name", "Unknown")))
        artist = strip_markup(r.get("ui_subtitle", r.get("artist", "")))
        return (
            audio_engine.current_track in (title, f"(Preview) {title}")
            and audio_engine.current_artist == artist
        )

    TYPE_ICONS = {
        "artist": ft.Icons.PERSON_ROUNDED,
        "album":  ft.Icons.ALBUM_ROUNDED,
        "track":  ft.Icons.MUSIC_NOTE_ROUNDED,
    }

    # Deliberately NO cover art in result rows. A page of 35 rows means 35
    # remote image fetches plus 35 decodes on every page turn and every
    # re-render, which cost more on device than the thumbnails were worth.
    # The typed icon carries the same "what kind of thing is this" signal for
    # free; artwork stays where it is cheap and already cached — the library
    # list, the mini-player and the download dock.

    def _leading_visual(self, r: dict, accent: str) -> ft.Control:
        """Type icon, or a selection tick while selection mode is active."""
        m_type = r.get("media_type", "track")
        icon = self.TYPE_ICONS.get(m_type, ft.Icons.MUSIC_NOTE)

        if not self.selection_mode:
            return ft.Icon(icon, color=accent, size=18)
        if not self._is_selectable(r):
            # Artists cannot be downloaded; dim them rather than offering a
            # tick that would do nothing.
            return ft.Icon(icon, color=apply_opacity(0.3, accent), size=18)
        selected = self._is_selected(r)
        return ft.Icon(
            ft.Icons.CHECK_CIRCLE_ROUNDED if selected
            else ft.Icons.RADIO_BUTTON_UNCHECKED_ROUNDED,
            color=CYAN if selected else DIM,
            size=20,
        )

    def _source_badge(self, r: dict) -> ft.Control:
        """Per-row source identity, as colour rather than prose.

        `ui_source_color` was computed by the searcher and never rendered. This
        is what lets every progress and status string stay backend-agnostic:
        the source is always legible, so it never has to be spelled out.
        """
        source = (r.get("source") or self.selected_source or "").lower()
        tint = src_color(source)
        return ft.Container(
            content=ft.Text(source.upper(), color=tint, size=8,
                            weight=ft.FontWeight.W_700),
            bgcolor=apply_opacity(0.15, tint),
            border_radius=3,
            padding=ft.Padding.symmetric(horizontal=4, vertical=1),
        )

    def _preview_visual(self, state: str) -> ft.Control:
        """Inner control of a track row's preview button for a given state."""
        if state == "loading":
            return ft.ProgressRing(width=16, height=16, stroke_width=2, color=CYAN)
        return ft.Icon(
            ft.Icons.PAUSE_CIRCLE_FILLED_ROUNDED if state == "playing"
            else ft.Icons.PLAY_CIRCLE_FILLED_ROUNDED,
            color=CYAN if state != "idle" else DIM,
            size=22,
        )

    def _expand_visual(self, is_expanded: bool, accent: str) -> ft.Icon:
        """Disclosure chevron for an artist/album row."""
        return ft.Icon(
            ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED if is_expanded
            else ft.Icons.CHEVRON_RIGHT_ROUNDED,
            color=accent if is_expanded else DIM,
            size=18,
        )

    def _apply_row_state(self, tile: ft.ListTile, r: dict) -> None:
        """Re-derive every state-dependent visual on an already-built row."""
        m_type = r.get("media_type", "track")
        accent = self._row_accent(m_type)
        state  = r.get("preview_state", "idle")
        is_expanded = f"{m_type}_{r.get('id')}" in self.expanded_nodes

        selected = self.selection_mode and self._is_selected(r)
        highlighted = self._is_row_playing(r) or state != "idle" or is_expanded
        if selected:
            tile.bgcolor = apply_opacity(0.20, CYAN)
        else:
            tile.bgcolor = apply_opacity(0.12, accent) if highlighted else "transparent"

        # Leading slot: type icon normally, selection tick in selection mode.
        leading = tile.leading
        if isinstance(leading, ft.Row) and len(leading.controls) >= 2:
            leading.controls[1] = self._leading_visual(r, accent)

        trailing = tile.trailing
        if not isinstance(trailing, ft.Row) or not trailing.controls:
            return

        # The per-row action cluster is meaningless while a batch is being
        # assembled, and tapping it mid-selection would be a mis-tap every time.
        trailing.visible = not self.selection_mode

        if m_type == "track" and isinstance(trailing.controls[0], ft.Container):
            trailing.controls[0].content = self._preview_visual(state)

        if m_type in ("artist", "album"):
            # Swap the control rather than mutating its fields. The old code did
            # `icon.name = ...`, but Flet 0.86 names that property `icon`, and
            # assigning an unknown attribute on a control is silently accepted —
            # so the disclosure chevron never actually changed on refresh. A
            # whole-control swap cannot go stale against a property rename.
            # Skipped while _toggle_search_node has its spinner in this slot.
            if isinstance(trailing.controls[-1], ft.Icon):
                trailing.controls[-1] = self._expand_visual(is_expanded, accent)

    def _result_card(self, index: int, r: dict, depth: int = 0,
                     stagger_index: int | None = None) -> ft.Control:
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
        
        accent = self._row_accent(m_type)

        title    = strip_markup(r.get("ui_title",    r.get("name",   "Unknown")))
        subtitle = strip_markup(r.get("ui_subtitle", r.get("artist", "")))
        detail   = strip_markup(r.get("ui_detail",   ""))
        
        is_in_library = r.get("is_in_library", False)
        download_icon = ft.Icons.CHECK_CIRCLE_ROUNDED if is_in_library else ft.Icons.ARROW_CIRCLE_DOWN_ROUNDED
        download_color = CYAN if is_in_library else DIM
             
        expand_icon = (
            self._expand_visual(is_expanded, accent)
            if m_type in ("artist", "album") else None
        )
        
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

        async def row_tap(e):
            # In selection mode a tap toggles membership instead of previewing
            # or expanding, so the two modes never fight over the same gesture.
            if self.selection_mode:
                if self._is_selectable(r):
                    self._toggle_selection(r)
                    self.app.trigger_haptic("selection")
                return
            if m_type == "track":
                await preview_click(e)
            else:
                await toggle_node(e)

        preview_btn = ft.Container(
            content=self._preview_visual(r.get("preview_state", "idle")),
            width=36, height=36, alignment=ft.Alignment(0, 0),
            on_click=preview_click,
            tooltip="Preview",
            border_radius=RADIUS_PILL,
        )

        # Subtitle omits the artist when it merely repeats the title (common on
        # self-titled releases and on artist rows).
        show_artist = bool(subtitle) and subtitle.strip().lower() != title.strip().lower()
        sub_parts = [p for p in (subtitle if show_artist else "", detail) if p]

        tile = ft.ListTile(
            leading=ft.Row([
                ft.Container(width=depth * 16, visible=depth > 0),
                self._leading_visual(r, accent),
            ], tight=True),
            title=ft.Text(title, color=TEXT, size=14, weight=ft.FontWeight.W_600, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Row(
                [
                    self._source_badge(r),
                    ft.Text("  \u00b7  ".join(sub_parts), color=DIM, size=12,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                ],
                spacing=6, tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
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
            bgcolor="transparent",
            on_click=row_tap,
            on_long_press=lambda e, data=r: self.enter_selection(data),
        )

        # Build the row neutral, then let the single state-applier decide every
        # state-dependent visual, so a freshly built row and a refreshed row are
        # byte-identical by construction.
        self._apply_row_state(tile, r)

        return AnimatedEntry(tile, target_height=66, data=r, depth=depth,
                             stagger_index=stagger_index)

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
                
            self.searcher.get_artist_albums(
                str(artist_id), callback, limit=limit, offset=offset,
                source=r.get("source") or self.selected_source,
            )
        
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

        # Read the depth off the wrapper, which stores it verbatim. It used to
        # be reverse-engineered from the indent spacer as `width / 20` while
        # the spacer is built at `depth * 16` — so depth 1 decoded as 0 and
        # every level below the first was indented as if it were a sibling.
        node_depth = getattr(parent_list[node_idx], "depth", 0)

        if is_expanding:
            self.expanded_nodes.add(node_id)
            accent = self._row_accent(m_type)
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
                                tile_ctrl.trailing.controls[2] = self._expand_visual(True, accent)
                            
                            curr_idx = -1
                            for j, entry in enumerate(parent_list):
                                if getattr(entry, "content", None) == tile_ctrl:
                                    curr_idx = j; break
                            if curr_idx == -1: return

                            for k, child in enumerate(children):
                                card = self._result_card(k, child, depth=node_depth + 1)
                                parent_list.insert(curr_idx + 1 + k, card)
                            self._results_list.update()
                        except: pass
                    self.app.safe_update(_insert)
                self.page.run_task(_process)

            if node_id in self.node_cache:
                children_callback(self.node_cache[node_id])
                return

            node_source = node_data.get("source") or self.selected_source
            if m_type == "artist":
                self.searcher.get_artist_albums(str(node_data.get("id")), children_callback, source=node_source)
            elif m_type == "album":
                self.searcher.get_album_tracks(str(node_data.get("id")), children_callback, source=node_source)

        else:
            self.expanded_nodes.discard(node_id)
            tile_ctrl.bgcolor = "transparent"
            if isinstance(tile_ctrl.trailing, ft.Row):
                tile_ctrl.trailing.controls[2] = self._expand_visual(False, self._row_accent(m_type))
            
            idx = node_idx + 1
            while idx < len(parent_list):
                child_entry = parent_list[idx]
                if not isinstance(child_entry, AnimatedEntry): break
                child_depth = getattr(child_entry, "depth", 0)
                if child_depth > node_depth:
                    if child_entry.data and isinstance(child_entry.data, dict):
                        c_id = f"{child_entry.data.get('media_type')}_{child_entry.data.get('id')}"
                        self.expanded_nodes.discard(c_id)
                    parent_list.pop(idx)
                    continue
                break
            self._results_list.update()

    def refresh_results_only(self):
        """Re-evaluate every state-dependent visual on the built result rows.

        Mutates in place rather than rebuilding: this runs on every playback
        transition, and rebuilding 35 rows per track change was needless churn.
        All visuals come from _apply_row_state, the same applier _result_card
        uses, so refreshing a row can never restyle it.
        """
        for entry in self._results_list.controls:
            if not isinstance(entry, AnimatedEntry):
                continue
            r = entry.data
            tile = entry.content
            if not r or not isinstance(tile, ft.ListTile):
                continue
            self._apply_row_state(tile, r)
            try:
                tile.update()
            except Exception:
                pass

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
                        stream_url = await self.searcher.get_track_stream_url(
                            str(track_id), quality=1,
                            source=data.get("source") or self.selected_source,
                        )
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
                        audio_engine.set_queue([meta], start_index=0)
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

    def show_search_progress(self, status: str, detail: str = ""):
        if self._hide_search_card_task:
            self._hide_search_card_task.cancel()
            self._hide_search_card_task = None
        def _mutate():
            self._setup_prompt.visible = False
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
            self._search_progress_card.offset = ft.Offset(0, -0.4)
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
            self._preview_progress_card.offset = ft.Offset(0, -0.4)
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


    def refresh_now_playing(self):
        """Update shadows on all visible cards to reflect the currently playing track."""
        self.refresh_results_only()
