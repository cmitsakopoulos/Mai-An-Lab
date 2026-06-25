import os
import sys
import math
import logging
import asyncio
from collections import Counter
import flet as ft
from ui.tokens import (
    BG, SURFACE, SURFACE2, CYAN, AMBER, TEXT, DIM, BORDER, 
    SOURCE_COLORS, LIB_ARTIST_COLOR, LIB_ALBUM_COLOR, LIB_TRACK_COLOR, 
    LIB_PLAYLIST_COLOR, LIB_PARTITION_COLOR, apply_opacity
)
from ui.widgets import AnimatedEntry, AccordionCard, src_color

if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

logger = logging.getLogger(__name__)

def get_app_dir() -> str:
    """Returns the primary writable directory for the app, prioritizing 'files'."""
    for env_var in ("APP_FILES_PATH", "FILES_DIR", "INTERNAL_STORAGE", "FLET_APP_STORAGE_DATA", "HOME"):
        val = os.getenv(env_var)
        if val and os.path.isdir(val):
            return val
    import tempfile
    return tempfile.gettempdir()


class LibraryView:
    def try_update(self, *controls):
        for c in controls:
            if c is not None:
                try:
                    c.update()
                except Exception:
                    pass

    def __init__(self, app: "StreamripFletApp"):
        from utils.streamrip_api import load_config
        self.app            = app
        self.page           = app.page
        
        # Resolve default sort and initial view mode from config
        try:
            cfg = load_config()
            self.sort_mode = cfg.get("general", {}).get("library_sort", "date")
            appearance = cfg.get("appearance", {})
        except:
            self.sort_mode = "date"
            appearance = {}

        show_moods = bool(appearance.get("show_moods", False))
        show_islets = bool(appearance.get("show_islets", False))
        show_partitions = show_moods or show_islets
        show_playlists = bool(appearance.get("show_playlists", True))
        show_artists = bool(appearance.get("show_artists", True))
        show_albums = bool(appearance.get("show_albums", True))
        show_tracks = bool(appearance.get("show_tracks", True))

        if show_tracks:
            self.view_mode = "tracks"
        elif show_albums:
            self.view_mode = "albums"
        elif show_artists:
            self.view_mode = "artists"
        elif show_playlists:
            self.view_mode = "playlists"
        elif show_partitions:
            self.view_mode = "partitions"
        else:
            self.view_mode = "tracks"

        self.search_query   = ""
        self.expanded_nodes: set[str] = set()
        self._toggling_nodes: set[str] = set()
        self._current_gen   = None   # generator for lazy scroll loading
        self._load_token    = 0      # incremented each load to cancel stale workers
        self._is_loading_chunk = False
        self._is_scanning   = False
        self._path_to_controls: dict[str, list[ft.Control]] = {}
        self._scan_timer    = None
        self._search_token  = 0
        self._lib_clear_btn = None  # assigned after TextField is built

        # Cache the flat tracks list when view_mode == "tracks", so subsequent
        # play_track taps don't re-issue the full get_all_tracks() query
        # (which is the dominant cost on large libraries; a few thousand
        # rows of dict marshalling per tap). Invalidated on every
        # load_library() call, which is the only path that can change the
        # underlying ordering or filter.
        self._tracks_cache: list[dict] | None = None
        self._tracks_cache_key: tuple | None = None

        # Partition calculation caches to prevent stupid startup recomputations.
        self._cached_moods: dict[str, list[dict]] | None = None
        # islet_name -> list of member tracks. Populated lazily by computing
        # membership against each saved islet's centroid via tg.tracks_in_islet.
        self._cached_islets: dict[str, list[dict]] | None = None
        self._cached_unanalysed: list[dict] | None = None
        self._mood_feedback_map: dict[str, dict[str, int]] = {}
        self._mood_recalc_pending = False

        # ── Controls ───────────────────────────────────────────────────────
        # ── Library Search Bar (matches SearchView unified design) ───────────
        # The TextField is intentionally borderless; the outer container owns
        # the visual border so it never resizes when the user types or focuses.
        self._search_field = ft.TextField(
            hint_text="Search artists, albums, tracks…",
            hint_style=ft.TextStyle(color=DIM, size=14),
            bgcolor="transparent",
            border=ft.InputBorder.NONE,
            text_style=ft.TextStyle(color=TEXT, size=14),
            content_padding=ft.Padding.only(left=4, right=4, top=14, bottom=14),
            expand=True,
            cursor_color=CYAN,
            multiline=False,
            max_lines=1,
            on_change=self._on_search_change,
            on_focus=self._on_search_focus,
            on_blur=self._on_search_blur,
        )
        self._lib_clear_btn = ft.IconButton(
            icon=ft.Icons.CLOSE,
            icon_color=DIM,
            icon_size=16,
            visible=False,
            on_click=self._clear_search,
        )
        self._search_spinner = ft.ProgressRing(
            width=16, height=16, stroke_width=2, color=CYAN, visible=False,
        )
        self._mic_btn = None # Moved to AssistantView
        self._search_bar_container = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.SEARCH_ROUNDED, color=CYAN, size=18),
                    self._search_field,
                    self._search_spinner,
                    self._lib_clear_btn,
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=SURFACE2,
            border=ft.Border.all(1.5, BORDER),
            border_radius=14,
            padding=ft.Padding.only(left=12, right=6, top=0, bottom=0),
            expand=True,
            animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
        )

        self._stats_label = ft.Text("", color=DIM, size=11, weight=ft.FontWeight.W_700)

        self.partition_sub_mode = "moods"
        self._partition_tabs = ft.Container(
            content=ft.Row(
                spacing=6,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            visible=False,
            padding=ft.Padding.symmetric(vertical=4),
        )
        self._view_tabs_row = ft.Row(spacing=6)
        self._update_view_tabs()
        self._update_partition_tabs_ui()

        # Define the buttons as class variables first
        self._sort_icon_btn = ft.IconButton(icon=ft.Icons.SORT, icon_color=DIM, icon_size=22, on_click=self._open_sort_menu)
        self._scan_btn      = ft.IconButton(icon=ft.Icons.REFRESH, icon_color=CYAN, icon_size=22, on_click=lambda e: self.start_scan())

        self._scan_progress = ft.ProgressBar(
            value=None,
            color=CYAN,
            bgcolor=SURFACE2,
        )
        self._scan_status_lbl = ft.Text("", color=DIM, size=11)

        self._scan_progress_container = ft.Container(
            content=ft.Column(
                [
                    self._scan_progress,
                    self._scan_status_lbl,
                ],
                spacing=8,
            ),
            visible=False,
            padding=ft.Padding.symmetric(vertical=12, horizontal=16),
            border_radius=12,
            border=ft.Border.all(1, apply_opacity(0.3, CYAN)),
        )

        self._search_token = 0  # Incremented each keystroke to cancel stale queries

        # Pagination variables for tracks view mode
        self.current_page = 0
        self.items_per_page = 35
        self.total_pages = 1
        self._flat_rows = []
        self._is_changing_page = False
        self._last_scroll_pixels = 0
        self._is_programmatic_scroll = False
        self._at_bottom_boundary = False
        self._bottom_boundary_time = 0.0
        self._at_top_boundary = False
        self._top_boundary_time = 0.0

        self._library_list = ft.ListView(
            expand=True,
            spacing=6,
            padding=ft.Padding.only(left=12, right=12, top=4, bottom=20),
            on_scroll=self._on_list_scroll,
            scroll_interval=150,
        )

        self._animated_list_wrapper = ft.Container(
            content=self._library_list,
            expand=True,
            opacity=1.0,
            offset=ft.Offset(0, 0),
            animate_opacity=ft.Animation(100, ft.AnimationCurve.EASE_OUT_QUAD),
            animate_offset=ft.Animation(100, ft.AnimationCurve.EASE_OUT_QUAD),
        )

        # Glassmorphic premium pagination bar
        self._prev_page_btn = ft.IconButton(
            icon=ft.Icons.CHEVRON_LEFT_ROUNDED,
            icon_color=DIM,
            icon_size=20,
            disabled=True,
            tooltip="Previous Page",
            on_click=lambda e: self.page.run_task(self.change_page, self.current_page - 1)
        )
        self._next_page_btn = ft.IconButton(
            icon=ft.Icons.CHEVRON_RIGHT_ROUNDED,
            icon_color=DIM,
            icon_size=20,
            disabled=True,
            tooltip="Next Page",
            on_click=lambda e: self.page.run_task(self.change_page, self.current_page + 1)
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

        # Mood selection for partitions page
        self.selected_mood_index = 0
        self._prev_mood_btn = ft.IconButton(
            icon=ft.Icons.CHEVRON_LEFT_ROUNDED,
            icon_color=LIB_PARTITION_COLOR,
            icon_size=20,
            tooltip="Previous Mood",
            on_click=lambda e: self.page.run_task(self._change_mood, -1)
        )
        self._next_mood_btn = ft.IconButton(
            icon=ft.Icons.CHEVRON_RIGHT_ROUNDED,
            icon_color=LIB_PARTITION_COLOR,
            icon_size=20,
            tooltip="Next Mood",
            on_click=lambda e: self.page.run_task(self._change_mood, 1)
        )
        self._mood_label = ft.Text(
            "",
            color=TEXT,
            size=14,
            weight=ft.FontWeight.BOLD,
        )
        self._mood_eq_btn = ft.IconButton(
            icon=ft.Icons.TUNE,
            icon_color=LIB_PARTITION_COLOR,
            icon_size=20,
            tooltip="Adjust EQ for this mood",
            on_click=lambda e: self.page.run_task(self._open_mood_eq_dialog),
        )
        self._mood_pagination_bar = ft.Container(
            content=ft.Row(
                [
                    self._prev_mood_btn,
                    self._mood_label,
                    self._next_mood_btn,
                    self._mood_eq_btn,
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=20,
            ),
            bgcolor=apply_opacity(0.05, LIB_PARTITION_COLOR),
            border=ft.Border.all(1, apply_opacity(0.15, LIB_PARTITION_COLOR)),
            border_radius=12,
            padding=ft.Padding.symmetric(vertical=4, horizontal=16),
            margin=ft.Margin.only(left=14, right=14, bottom=6),
            visible=False,
        )

        self._empty_label = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.LIBRARY_MUSIC_OUTLINED, color=apply_opacity(0.3, CYAN), size=80),
                    ft.Text("It's empty in here.", color=TEXT, size=20, weight=ft.FontWeight.W_800),
                    ft.Text(
                        "Your music library is currently empty.\nIndex your folders to start listening.", 
                        color=DIM, size=14, text_align=ft.TextAlign.CENTER
                    ),
                    ft.Container(height=10),
                    ft.TextButton(
                        content=ft.Row([ft.Icon(ft.Icons.SETTINGS_ROUNDED, size=16), ft.Text("ENTER PATHS", size=13)], spacing=6),
                        on_click=self._on_enter_paths_click,
                        style=ft.ButtonStyle(color=CYAN)
                    )
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=12,
            ),
            expand=True,
            visible=False,
        )

        self._root = ft.Column(
            [
                # toolbar
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Column(
                                        [
                                            ft.Text("Library", size=26, weight=ft.FontWeight.W_800, color=TEXT),
                                            self._stats_label,
                                        ],
                                        spacing=0,
                                        expand=True,
                                    ),
                                    self._sort_icon_btn,
                                    self._scan_btn,
                                    ft.IconButton(
                                        icon=ft.Icons.SETTINGS_OUTLINED, icon_color=DIM,
                                        icon_size=22,
                                        on_click=lambda e: self.app._switch_tab(3),
                                    ),
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            ft.Row([self._search_bar_container], spacing=0),
                            self._view_tabs_row,
                            self._partition_tabs,
                            self._mood_pagination_bar,
                            self._scan_progress_container,
                        ],
                        spacing=10,
                    ),
                    padding=ft.Padding.only(left=14, right=14, top=18, bottom=8),
                ),
                # list or empty state
                self._animated_list_wrapper,
                self._pagination_bar,
                self._empty_label,
            ],
            expand=True,
            spacing=0,
        )

    async def _on_enter_paths_click(self, e):
        is_cached = 3 in self.app._view_cache
        if not is_cached:
            self.app.settings_view.initial_subpage = "Storage"
            self.app._switch_tab(3)
        else:
            self.app._switch_tab(3)
            await asyncio.sleep(0.1)
            self.app.settings_view._show_sub_page("Storage", self.app.settings_view._build_storage_group())

    def build(self) -> ft.Control:
        self.page.run_task(self.load_library)
        return self._root

    # ── search / view / sort ────────────────────────────────────────────────
    def _on_search_change(self, e):
        self.search_query = e.control.value or ""
        self._lib_clear_btn.visible = bool(self.search_query)
        self.expanded_nodes.clear() # Always collapse on search
        
        # Debounce library search via asyncio
        if hasattr(self, "_search_debounce_task") and self._search_debounce_task:
            self._search_debounce_task.cancel()
        
        async def _delayed_load():
            await asyncio.sleep(0.3)
            await self.load_library()
        
        self._search_debounce_task = asyncio.create_task(_delayed_load())

    def _on_search_focus(self, _e):
        def _mutate():
            self._search_bar_container.border = ft.Border.all(1.5, CYAN + "99")
            self._search_bar_container.bgcolor = SURFACE
        self.app.safe_update(_mutate)

    def _on_search_blur(self, _e):
        def _mutate():
            self._search_bar_container.border = ft.Border.all(1.5, BORDER)
            self._search_bar_container.bgcolor = SURFACE2
        self.app.safe_update(_mutate)


    def _set_view_mode(self, mode: str):
        self.view_mode = mode
        if mode == "artists" and self.sort_mode not in ("name", "tracks", "albums"):
            self.sort_mode = "name"
        elif mode in ("albums", "tracks") and self.sort_mode not in ("date", "artist", "album", "track"):
            self.sort_mode = "date"
        elif mode == "playlists" and self.sort_mode not in ("name", "date"):
            self.sort_mode = "date"
        self.expanded_nodes.clear()
        
        # Toggle sub-mode tabs and sort button visibility
        self._partition_tabs.visible = (mode == "partitions")
        self._sort_icon_btn.visible = (mode != "partitions")
        self.try_update(self._partition_tabs, self._sort_icon_btn)

        self._update_view_tabs()
        self.page.run_task(self.load_library)

    def _clear_search(self, _e=None):
        self._search_field.value = ""
        self.search_query = ""
        self._lib_clear_btn.visible = False
        self.expanded_nodes.clear()
        self._search_spinner.visible = False
        self.page.run_task(self.load_library)

    def _set_partition_sub_mode(self, sub_mode: str):
        self.partition_sub_mode = sub_mode
        self._update_partition_tabs_ui()
        self.page.run_task(self.load_library)

    def _select_mood_index(self, index: int):
        self.selected_mood_index = index
        self.current_page = 0
        self.page.run_task(self.load_library)

    async def _change_mood(self, delta: int):
        # Gather active sections matching the search query
        active_moods = []
        sq = self.search_query.lower() if self.search_query else ""
        def matches_query(t):
            if not sq: return True
            return (
                sq in (t.get("title") or "").lower() or
                sq in (t.get("artist") or "").lower() or
                sq in (t.get("album") or "").lower() or
                sq in (t.get("path") or "").lower()
            )

        from utils import track_graph as tg
        for mood in tg.MOODS.keys():
            tracks = (self._cached_moods or {}).get(mood, [])
            filtered = [t for t in tracks if matches_query(t)]
            active_moods.append((mood, filtered))

        active_moods.sort(key=lambda x: len(x[1]), reverse=True)

        unanalysed_searched = [t for t in (self._cached_unanalysed or []) if matches_query(t)]

        active_sections = []
        for mood, tracks in active_moods:
            active_sections.append(mood.capitalize())
        if unanalysed_searched:
            active_sections.append("Unanalysed Tracks")

        if not active_sections:
            return

        # Cycle selected mood index with wrapping
        self.selected_mood_index = (self.selected_mood_index + delta) % len(active_sections)
        self.current_page = 0
        await self.load_library()

    def _refresh_partitions_click(self, e):
        self.app.show_snackbar("Recalculating Default Moods...")
        self.page.run_task(self.recalculate_partitions_worker)

    async def _open_mood_eq_dialog(self):
        """Open a popover with raw feature quartile dropdowns to optimize the active mood."""
        from utils import track_graph as tg
        label_value = (self._mood_label.value or "").strip()
        canonical = tg.mood_canonical(label_value)
        if not canonical:
            self.app.show_snackbar("Select a mood partition first.", icon=ft.Icons.INFO_OUTLINE)
            return

        db = self.app.db_manager
        try:
            definition = await tg.get_mood_definition(db, canonical)
        except Exception as exc:
            logger.exception("EQ dialog: failed to fetch mood definition: %s", exc)
            definition = None

        # Raw scalar features the EQ exposes as 1–4 bands (these map to MOOD_TARGETS).
        raw_features = ["bpm", "brightness", "energy", "rolloff", "beat_strength", "spectral_flatness", "spectral_contrast", "key_mode"]
        
        friendly_names = {
            "bpm": "Tempo / BPM",
            "brightness": "Brightness",
            "energy": "Energy",
            "rolloff": "Treble Rolloff",
            "beat_strength": "Beat Strength",
            "spectral_flatness": "Flatness / Acoustic",
            "spectral_contrast": "Timbre Contrast",
            "key_mode": "Key Mode (Major/Minor)"
        }
        
        feature_explanations = {
            "bpm": "Aligns the overall playback tempo and speed.",
            "brightness": "Boosts high frequencies for crisp vocals and acoustics.",
            "energy": "Loud, intense tracks vs. quiet, minimal soundscapes.",
            "rolloff": "Warm, mellow tones vs. sharp, crisp treble.",
            "beat_strength": "Pronounced rhythm and percussion vs. ambient washes.",
            "spectral_flatness": "Noisy, complex textures vs. clean melodic tones.",
            "spectral_contrast": "Rich orchestration vs. focused, narrow synth sounds.",
            "key_mode": "Major keys (bright/optimistic) vs. minor keys (dark/somber).",
        }
        
        # The legacy "Sonic Variance Profile" / PC-colour decoration was tied to
        # the retired 3-D mood PCA and no longer applies under the unified graph
        # geometry. Sliders render plain; the EQ band values drive tracks_by_mood.
        feature_pc = {f: 0 for f in raw_features}
        header_controls = []

        def _weight_for(feature: str) -> float:
            if not definition:
                return 0.0
            entry = definition.get(feature)
            if not entry:
                return 0.0
            try:
                target_val, weight_val = entry
                if weight_val == 0.0:
                    return 0.0
                return float(target_val)
            except (TypeError, ValueError, IndexError):
                return 0.0

        sliders: dict[str, ft.Slider] = {}
        
        pc_metadata = {
            0: {"label": "Timbre (PC1)", "color": "#00E5FF"},   # cyan
            1: {"label": "Tempo (PC2)", "color": "#CE93D8"},   # purple-200
            2: {"label": "Harmonic (PC3)", "color": "#FFB300"},  # amber
        }

        labels = {
            0: "Any / Neutral",
            1: "Very Low",
            2: "Low",
            3: "High",
            4: "Very High",
        }

        def _make_row(feature: str) -> ft.Container:
            initial = int(_weight_for(feature))
            
            dom_pc = feature_pc.get(feature, 0)
            meta = pc_metadata[dom_pc]
            pc_color = meta["color"]
            
            value_text_control = ft.Text(
                labels.get(initial, "Any / Neutral"),
                color=pc_color,
                size=11,
                weight=ft.FontWeight.W_600
            )

            def _on_slider_change(e, val_text=value_text_control):
                val = int(e.control.value)
                val_text.value = labels.get(val, "Any / Neutral")
                val_text.update()

            slider = ft.Slider(
                min=0,
                max=4,
                divisions=4,
                value=initial,
                active_color=pc_color,
                inactive_color=apply_opacity(0.15, pc_color),
                thumb_color=TEXT,
                on_change=_on_slider_change,
            )
            sliders[feature] = slider
            
            label_text = friendly_names.get(feature, feature)
            explanation = feature_explanations.get(feature, "")
            
            dot = ft.Container(
                width=8,
                height=8,
                border_radius=4,
                bgcolor=pc_color,
                margin=ft.Margin.only(right=6),
            )
            
            return ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Row(
                                    [
                                        dot,
                                        ft.Text(label_text, color=TEXT, size=12, weight=ft.FontWeight.W_700, no_wrap=True),
                                    ],
                                    alignment=ft.MainAxisAlignment.START,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    spacing=0,
                                ),
                                value_text_control,
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Container(
                            content=ft.Text(
                                explanation,
                                color=DIM,
                                size=9.5,
                                weight=ft.FontWeight.W_400,
                                no_wrap=False,
                            ),
                            margin=ft.Margin.only(left=14, right=4),
                            padding=ft.Padding.only(bottom=2),
                        ),
                        ft.Container(
                            content=slider,
                            margin=ft.Margin.only(top=-6, bottom=-4),
                        ),
                    ],
                    spacing=2,
                    tight=True,
                ),
                padding=ft.Padding.only(bottom=4),
            )

        redundant_features = getattr(tg, "REDUNDANT_FEATURES", set())
        active_features = [f for f in raw_features if f not in redundant_features]

        rows = []
        if header_controls:
            rows.extend(header_controls)
        rows.extend([_make_row(f) for f in active_features])

        dlg = ft.AlertDialog(
            title=ft.Text(f"{label_value or canonical.capitalize()} Quartile Optimization", color=TEXT),
            content=ft.Container(
                content=ft.Column(rows, spacing=16, tight=True, scroll=ft.ScrollMode.ADAPTIVE),
                padding=ft.Padding.only(top=8, right=8),
                width=380,
                height=460,
            ),
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=SURFACE,
        )

        def _close(_e=None):
            dlg.open = False
            self.page.update()

        def _on_apply(_e):
            weights_to_save = {}
            for f in raw_features:
                if f in redundant_features:
                    weights_to_save[f] = 0.0
                else:
                    weights_to_save[f] = float(sliders[f].value)
            
            _close()
            self.page.run_task(self._apply_mood_eq, canonical, weights_to_save)

        dlg.actions = [
            ft.TextButton("Cancel", on_click=_close),
            ft.TextButton(
                content=ft.Text("Apply", weight=ft.FontWeight.BOLD, color=CYAN),
                on_click=_on_apply,
            ),
        ]
        self.page.overlay.append(dlg)
        dlg.open = True
        self.page.update()

    async def _apply_mood_eq(self, canonical_mood: str, weights: dict[str, float]):
        from utils import track_graph as tg
        db = self.app.db_manager
        try:
            await tg.set_mood_eq(db, canonical_mood, weights)
        except Exception as exc:
            logger.exception("Failed to set mood EQ: %s", exc)
            self.app.show_snackbar(f"EQ update failed: {exc}", icon=ft.Icons.ERROR_OUTLINE)
            return
        self.app.show_snackbar(f"EQ updated for {canonical_mood.capitalize()}.", icon=ft.Icons.CHECK_CIRCLE_OUTLINE)
        try:
            current_label = (self._mood_label.value or "").strip()
            if (
                self.partition_sub_mode == "moods"
                and tg.mood_canonical(current_label) == canonical_mood
            ):
                await self.load_library()
        except Exception as exc:
            logger.debug("EQ apply: post-save refresh failed: %s", exc)

    async def _toggle_mood_like(self, track_path: str, mood: str, btn: ft.IconButton = None):
        db = self.app.db_manager
        from utils import track_graph as tg
        try:
            feedback_map = await db.get_mood_feedback()
            is_liked = feedback_map.get(track_path, {}).get(mood, 0) == 1
            
            new_fb = 0 if is_liked else 1
            await db.save_mood_feedback(track_path, mood, new_fb)
            self._mood_feedback_map.setdefault(track_path, {})[mood] = new_fb
            
            feedback_str = "Like removed" if is_liked else "Track pinned to mood"
            
            if btn:
                btn.icon = ft.Icons.THUMB_UP_OUTLINED if is_liked else ft.Icons.THUMB_UP_ROUNDED
                btn.icon_color = DIM if is_liked else CYAN
                self.try_update(btn)
            
            for ctrl in self._path_to_controls.get(track_path, []):
                try:
                    tile = self._get_tile(ctrl)
                    if isinstance(tile.trailing, ft.Row) and len(tile.trailing.controls) >= 2:
                        l_btn = tile.trailing.controls[0]
                        l_btn.icon = ft.Icons.THUMB_UP_OUTLINED if is_liked else ft.Icons.THUMB_UP_ROUNDED
                        l_btn.icon_color = DIM if is_liked else CYAN
                        self.try_update(l_btn)
                except:
                    pass

            if not is_liked:
                async def run_learning_and_walk():
                    try:
                        walk_tracks = await tg.walk(db, track_path, length=5, edge_kinds=(tg.KIND_ACOUSTIC,))
                        for wt in walk_tracks:
                            await db.save_mood_feedback(wt, mood, 1)
                            self._mood_feedback_map.setdefault(wt, {})[mood] = 1
                            for ctrl in self._path_to_controls.get(wt, []):
                                try:
                                    tile = self._get_tile(ctrl)
                                    if isinstance(tile.trailing, ft.Row) and len(tile.trailing.controls) >= 2:
                                        l_btn = tile.trailing.controls[0]
                                        l_btn.icon = ft.Icons.THUMB_UP_ROUNDED
                                        l_btn.icon_color = CYAN
                                        self.try_update(l_btn)
                                except:
                                    pass
                    except Exception as walk_err:
                        logger.exception("Random walk failed during like toggle: %s", walk_err)

                    try:
                        await tg.adjust_mood_profile(db, mood, track_path, 1)
                    except Exception as shift_err:
                        logger.exception("Online learning shift failed during like: %s", shift_err)

                self.page.run_task(run_learning_and_walk)
            else:
                async def run_unlike_walk():
                    try:
                        walk_tracks = await tg.walk(db, track_path, length=5, edge_kinds=(tg.KIND_ACOUSTIC,))
                        feedback_map_latest = await db.get_mood_feedback()
                        for wt in walk_tracks:
                            if feedback_map_latest.get(wt, {}).get(mood, 0) == 1:
                                await db.save_mood_feedback(wt, mood, 0)
                                self._mood_feedback_map.setdefault(wt, {})[mood] = 0
                                for ctrl in self._path_to_controls.get(wt, []):
                                    try:
                                        tile = self._get_tile(ctrl)
                                        if isinstance(tile.trailing, ft.Row) and len(tile.trailing.controls) >= 2:
                                            l_btn = tile.trailing.controls[0]
                                            l_btn.icon = ft.Icons.THUMB_UP_OUTLINED
                                            l_btn.icon_color = DIM
                                            self.try_update(l_btn)
                                    except:
                                        pass
                    except Exception as walk_err:
                        logger.exception("Random walk failed during unlike toggle: %s", walk_err)

                self.page.run_task(run_unlike_walk)

            self._mood_recalc_pending = True
            self._update_partition_tabs_ui()
            self.app.show_snackbar(f"{feedback_str}. Recalculation pending.")
        except Exception as e:
            logger.exception("Failed to toggle mood like: %s", e)
            self.app.show_snackbar(f"Failed to like track: {e}")

    async def _register_mood_dislike(self, track_path: str, mood: str, btn: ft.IconButton = None):
        db = self.app.db_manager
        from utils import track_graph as tg
        import numpy as np
        try:
            feedback_map = await db.get_mood_feedback()
            was_liked = feedback_map.get(track_path, {}).get(mood, 0) == 1

            await db.save_mood_feedback(track_path, mood, -1)
            self._mood_feedback_map.setdefault(track_path, {})[mood] = -1

            try:
                rows, percentile_matrix = await tg._load_percentile_matrix(db, tg.FEATURES_VERSION)
                track_idx = None
                for idx, r in enumerate(rows):
                    if r["path"] == track_path:
                        track_idx = idx
                        break

                if track_idx is not None:
                    adjusted_profiles = await db.get_all_adjusted_mood_profiles()
                    raw_mood_scores = tg.score_tracks_for_repartition(rows, adjusted_profiles)
                    mood_scores = {}
                    for m in tg.MOODS.keys():
                        if m in raw_mood_scores:
                            mood_scores[m] = float(raw_mood_scores[m][track_idx])
                        else:
                            mood_scores[m] = -np.inf

                    dislikes = {m for m, fb in self._mood_feedback_map.get(track_path, {}).items() if fb == -1}
                    
                    best_mood = None
                    best_score = -np.inf
                    for m in tg.MOODS.keys():
                        if m in dislikes:
                            continue
                        score = mood_scores[m]
                        if score > best_score:
                            best_score = score
                            best_mood = m

                    if best_mood is not None and best_score >= -2.0:
                        await db.save_partitions([(track_path, best_mood, None)])
                        logger.info("register_mood_dislike: Re-routed track '%s' from '%s' to next-best mood '%s' (score: %.3f)",
                                    track_path, mood, best_mood, best_score)
                    else:
                        async with db._write_lock:
                            conn = await db.get_connection()
                            await conn.execute("DELETE FROM track_partitions WHERE track_path = ?", (track_path,))
                            await conn.commit()
                        best_mood = None
                        logger.info("register_mood_dislike: Track '%s' has no compatible default partition, removed from partitions",
                                    track_path)

                    if self._cached_moods is not None:
                        track_obj = None
                        if mood in self._cached_moods:
                            for t in list(self._cached_moods[mood]):
                                if t["path"] == track_path:
                                    track_obj = t
                                    self._cached_moods[mood].remove(t)
                                    break
                        if track_obj is not None and best_mood is not None and best_mood in self._cached_moods:
                            self._cached_moods[best_mood].append(track_obj)
            except Exception as routing_err:
                logger.exception("Failed to calculate next-best matching mood partition during dislike: %s", routing_err)

            controls = self._path_to_controls.get(track_path, [])
            removed_any = False
            for ctrl in controls:
                if ctrl in self._library_list.controls:
                    self._library_list.controls.remove(ctrl)
                    removed_any = True
            
            if removed_any:
                self.try_update(self._library_list)
            
            try:
                if self._stats_label.text:
                    parts = self._stats_label.text.split()
                    if parts and parts[0].isdigit():
                        cnt = max(0, int(parts[0]) - 1)
                        self._stats_label.text = f"{cnt} {'TRACK' if cnt == 1 else 'TRACKS'}"
                        self.try_update(self._stats_label)
            except:
                pass

            async def run_learning_dislike():
                if was_liked:
                    try:
                        walk_tracks = await tg.walk(db, track_path, length=5, edge_kinds=(tg.KIND_ACOUSTIC,))
                        feedback_map_latest = await db.get_mood_feedback()
                        for wt in walk_tracks:
                            if feedback_map_latest.get(wt, {}).get(mood, 0) == 1:
                                await db.save_mood_feedback(wt, mood, 0)
                                self._mood_feedback_map.setdefault(wt, {})[mood] = 0
                                for ctrl in self._path_to_controls.get(wt, []):
                                    try:
                                        tile = self._get_tile(ctrl)
                                        if isinstance(tile.trailing, ft.Row) and len(tile.trailing.controls) >= 2:
                                            l_btn = tile.trailing.controls[0]
                                            l_btn.icon = ft.Icons.THUMB_UP_OUTLINED
                                            l_btn.icon_color = DIM
                                            self.try_update(l_btn)
                                    except:
                                        pass
                    except Exception as walk_err:
                        logger.exception("Random walk failed during dislike unlike propagation: %s", walk_err)

                try:
                    await tg.adjust_mood_profile(db, mood, track_path, -1)
                except Exception as shift_err:
                    logger.exception("Online learning shift failed during dislike: %s", shift_err)

            self.page.run_task(run_learning_dislike)

            self._mood_recalc_pending = True
            self._update_partition_tabs_ui()
            self.app.show_snackbar("Track excluded from mood subset and re-routed.", color=CYAN)
        except Exception as e:
            logger.exception("Failed to register mood dislike: %s", e)
            self.app.show_snackbar(f"Failed to dislike track: {e}")

    def _open_reset_feedback_confirmation(self):
        """Open a confirmation dialog before resetting mood feedback and profiles."""
        def _on_confirm(e):
            dlg.open = False
            self.page.update()
            self.page.run_task(self._reset_mood_feedback)

        def _on_cancel(e):
            dlg.open = False
            self.page.update()

        dlg = ft.AlertDialog(
            title=ft.Text("Reset Mood Configurations?", color=TEXT),
            content=ft.Text("This will permanently delete all your custom mood EQ tunings, partition assignments, and feedback. Are you sure you want to begin calibration from scratch?", color=DIM),
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=SURFACE,
            actions=[
                ft.TextButton("Cancel", on_click=_on_cancel),
                ft.TextButton(
                    content=ft.Text("Reset", weight=ft.FontWeight.BOLD, color=ft.Colors.RED_400),
                    on_click=_on_confirm,
                ),
            ],
        )
        self.page.overlay.append(dlg)
        dlg.open = True
        self.page.update()

    async def _reset_mood_feedback(self):
        db = self.app.db_manager
        try:
            self.app.show_snackbar("Resetting mood feedback & profiles...")
            await db.clear_all_mood_feedback()
            await db.clear_all_adjusted_mood_profiles()
            await db.save_partitions([])  # Wipe all track partitions from SQLite
            self._mood_feedback_map.clear()
            self._mood_recalc_pending = False
            
            self._cached_moods = None
            self._cached_islets = None
            self._cached_unanalysed = None
            
            from utils import track_graph as tg
            tg.invalidate_mood_cache()
            
            await self.load_library()
            self.app.show_snackbar("Mood feedback & profiles reset successfully. Ready for clean calibration.", color=CYAN)
        except Exception as e:
            logger.exception("Failed to reset mood feedback: %s", e)
            self.app.show_snackbar(f"Failed to reset mood feedback: {e}")


    async def recalculate_partitions_worker(self):
        self.app.safe_update(lambda: setattr(self._search_spinner, "visible", True))
        try:
            db = self.app.db_manager
            
            import numpy as np
            from utils import track_graph as tg

            rows, percentile_matrix = await tg._load_percentile_matrix(db, tg.FEATURES_VERSION)
            
            all_tracks = await db.get_all_tracks()
            all_paths_to_track = {t["path"]: t for t in all_tracks}
            
            if not all_tracks:
                self.app.show_snackbar("No tracks found in library. Scan your music folder first.")
                return
            
            analysed_rows = []
            analysed_indices = []
            for idx, r in enumerate(rows):
                if r["path"] in all_paths_to_track:
                    analysed_rows.append(r)
                    analysed_indices.append(idx)
            
            mood_assignments = {}
            if analysed_rows and len(analysed_indices) > 0:
                feedback_map = await db.get_mood_feedback()
                self._mood_feedback_map = feedback_map
                adjusted_profiles = await db.get_all_adjusted_mood_profiles()
                
                if not adjusted_profiles:
                    for mood in tg.MOODS.keys():
                        default_profile = tg._get_default_quartiles(mood)
                        await db.save_adjusted_mood_profile(mood, default_profile)
                    adjusted_profiles = await db.get_all_adjusted_mood_profiles()
                
                raw_mood_scores = tg.score_tracks_for_repartition(analysed_rows, adjusted_profiles)
                mood_scores = {}
                for mood in tg.MOODS.keys():
                    if mood in raw_mood_scores:
                        mood_scores[mood] = raw_mood_scores[mood]
                    else:
                        raise ValueError(f"No adjusted mood profile found in database for '{mood}'. Built-in default profiles are disabled.")
                        
                for i, track in enumerate(analysed_rows):
                    path = track["path"]
                    track_feedback = feedback_map.get(path, {})
                    
                    track_likes = [m for m, fb in track_feedback.items() if fb == 1]
                    if track_likes:
                        best_mood = None
                        best_score = -np.inf
                        for mood in track_likes:
                            if mood in mood_scores:
                                score = mood_scores[mood][i]
                                if score > best_score:
                                    best_score = score
                                    best_mood = mood
                        if best_mood is None:
                            best_mood = track_likes[0]
                    else:
                        dislikes = {m for m, fb in track_feedback.items() if fb == -1}
                        best_mood = None
                        best_score = -np.inf
                        for mood in tg.MOODS.keys():
                            if mood in dislikes:
                                continue
                            score = mood_scores[mood][i]
                            if score > best_score:
                                best_score = score
                                best_mood = mood
                                
                        if best_score is not None and best_score < -2.0:
                            best_mood = None
                                
                    if best_mood is not None:
                        mood_assignments[path] = best_mood
            
            assignments = [
                (path, mood_assignments.get(path), None)
                for path in all_paths_to_track.keys()
                if mood_assignments.get(path) is not None
            ]

            await db.save_partitions(assignments)

            self._cached_moods = None
            self._cached_islets = None
            self._cached_unanalysed = None

            self._mood_recalc_pending = False
            self._update_partition_tabs_ui()
            self.app.show_snackbar("Default moods regenerated.")
            await self.load_library()
        except Exception as ex:
            logger.exception("Failed to recalculate partitions: %s", ex)
            self.app.show_snackbar(f"Failed to generate partitions: {ex}")
        finally:
            self.app.safe_update(lambda: setattr(self._search_spinner, "visible", False))

    def _update_partition_tabs_ui(self):
        from utils.streamrip_api import load_config
        try:
            cfg = load_config()
            appearance = cfg.get("appearance", {})
        except:
            appearance = {}

        show_moods = bool(appearance.get("show_moods", False))
        show_islets = bool(appearance.get("show_islets", False))

        visible_submodes = []
        if show_moods: visible_submodes.append("moods")
        if show_islets: visible_submodes.append("islets")

        if not visible_submodes:
            visible_submodes = ["moods"]
            show_moods = True

        if self.partition_sub_mode not in visible_submodes:
            self.partition_sub_mode = visible_submodes[0]

        tabs = []
        all_submodes = [
            ("moods", "Default", ft.Icons.EMOJI_EMOTIONS_ROUNDED, show_moods),
            ("islets", "Custom", ft.Icons.DIVERSITY_3_ROUNDED, show_islets),
        ]
        
        active_col = LIB_PARTITION_COLOR
        for mode, label, icon, enabled in all_submodes:
            if not enabled:
                continue
            is_active = (self.partition_sub_mode == mode)
            
            tabs.append(
                ft.GestureDetector(
                    content=ft.Container(
                        content=ft.Row(
                            [
                                ft.Icon(icon, color=BG if is_active else active_col, size=16),
                                ft.Text(label, size=12, weight=ft.FontWeight.W_700,
                                        color=BG if is_active else TEXT, no_wrap=True),
                            ],
                            spacing=6,
                            alignment=ft.MainAxisAlignment.CENTER,
                        ),
                        bgcolor=active_col if is_active else apply_opacity(0.08, active_col),
                        border=ft.Border.all(1, active_col if is_active else apply_opacity(0.2, active_col)),
                        border_radius=12,
                        padding=ft.Padding.symmetric(horizontal=16, vertical=8),
                        animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
                    ),
                    on_tap=lambda e, m=mode: self._set_partition_sub_mode(m),
                    expand=True,
                )
            )
        if self.partition_sub_mode == "islets" and show_islets:
            tabs.append(
                ft.IconButton(
                    icon=ft.Icons.ADD_CIRCLE_OUTLINE_ROUNDED,
                    icon_color=active_col,
                    icon_size=18,
                    tooltip="New Islet",
                    bgcolor=apply_opacity(0.08, active_col),
                    on_click=lambda _: self._open_create_islet_dialog(),
                )
            )
        if self.partition_sub_mode == "moods" and show_moods:
            tabs.append(
                ft.IconButton(
                    icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                    icon_color=ft.Colors.WHITE,
                    icon_size=18,
                    tooltip="Reset Mood Feedback",
                    bgcolor=apply_opacity(0.08, active_col),
                    on_click=lambda _: self._open_reset_feedback_confirmation(),
                )
            )
        is_pending = getattr(self, "_mood_recalc_pending", False)
        tabs.append(
            ft.IconButton(
                icon=ft.Icons.REFRESH_ROUNDED,
                icon_color=ft.Colors.WHITE if is_pending else active_col,
                icon_size=18,
                tooltip="Recalculate Default Moods (Changes Pending)" if is_pending else "Recalculate Default Moods",
                bgcolor=apply_opacity(0.08, active_col),
                on_click=self._refresh_partitions_click
            )
        )
        self._partition_tabs.content.controls = tabs
        self.try_update(self._partition_tabs)

    # ── Islet creation dialog ────────────────────────────────────────────────
    def _open_create_islet_dialog(self):
        """Open the New Islet modal. Seeds the islet from the currently-playing
        track (its timbre vector becomes the centroid). Membership is computed
        on demand against ISLET_THRESHOLD when the library reloads."""
        current_path = audio_engine.current_path or ""
        current_title = audio_engine.current_track or ""
        current_artist = audio_engine.current_artist or ""

        name_field = ft.TextField(
            label="Islet name",
            autofocus=True,
            border_color=LIB_PARTITION_COLOR,
            cursor_color=LIB_PARTITION_COLOR,
        )

        if current_path:
            seed_line = ft.Text(
                f"Seed: {current_title} — {current_artist}",
                color=DIM, size=12, max_lines=2,
            )
            disabled_reason = ""
        else:
            seed_line = ft.Text(
                "No track is currently playing. Play the exemplar track first, then reopen this dialog.",
                color="#FF8866", size=12, max_lines=3,
            )
            disabled_reason = "Play a track first."

        dlg = ft.AlertDialog(
            title=ft.Text("New Islet"),
            content=ft.Container(
                content=ft.Column(
                    [name_field, ft.Container(height=6), seed_line],
                    spacing=4, tight=True,
                ),
                padding=ft.Padding.only(top=10),
                width=360,
            ),
            actions_alignment=ft.MainAxisAlignment.END,
        )

        def _close(_e=None):
            dlg.open = False
            self.page.update()

        def _on_save(_e):
            raw_name = (name_field.value or "").strip().strip("\"'").strip()
            if not raw_name:
                name_field.error_text = "Name required"
                self.page.update()
                return
            if disabled_reason:
                self.app.show_snackbar(disabled_reason, icon=ft.Icons.WARNING_AMBER_ROUNDED)
                return
            from utils import track_graph as tg
            if raw_name.lower() in tg.MOOD_PROFILES:
                name_field.error_text = "Conflicts with a built-in mood"
                self.page.update()
                return
            _close()
            self.page.run_task(self._save_islet_from_dialog, raw_name, current_path)

        dlg.actions = [
            ft.TextButton("Cancel", on_click=_close),
            ft.TextButton("Save", on_click=_on_save),
        ]
        self.page.overlay.append(dlg)
        dlg.open = True
        self.page.update()

    async def _exclude_track_from_islet(self, islet_name: str, track_path: str, track_title: str):
        from utils import track_graph as tg
        ok = tg.blacklist_track_from_islet(islet_name, track_path)
        if ok:
            try:
                await tg.record_islet_negative(self.app.db_manager, islet_name, track_path)
            except Exception as exc:
                logger.debug("islet regressor negative update failed: %s", exc)
            self._cached_islets = None
            self.app.show_snackbar(
                f"'{track_title}' excluded from custom mood '{islet_name.title()}'.",
                icon=ft.Icons.CHECK_CIRCLE_OUTLINE
            )
            await self.load_library()
        else:
            self.app.show_snackbar(
                "Failed to exclude track from custom mood.",
                icon=ft.Icons.ERROR_OUTLINE
            )

    async def _clear_islet_blacklist_action(self, islet_name: str, dialog: ft.AlertDialog):
        from utils import track_graph as tg
        dialog.open = False
        self.page.update()
        
        ok = tg.clear_islet_blacklist(islet_name)
        if ok:
            self._cached_islets = None
            self.app.show_snackbar(
                f"Exclusion blacklist cleared for custom mood '{islet_name.title()}'.",
                icon=ft.Icons.CHECK_CIRCLE_OUTLINE
            )
            await self.load_library()
        else:
            self.app.show_snackbar(
                "Failed to clear exclusion blacklist.",
                icon=ft.Icons.ERROR_OUTLINE
            )

    def _open_edit_islet_dialog(self, name: str):
        """Rename + retune-threshold for an existing islet. Centroid and
        exemplar stay locked — to re-seed, delete and create fresh."""
        from utils import track_graph as tg
        entry = tg.load_custom_moods().get(name)
        if entry is None:
            self.app.show_snackbar(f"Islet '{name}' no longer exists.", icon=ft.Icons.ERROR_OUTLINE)
            return
        current_threshold = float(entry.get("threshold", tg.ISLET_THRESHOLD))

        name_field = ft.TextField(
            label="Name",
            value=name,
            autofocus=True,
            border_color=LIB_PARTITION_COLOR,
            cursor_color=LIB_PARTITION_COLOR,
        )
        threshold_label = ft.Text(f"Threshold: {current_threshold:.2f}", color=DIM, size=12)
        threshold_slider = ft.Slider(
            min=0.70, max=0.99, divisions=29, value=current_threshold,
            active_color=LIB_PARTITION_COLOR,
            inactive_color=apply_opacity(0.2, LIB_PARTITION_COLOR),
            on_change=lambda e: (
                setattr(threshold_label, "value", f"Threshold: {float(e.control.value):.2f}"),
                self.page.update(),
            ),
        )

        blacklist = entry.get("blacklist", [])
        blacklist_container = ft.Container(visible=bool(blacklist))
        if blacklist:
            blacklist_container.content = ft.Row(
                [
                    ft.Text(f"{len(blacklist)} track(s) excluded", size=11, color=DIM, weight=ft.FontWeight.W_600),
                    ft.TextButton(
                        "Clear Exclusions",
                        icon=ft.Icons.RESTORE_ROUNDED,
                        style=ft.ButtonStyle(color="#FF4444"),
                        on_click=lambda _e: self.page.run_task(self._clear_islet_blacklist_action, name, dlg)
                    )
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN
            )

        dlg = ft.AlertDialog(
            title=ft.Text("Edit Islet"),
            content=ft.Container(
                content=ft.Column(
                    [
                        name_field,
                        ft.Container(height=6),
                        threshold_label,
                        threshold_slider,
                        ft.Text(
                            "Tighter values keep the islet closer to the seed track. "
                            "Looser values pull in more distant neighbours.",
                            color=DIM, size=11, max_lines=3,
                        ),
                        ft.Container(height=6, visible=bool(blacklist)),
                        blacklist_container,
                    ],
                    spacing=4, tight=True,
                ),
                padding=ft.Padding.only(top=10),
                width=380,
            ),
            actions_alignment=ft.MainAxisAlignment.END,
        )

        def _close(_e=None):
            dlg.open = False
            self.page.update()

        def _on_save(_e):
            raw_new = (name_field.value or "").strip().strip("\"'").strip()
            if not raw_new:
                name_field.error_text = "Name required"
                self.page.update()
                return
            if raw_new.lower() != name and raw_new.lower() in tg.MOOD_PROFILES:
                name_field.error_text = "Conflicts with a built-in mood"
                self.page.update()
                return
            ok = tg.update_custom_mood(name, raw_new, float(threshold_slider.value))
            if not ok:
                name_field.error_text = "Name already in use"
                self.page.update()
                return
            _close()
            self._cached_islets = None
            self.app.show_snackbar(f"Islet '{raw_new}' updated.", icon=ft.Icons.CHECK_CIRCLE_OUTLINE)
            self.page.run_task(self.load_library)

        dlg.actions = [
            ft.TextButton("Cancel", on_click=_close),
            ft.TextButton("Save", on_click=_on_save),
        ]
        self.page.overlay.append(dlg)
        dlg.open = True
        self.page.update()

    def _confirm_delete_islet(self, name: str):
        """Two-step delete with confirmation dialog. The centroid file gets
        rewritten without this entry; library reloads to drop the accordion."""
        from utils import track_graph as tg

        dlg = ft.AlertDialog(
            title=ft.Text("Delete Islet"),
            content=ft.Text(
                f"Remove '{name.title()}'? The exemplar track stays in your library; "
                "only the islet definition is deleted.",
                color=TEXT, size=13,
            ),
            actions_alignment=ft.MainAxisAlignment.END,
        )

        def _close(_e=None):
            dlg.open = False
            self.page.update()

        def _on_delete(_e):
            _close()
            if tg.delete_custom_mood(name):
                self._cached_islets = None
                self.app.show_snackbar(f"Islet '{name}' deleted.", icon=ft.Icons.DELETE_OUTLINE)
                self.page.run_task(self.load_library)
            else:
                self.app.show_snackbar(f"Islet '{name}' was already gone.", icon=ft.Icons.ERROR_OUTLINE)

        dlg.actions = [
            ft.TextButton("Cancel", on_click=_close),
            ft.TextButton("Delete", on_click=_on_delete),
        ]
        self.page.overlay.append(dlg)
        dlg.open = True
        self.page.update()

    async def _save_islet_from_dialog(self, name: str, exemplar_path: str):
        from utils import track_graph as tg
        from utils.dsp import unpack_timbre
        try:
            row = await self.app.db_manager.get_track_full(exemplar_path)
            timbre = unpack_timbre(row.get("timbre")) if row else None
            if timbre is None:
                self.app.show_snackbar(
                    "Exemplar has no DSP features. Run a rescan first.",
                    icon=ft.Icons.ERROR_OUTLINE,
                )
                return
            tg.save_custom_mood(
                name,
                centroid=[float(x) for x in timbre],
                exemplar_path=exemplar_path,
            )
            self._cached_islets = None
            self.app.show_snackbar(f"Islet '{name}' saved.", icon=ft.Icons.CHECK_CIRCLE_OUTLINE)
            await self.load_library()
        except Exception as ex:
            logger.exception("Failed to save islet %s", name)
            self.app.show_snackbar(f"Failed to save islet: {ex}", icon=ft.Icons.ERROR_OUTLINE)

    def _build_islet_empty_state(self) -> ft.Control:
        """Shown in the Islets sub-tab when no user islets exist yet."""
        return ft.Container(
            content=ft.Column(
                [
                    ft.Container(
                        content=ft.Icon(
                            ft.Icons.DIVERSITY_3_ROUNDED,
                            color=LIB_PARTITION_COLOR,
                            size=40,
                        ),
                        bgcolor=apply_opacity(0.1, LIB_PARTITION_COLOR),
                        border_radius=20,
                        padding=16,
                    ),
                    ft.Text(
                        "No islets yet",
                        color=TEXT, size=18, weight=ft.FontWeight.W_700,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        "Play a track you'd like to anchor a vibe around, then create an "
                        "islet from it. Tracks acoustically close to that exemplar become "
                        "members automatically.",
                        color=DIM, size=13, text_align=ft.TextAlign.CENTER, max_lines=4,
                    ),
                    ft.Container(height=8),
                    ft.Button(
                        "New Islet",
                        icon=ft.Icons.ADD_CIRCLE_OUTLINE_ROUNDED,
                        color=BG,
                        bgcolor=LIB_PARTITION_COLOR,
                        on_click=lambda _: self._open_create_islet_dialog(),
                        style=ft.ButtonStyle(
                            shape=ft.RoundedRectangleBorder(radius=10),
                            padding=ft.Padding.symmetric(horizontal=24, vertical=12),
                        ),
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
            ),
            bgcolor="#0DFFFFFF",
            border=ft.Border.all(1, apply_opacity(0.1, TEXT)),
            border_radius=16,
            padding=32,
            margin=ft.Margin.symmetric(horizontal=16, vertical=24),
            alignment=ft.Alignment(0, 0),
        )

    def _update_view_tabs(self):
        from utils.streamrip_api import load_config
        try:
            cfg = load_config()
            appearance = cfg.get("appearance", {})
        except:
            appearance = {}

        show_moods = bool(appearance.get("show_moods", False))
        show_islets = bool(appearance.get("show_islets", False))
        show_partitions = show_moods or show_islets

        show_playlists = bool(appearance.get("show_playlists", True))
        show_artists = bool(appearance.get("show_artists", True))
        show_albums = bool(appearance.get("show_albums", True))
        show_tracks = bool(appearance.get("show_tracks", True))

        visible_modes = []
        if show_partitions: visible_modes.append("partitions")
        if show_playlists: visible_modes.append("playlists")
        if show_artists: visible_modes.append("artists")
        if show_albums: visible_modes.append("albums")
        if show_tracks: visible_modes.append("tracks")

        if not visible_modes:
            visible_modes = ["tracks"]
            show_tracks = True

        if self.view_mode not in visible_modes:
            self.view_mode = visible_modes[0]

        icons = {
            "playlists": ft.Icons.QUEUE_MUSIC_ROUNDED,
            "artists":   ft.Icons.PERSON_ROUNDED,
            "albums":    ft.Icons.ALBUM_ROUNDED,
            "tracks":    ft.Icons.MUSIC_NOTE_ROUNDED,
            "partitions": ft.Icons.DIVERSITY_3_ROUNDED,
        }
        accents = {
            "playlists": LIB_PLAYLIST_COLOR,
            "artists":   LIB_ARTIST_COLOR,
            "albums":    LIB_ALBUM_COLOR,
            "tracks":    LIB_TRACK_COLOR,
            "partitions": LIB_PARTITION_COLOR,
        }
        tabs = []
        
        all_modes = [
            ("partitions", "Moods", show_partitions),
            ("playlists", "Playlists", show_playlists),
            ("artists", "Artists", show_artists),
            ("albums", "Albums", show_albums),
            ("tracks", "Tracks", show_tracks),
        ]
        
        for mode, label, enabled in all_modes:
            if not enabled:
                continue
            is_active = (self.view_mode == mode)
            col = accents[mode]
            tabs.append(
                ft.GestureDetector(
                    content=ft.Container(
                        content=ft.Column(
                            [
                                ft.Icon(icons[mode], color=BG if is_active else col, size=18),
                                ft.Text(label, size=10, weight=ft.FontWeight.W_700,
                                        color=BG if is_active else TEXT, no_wrap=True),
                            ],
                            spacing=2,
                            alignment=ft.MainAxisAlignment.CENTER,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        bgcolor=col if is_active else apply_opacity(0.08, col),
                        border=ft.Border.all(1, col if is_active else apply_opacity(0.2, col)),
                        height=52,
                        border_radius=12,
                        padding=ft.Padding.symmetric(horizontal=4),
                    ),
                    on_tap=lambda e, m=mode: self._set_view_mode(m),
                    expand=True,
                )
            )
        self._view_tabs_row.controls = tabs
        self.try_update(self._view_tabs_row)

    def _open_sort_menu(self, _e):
        if self.view_mode == "artists":
            options = [("Artist (A–Z)", "artist"), ("Most Tracks", "tracks"), ("Most Albums", "albums")]
        elif self.view_mode == "playlists":
            options = [("Name (A–Z)", "name"), ("Date Created", "date")]
        else:
            options = [
                ("Date Added", "date"),
                ("Artist (A–Z)", "artist"),
                ("Album (A–Z)", "album"),
                ("Track (A–Z)", "track"),
            ]

        def pick(val):
            self.sort_mode = val
            self._sort_icon_btn.tooltip = f"Sort: {val.capitalize()}"
            self._sort_bs.open = False
            self.page.update()
            self.page.run_task(self.load_library)

        self._sort_bs = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text("Sort by", color=TEXT, weight=ft.FontWeight.W_700, size=14),
                        ft.Divider(color=BORDER),
                        *[
                            ft.ListTile(
                                leading=ft.Icon(
                                    ft.Icons.CHECK_ROUNDED if val == self.sort_mode else ft.Icons.SORT,
                                    color=CYAN if val == self.sort_mode else DIM,
                                    size=18,
                                ),
                                title=ft.Text(
                                    label,
                                    color=CYAN if val == self.sort_mode else TEXT,
                                    weight=ft.FontWeight.W_600 if val == self.sort_mode else None,
                                ),
                                on_click=lambda _ev, v=val: pick(v),
                            )
                            for label, val in options
                        ],
                    ],
                    tight=True,
                    spacing=0,
                ),
                bgcolor=SURFACE,
                padding=16,
            ),
            bgcolor=SURFACE,
        )
        self.page.overlay.append(self._sort_bs)
        self._sort_bs.open = True
        self.page.update()

    # ── flat tree loading ─────────────────────────────────────────────────────
    async def _build_rows_generator(self):
        db = self.app.db_manager

        if self.view_mode == "artists":
            artists = await db.get_all_artists(search_query=self.search_query, sort_mode=self.sort_mode)
            suffix = " (CLOSEST MATCH)" if getattr(artists, "is_closest", False) else ""
            stats_text = f"{len(artists)} {'ARTIST' if len(artists) == 1 else 'ARTISTS'}{suffix}"
            
            async def _gen():
                for a in artists:
                    node_id = f"artist_{a['name']}"
                    expanded = node_id in self.expanded_nodes
                    yield self._artist_row(a, node_id, expanded)
                    if expanded:
                        for al in await db.get_albums_by_artist(a['name']):
                            alb_id  = f"album_{al['artist']}_{al['album']}"
                            alb_exp = alb_id in self.expanded_nodes
                            yield self._album_row(al, alb_id, alb_exp, depth=1)
                            if alb_exp:
                                for t in await db.get_tracks_by_album(al['album'], al['artist']):
                                    yield self._track_row(t, depth=2, album_context=(al['artist'], al['album']))
            return _gen(), stats_text

        elif self.view_mode == "albums":
            albums = await db.get_all_albums(search_query=self.search_query, sort_mode=self.sort_mode)
            suffix = " (CLOSEST MATCH)" if getattr(albums, "is_closest", False) else ""
            stats_text = f"{len(albums)} {'ALBUM' if len(albums) == 1 else 'ALBUMS'}{suffix}"
            
            async def _gen():
                for a in albums:
                    node_id = f"album_{a['artist']}_{a['album']}"
                    expanded = node_id in self.expanded_nodes
                    yield self._album_row(a, node_id, expanded, depth=0)
                    if expanded:
                        for t in await db.get_tracks_by_album(a['album'], a['artist']):
                            yield self._track_row(t, depth=1, album_context=(a['artist'], a['album']))
            return _gen(), stats_text

        elif self.view_mode == "playlists":
            playlists = await db.get_all_playlists(search_query=self.search_query, sort_mode=self.sort_mode)
            suffix = " (CLOSEST MATCH)" if getattr(playlists, "is_closest", False) else ""
            stats_text = f"{len(playlists)} {'PLAYLIST' if len(playlists) == 1 else 'PLAYLISTS'}{suffix}"

            async def _gen():
                for pl in playlists:
                    node_id  = f"playlist_{pl['id']}"
                    expanded = node_id in self.expanded_nodes
                    yield self._playlist_row(pl, node_id, expanded)
                    if expanded:
                        for t in await db.get_tracks_in_playlist(pl['id']):
                            yield self._track_row(t, depth=1)
                
                if not self.search_query:
                    yield self._new_playlist_row()

            return _gen(), stats_text

        else:  # tracks
            tracks = await db.get_all_tracks(search_query=self.search_query, sort_mode=self.sort_mode)
            suffix = " (CLOSEST MATCH)" if getattr(tracks, "is_closest", False) else ""
            stats_text = f"{len(tracks)} {'TRACK' if len(tracks) == 1 else 'TRACKS'}{suffix}"
            self._tracks_cache = tracks
            self._tracks_cache_key = (self.view_mode, self.search_query, self.sort_mode)

            async def _gen():
                for t in tracks:
                    yield self._track_row(t, depth=0)
            return _gen(), stats_text

    def _on_pagination_swipe(self, e):
        """Switch pages on horizontal swipe of the pagination bar."""
        if self._is_changing_page or getattr(self, "_is_programmatic_scroll", False):
            return
        vx = getattr(e, "primary_velocity", 0) or 0
        if abs(vx) < 300:
            return
            
        if vx < 0:
            if self.current_page < self.total_pages - 1:
                self.page.run_task(self.change_page, self.current_page + 1)
        elif vx > 0:
            if self.current_page > 0:
                self.page.run_task(self.change_page, self.current_page - 1, scroll_to_bottom=True)

    def _on_list_scroll(self, e: ft.OnScrollEvent):
        if self._is_changing_page or getattr(self, "_is_programmatic_scroll", False):
            return
        if self.view_mode in ("tracks", "albums", "artists"):
            self._last_scroll_pixels = e.pixels
            return

        if self._is_loading_chunk or not self._current_gen:
            return
        if e.max_scroll_extent <= 0 or e.pixels < e.max_scroll_extent - 800:
            return

        self._is_loading_chunk = True
        token = self._load_token

        async def load_chunk():
            try:
                if self._load_token != token or not self._current_gen:
                    return
                chunk = []
                try:
                    for _ in range(50):
                        chunk.append(await anext(self._current_gen))
                        await asyncio.sleep(0)
                except StopAsyncIteration:
                    self._current_gen = None
                if chunk:
                    self.app.safe_update(lambda c=chunk: self._library_list.controls.extend(c))
            finally:
                self._is_loading_chunk = False

        asyncio.create_task(load_chunk())

    def _build_top_ghost(self) -> ft.Control:
        return ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.KEYBOARD_DOUBLE_ARROW_UP_ROUNDED, color=CYAN, size=16),
                    ft.Text("Tap here to load previous page", color=TEXT, size=11, weight=ft.FontWeight.W_500),
                    ft.Icon(ft.Icons.KEYBOARD_DOUBLE_ARROW_UP_ROUNDED, color=CYAN, size=16),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=10,
            ),
            height=48,
            alignment=ft.Alignment(0, 0),
            bgcolor=apply_opacity(0.03, CYAN),
            border=ft.Border.all(1, apply_opacity(0.08, CYAN)),
            border_radius=12,
            margin=ft.Margin.only(bottom=12),
            on_click=lambda e: self.page.run_task(self.change_page, self.current_page - 1, scroll_to_bottom=True),
        )

    def _build_bottom_ghost(self) -> ft.Control:
        return ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.KEYBOARD_DOUBLE_ARROW_DOWN_ROUNDED, color=CYAN, size=16),
                    ft.Text("Tap here to load next page", color=TEXT, size=11, weight=ft.FontWeight.W_500),
                    ft.Icon(ft.Icons.KEYBOARD_DOUBLE_ARROW_DOWN_ROUNDED, color=CYAN, size=16),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=10,
            ),
            height=48,
            alignment=ft.Alignment(0, 0),
            bgcolor=apply_opacity(0.03, CYAN),
            border=ft.Border.all(1, apply_opacity(0.08, CYAN)),
            border_radius=12,
            margin=ft.Margin.only(top=12),
            on_click=lambda e: self.page.run_task(self.change_page, self.current_page + 1, scroll_to_bottom=False),
        )

    def _update_pagination_ui(self):
        total = max(1, self.total_pages)
        self._page_label.value = f"Page {self.current_page + 1} of {total}"
        
        color = LIB_PARTITION_COLOR if self.view_mode == "partitions" else CYAN
        self._prev_page_btn.disabled = self.current_page <= 0
        self._prev_page_btn.icon_color = DIM if self.current_page <= 0 else color
        
        self._next_page_btn.disabled = self.current_page >= self.total_pages - 1
        self._next_page_btn.icon_color = DIM if self.current_page >= self.total_pages - 1 else color
        
        is_partitions_moods = (self.view_mode == "partitions" and self.partition_sub_mode == "moods")
        self._pagination_bar.visible = self.total_pages > 1 and (
            self.view_mode in ("tracks", "albums", "artists") or is_partitions_moods
        )
        self.try_update(self._pagination_bar)

    async def change_page(self, new_page: int, scroll_to_bottom: bool = False):
        if self._is_changing_page or new_page < 0 or new_page >= self.total_pages:
            return
        
        self._is_changing_page = True
        try:
            is_forward = new_page > self.current_page
            exit_offset = ft.Offset(-0.15, 0) if is_forward else ft.Offset(0.15, 0)
            entry_offset = ft.Offset(0.15, 0) if is_forward else ft.Offset(-0.15, 0)
            
            self._animated_list_wrapper.offset = exit_offset
            self._animated_list_wrapper.opacity = 0.0
            self.try_update(self._animated_list_wrapper)
            
            await asyncio.sleep(0.08)
            
            self.current_page = new_page
            
            start_idx = self.current_page * self.items_per_page
            end_idx = start_idx + self.items_per_page
            page_items = self._flat_rows[start_idx:end_idx]
            
            controls = []
            self._path_to_controls.clear()
            
            if self.current_page > 0:
                controls.append(self._build_top_ghost())
                
            if self.view_mode == "tracks":
                for item in page_items:
                    controls.append(self._track_row(item["data"], item["depth"]))
            elif self.view_mode == "albums":
                db = self.app.db_manager
                for item in page_items:
                    a = item["data"]
                    node_id = f"album_{a['artist']}_{a['album']}"
                    expanded = node_id in self.expanded_nodes
                    controls.append(self._album_row(a, node_id, expanded, depth=0))
                    if expanded:
                        sub_tracks = await db.get_tracks_by_album(a['album'], a['artist'])
                        for t in sub_tracks:
                            controls.append(self._track_row(t, depth=1, album_context=(a['artist'], a['album'])))
            elif self.view_mode == "artists":
                db = self.app.db_manager
                for item in page_items:
                    a = item["data"]
                    node_id = f"artist_{a['name']}"
                    expanded = node_id in self.expanded_nodes
                    controls.append(self._artist_row(a, node_id, expanded))
                    if expanded:
                        sub_albums = await db.get_albums_by_artist(a['name'])
                        for al in sub_albums:
                            alb_id = f"album_{al['artist']}_{al['album']}"
                            alb_exp = alb_id in self.expanded_nodes
                            controls.append(self._album_row(al, alb_id, alb_exp, depth=1))
                            if alb_exp:
                                sub_tracks = await db.get_tracks_by_album(al['album'], al['artist'])
                                for t in sub_tracks:
                                    controls.append(self._track_row(t, depth=2, album_context=(al['artist'], al['album'])))
            elif self.view_mode == "partitions" and self.partition_sub_mode == "moods":
                for item in page_items:
                    controls.append(self._build_partition_track_row(item["data"], item["tracks"], depth=0))
                
            if self.current_page < self.total_pages - 1:
                controls.append(self._build_bottom_ghost())
                    
            self._library_list.controls = controls
            self._update_pagination_ui()
            
            self._animated_list_wrapper.offset = entry_offset
            self.try_update(self._animated_list_wrapper, self._library_list)
            
            await asyncio.sleep(0.04)
            
            self._is_programmatic_scroll = True
            try:
                if scroll_to_bottom:
                    target_offset = 3080 if self.current_page < self.total_pages - 1 else 3250
                else:
                    target_offset = 45 if self.current_page > 0 else 0
                await self._library_list.scroll_to(offset=target_offset, duration=0)
                self._last_scroll_pixels = target_offset
            except Exception:
                pass
            finally:
                await asyncio.sleep(0.03)
                self._is_programmatic_scroll = False
                
            self._animated_list_wrapper.offset = ft.Offset(0, 0)
            self._animated_list_wrapper.opacity = 1.0
            self.try_update(self._animated_list_wrapper)
        finally:
            await asyncio.sleep(0.15)
            self._is_changing_page = False

    async def load_library(self):
        """Rebuild the library list using an async generator."""
        self._load_token += 1
        token = self._load_token
        self._current_gen = None
        self._is_loading_chunk = False
        
        self.current_page = 0
        self._tracks_cache = None
        self._tracks_cache_key = None

        if not (self.view_mode == "partitions" and self.partition_sub_mode == "moods"):
            old_content = self._animated_list_wrapper.content
            self._animated_list_wrapper.content = self._library_list
            if old_content != self._library_list:
                self.try_update(self._animated_list_wrapper)

        if self.view_mode == "partitions":
            self._cached_islets = None
            self._cached_moods = None
            self._cached_unanalysed = None

        self._last_highlighted_path = audio_engine.current_path or None

        self._search_spinner.visible = True
        self._library_list.controls.clear()
        self._path_to_controls.clear()
        self._empty_label.visible = False
        self._pagination_bar.visible = False
        self._mood_pagination_bar.visible = False
        
        self.try_update(
            self._search_spinner,
            self._library_list,
            self._empty_label,
            self._pagination_bar,
            self._mood_pagination_bar,
        )

        try:
            if self.view_mode in ("tracks", "albums", "artists"):
                db = self.app.db_manager
                
                if self.view_mode == "tracks":
                    tracks = await db.get_all_tracks(search_query=self.search_query, sort_mode=self.sort_mode)
                    self._tracks_cache = tracks
                    self._tracks_cache_key = (self.view_mode, self.search_query, self.sort_mode)
                    self._flat_rows = [{"type": "track", "data": t, "depth": 0} for t in tracks]
                    suffix = " (CLOSEST MATCH)" if getattr(tracks, "is_closest", False) else ""
                    stats_text = f"{len(tracks)} {'TRACK' if len(tracks) == 1 else 'TRACKS'}{suffix}"
                elif self.view_mode == "albums":
                    albums = await db.get_all_albums(search_query=self.search_query, sort_mode=self.sort_mode)
                    self._flat_rows = [{"type": "album", "data": a, "depth": 0} for a in albums]
                    suffix = " (CLOSEST MATCH)" if getattr(albums, "is_closest", False) else ""
                    stats_text = f"{len(albums)} {'ALBUM' if len(albums) == 1 else 'ALBUMS'}{suffix}"
                else:
                    artists = await db.get_all_artists(search_query=self.search_query, sort_mode=self.sort_mode)
                    self._flat_rows = [{"type": "artist", "data": a, "depth": 0} for a in artists]
                    suffix = " (CLOSEST MATCH)" if getattr(artists, "is_closest", False) else ""
                    stats_text = f"{len(artists)} {'ARTIST' if len(artists) == 1 else 'ARTISTS'}{suffix}"
                
                self.total_pages = math.ceil(len(self._flat_rows) / self.items_per_page)
                
                if self._load_token != token:
                    return

                start_idx = self.current_page * self.items_per_page
                end_idx = start_idx + self.items_per_page
                page_items = self._flat_rows[start_idx:end_idx]
                
                first_chunk = []
                if self.current_page > 0:
                    first_chunk.append(self._build_top_ghost())
                    
                if self.view_mode == "tracks":
                    for item in page_items:
                        first_chunk.append(self._track_row(item["data"], item["depth"]))
                elif self.view_mode == "albums":
                    for item in page_items:
                        a = item["data"]
                        node_id = f"album_{a['artist']}_{a['album']}"
                        expanded = node_id in self.expanded_nodes
                        first_chunk.append(self._album_row(a, node_id, expanded, depth=0))
                        if expanded:
                            sub_tracks = await db.get_tracks_by_album(a['album'], a['artist'])
                            for t in sub_tracks:
                                first_chunk.append(self._track_row(t, depth=1, album_context=(a['artist'], a['album'])))
                else:
                    for item in page_items:
                        a = item["data"]
                        node_id = f"artist_{a['name']}"
                        expanded = node_id in self.expanded_nodes
                        first_chunk.append(self._artist_row(a, node_id, expanded))
                        if expanded:
                            sub_albums = await db.get_albums_by_artist(a['name'])
                            for al in sub_albums:
                                alb_id = f"album_{al['artist']}_{al['album']}"
                                alb_exp = alb_id in self.expanded_nodes
                                first_chunk.append(self._album_row(al, alb_id, alb_exp, depth=1))
                                if alb_exp:
                                    sub_tracks = await db.get_tracks_by_album(al['album'], al['artist'])
                                    for t in sub_tracks:
                                        first_chunk.append(self._track_row(t, depth=2, album_context=(al['artist'], al['album'])))
                    
                if self.current_page < self.total_pages - 1:
                    first_chunk.append(self._build_bottom_ghost())

                def finalize_paginated():
                    self._stats_label.text = stats_text
                    self._library_list.controls.extend(first_chunk)
                    self._search_spinner.visible = False
                    
                    is_empty = not first_chunk
                    if is_empty:
                        self._empty_label.visible = True
                        self._empty_label.content.controls[0].name = ft.Icons.LIBRARY_MUSIC_OUTLINED
                        self._empty_label.content.controls[0].color = apply_opacity(0.3, CYAN)
                        self._empty_label.content.controls[1].value = "It's empty in here."
                        self._empty_label.content.controls[2].value = "Index your folders to start listening."
                        # reset action button to "ENTER PATHS"
                        self._empty_label.content.controls[3].visible = True
                        self._empty_label.content.controls[4].visible = True
                        self._empty_label.content.controls[4].content.controls[0].name = ft.Icons.SETTINGS_ROUNDED
                        self._empty_label.content.controls[4].content.controls[1].value = "ENTER PATHS"
                        self._empty_label.content.controls[4].on_click = self._on_enter_paths_click
                        self._empty_label.content.controls[4].style = ft.ButtonStyle(color=CYAN)
                    
                    old_content = self._animated_list_wrapper.content
                    self._animated_list_wrapper.content = self._library_list
                    if old_content != self._library_list:
                        self._animated_list_wrapper.update()
                    
                    self._update_pagination_ui()
                    self.page.update()

                self.app.safe_update(finalize_paginated)

            elif self.view_mode == "partitions":
                import numpy as np
                from utils import track_graph as tg
                db = self.app.db_manager
                
                saved_partitions = await db.get_saved_partitions()
                self._has_partitions = bool(saved_partitions)
                self._mood_feedback_map = await db.get_mood_feedback()
                
                if self._cached_moods is None or self._cached_islets is None:
                    all_tracks = await db.get_all_tracks()
                    all_paths_to_track = {t["path"]: t for t in all_tracks}

                    cached_moods = {mood: [] for mood in tg.MOODS.keys()}
                    cached_unanalysed = []

                    if saved_partitions:
                        for t in all_tracks:
                            path = t["path"]
                            if path in saved_partitions:
                                mood = saved_partitions[path].get("mood")
                                if mood in cached_moods:
                                    cached_moods[mood].append(t)
                            else:
                                cached_unanalysed.append(t)
                    else:
                        cached_unanalysed = list(all_tracks)

                    cached_islets = {}
                    for islet_name in tg.list_islets():
                        members = await tg.tracks_in_islet(db, islet_name, min_count=0)
                        members = [m for m in members if m.get("path") in all_paths_to_track]
                        cached_islets[islet_name] = members

                    if self._load_token != token:
                        return

                    self._cached_moods = cached_moods
                    self._cached_unanalysed = cached_unanalysed
                    self._cached_islets = cached_islets

                if self.search_query:
                    sq = self.search_query.lower()
                    def matches_query(t):
                        return (
                            sq in (t.get("title") or "").lower() or
                            sq in (t.get("artist") or "").lower() or
                            sq in (t.get("album") or "").lower() or
                            sq in (t.get("path") or "").lower()
                        )
                else:
                    def matches_query(t):
                        return True

                first_chunk = []
                total_searched_count = 0
                
                if not saved_partitions:
                    setup_card = ft.Container(
                        content=ft.Column(
                            [
                                ft.Container(
                                    content=ft.Icon(ft.Icons.DIVERSITY_3_ROUNDED, color=LIB_PARTITION_COLOR, size=40),
                                    bgcolor=apply_opacity(0.1, LIB_PARTITION_COLOR),
                                    border_radius=20,
                                    padding=16,
                                ),
                                ft.Text("Sonic Library Moods", color=TEXT, size=18, weight=ft.FontWeight.W_700, text_align=ft.TextAlign.CENTER),
                                ft.Text(
                                    "Analyze your library's DSP features to segment your music "
                                    "collection into cohesive Default and Custom moods.",
                                    color=DIM, size=13, text_align=ft.TextAlign.CENTER, max_lines=3,
                                ),
                                ft.Container(height=8),
                                ft.Button(
                                    "Generate Moods",
                                    icon=ft.Icons.AUTO_AWESOME_ROUNDED,
                                    color=BG,
                                    bgcolor=LIB_PARTITION_COLOR,
                                    on_click=lambda _: self.page.run_task(self.recalculate_partitions_worker),
                                    style=ft.ButtonStyle(
                                        shape=ft.RoundedRectangleBorder(radius=10),
                                        padding=ft.Padding.symmetric(horizontal=24, vertical=12),
                                    )
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.CENTER,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=12,
                        ),
                        bgcolor="#0DFFFFFF",
                        border=ft.Border.all(1, apply_opacity(0.1, TEXT)),
                        border_radius=16,
                        padding=32,
                        margin=ft.Margin.symmetric(horizontal=16, vertical=24),
                        alignment=ft.Alignment(0, 0),
                    )
                    first_chunk.append(setup_card)
                    stats_text = "0 TRACKS"
                else:
                    if self.partition_sub_mode == "moods":
                        active_moods = []
                        mood_icons = {
                            "chill": ft.Icons.SPA_ROUNDED,
                            "dark": ft.Icons.NIGHTLIGHT_ROUNDED,
                            "upbeat": ft.Icons.CELEBRATION_ROUNDED,
                            "rock": ft.Icons.FESTIVAL_ROUNDED,
                            "beats": ft.Icons.SPEAKER_ROUNDED,
                            "intense": ft.Icons.WHATSHOT_ROUNDED,
                        }
                        
                        for mood in tg.MOODS.keys():
                            tracks = (self._cached_moods or {}).get(mood, [])
                            filtered_tracks = [t for t in tracks if matches_query(t)]
                            active_moods.append((mood, filtered_tracks))
                            total_searched_count += len(filtered_tracks)
                                
                        active_moods.sort(key=lambda x: len(x[1]), reverse=True)
                        
                        unanalysed_searched = [t for t in (self._cached_unanalysed or []) if matches_query(t)]
                        if unanalysed_searched:
                            total_searched_count += len(unanalysed_searched)
                            
                        active_sections = []
                        for mood, tracks in active_moods:
                            icon = mood_icons.get(mood.lower(), ft.Icons.EMOJI_EMOTIONS_ROUNDED)
                            active_sections.append((mood.capitalize(), tracks, icon))
                        if unanalysed_searched:
                            active_sections.append(("Unanalysed Tracks", unanalysed_searched, ft.Icons.HELP_OUTLINE_ROUNDED))
                            
                        if active_sections:
                            if self.selected_mood_index >= len(active_sections):
                                self.selected_mood_index = 0
                            elif self.selected_mood_index < 0:
                                self.selected_mood_index = max(0, len(active_sections) - 1)
                                
                            sec_title, sec_tracks, sec_icon = active_sections[self.selected_mood_index]
                            self._mood_label.value = sec_title
                            
                            self._flat_rows = [{"type": "partition_track", "data": t, "tracks": sec_tracks, "depth": 0} for t in sec_tracks][:35]
                            self.total_pages = 1
                            self.current_page = 0
                            self._mood_pagination_bar.visible = False
                            
                            if hasattr(self, "_mood_wheel_list") and self._mood_wheel_list is not None and len(self._mood_wheel_list.controls) == len(active_sections) + 1:
                                for idx, (title, tracks, icon) in enumerate(active_sections):
                                    is_selected = (idx == self.selected_mood_index)
                                    accent = LIB_PARTITION_COLOR if is_selected else DIM
                                    
                                    chip = self._mood_wheel_list.controls[idx + 1]
                                    container = chip.content
                                    column = container.content
                                    
                                    column.controls[0].name = icon
                                    column.controls[0].color = accent
                                    
                                    short_title = title.split()[0]
                                    column.controls[1].value = short_title
                                    column.controls[1].color = accent
                                    
                                    container.bgcolor = apply_opacity(0.12, LIB_PARTITION_COLOR) if is_selected else "transparent"
                                    container.border = ft.Border.all(1.5, LIB_PARTITION_COLOR if is_selected else apply_opacity(0.15, TEXT))
                                    chip.on_tap = lambda _e, index=idx: self._select_mood_index(index)
                            else:
                                wheel_controls = []
                                
                                eq_btn = ft.GestureDetector(
                                    content=ft.Container(
                                        content=ft.Column(
                                            [
                                                ft.Icon(ft.Icons.TUNE_ROUNDED, color=CYAN, size=18),
                                                ft.Text("TUNE", size=8.5, weight=ft.FontWeight.W_700, color=CYAN, text_align=ft.TextAlign.CENTER, no_wrap=True),
                                            ],
                                            alignment=ft.MainAxisAlignment.CENTER,
                                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                            spacing=2,
                                        ),
                                        width=58, height=58, border_radius=29,
                                        bgcolor="transparent", border=ft.Border.all(1.5, apply_opacity(0.4, CYAN)),
                                        padding=4, animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
                                    ),
                                    on_tap=lambda _e: self.page.run_task(self._open_mood_eq_dialog),
                                )
                                wheel_controls.append(eq_btn)
                                
                                for idx, (title, tracks, icon) in enumerate(active_sections):
                                    is_selected = (idx == self.selected_mood_index)
                                    accent = LIB_PARTITION_COLOR if is_selected else DIM
                                    short_title = title.split()[0]
                                    
                                    chip = ft.GestureDetector(
                                        content=ft.Container(
                                            content=ft.Column(
                                                [
                                                    ft.Icon(icon, color=accent, size=18),
                                                    ft.Text(short_title, size=8.5, weight=ft.FontWeight.W_700, color=accent, text_align=ft.TextAlign.CENTER, no_wrap=True),
                                                ],
                                                alignment=ft.MainAxisAlignment.CENTER,
                                                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                                spacing=2,
                                            ),
                                            width=58, height=58, border_radius=29,
                                            bgcolor=apply_opacity(0.12, LIB_PARTITION_COLOR) if is_selected else "transparent",
                                            border=ft.Border.all(1.5, LIB_PARTITION_COLOR if is_selected else apply_opacity(0.15, TEXT)),
                                            padding=4, animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
                                        ),
                                        on_tap=lambda _e, index=idx: self._select_mood_index(index),
                                    )
                                    wheel_controls.append(chip)
                                    
                                if not hasattr(self, "_mood_wheel_list") or self._mood_wheel_list is None:
                                    self._mood_wheel_list = ft.ListView(
                                        spacing=12, width=68,
                                        padding=ft.Padding.only(left=2, right=2, top=6, bottom=20),
                                    )
                                self._mood_wheel_list.controls = wheel_controls
                            
                            if self._flat_rows:
                                for item in self._flat_rows:
                                    first_chunk.append(self._build_partition_track_row(item["data"], item["tracks"], depth=0))
                            else:
                                first_chunk.append(
                                    ft.Container(
                                        content=ft.Column(
                                            [
                                                ft.Icon(ft.Icons.MUSIC_NOTE_ROUNDED, color=apply_opacity(0.3, LIB_PARTITION_COLOR), size=32),
                                                ft.Text(f"No tracks assigned to {sec_title}", color=DIM, size=13, weight=ft.FontWeight.W_600),
                                                ft.Text("Analyze more tracks or adjust liked feedback.", color=apply_opacity(0.5, TEXT), size=11),
                                            ],
                                            alignment=ft.MainAxisAlignment.CENTER,
                                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                            spacing=6,
                                        ),
                                        padding=32, alignment=ft.Alignment(0, 0), expand=True,
                                    )
                                )
                        else:
                            self._flat_rows = []
                            self.total_pages = 1
                            self._mood_pagination_bar.visible = False
                            
                    else:
                        self._mood_pagination_bar.visible = False
                        named_islets = []
                        for name, member_tracks in (self._cached_islets or {}).items():
                            filtered = [t for t in member_tracks if matches_query(t)]
                            if filtered or (not self.search_query and not member_tracks):
                                named_islets.append((name, filtered))
                                total_searched_count += len(filtered)

                        named_islets.sort(key=lambda x: len(x[1]), reverse=True)

                        for name, member_tracks in named_islets:
                            if member_tracks:
                                artists = [t.get("artist") or "Unknown" for t in member_tracks]
                                dominant_artist = Counter(artists).most_common(1)[0][0]
                                subtitle = f"{len(member_tracks)} tracks · Featuring {dominant_artist}"
                            else:
                                subtitle = "0 tracks · threshold too tight — edit to loosen"
                            
                            rendered_tracks = member_tracks[:35]
                            content_controls = [
                                self._build_partition_track_row(t, member_tracks, depth=1, islet_name=name)
                                for t in rendered_tracks
                            ]
                            if len(member_tracks) > 35:
                                remaining = len(member_tracks) - 35
                                content_controls.append(
                                    ft.Container(
                                        content=ft.Text(f"+ {remaining} more tracks in this custom islet", size=11, color=DIM, italic=True),
                                        padding=ft.Padding.only(left=24, top=6, bottom=6),
                                    )
                                )
                                
                            edit_btn = ft.IconButton(
                                icon=ft.Icons.EDIT_OUTLINED, icon_color=DIM, icon_size=18, tooltip="Edit islet",
                                on_click=lambda _e, n=name: self._open_edit_islet_dialog(n),
                            )
                            del_btn = ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE, icon_color=DIM, icon_size=18, tooltip="Delete islet",
                                on_click=lambda _e, n=name: self._confirm_delete_islet(n),
                            )
                            node_id = f"islet_{name.lower().strip()}"
                            initially_open = node_id in self.expanded_nodes
                            
                            def make_toggle_cb(nid):
                                return lambda open_state: (
                                    self.expanded_nodes.add(nid) if open_state else self.expanded_nodes.discard(nid)
                                )

                            accordion = AccordionCard(
                                icon=ft.Icons.DIVERSITY_3_ROUNDED,
                                title=name.title(),
                                subtitle=subtitle,
                                content_controls=content_controls,
                                header_actions=[edit_btn, del_btn],
                                initially_open=initially_open,
                                on_toggle=make_toggle_cb(node_id),
                            )
                            first_chunk.append(accordion)

                        if not (self._cached_islets or {}):
                            first_chunk.append(self._build_islet_empty_state())
                            
                    stats_text = f"{total_searched_count} TRACKS"
                
                if self._load_token != token:
                    return
                    
                def finalize_partitions():
                    self._stats_label.text = stats_text
                    self._library_list.controls.extend(first_chunk)
                    self._search_spinner.visible = False
                    
                    is_empty = not first_chunk
                    if is_empty:
                        self._empty_label.visible = True
                        self._empty_label.content.controls[0].name = ft.Icons.LIBRARY_MUSIC_OUTLINED
                        self._empty_label.content.controls[0].color = apply_opacity(0.3, LIB_PARTITION_COLOR)
                        self._empty_label.content.controls[1].value = "No partition results found."
                        self._empty_label.content.controls[2].value = "Try checking your filters or search query."
                        # Hide action button and its spacing container
                        self._empty_label.content.controls[3].visible = False
                        self._empty_label.content.controls[4].visible = False
                    
                    if self.partition_sub_mode == "moods" and getattr(self, "_has_partitions", False) and not is_empty:
                        is_row_already_set = (
                            isinstance(self._animated_list_wrapper.content, ft.Row) and
                            len(self._animated_list_wrapper.content.controls) == 2 and
                            hasattr(self, "_mood_wheel_list") and
                            self._mood_wheel_list is not None and
                            self._animated_list_wrapper.content.controls[0].content == self._mood_wheel_list
                        )
                        if not is_row_already_set:
                            self._animated_list_wrapper.content = ft.Row(
                                [
                                    ft.Container(
                                        content=self._mood_wheel_list,
                                        border=ft.Border(right=ft.BorderSide(1, apply_opacity(0.1, TEXT))),
                                        padding=ft.Padding.only(right=6),
                                    ),
                                    self._library_list
                                ],
                                spacing=6, expand=True,
                            )
                            self._animated_list_wrapper.update()
                        else:
                            self._mood_wheel_list.update()
                            self._library_list.update()
                    else:
                        old_content = self._animated_list_wrapper.content
                        self._animated_list_wrapper.content = self._library_list
                        if old_content != self._library_list:
                            self._animated_list_wrapper.update()
                        else:
                            self._library_list.update()
                    
                    self._stats_label.update()
                    self._search_spinner.update()
                    if self._empty_label.visible:
                        self._empty_label.update()
                    self._update_pagination_ui()
                    
                finalize_partitions()

            else:
                self.total_pages = 1
                gen, stats_text = await self._build_rows_generator()

                first_chunk = []
                async for item in gen:
                    first_chunk.append(item)
                    if len(first_chunk) >= 100:
                        break

                if self._load_token != token:
                    return

                self._current_gen = gen

                def finalize():
                    self._stats_label.text = stats_text
                    self._library_list.controls.extend(first_chunk)
                    self._search_spinner.visible = False
                    
                    is_empty = not first_chunk or (self.view_mode == "playlists" and len(first_chunk) == 1)
                    
                    if is_empty:
                        self._empty_label.visible = True
                        if self.view_mode == "playlists":
                            self._empty_label.content.controls[0].name = ft.Icons.QUEUE_MUSIC_ROUNDED
                            self._empty_label.content.controls[0].color = apply_opacity(0.3, LIB_PLAYLIST_COLOR)
                            self._empty_label.content.controls[1].value = "No playlists yet."
                            self._empty_label.content.controls[2].value = "Create your first playlist below."
                            # Show action button as "CREATE PLAYLIST"
                            self._empty_label.content.controls[3].visible = True
                            self._empty_label.content.controls[4].visible = True
                            self._empty_label.content.controls[4].content.controls[0].name = ft.Icons.ADD_ROUNDED
                            self._empty_label.content.controls[4].content.controls[1].value = "CREATE PLAYLIST"
                            self._empty_label.content.controls[4].on_click = lambda e: self._create_playlist_dialog()
                            self._empty_label.content.controls[4].style = ft.ButtonStyle(color=LIB_PLAYLIST_COLOR)
                        else:
                            self._empty_label.content.controls[0].name = ft.Icons.LIBRARY_MUSIC_OUTLINED
                            self._empty_label.content.controls[0].color = apply_opacity(0.3, CYAN)
                            self._empty_label.content.controls[1].value = "It's empty in here."
                            self._empty_label.content.controls[2].value = "Index your folders to start listening."
                            # Reset action button to "ENTER PATHS"
                            self._empty_label.content.controls[3].visible = True
                            self._empty_label.content.controls[4].visible = True
                            self._empty_label.content.controls[4].content.controls[0].name = ft.Icons.SETTINGS_ROUNDED
                            self._empty_label.content.controls[4].content.controls[1].value = "ENTER PATHS"
                            self._empty_label.content.controls[4].on_click = self._on_enter_paths_click
                            self._empty_label.content.controls[4].style = ft.ButtonStyle(color=CYAN)
                    
                    old_content = self._animated_list_wrapper.content
                    self._animated_list_wrapper.content = self._library_list
                    if old_content != self._library_list:
                        self._animated_list_wrapper.update()
                    
                    self.page.update()

                self.app.safe_update(finalize)

        except Exception as e:
            logger.error(f"Library load failed: {e}")
            self.app.safe_update(lambda: setattr(self._search_spinner, "visible", False))

    async def _toggle_node(self, nid: str, ctrl: ft.Control):
        if nid in self._toggling_nodes: return
        self._toggling_nodes.add(nid)
        
        try:
            node_data = getattr(ctrl, "data", {})
            node_type = node_data.get("type")
            depth     = node_data.get("depth", 0)
            expanding = nid not in self.expanded_nodes
            
            db = self.app.db_manager
            new_rows = []
            if expanding:
                if node_type == "artist":
                    res = await db.get_albums_by_artist(node_data.get("name", ""))
                    new_rows = [self._album_row(a, f"album_{a['artist']}_{a['album']}", False, depth + 1) for a in res]
                elif node_type == "album":
                    alb  = node_data.get("album", "")
                    arti = node_data.get("artist", "")
                    res  = await db.get_tracks_by_album(alb, arti)
                    new_rows = [
                        self._track_row(t, depth + 1, album_context=(arti, alb))
                        for t in res
                    ]
                elif node_type == "playlist":
                    pl_id = node_data.get("playlist_id", 0)
                    res = await db.get_tracks_in_playlist(pl_id)
                    if res:
                        new_rows = [self._track_row(t, depth + 1, playlist_id=pl_id) for t in res]
                    else:
                        new_rows = [self._empty_playlist_widget(depth + 1)]

            def _mutate():
                controls = self._library_list.controls
                try:
                    idx = controls.index(ctrl)
                except ValueError: return

                if expanding:
                    if idx + 1 < len(controls):
                        next_child = controls[idx + 1]
                        next_data = getattr(next_child, "data", {}) or {}
                        if next_data.get("depth", 0) > depth:
                            self.expanded_nodes.add(nid)
                            return

                    self.expanded_nodes.add(nid)
                    for i, row in enumerate(new_rows):
                        controls.insert(idx + 1 + i, row)
                    
                    accent = {
                        "artist": LIB_ARTIST_COLOR,
                        "album": LIB_ALBUM_COLOR,
                        "playlist": LIB_PLAYLIST_COLOR
                    }.get(node_type, CYAN)
                    ctrl.bgcolor = apply_opacity(0.07 if node_type == "artist" else 0.06, accent)
                    
                    icon = getattr(ctrl, "_chevron", None)
                    if icon:
                        icon.rotate = ft.Rotate(1.57)
                        icon.color = accent
                        try: icon.update()
                        except: pass
                else:
                    self.expanded_nodes.discard(nid)
                    while idx + 1 < len(controls):
                        child = controls[idx + 1]
                        cdata = getattr(child, "data", {}) or {}
                        if cdata.get("depth", 0) > depth:
                            cnid = cdata.get("node_id")
                            if cnid: self.expanded_nodes.discard(cnid)
                            controls.pop(idx + 1)
                        else:
                            break
                    ctrl.bgcolor = "transparent"
                    
                    icon = getattr(ctrl, "_chevron", None)
                    if icon:
                        icon.rotate = ft.Rotate(0)
                        icon.color = DIM
                        try: icon.update()
                        except: pass
                
                try:
                    ctrl.update()
                    self._library_list.update()
                except:
                    pass
                
            self.app.safe_update(_mutate)
        finally:
            self._toggling_nodes.discard(nid)

    def _artist_row(self, a: dict, node_id: str, expanded: bool) -> ft.Control:
        name = a.get("name") or "Unknown Artist"
        tc   = a.get("track_count", 0)
        ac   = a.get("album_count", 0)
        sub  = f"{ac} albums  ·  {tc} tracks"
        accent = LIB_ARTIST_COLOR
        Che = ft.Icon(
            ft.Icons.KEYBOARD_ARROW_RIGHT, 
            color=accent if expanded else DIM,
            rotate=ft.Rotate(1.57) if expanded else ft.Rotate(0),
            animate_rotation=ft.Animation(200, ft.AnimationCurve.DECELERATE),
            data="chevron"
        )
        tile = ft.ListTile(
            data={"node_id": node_id, "depth": 0, "type": "artist", "name": name},
            leading=ft.Icon(ft.Icons.PERSON_ROUNDED, color=accent),
            title=ft.Text(name, color=TEXT, size=14, weight=ft.FontWeight.W_600, max_lines=3),
            subtitle=ft.Text(sub, color=DIM, size=12, max_lines=2),
            trailing=ft.Row(
                [
                    self._edit_btn("artist", {"artist_name": name}),
                    Che,
                ],
                tight=True, spacing=0,
            ),
            bgcolor=apply_opacity(0.07, accent) if expanded else "transparent",
        )
        tile._chevron = Che
        tile.on_click = lambda e: self.page.run_task(self._toggle_node, node_id, tile)
        return tile

    def _album_row(self, a: dict, node_id: str, expanded: bool, depth: int = 0) -> ft.Control:
        album  = a.get("album") or "Unknown Album"
        artist = a.get("artist") or "Unknown Artist"
        tc     = a.get("track_count", "?")
        accent = LIB_ALBUM_COLOR

        Che = ft.Icon(
            ft.Icons.KEYBOARD_ARROW_RIGHT, 
            color=accent if expanded else DIM,
            rotate=ft.Rotate(1.57) if expanded else ft.Rotate(0),
            animate_rotation=ft.Animation(200, ft.AnimationCurve.DECELERATE),
            data="chevron"
        )
        meta = {"artist_name": artist, "album_title": album}
        tile = ft.ListTile(
            data={"node_id": node_id, "depth": depth, "type": "album", "album": album, "artist": artist},
            leading=ft.Row(
                [
                    ft.Container(width=depth * 20, visible=depth > 0),
                    ft.Icon(ft.Icons.ALBUM_ROUNDED, color=accent),
                ],
                tight=True
            ),
            title=ft.Text(album, color=TEXT, size=14, weight=ft.FontWeight.W_600, max_lines=3),
            subtitle=ft.Text(f"{artist}  ·  {tc} tracks", color=DIM, size=12, max_lines=2),
            trailing=ft.Row(
                [
                    self._edit_btn("album", meta),
                    Che,
                ],
                tight=True, spacing=0,
            ),
            bgcolor=apply_opacity(0.06, accent) if expanded else "transparent",
        )
        tile._chevron = Che
        tile.on_click = lambda e: self.page.run_task(self._toggle_node, node_id, tile)
        return tile

    def _new_playlist_row(self) -> ft.Control:
        accent = LIB_PLAYLIST_COLOR
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.ADD_CIRCLE_OUTLINE_ROUNDED, color=accent),
            title=ft.Text("Create New Playlist", color=accent, weight=ft.FontWeight.W_800, size=14),
            subtitle=ft.Text("Organize your music collection", color=DIM, size=12),
            on_click=lambda e: self._create_playlist_dialog(),
            bgcolor=apply_opacity(0.05, accent)
        )

    def _create_playlist_dialog(self):
        name_field = ft.TextField(
            label="Playlist Name",
            hint_text="ex. Mai An",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=LIB_PLAYLIST_COLOR,
            text_style=ft.TextStyle(color=TEXT),
            autofocus=True,
        )

        def on_create(e):
            async def _do():
                name = name_field.value.strip()
                if not name: return
                self.dlg.open = False
                self.page.update()
                try:
                    await self.app.db_manager.create_playlist(name)
                    await self.load_library()
                    self.app.show_snackbar(f"Playlist '{name}' created!", icon=ft.Icons.CHECK_CIRCLE_OUTLINE, color=LIB_PLAYLIST_COLOR)
                except Exception as ex:
                    self.app.show_snackbar(f"Failed: {ex}")
            self.page.run_task(_do)

        self.dlg = ft.AlertDialog(
            title=ft.Text("New Playlist"),
            content=ft.Container(content=name_field, padding=ft.Padding.only(top=10)),
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: setattr(self.dlg, "open", False) or self.page.update()),
                ft.TextButton("Create", on_click=on_create),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.overlay.append(self.dlg)
        self.dlg.open = True
        self.page.update()

    def _playlist_row(self, pl: dict, node_id: str, expanded: bool) -> ft.Control:
        name   = pl.get("name") or "Untitled Playlist"
        pl_id  = pl.get("id", 0)
        tc     = pl.get("track_count", 0)
        accent = LIB_PLAYLIST_COLOR

        def _confirm_delete(_e):
            async def _do():
                await self.app.db_manager.delete_playlist(pl_id)
                await self.load_library()
                self.app.show_snackbar(f"'{name}' deleted.", icon=ft.Icons.DELETE_OUTLINE)
            self.page.run_task(_do)

        def _edit_pl(_e):
            self.app.playlist_editor.open(pl_id, name, pl.get("color") or LIB_PLAYLIST_COLOR)

        Che = ft.Icon(
            ft.Icons.KEYBOARD_ARROW_RIGHT,
            color=(pl.get("color") or accent) if expanded else DIM,
            rotate=ft.Rotate(1.57) if expanded else ft.Rotate(0),
            animate_rotation=ft.Animation(200, ft.AnimationCurve.DECELERATE),
            data="chevron"
        )
        tile = ft.ListTile(
            data={"node_id": node_id, "depth": 0, "type": "playlist", "playlist_id": pl_id, "name": name},
            leading=ft.Icon(ft.Icons.QUEUE_MUSIC_ROUNDED, color=pl.get("color") or accent),
            title=ft.Text(name, color=TEXT, size=14, weight=ft.FontWeight.W_600, max_lines=3),
            subtitle=ft.Text(f"{tc} {'track' if tc == 1 else 'tracks'}", color=DIM, size=12, max_lines=2),
            trailing=ft.Row(
                [
                    ft.IconButton(
                        icon=ft.Icons.EDIT_OUTLINED,
                        icon_color=apply_opacity(0.6, accent),
                        icon_size=16,
                        on_click=_edit_pl,
                    ),
                    Che,
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE,
                        icon_color=apply_opacity(0.5, "#FF5555"),
                        icon_size=16,
                        tooltip="Delete playlist",
                        on_click=_confirm_delete,
                    ),
                ],
                tight=True, spacing=0,
            ),
            bgcolor=apply_opacity(0.06, pl.get("color") or accent) if expanded else "transparent",
        )
        tile._chevron = Che
        tile.on_click = lambda e: self.page.run_task(self._toggle_node, node_id, tile)
        return tile

    def _get_tile(self, ctrl: ft.Control) -> ft.ListTile | None:
        tile = ctrl
        while tile and not isinstance(tile, ft.ListTile):
            tile = getattr(tile, "content", None)
        return tile

    def _update_row_highlight(self, ctrl: ft.Control, is_current: bool) -> bool:
        try:
            tile = self._get_tile(ctrl)
            if not tile:
                return False
            active_color = apply_opacity(0.1, CYAN)
            
            icon = tile.leading.controls[1]
            icon.name = ft.Icons.EQUALIZER if is_current else ft.Icons.MUSIC_NOTE_ROUNDED
            icon.color = CYAN if is_current else LIB_TRACK_COLOR
            
            if isinstance(tile.title, ft.Row):
                tile.title.controls[0].color = CYAN if is_current else TEXT
                if len(tile.title.controls) > 1:
                    tile.title.controls[1].bgcolor = CYAN if is_current else DIM
            else:
                tile.title.color = CYAN if is_current else TEXT

            if isinstance(tile.subtitle, ft.Column):
                tile.subtitle.controls[0].color = CYAN if is_current else DIM
            else:
                tile.subtitle.color = CYAN if is_current else DIM
                
            tile.bgcolor = active_color if is_current else "transparent"
            
            if tile.page:
                tile.update()
            return True
        except Exception:
            return False

    def refresh_now_playing(self):
        """Optimized highlight update using the path map."""
        if self.app.is_background:
            return

        current_path = audio_engine.current_path
        prev_path = getattr(self, "_last_highlighted_path", None)
        if prev_path == current_path:
            return

        if prev_path:
            for ctrl in self._path_to_controls.get(prev_path, []):
                self._update_row_highlight(ctrl, is_current=False)
        
        if current_path:
            for ctrl in self._path_to_controls.get(current_path, []):
                self._update_row_highlight(ctrl, is_current=True)

        self._last_highlighted_path = current_path

    def _empty_playlist_widget(self, depth) -> ft.Control:
        return ft.Container(
            data={"depth": depth, "type": "empty_playlist"},
            content=ft.Row([
                ft.Icon(ft.Icons.PLAYLIST_REMOVE_ROUNDED, color=DIM, size=18),
                ft.Text("Playlist is empty", color=DIM, size=12, weight=ft.FontWeight.W_600, expand=True),
            ], spacing=8),
            padding=ft.Padding.only(left=20 * depth + 12, right=15, top=8, bottom=8),
            margin=ft.Margin.only(left=20 * depth, right=10, top=5, bottom=5),
        )

    def _find_playlist_track_indices(self, playlist_id):
        out = []
        rel = 0
        for gi, c in enumerate(self._library_list.controls):
            d = getattr(c, "data", None) or {}
            if d.get("type") == "track" and d.get("playlist_id") == playlist_id:
                out.append((gi, rel))
                rel += 1
        return out

    def _move_playlist_track_in_place(self, playlist_id, path, direction):
        entries = self._find_playlist_track_indices(playlist_id)
        if not entries:
            return
        target = next(
            (e for e in entries if (getattr(self._library_list.controls[e[0]], "data", None) or {}).get("path") == path),
            None,
        )
        if target is None:
            return
        target_gi, target_rel = target
        new_rel = target_rel + direction
        if new_rel < 0 or new_rel >= len(entries):
            return

        swap_gi = entries[new_rel][0]
        controls = self._library_list.controls
        controls[target_gi], controls[swap_gi] = controls[swap_gi], controls[target_gi]
        self._library_list.update()

        async def _commit():
            await self.app.db_manager.move_playlist_track(playlist_id, target_rel, new_rel)
        self.page.run_task(_commit)

    def _move_playlist_track_to_target(self, playlist_id, src_path, dst_path):
        entries = self._find_playlist_track_indices(playlist_id)
        if not entries:
            return

        controls = self._library_list.controls
        src_entry = None
        dst_entry = None
        for gi, rel in entries:
            ctrl = controls[gi]
            d = getattr(ctrl, "data", None) or {}
            ctrl_path = d.get("path")
            if ctrl_path == src_path:
                src_entry = (gi, rel)
            if ctrl_path == dst_path:
                dst_entry = (gi, rel)

        if src_entry is None or dst_entry is None:
            return

        src_gi, src_rel = src_entry
        dst_gi, dst_rel = dst_entry

        # Move control in controls list in memory
        ctrl = controls.pop(src_gi)
        controls.insert(dst_gi, ctrl)
        self._library_list.update()

        async def _commit():
            await self.app.db_manager.move_playlist_track(playlist_id, src_rel, dst_rel)
        self.page.run_task(_commit)

    def _remove_playlist_track_in_place(self, playlist_id, path, title):
        controls = self._library_list.controls
        target_gi = None
        for gi, c in enumerate(controls):
            d = getattr(c, "data", None) or {}
            if d.get("type") == "track" and d.get("playlist_id") == playlist_id and d.get("path") == path:
                target_gi = gi
                break
        if target_gi is None:
            return
        controls.pop(target_gi)

        # Check if the playlist is now empty in the UI list
        has_tracks = False
        playlist_idx = None
        for gi, c in enumerate(controls):
            d = getattr(c, "data", None) or {}
            if d.get("playlist_id") == playlist_id:
                if d.get("type") == "track":
                    has_tracks = True
                    break
                elif d.get("type") == "playlist":
                    playlist_idx = gi

        if not has_tracks and playlist_idx is not None:
            pl_depth = (getattr(controls[playlist_idx], "data", None) or {}).get("depth", 0)
            controls.insert(playlist_idx + 1, self._empty_playlist_widget(pl_depth + 1))

        self._library_list.update()

        async def _commit():
            await self.app.db_manager.remove_track_from_playlist(playlist_id, path)
            self.app.show_snackbar(f"Removed '{title}' from playlist.")
        self.page.run_task(_commit)

    def _track_row(self, t: dict, depth: int = 0, playlist_id: int = None,
                   album_context: tuple | None = None) -> ft.Control:
        path   = t.get("path", "")
        title  = t.get("title") or os.path.basename(path)
        artist = t.get("artist") or "Unknown"
        album  = t.get("album")  or "Unknown"
        tnum   = t.get("track_num")
        meta   = {"path": path, "track_title": title, "artist_name": artist, "album_title": album}
        accent = LIB_TRACK_COLOR

        is_current = (path == audio_engine.current_path and bool(path))

        fmt = (t.get("format") or "").upper()
        badge = ft.Container(
            content=ft.Text(fmt, size=10, weight=ft.FontWeight.BOLD, color=BG),
            bgcolor=CYAN if is_current else DIM,
            padding=ft.Padding.symmetric(horizontal=6, vertical=2),
            border_radius=4,
            visible=bool(fmt),
        )

        def move_up(e):
            self._move_playlist_track_in_place(playlist_id, path, -1)

        def move_down(e):
            self._move_playlist_track_in_place(playlist_id, path, +1)

        def remove_from_pl(e):
            self._remove_playlist_track_in_place(playlist_id, path, title)

        trailing_controls = []
        if playlist_id:
            drag_handle = ft.Draggable(
                group=f"playlist_{playlist_id}",
                data=path,
                content=ft.Container(
                    content=ft.Icon(ft.Icons.DRAG_HANDLE_ROUNDED, color=DIM, size=18),
                    padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                )
            )
            trailing_controls.insert(0, ft.Row(
                [
                    drag_handle,
                    ft.IconButton(ft.Icons.REMOVE_CIRCLE_OUTLINE, icon_size=18, icon_color="#FF4444", on_click=remove_from_pl, tooltip="Remove from Playlist"),
                ],
                spacing=4, tight=True
            ))

        tile = ft.ListTile(
            data={
                "type": "track",
                "depth": depth,
                "playlist_id": playlist_id,
                "path": path,
            },
            leading=ft.Row(
                [
                    ft.Container(width=depth * 20, visible=depth > 0),
                    ft.Icon(
                        ft.Icons.EQUALIZER if is_current else ft.Icons.MUSIC_NOTE_ROUNDED,
                        color=CYAN if is_current else accent,
                    ),
                ],
                tight=True
            ),
            title=ft.Row(
                [
                    ft.Text(
                        title,
                        color=CYAN if is_current else TEXT,
                        size=14,
                        weight=ft.FontWeight.W_600,
                        max_lines=3,
                        expand=True,
                    ),
                    badge,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
            subtitle=ft.Text(
                f"Track {tnum}  ·  {artist}" if tnum else artist,
                color=CYAN if is_current else DIM,
                size=12,
                max_lines=2,
            ),
            trailing=ft.Row(trailing_controls, tight=True, spacing=0) if playlist_id else None,
            bgcolor=apply_opacity(0.1, CYAN) if is_current else "transparent",
            on_click=lambda e: self.page.run_task(
                self.app.play_track,
                path,
                ("playlist", playlist_id) if playlist_id is not None else
                ("album", album_context[0], album_context[1]) if album_context else
                ("library", None),
            ),
        )

        async def _on_swipe_right(e):
            self.app.trigger_haptic("swipe_queue")
            audio_engine.queue_next({
                "path":        path,
                "track_title": title,
                "artist_name": artist,
                "album_title": album,
            })
            await e.control.confirm_dismiss(False)
            self.app.show_snackbar(f"'{title}' will play next", icon=ft.Icons.QUEUE_MUSIC_ROUNDED, color=CYAN)

        dismissible = ft.Dismissible(
            data={"path": path, "depth": depth, "type": "track"},
            key=f"swipe_{abs(hash(path))}",
            content=tile,
            dismiss_direction=ft.DismissDirection.START_TO_END,
            dismiss_thresholds={ft.DismissDirection.START_TO_END: 0.35},
            movement_duration=ft.Duration(milliseconds=180),
            background=ft.Container(
                content=ft.Row(
                    [
                        ft.Container(width=20),
                        ft.Icon(ft.Icons.QUEUE_MUSIC_ROUNDED, color=ft.Colors.WHITE, size=20),
                        ft.Text("Next Up", color=ft.Colors.WHITE, size=13, weight=ft.FontWeight.W_700),
                    ],
                    tight=True,
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                bgcolor=CYAN,
                expand=True,
            ),
            on_confirm_dismiss=_on_swipe_right,
        )

        res = ft.GestureDetector(
            content=dismissible,
            data={"path": path, "depth": depth, "type": "track"},
            on_long_press_start=lambda e: self._on_track_long_press(meta),
        )

        if playlist_id is not None:
            def drag_accept(e):
                src_control = self.page.get_control(e.src_id)
                if not src_control:
                    return
                src_path = src_control.data
                dst_path = path
                if src_path == dst_path:
                    return
                self._move_playlist_track_to_target(playlist_id, src_path, dst_path)

            drag_target = ft.DragTarget(
                group=f"playlist_{playlist_id}",
                content=res,
                on_accept=drag_accept,
            )
            drag_target.data = {"path": path, "depth": depth, "type": "track", "playlist_id": playlist_id}
            self._path_to_controls.setdefault(path, []).append(drag_target)
            return drag_target

        self._path_to_controls.setdefault(path, []).append(res)
        return res

    def _build_partition_track_row(self, t: dict, partition_tracks: list[dict], depth: int = 0, islet_name: str = None) -> ft.Control:
        res = self._track_row(t, depth=depth)
        path = t.get("path", "")
        tile = res.content.content
        title = t.get("title") or os.path.basename(path)

        if islet_name:
            exclude_btn = ft.IconButton(
                icon=ft.Icons.REMOVE_CIRCLE_OUTLINE,
                icon_size=18,
                icon_color="#FF4444",
                tooltip="Exclude track from this custom mood",
                on_click=lambda e, p=path, n=islet_name, t=title: self.page.run_task(self._exclude_track_from_islet, n, p, t)
            )
            tile.trailing = ft.Row([exclude_btn], tight=True, spacing=0)
        
        def play_partition_track(_e):
            self._tracks_cache = partition_tracks
            self._tracks_cache_key = ("partitions", self.search_query, self.sort_mode)
            self.page.run_task(self.app.play_track, path, ("library", None))
            
        tile.on_click = play_partition_track

        from utils import track_graph as tg
        mood = tg.mood_canonical(self._mood_label.value) if self.partition_sub_mode == "moods" and hasattr(self, "_mood_label") and self._mood_label else None

        if mood:
            artist = t.get("artist") or "Unknown"
            tnum = t.get("track_num")
            is_current = (path == audio_engine.current_path and bool(path))

            tile.subtitle = ft.Text(
                f"Track {tnum}  ·  {artist}" if tnum else artist,
                color=CYAN if is_current else DIM,
                size=11,
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
            )
            tile.trailing = None

        return res

    def _edit_btn(self, edit_type: str, meta: dict, color: str = DIM) -> ft.Control:
        return ft.IconButton(
            icon=ft.Icons.EDIT_OUTLINED,
            icon_color=apply_opacity(0.6, color), icon_size=20,
            tooltip="Edit metadata",
            on_click=lambda e, et=edit_type, m=meta: self.app.open_metadata_editor(et, m),
        )

    def _on_track_long_press(self, meta: dict):
        self.app.trigger_haptic("long_press")
        self._open_track_context_menu(meta)

    def _open_track_context_menu(self, meta: dict):
        bs_holder = [None]
        
        def _close():
            if bs_holder[0]:
                bs_holder[0].open = False
                bs_holder[0].update()
                self.page.update()

        def _play_next(_e):
            _close()
            audio_engine.queue_next(meta)
            self.app.show_snackbar(f"'{meta.get('track_title')}' will play next", icon=ft.Icons.QUEUE_MUSIC_ROUNDED, color=CYAN)

        def _add_to_queue(_e):
            _close()
            audio_engine.queue_last(meta)
            self.app.show_snackbar(f"'{meta.get('track_title')}' added to queue", icon=ft.Icons.PLAYLIST_ADD_ROUNDED, color=CYAN)

        def _add_to_playlist(_e):
            self.page.run_task(self._open_add_to_playlist_sheet, meta, bs_holder[0])

        def _edit_meta(_e):
            _close()
            self.app.open_metadata_editor("track", meta)

        def _redownload(_e):
            _close()
            title = meta.get("track_title", "")
            artist = meta.get("artist_name", "")
            
            query_parts = []
            if title:
                query_parts.append(title)
            if artist and artist != "Unknown":
                query_parts.append(artist)
                
            query = " ".join(query_parts).strip()
            if not query: return
            
            self.app._switch_tab(1)
            
            self.app.search_view._search_field.value = query
            self.app.search_view.view_mode = "tracks"
            self.app.search_view._update_view_tabs()
            
            self.page.run_task(self.app.search_view.start_search)

        bs = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(meta.get("track_title", "Track Actions"), color=TEXT, weight=ft.FontWeight.W_700, size=14, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Divider(color=BORDER),
                        ft.ListTile(
                            leading=ft.Icon(ft.Icons.QUEUE_MUSIC_ROUNDED, color=CYAN),
                            title=ft.Text("Play Next", color=TEXT),
                            on_click=_play_next,
                        ),
                        ft.ListTile(
                            leading=ft.Icon(ft.Icons.PLAYLIST_ADD_CHECK_ROUNDED, color=CYAN),
                            title=ft.Text("Add to Queue", color=TEXT),
                            on_click=_add_to_queue,
                        ),
                        ft.ListTile(
                            leading=ft.Icon(ft.Icons.PLAYLIST_ADD_ROUNDED, color=LIB_PLAYLIST_COLOR),
                            title=ft.Text("Add to Playlist", color=TEXT),
                            on_click=_add_to_playlist,
                        ),
                        ft.ListTile(
                            leading=ft.Icon(ft.Icons.DOWNLOAD_ROUNDED, color=DIM),
                            title=ft.Text("Redownload (Different Quality)", color=TEXT),
                            on_click=_redownload,
                        ),
                        ft.ListTile(
                            leading=ft.Icon(ft.Icons.EDIT_OUTLINED, color=DIM),
                            title=ft.Text("Edit Metadata", color=TEXT),
                            on_click=_edit_meta,
                        ),
                    ],
                    tight=True,
                    spacing=0,
                ),
                bgcolor=SURFACE,
                padding=16,
            ),
            bgcolor=SURFACE,
        )
        bs_holder[0] = bs
        self.page.overlay.append(bs)
        bs.open = True
        self.page.update()

    async def _open_add_to_playlist_sheet(self, meta: dict, existing_bs: ft.BottomSheet = None):
        playlists = await self.app.db_manager.get_all_playlists(sort_mode="name")
        bs_holder = [existing_bs]
        
        def _close():
            if bs_holder[0]:
                bs_holder[0].open = False
                bs_holder[0].update()
                self.page.update()

        def _create_new(_e):
            _close()
            
            dlg_holder = [None]
            name_field = ft.TextField(label="Playlist Name", autofocus=True)
            
            async def _submit(_e2):
                name = name_field.value.strip()
                if not name: return
                try:
                    pl_id = await self.app.db_manager.create_playlist(name)
                    await self.app.db_manager.add_track_to_playlist(pl_id, meta["path"])
                    if dlg_holder[0]:
                        dlg_holder[0].open = False
                        dlg_holder[0].update()
                    self.app.show_snackbar(f"Added to '{name}'", icon=ft.Icons.CHECK_CIRCLE_OUTLINE, color=CYAN)
                    if self.view_mode == "playlists":
                        await self.load_library()
                except Exception as exc:
                    self.app.show_snackbar(f"Could not create playlist: {exc}")

            dlg = ft.AlertDialog(
                title=ft.Text("New Playlist"),
                content=name_field,
                actions=[
                    ft.TextButton("Cancel", on_click=lambda _: setattr(dlg_holder[0], 'open', False) or dlg_holder[0].update()),
                    ft.Button("Create", on_click=_submit, bgcolor=CYAN, color=BG),
                ],
            )
            dlg_holder[0] = dlg
            self.page.overlay.append(dlg)
            dlg.open = True
            self.page.update()

        def _add_to_existing(pl_id, pl_name):
            async def _do():
                _close()
                try:
                    await self.app.db_manager.add_track_to_playlist(pl_id, meta["path"])
                    self.app.show_snackbar(f"Added to '{pl_name}'", icon=ft.Icons.CHECK_CIRCLE_OUTLINE, color=CYAN)
                    if self.view_mode == "playlists" and f"playlist_{pl_id}" in self.expanded_nodes:
                        await self.load_library()
                except Exception as exc:
                    self.app.show_snackbar(f"Failed to add track: {exc}")
            self.page.run_task(_do)

        lv = ft.ListView(spacing=2, height=300)
        lv.controls.append(
            ft.ListTile(
                leading=ft.Icon(ft.Icons.ADD_ROUNDED, color=CYAN),
                title=ft.Text("New Playlist", color=CYAN, weight=ft.FontWeight.W_600),
                on_click=_create_new,
            )
        )
        lv.controls.append(ft.Divider(color=BORDER))
        for pl in playlists:
            lv.controls.append(
                ft.ListTile(
                    leading=ft.Icon(ft.Icons.QUEUE_MUSIC_ROUNDED, color=LIB_PLAYLIST_COLOR),
                    title=ft.Text(pl["name"], color=TEXT),
                    subtitle=ft.Text(f"{pl['track_count']} tracks", color=DIM, size=10),
                    on_click=lambda e, i=pl["id"], n=pl["name"]: _add_to_existing(i, n),
                )
            )

        container_content = ft.Container(
            content=ft.Column(
                [
                    ft.Text("Add to Playlist", color=TEXT, weight=ft.FontWeight.W_700, size=14),
                    lv,
                ],
                tight=True,
                spacing=8,
            ),
            bgcolor=SURFACE,
            padding=16,
        )

        if bs_holder[0]:
            bs_holder[0].content = container_content
            bs_holder[0].update()
        else:
            bs = ft.BottomSheet(
                content=container_content,
                bgcolor=SURFACE,
            )
            bs_holder[0] = bs
            self.page.overlay.append(bs)
            bs.open = True
            self.page.update()

    # ── library scan ─────────────────────────────────────────────────────────
    def start_scan(self):
        if self._is_scanning:
            return
        self._is_scanning = True

        target = self.app.library_folder or self.app.target_folder or get_app_dir()
        self._scan_progress_container.visible = True
        self._scan_progress.value = None
        self._scan_status_lbl.value = "Initializing active scan…"
        self._scan_btn.disabled = True
        self._empty_label.visible = False
        self.expanded_nodes = set()
        self._path_to_controls = {}
        self.app.safe_update(lambda: None)

        async def _scan():
            from utils.library_scanner import LibraryScanner
            scanner = LibraryScanner(
                target_folder=target,
                db_manager=self.app.db_manager,
                progress_callback=self._on_scan_progress,
                completion_callback=self._on_scan_complete,
            )
            await scanner.run()
        self.page.run_task(self.app.error_boundary.capture(_scan))

    def _on_scan_progress(self, percent: float, track_title: str):
        if not hasattr(self, "_scan_update_count"):
            self._scan_update_count = 0
        self._scan_update_count += 1
        _pct = percent
        _title = track_title
        def _apply():
            if _pct == -1:
                self._scan_progress.value = None
                self._scan_status_lbl.value = f"Searching: {_title}"
            else:
                self._scan_progress.value = _pct / 100
                self._scan_status_lbl.value = _title
        self.app.safe_update(_apply)

    def _on_scan_complete(self, count: int, _skipped: int):
        self._cached_moods = None
        self._cached_islets = None
        self._cached_unanalysed = None
        self._scan_update_count = 0
        self._is_scanning = False
        self._toggling_nodes = set()
        self.page.run_task(self.load_library)

        def _hide_scanner():
            self._scan_progress_container.visible = False
            self._scan_btn.disabled = False
        self.app.safe_update(_hide_scanner)

        assistant = getattr(self.app, "assistant_view", None)
        if assistant is not None:
            assistant._init_greeted = False

        msg = f"Scan complete. Indexed {count} items." if count else "Library is up to date."
        self.app.show_snackbar(msg, icon=ft.Icons.CHECK_CIRCLE_OUTLINE if "Indexed" in msg else ft.Icons.INFO_OUTLINE, color=CYAN)


    def _ui(self, fn):
        try:
            fn()
        except Exception as exc:
            logger.warning("LibraryView UI error: %s", exc)
