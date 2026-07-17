import os
import sys
import math
import logging
import asyncio
from collections import Counter
import flet as ft
import flet.canvas as cv
from ui.tokens import (
    BG, SURFACE, SURFACE2, CYAN, AMBER, TEXT, DIM, BORDER, 
    SOURCE_COLORS, LIB_ARTIST_COLOR, LIB_ALBUM_COLOR, LIB_TRACK_COLOR, 
    LIB_PLAYLIST_COLOR, apply_opacity
)
from ui.widgets import AnimatedEntry, AccordionCard, src_color, build_page_ghost_top, build_page_ghost_bottom

if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

from utils.filepath_utils import get_app_dir

logger = logging.getLogger(__name__)


# Categorical palette for colouring network nodes by genre. Picked for
# distinctness on the dark theme; cycles for libraries with more genres than
# colours. Tracks with no genre fall back to grey.
_CLUSTER_PALETTE = [
    "#40C4FF", "#FF80AB", "#FF5252", "#FFD740", "#76FF03", "#B388FF",
    "#FFFFFF", "#FF6E40", "#26C6DA", "#FF7043", "#66BB6A", "#BA68C8",
    "#5C6BC0", "#F50057", "#E040FB", "#00E676", "#FF9100", "#D500F9",
    "#00E5FF", "#1DE9B6", "#FF3D00", "#C0CA33", "#00B0FF", "#E6EE9C",
]
_CLUSTER_NEUTRAL = "#8A93A0"


def _canon_genre(genre) -> str:
    """Punctuation/case/whitespace-insensitive key for a free-text genre tag, so
    'Hip-Hop', 'Hip hop' and 'HIP  HOP' all collapse to one key ('hiphop')."""
    if not genre:
        return ""
    return "".join(ch for ch in str(genre).lower() if ch.isalnum())


# Semantic synonyms for tags that pca_engine.genre_bucket's coarse rules don't
# cover, keyed by canonical token (alnum-lower) → (group_key, display_label).
# Spelling/spacing variants are already unified by _canon_genre, so we only need
# one canonical token per synonym. A group_key equal to a real bucket label
# ('Electronic', 'Soul/R&B') deliberately merges these into that bucket's colour
# + legend row; the rest give niche families a stable label of their own. Exact
# (not substring) lookup, so e.g. 'reggaeton' lands in Latin, never Reggae.
_GENRE_ALIASES: dict[str, tuple[str, str]] = {
    "electro":        ("Electronic", "Electronic"),
    "electronica":    ("Electronic", "Electronic"),
    "electronicdance":("Electronic", "Electronic"),
    "dance":          ("Electronic", "Electronic"),
    "house":          ("Electronic", "Electronic"),
    "techno":         ("Electronic", "Electronic"),
    "trance":         ("Electronic", "Electronic"),
    "synthpop":       ("Electronic", "Electronic"),
    "drumandbass":    ("Electronic", "Electronic"),
    "drumnbass":      ("Electronic", "Electronic"),
    "idm":            ("Electronic", "Electronic"),
    "ebm":            ("Electronic", "Electronic"),
    "coldwave":       ("Electronic", "Electronic"),
    "grime":          ("Hip-Hop",    "Hip-Hop"),
    "laika":          ("Folk/Cntry", "Folk/Cntry"),
    "laiko":          ("Folk/Cntry", "Folk/Cntry"),
    "laiki":          ("Folk/Cntry", "Folk/Cntry"),
    "laikopo":        ("Folk/Cntry", "Folk/Cntry"),
    "laikopop":       ("Folk/Cntry", "Folk/Cntry"),
    "greekpop":       ("Pop",        "Pop"),
    "greekfolk":      ("Folk/Cntry", "Folk/Cntry"),
    "entechno":       ("Folk/Cntry", "Folk/Cntry"),
    "rebetiko":       ("Folk/Cntry", "Folk/Cntry"),
    "postpunk":       ("Rock/Alt",   "Rock/Alt"),
    "rhythmandblues": ("Soul/R&B",   "Soul/R&B"),
    "randb":          ("Soul/R&B",   "Soul/R&B"),
    "triphop":        ("Trip-Hop",   "Trip-Hop"),
    "jazz":           ("Jazz",       "Jazz"),
    "nujazz":         ("Jazz",       "Jazz"),
    "acidjazz":       ("Jazz",       "Jazz"),
    "smoothjazz":     ("Jazz",       "Jazz"),
    "reggae":         ("Reggae",     "Reggae"),
    "ragga":          ("Reggae",     "Reggae"),
    "dancehall":      ("Reggae",     "Reggae"),
    "ambient":        ("Ambient",    "Ambient"),
    "soundtrack":     ("Soundtrack", "Soundtrack"),
    "score":          ("Soundtrack", "Soundtrack"),
    "ost":            ("Soundtrack", "Soundtrack"),
    "disco":          ("Disco",      "Disco"),
    "gospel":         ("Gospel",     "Gospel"),
    "latin":          ("Latin",      "Latin"),
    "salsa":          ("Latin",      "Latin"),
    "reggaeton":      ("Latin",      "Latin"),
    "bachata":        ("Latin",      "Latin"),
    "world":          ("World",      "World"),
    "afrobeat":       ("World",      "World"),
    "afrobeats":      ("World",      "World"),
}


def _genre_group(genre) -> tuple[str, str]:
    """(grouping_key, display_label) for a genre tag. Known families collapse via
    pca_engine.genre_bucket so 'Hip-Hop' / 'hip hop' / 'rap' / 'trap' share one
    key, colour and legend row. Multi-genre tags map via their primary segment."""
    from utils.pca_engine import genre_bucket
    import re
    if not genre:
        return "", ""

    raw_str = str(genre).strip()
    primary = re.split(r'[/,;&+]', raw_str)[0].strip()

    norm_primary = " ".join(primary.split())
    bucket_primary = genre_bucket(norm_primary)
    if bucket_primary not in ("Unknown", "Other"):
        return bucket_primary, bucket_primary

    norm_full = " ".join(raw_str.split())
    bucket_full = genre_bucket(norm_full)
    if bucket_full not in ("Unknown", "Other"):
        return bucket_full, bucket_full

    canon = _canon_genre(primary) or _canon_genre(raw_str)
    if not canon:
        return "", ""

    alias = _GENRE_ALIASES.get(canon)
    if alias is not None:
        return alias

    return canon, raw_str.title()


def _genre_color(genre) -> str:
    """Deterministic colour for a genre, mapped directly to mega-genre palettes
    or hashed across an expanded palette for niche genres."""
    from utils.pca_engine import _GENRE_PALETTE
    key, _label = _genre_group(genre)
    if not key:
        return _CLUSTER_NEUTRAL
    if key in _GENRE_PALETTE:
        return _GENRE_PALETTE[key]
    h = 2166136261
    for ch in key:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return _CLUSTER_PALETTE[h % len(_CLUSTER_PALETTE)]


def _node_radius(base: float, play_count) -> float:
    """Node radius grows with play count so favourites read as larger hubs.
    sqrt keeps the growth gentle and capped so heavy-rotation tracks don't
    swamp the canvas."""
    try:
        pc = max(0, int(play_count or 0))
    except (TypeError, ValueError):
        pc = 0
    return base + min(7.0, 2.2 * math.sqrt(pc))


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

        show_playlists = bool(appearance.get("show_playlists", True))
        show_artists = bool(appearance.get("show_artists", True))
        show_albums = bool(appearance.get("show_albums", True))
        show_tracks = bool(appearance.get("show_tracks", True))
        show_network = bool(appearance.get("show_network", False))

        if show_tracks:
            self.view_mode = "tracks"
        elif show_albums:
            self.view_mode = "albums"
        elif show_artists:
            self.view_mode = "artists"
        elif show_playlists:
            self.view_mode = "playlists"
        elif show_network:
            self.view_mode = "network"
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

        self._cached_unanalysed: list[dict] | None = None

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

        self.selected_network_index = 0  # 0=Local, 1=Walk

        # Interactive network canvas state
        self._net_nodes: list[dict] = []
        self._net_node_by_path: dict[str, dict] = {}
        self._net_edges: list[dict] = []          # {src, dst, weight}
        self._net_mode: int = 0
        self._net_dims: tuple[int, int, int] = (0, 0, 0)  # (w, h, pad)
        self._net_canvas_obj: ft.Control | None = None    # the cv.Canvas itself
        self._net_pressed: dict | None = None     # node under last tap-down
        self._net_pulse_overlay: ft.Control | None = None
        self._net_pulse_token: int = 0
        self._net_tooltip_overlay: ft.Control | None = None
        self._network_seed_path: str | None = None  # pinned seed for navigation
        self._net_selected_path: str | None = None  # focused node path
        self._net_k_neighbors: int = 24             # neighborhood depth (12, 24, 36, 48)
        self._net_walk_length: int = 10             # walk path length (5, 10, 15, 20)
        self._net_canvas: ft.Control | None = None
        self._net_track_panel: ft.Control | None = None
        self._net_list_view: ft.ListView | None = None
        self._net_viewer: ft.Control | None = None  # InteractiveViewer (native pan/zoom)
        self._net_graph_collapsed = False           # graph hidden → list takes the pane
        self._net_collapse_icon: ft.Icon | None = None
        # Live-tracking of the playing node. When ON, the graph recenters on the
        # active track once it falls off-screen (maps-style); the pulse always
        # follows it in place while it's a visible node. OFF lets the user
        # explore a pinned neighbourhood freely (the pulse still glows if shown).
        self._net_follow_current: bool = True
        self._net_follow_btn: ft.Control | None = None
        self._net_follow_icon: ft.Control | None = None
        self._net_reseed_task: asyncio.Task | None = None  # debounced follow rebuild
        self._view_tabs_row = ft.Row(spacing=6)
        self._update_view_tabs()

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
        
        # Toggle sort button visibility
        self._sort_icon_btn.visible = (mode != "network")
        self.try_update(self._sort_icon_btn)

        self._update_view_tabs()
        self.page.run_task(self.load_library)

    def _clear_search(self, _e=None):
        self._search_field.value = ""
        self.search_query = ""
        self._lib_clear_btn.visible = False
        self.expanded_nodes.clear()
        self._search_spinner.visible = False
        self.page.run_task(self.load_library)

    def _select_network_index(self, index: int):
        self.selected_network_index = index
        self.current_page = 0
        self.page.run_task(self.load_library)

    def _build_interactive_network_canvas(
        self, rows, mode, current_path, neighbors_list, walk_paths,
    ) -> ft.Control:
        """
        Build an interactive Flet Canvas graph with modern visual feel and
        enhanced track traversal capabilities. Supports canvas pan/zoom, node tap
        inspection, cyan selection halos, parameter depth controls, and walk stepping.
        """
        # ── Resolve canvas dimensions from the page ────────────────────────
        avail_w = max(260, (self.page.width or 360) - 24)
        avail_h = self.page.height or 640
        # Split layout: the graph takes the upper portion of the pane; the
        # ordered track list fills (and scrolls) the rest. avail_h is the FULL
        # screen, but graph+list live in a wrapper that's much shorter (search
        # bar, tabs, pagination, bottom nav, mini-player all sit outside it), so
        # keep the graph modest and hard-capped — the collapse toggle gives the
        # list the whole pane when the user wants to browse.
        canvas_h = max(150, min(int(avail_h * 0.32), 280))
        canvas_w = int(avail_w)
        pad = 32                                   # edge padding in px
        self._net_dims = (canvas_w, canvas_h, pad)
        self._net_mode = mode

        now_playing = audio_engine.current_path or ""
        path_to_row = {r["path"]: r for r in rows if r.get("pca_coords")}

        raw_nodes: list[dict] = []
        raw_edges: list[dict] = []     # {src, dst, weight}

        def _trunc(s: str, n: int) -> str:
            return s[:n] + "…" if len(s) > n else s

        def _mk_node(rx, ry, *, is_seed, base_radius, label, genre,
                     play_count, path, title, artist, album=None, duration=None):
            return {
                "rx": rx, "ry": ry, "path": path,
                "title": title, "artist": artist, "album": album or "",
                "duration": duration,
                "genre": genre,
                "color": _genre_color(genre),
                "radius": _node_radius(base_radius, play_count),
                "is_seed": is_seed,
                "is_now_playing": (path == now_playing),
                "label": label,
                "play_count": play_count or 0,
            }

        if mode == 0:  # Local — seed + neighbours
            seed_row = path_to_row.get(current_path) if current_path else None
            if not seed_row:
                for r in rows:
                    if r.get("pca_coords"):
                        seed_row = r
                        break
            if seed_row:
                sc = seed_row["pca_coords"][:2]
                seed_title = seed_row.get("title") or "Active Seed"
                raw_nodes.append(_mk_node(
                    sc[0], sc[1], is_seed=True, base_radius=14,
                    label=_trunc(seed_title, 14),
                    genre=seed_row.get("genre"),
                    play_count=seed_row.get("play_count"),
                    path=seed_row["path"], title=seed_title,
                    artist=seed_row.get("artist") or "Unknown",
                    album=seed_row.get("album"),
                    duration=seed_row.get("duration"),
                ))
                for idx, n in enumerate(neighbors_list):
                    n_path = n.get("path")
                    n_weight = n.get("weight") or 0.5
                    n_row = path_to_row.get(n_path)
                    if n_row:
                        nc = n_row["pca_coords"][:2]
                        n_title = n_row.get("title") or n.get("title") or "Neighbor"
                        n_artist = n_row.get("artist") or n.get("artist") or "Unknown"
                        n_album = n_row.get("album") or n.get("album")
                        n_genre = n_row.get("genre")
                        n_pc = n_row.get("play_count")
                        n_dur = n_row.get("duration")
                    else:
                        theta = 2 * math.pi * idx / max(1, len(neighbors_list))
                        nc = [sc[0] + 0.8 * math.cos(theta), sc[1] + 0.8 * math.sin(theta)]
                        n_title = n.get("title") or "Neighbor"
                        n_artist = n.get("artist") or "Unknown"
                        n_album = n.get("album")
                        n_genre = None
                        n_pc = 0
                        n_dur = None
                    raw_nodes.append(_mk_node(
                        nc[0], nc[1], is_seed=False, base_radius=8.5,
                        label="",  # neighbour labels off for graph clarity
                        genre=n_genre, play_count=n_pc,
                        path=n_path, title=n_title, artist=n_artist, album=n_album,
                        duration=n_dur,
                    ))
                    raw_edges.append({"src": seed_row["path"], "dst": n_path, "weight": float(n_weight)})

        elif mode == 1:  # Walk — sequential trajectory
            seed_row = path_to_row.get(current_path) if current_path else None
            if not seed_row:
                for r in rows:
                    if r.get("pca_coords"):
                        seed_row = r
                        break
            walk_rows_ordered = []
            if seed_row:
                walk_rows_ordered.append(seed_row)
                for wp in walk_paths:
                    wr = path_to_row.get(wp)
                    if wr:
                        walk_rows_ordered.append(wr)
            for idx, wr in enumerate(walk_rows_ordered):
                c = wr["pca_coords"][:2]
                is_seed = (idx == 0)
                raw_nodes.append(_mk_node(
                    c[0], c[1], is_seed=is_seed,
                    base_radius=13 if is_seed else 8,
                    label=str(idx),
                    genre=wr.get("genre"),
                    play_count=wr.get("play_count"),
                    path=wr["path"],
                    title=wr.get("title") or f"Step {idx}",
                    artist=wr.get("artist") or "Unknown",
                    album=wr.get("album"),
                    duration=wr.get("duration"),
                ))
            for i in range(len(walk_rows_ordered) - 1):
                alpha_w = max(0.35, 0.85 - i / max(1, len(walk_rows_ordered)) * 0.5)
                raw_edges.append({
                    "src": walk_rows_ordered[i]["path"],
                    "dst": walk_rows_ordered[i + 1]["path"],
                    "weight": float(alpha_w),
                })

        if not raw_nodes:
            self._net_nodes = []
            self._net_node_by_path = {}
            self._net_edges = []
            self._net_canvas_obj = None
            self._net_pulse_overlay = None
            self._net_list_view = None
            return ft.Container(
                content=ft.Column(
                    [
                        ft.Icon(ft.Icons.HUB_ROUNDED, color=apply_opacity(0.4, CYAN), size=36),
                        ft.Text("Play a track to seed the network.", color=DIM, size=13, text_align=ft.TextAlign.CENTER),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                ),
                alignment=ft.Alignment(0, 0),
                padding=40,
            )

        # ── Normalise raw coords → canvas pixel coords ─────────────────────
        # Focal-centred, UNIFORM scale: the focal node (the now-playing track if
        # it's on the graph, else the seed) maps to the canvas centre, and ONE
        # scale is applied to both axes so acoustic distances stay faithful
        # (circles stay circular) and the graph opens centred on "you are here".
        # Uniform + centred keeps every node within a centred square inside the
        # canvas (no clipping); off-centre detail is reached via pan/zoom.
        focal = (
            next((n for n in raw_nodes if n.get("is_now_playing")), None)
            or next((n for n in raw_nodes if n.get("is_seed")), None)
            or raw_nodes[0]
        )
        fcx, fcy = focal["rx"], focal["ry"]
        half_span = max(
            max((abs(n["rx"] - fcx) for n in raw_nodes), default=1.0),
            max((abs(n["ry"] - fcy) for n in raw_nodes), default=1.0),
        ) or 1.0
        scale = (min(canvas_w, canvas_h) / 2 - pad) / half_span

        def to_px(rx, ry):
            px = canvas_w / 2 + (rx - fcx) * scale
            py = canvas_h / 2 + (ry - fcy) * scale
            return px, py

        self._net_nodes = []
        self._net_node_by_path = {}
        for n in raw_nodes:
            px, py = to_px(n["rx"], n["ry"])
            nd = {
                "px": px, "py": py,
                "path": n["path"], "title": n["title"], "artist": n["artist"],
                "album": n.get("album", ""),
                "duration": n.get("duration"),
                "color": n["color"], "radius": n["radius"],
                "is_seed": n["is_seed"], "is_now_playing": n["is_now_playing"],
                "genre": n["genre"], "label": n.get("label", ""),
                "play_count": n.get("play_count", 0),
            }
            self._net_nodes.append(nd)
            self._net_node_by_path[nd["path"]] = nd
        self._net_edges = raw_edges

        # Auto-select initial node if none selected or invalid
        if not self._net_selected_path or self._net_selected_path not in self._net_node_by_path:
            if current_path and current_path in self._net_node_by_path:
                self._net_selected_path = current_path
            elif self._net_nodes:
                self._net_selected_path = self._net_nodes[0]["path"]

        # ── Tooltip overlay ────────────────────────────────────────────────
        tooltip_title = ft.Text("", color=TEXT, size=11, weight=ft.FontWeight.W_700,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, no_wrap=True)
        tooltip_artist = ft.Text("", color=DIM, size=9.5,
                                  max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, no_wrap=True)
        tooltip_container = ft.Container(
            content=ft.Column([tooltip_title, tooltip_artist], spacing=1, tight=True),
            bgcolor=apply_opacity(0.92, SURFACE2),
            border=ft.Border.all(1, apply_opacity(0.3, CYAN)),
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            visible=False,
            shadow=ft.BoxShadow(blur_radius=12, color=apply_opacity(0.3, "#000000")),
            animate_opacity=ft.Animation(120, ft.AnimationCurve.EASE_OUT),
        )
        self._net_tooltip_overlay = tooltip_container
        self._net_tooltip_title = tooltip_title
        self._net_tooltip_artist = tooltip_artist

        # ── Canvas Control ──────────────────────────────────────────────────
        canvas = cv.Canvas(
            shapes=self._emit_net_shapes(),
            width=canvas_w,
            height=canvas_h,
        )
        self._net_canvas_obj = canvas

        # ── Hit-test helpers ────────────────────────────────────────────────
        def _find_node_at(lx: float, ly: float) -> dict | None:
            best, best_dist = None, float("inf")
            for nd in self._net_nodes:
                dx = lx - nd["px"]
                dy = ly - nd["py"]
                dist = math.sqrt(dx * dx + dy * dy)
                tap_radius = nd["radius"] + 14
                if dist <= tap_radius and dist < best_dist:
                    best = nd
                    best_dist = dist
            return best

        def _evt_xy(e):
            lp = getattr(e, "local_position", None)
            if lp is not None:
                return lp.x, lp.y
            return getattr(e, "local_x", 0.0) or 0.0, getattr(e, "local_y", 0.0) or 0.0

        def _show_tooltip(nd, lx, ly, subtitle=None):
            # Anchored to a FIXED spot (bottom-left of the graph), not the tap
            # point: the graph pans/zooms inside the InteractiveViewer, so a
            # child-space (lx, ly) no longer maps onto this fixed overlay.
            self._net_tooltip_title.value = nd["title"]
            self._net_tooltip_artist.value = subtitle if subtitle is not None else nd["artist"]
            tooltip_container.visible = True
            tooltip_container.left = 8
            tooltip_container.top = max(8, canvas_h - 52)
            self.try_update(tooltip_container)

        def _hide_tooltip_now():
            if tooltip_container.visible:
                tooltip_container.visible = False
                self.try_update(tooltip_container)

        def _on_tap_down(e):
            # Only record what was pressed here — showing the tooltip on tap-DOWN
            # would flash it at the start of a pan gesture. The tooltip is shown
            # on tap COMPLETION (a pan cancels on_tap), so pans stay clean.
            lx, ly = _evt_xy(e)
            self._net_pressed = _find_node_at(lx, ly)

        def _on_tap(e=None):
            nd = self._net_pressed
            if not nd:
                _hide_tooltip_now()
                return
            # Tap = SELECT (paint the selection halo on the canvas + highlight the
            # row in the list). Navigation is the InteractiveViewer's native
            # pan/zoom now, so nodes no longer move; a single one-shot redraw
            # repaints the halo — no per-frame shape re-serialisation.
            self._net_selected_path = nd["path"]
            self.app.trigger_haptic("network_tap")
            _show_tooltip(nd, 0, 0)
            self._redraw_net_canvas()           # repaint selection halo (one-shot)
            self._refresh_net_list_selection()  # highlight the tapped node's row
            async def _hide_later():
                await asyncio.sleep(1.8)
                _hide_tooltip_now()
            self.page.run_task(_hide_later)

        # ── Gesture detector (node tap SELECTION only) ────────────────────────
        # Panning/zooming is handled natively by the enclosing InteractiveViewer
        # (a GPU transform — no per-frame Python, no bridge traffic), so this
        # detector only does tap-to-select. A tap fires with local coords in the
        # canvas's own (untransformed) space, so hit-testing against node px/py
        # stays correct at any pan/zoom. Drags are claimed by the viewer, so they
        # cancel on_tap and don't misfire a selection.
        gesture = ft.GestureDetector(
            content=canvas,
            on_tap_down=_on_tap_down,
            on_tap=_on_tap,
        )

        # ── Top Controls Header (Segmented mode switch + Depth chip + Follow)
        def _build_segmented_tab(label: str, icon_name, is_active: bool, on_tap):
            return ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(icon_name, size=13, color=CYAN if is_active else DIM),
                        ft.Text(label, size=10, weight=ft.FontWeight.W_700 if is_active else ft.FontWeight.W_500, color=TEXT if is_active else DIM),
                    ],
                    spacing=4,
                    tight=True,
                ),
                padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                bgcolor=apply_opacity(0.18, CYAN) if is_active else "transparent",
                border_radius=8,
                border=ft.Border.all(1, apply_opacity(0.35, CYAN)) if is_active else None,
                on_click=on_tap,
            )

        def _switch_mode(target_idx: int):
            self.app.trigger_haptic("network_tap")
            self._select_network_index(target_idx)

        mode_tabs = ft.Row(
            [
                _build_segmented_tab("Local", ft.Icons.HUB_ROUNDED, mode == 0, lambda _e: _switch_mode(0)),
                _build_segmented_tab("Walk", ft.Icons.SHUFFLE_ROUNDED, mode == 1, lambda _e: _switch_mode(1)),
            ],
            spacing=3,
            tight=True,
        )

        def _set_depth_value(val: int):
            self.app.trigger_haptic("network_tap")
            if mode == 0:
                self._net_k_neighbors = val
            else:
                self._net_walk_length = val
            self.page.run_task(self.load_library)

        options = [12, 24, 36, 48] if mode == 0 else [5, 10, 15, 20]
        curr_val = self._net_k_neighbors if mode == 0 else self._net_walk_length
        depth_label = f"Density: {curr_val}" if mode == 0 else f"Steps: {curr_val}"

        depth_menu_items = []
        for opt in options:
            label_text = f"Density: {opt} tracks" if mode == 0 else f"Steps: {opt} steps"
            depth_menu_items.append(
                ft.PopupMenuItem(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.CHECK_ROUNDED if opt == curr_val else ft.Icons.TUNE_ROUNDED, size=13, color=CYAN if opt == curr_val else DIM),
                            ft.Text(label_text, size=11, color=TEXT if opt == curr_val else DIM, weight=ft.FontWeight.W_700 if opt == curr_val else ft.FontWeight.W_400),
                        ],
                        spacing=6,
                        tight=True,
                    ),
                    on_click=lambda _e, v=opt: _set_depth_value(v),
                )
            )

        depth_chip = ft.PopupMenuButton(
            content=ft.Container(
                content=ft.Row(
                    [
                        ft.Text(depth_label, size=9.5, weight=ft.FontWeight.W_700, color=CYAN),
                        ft.Icon(ft.Icons.ARROW_DROP_DOWN_ROUNDED, size=14, color=CYAN),
                    ],
                    spacing=2,
                    tight=True,
                ),
                bgcolor=apply_opacity(0.85, SURFACE2),
                border=ft.Border.all(1, apply_opacity(0.25, CYAN)),
                border_radius=8,
                padding=ft.Padding.symmetric(horizontal=7, vertical=4),
                tooltip="Select neighborhood density / walk steps",
            ),
            items=depth_menu_items,
            bgcolor=SURFACE2,
        )

        # Follow live track toggle chip
        follow_icon = ft.Icon(
            ft.Icons.MY_LOCATION if self._net_follow_current else ft.Icons.LOCATION_SEARCHING,
            color=CYAN if self._net_follow_current else DIM, size=13,
        )
        self._net_follow_icon = follow_icon
        follow_chip = ft.Container(
            content=follow_icon,
            bgcolor=apply_opacity(0.85, SURFACE2),
            border=ft.Border.all(
                1, apply_opacity(0.22, CYAN) if self._net_follow_current else apply_opacity(0.14, TEXT),
            ),
            border_radius=8,
            padding=ft.Padding.all(5),
            tooltip="Follow live playing track",
            on_click=self._toggle_net_follow,
        )
        self._net_follow_btn = follow_chip

        # Walk Tuning parameters chip
        tune_icon = ft.Icon(ft.Icons.TUNE_ROUNDED, color=CYAN, size=13)
        tune_chip = ft.Container(
            content=tune_icon,
            bgcolor=apply_opacity(0.85, SURFACE2),
            border=ft.Border.all(1, apply_opacity(0.22, CYAN)),
            border_radius=8,
            padding=ft.Padding.all(5),
            tooltip="Tune Walk Parameters",
            on_click=self._show_tuning_popup,
        )

        # Fit-to-view: snap the InteractiveViewer's pan/zoom back to the seed-
        # centred default. Native reset() — no rebuild, no DB, no bridge churn.
        fit_chip = ft.Container(
            content=ft.Icon(ft.Icons.FILTER_CENTER_FOCUS_ROUNDED, color=CYAN, size=13),
            bgcolor=apply_opacity(0.85, SURFACE2),
            border=ft.Border.all(1, apply_opacity(0.22, CYAN)),
            border_radius=8,
            padding=ft.Padding.all(5),
            tooltip="Reset view (fit & centre)",
            on_click=self._reset_net_view,
        )

        right_chips = [depth_chip, follow_chip, tune_chip, fit_chip]

        top_controls_overlay = ft.Container(
            content=ft.Row(
                [
                    mode_tabs,
                    ft.Row(right_chips, spacing=3, tight=True),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            left=6, right=6, top=6,
            bgcolor=apply_opacity(0.88, SURFACE2),
            border=ft.Border.all(1, apply_opacity(0.18, CYAN)),
            border_radius=10,
            padding=ft.Padding.symmetric(horizontal=6, vertical=4),
            clip_behavior=ft.ClipBehavior.NONE,
        )

        # ── Genre legend (top-left below control header) ────────────────────
        genres_present: dict[str, tuple[str, str]] = {}
        for nd in self._net_nodes:
            g = nd.get("genre")
            if not g:
                continue
            key, label = _genre_group(g)
            if key:
                norm_label = label.lower().strip()
                if norm_label not in genres_present:
                    genres_present[norm_label] = (label, nd["color"])
        legend_overlay = None
        if len(genres_present) > 1:
            legend_rows = []
            for label, color in sorted(genres_present.values())[:6]:
                legend_rows.append(ft.Row(
                    [
                        ft.Container(width=8, height=8, border_radius=4, bgcolor=color),
                        ft.Text(label[:14] + ("…" if len(label) > 14 else ""), size=8, color=TEXT, no_wrap=True),
                    ],
                    spacing=4, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ))
            legend_overlay = ft.Container(
                content=ft.Column(legend_rows, spacing=2, tight=True),
                left=8, top=44,
                bgcolor=apply_opacity(0.82, SURFACE2),
                border=ft.Border.all(1, apply_opacity(0.16, CYAN)),
                border_radius=8,
                padding=ft.Padding.symmetric(horizontal=6, vertical=4),
            )

        # ── Now-playing pulse ring overlay ──────────────────────────────────
        np_node = self._net_node_by_path.get(now_playing) if now_playing else None
        if np_node is not None:
            d = (np_node["radius"] + 9) * 2
            pulse_left, pulse_top, pulse_visible = np_node["px"] - d / 2, np_node["py"] - d / 2, True
        else:
            d, pulse_left, pulse_top, pulse_visible = 24, -100.0, -100.0, False
        pulse_overlay = ft.Container(
            width=d, height=d, border_radius=d / 2,
            border=ft.Border.all(2, CYAN),
            left=pulse_left, top=pulse_top,
            visible=pulse_visible,
            animate_scale=ft.Animation(700, ft.AnimationCurve.EASE_IN_OUT),
            animate_opacity=ft.Animation(700, ft.AnimationCurve.EASE_IN_OUT),
            animate_position=ft.Animation(280, ft.AnimationCurve.EASE_OUT),
            scale=1.0, opacity=0.9,
        )
        self._net_pulse_overlay = pulse_overlay

        # The graph (canvas + now-playing pulse ring) lives inside an
        # InteractiveViewer so pan/zoom is a NATIVE Flutter GPU transform — zero
        # per-frame Python work and zero bridge traffic, unlike the old drag-pan
        # that re-serialised the whole shape list ~60×/s (its battery/jank cost).
        # The pulse ring sits in the transformed child so it stays glued to its
        # node while panning/zooming. Fixed chrome (mode tabs, legend, tap
        # tooltip) stays OUTSIDE the viewer so it doesn't move.
        graph_stack = ft.Stack(
            controls=[gesture, pulse_overlay],
            width=canvas_w,
            height=canvas_h,
        )
        graph_viewer = ft.InteractiveViewer(
            content=graph_stack,
            pan_enabled=True,
            scale_enabled=True,
            min_scale=0.8,
            max_scale=4.0,
            boundary_margin=ft.Margin.all(160),   # allow panning past the edges
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            width=canvas_w,
            height=canvas_h,
        )
        self._net_viewer = graph_viewer

        stack_controls: list = [graph_viewer]
        if legend_overlay is not None:
            stack_controls.append(legend_overlay)
        stack_controls.append(top_controls_overlay)
        stack_controls.append(tooltip_container)

        stack = ft.Stack(
            controls=stack_controls,
            width=canvas_w,
            height=canvas_h,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

        canvas_container = ft.Container(
            content=stack,
            border=ft.Border.all(1, apply_opacity(0.14, CYAN)),
            border_radius=14,
            bgcolor=BG,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            margin=ft.Margin.symmetric(horizontal=4, vertical=4),
            alignment=ft.Alignment(0, 0),
        )
        # Honour the collapse toggle across rebuilds (depth change, reseed, etc.)
        canvas_container.visible = not self._net_graph_collapsed
        self._net_canvas = canvas_container

        # ── Build ordered track-list panel ─────────────────────────────────
        track_panel = self._build_net_track_list()
        self._net_track_panel = track_panel

        # Start pulse animation token
        self._net_pulse_token += 1
        if self._net_pulse_overlay is not None:
            self.page.run_task(self._run_net_pulse, self._net_pulse_token)

        # Split layout: graph on top (fixed height), ordered track list below
        # (expands + scrolls). track_panel is itself an expanding Column whose
        # last child is the ListView, so keep it a DIRECT child here — no
        # expand-Container wrapper (that unbounds the list; see _build_net_track_list).
        return ft.Column(
            [
                canvas_container,
                track_panel,
            ],
            spacing=6,
            expand=True,
        )

    def _emit_net_shapes(self) -> list:
        """Build the canvas shape list from current node/edge state with enhanced
        neon visuals, radial glow auras, and cyan selection halos.
        """
        canvas_w, canvas_h, _pad = self._net_dims
        mode = self._net_mode
        nbp = self._net_node_by_path
        sel_path = self._net_selected_path

        shapes: list = [
            cv.Rect(0, 0, canvas_w, canvas_h, paint=ft.Paint(color=BG, style=ft.PaintingStyle.FILL)),
        ]

        # Render edges
        for e in self._net_edges:
            a = nbp.get(e["src"])
            b = nbp.get(e["dst"])
            if a is None or b is None:
                continue
            w = max(0.0, min(1.0, float(e.get("weight", 0.5))))
            edge_color = apply_opacity(0.18 + 0.72 * w, a["color"])
            paint = ft.Paint(
                color=edge_color,
                stroke_width=0.9 + 2.5 * w,
                style=ft.PaintingStyle.STROKE,
                stroke_cap=ft.StrokeCap.ROUND,
            )
            elems = [cv.Path.MoveTo(a["px"], a["py"]), cv.Path.LineTo(b["px"], b["py"])]
            if mode == 1:
                dx, dy = b["px"] - a["px"], b["py"] - a["py"]
                length = math.sqrt(dx * dx + dy * dy) or 1.0
                ux, uy = dx / length, dy / length
                al = 7
                elems += [
                    cv.Path.MoveTo(b["px"], b["py"]),
                    cv.Path.LineTo(b["px"] - al * (ux * 0.866 + uy * 0.5),
                                   b["py"] - al * (-ux * 0.5 + uy * 0.866)),
                    cv.Path.MoveTo(b["px"], b["py"]),
                    cv.Path.LineTo(b["px"] - al * (ux * 0.866 - uy * 0.5),
                                   b["py"] - al * (ux * 0.5 + uy * 0.866)),
                ]
            shapes.append(cv.Path(elements=elems, paint=paint))

        # Render node auras & halos first (so they sit underneath main circles)
        for nd in self._net_nodes:
            px, py, r = nd["px"], nd["py"], nd["radius"]
            col = nd["color"]
            is_selected = (nd["path"] == sel_path)

            if nd["is_seed"]:
                # Seed node radial glow aura
                shapes.append(cv.Circle(px, py, r + 9, paint=ft.Paint(color=apply_opacity(0.20, col), style=ft.PaintingStyle.FILL)))
                shapes.append(cv.Circle(px, py, r + 3, paint=ft.Paint(color=apply_opacity(0.75, "#FFFFFF"), stroke_width=1.8, style=ft.PaintingStyle.STROKE)))

            if is_selected:
                # Selected node Cyan Halo ring & aura
                shapes.append(cv.Circle(px, py, r + 11, paint=ft.Paint(color=apply_opacity(0.30, CYAN), style=ft.PaintingStyle.FILL)))
                shapes.append(cv.Circle(px, py, r + 4.5, paint=ft.Paint(color=CYAN, stroke_width=2.4, style=ft.PaintingStyle.STROKE)))

        # Render main node circles & labels
        for nd in self._net_nodes:
            px, py, r = nd["px"], nd["py"], nd["radius"]
            col = nd["color"]

            shapes.append(cv.Circle(px, py, r, paint=ft.Paint(color=col, style=ft.PaintingStyle.FILL)))

            if nd["label"]:
                # Caption pill above the node. Canvas text can't be measured, so
                # size the pill from a GENEROUS per-char estimate (over-estimating
                # only pads the pill; under-estimating spills the text past it —
                # the old bug) and hard-cap the label so it never runs off-node.
                lbl_str = str(nd["label"])
                if len(lbl_str) > 12:
                    lbl_str = lbl_str[:12] + "…"
                pill_w = len(lbl_str) * 6.6 + 10
                pill_x = px - pill_w / 2
                shapes.append(cv.Rect(
                    pill_x, py - r - 17, pill_w, 14, border_radius=4,
                    paint=ft.Paint(color=apply_opacity(0.82, SURFACE2), style=ft.PaintingStyle.FILL),
                ))
                shapes.append(cv.Text(
                    pill_x + 5, py - r - 15.5, lbl_str,
                    style=ft.TextStyle(size=8.5, weight=ft.FontWeight.W_700, color="#FFFFFF"),
                    max_width=pill_w,
                ))

        return shapes

    # ── Ordered track-list panel (split layout, replaces single-node card) ────
    def _node_to_track(self, nd: dict) -> dict:
        """Map a network node into the engine's queue-track schema
        (path + track_title/artist_name/album_title), carrying duration so the
        slider shows the right length before Dart's decoder reports back."""
        path = nd.get("path") or ""
        return {
            "path":        path,
            "track_title": nd.get("title") or os.path.basename(path) or "Unknown",
            "artist_name": nd.get("artist") or "Unknown Artist",
            "album_title": nd.get("album") or "Unknown Album",
            "genre":       nd.get("genre"),
            "duration":    nd.get("duration"),
        }

    def _enqueue_network_tracks(self, tracks: list[dict], replace: bool) -> int:
        """Enqueue an ORDERED block of network tracks. replace=True makes the
        sequence the new queue and plays it IN ORDER — a walk/neighbour chain is
        an intentional ordering, so shuffle is forced off first (otherwise Dart
        would scramble the very sequence the graph shows). replace=False appends
        the block in one batched op via queue_extend, respecting current shuffle."""
        tracks = [t for t in tracks if t.get("path")]
        if not tracks:
            return 0
        if replace:
            if audio_engine.is_shuffle:
                # Set the flag directly (skips a redundant Dart set_shuffle op);
                # the set_queue push below re-declares shuffle=False anyway.
                audio_engine._is_shuffle = False
                try:
                    self.app.now_playing.update_shuffle(False)
                except Exception:
                    pass
                try:
                    self.app._save_pref("is_shuffle", False)
                except Exception:
                    pass
            # A live Similar/Auto-DJ session would otherwise append over the walk.
            if getattr(self.app, "play_similar_mode", False):
                self.app.play_similar_mode = False
            audio_engine.play_similar_seed_path = ""
            audio_engine.jarvis_controlled = False
            audio_engine.set_queue(tracks, start_index=0)
        else:
            audio_engine.queue_extend(tracks)
        return len(tracks)

    def _build_net_track_list(self) -> ft.Control:
        """Panel beneath the graph: a header with batch Play/Add over a
        scrollable, ordered list of the walk steps (or neighbourhood). Each row
        focuses its node on the graph and offers a one-tap add; long-press opens
        reseed / start-walk-here / play-from-here."""
        nodes = self._net_nodes
        is_walk = (self._net_mode == 1)
        title_txt = "The Walk" if is_walk else "Neighbourhood"
        count = len(nodes)

        def _play_all(_e):
            self.app.trigger_haptic("network_walk")
            n = self._enqueue_network_tracks([self._node_to_track(nd) for nd in nodes], replace=True)
            if n:
                self.app.show_snackbar(f"Playing {title_txt.lower()} · {n} tracks",
                                       icon=ft.Icons.PLAY_ARROW_ROUNDED, color=CYAN)

        def _add_all(_e):
            self.app.trigger_haptic("swipe_queue")
            n = self._enqueue_network_tracks([self._node_to_track(nd) for nd in nodes], replace=False)
            if n:
                self.app.show_snackbar(f"Added {n} tracks to queue",
                                       icon=ft.Icons.PLAYLIST_ADD_ROUNDED, color=CYAN)

        def _batch_btn(label, icon_name, on_click, primary=False):
            return ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(icon_name, size=14, color=BG if primary else CYAN),
                        ft.Text(label, size=11, weight=ft.FontWeight.W_800, color=BG if primary else CYAN),
                    ],
                    spacing=5, tight=True,
                ),
                bgcolor=CYAN if primary else apply_opacity(0.14, CYAN),
                border=None if primary else ft.Border.all(1, apply_opacity(0.35, CYAN)),
                border_radius=9,
                padding=ft.Padding.symmetric(horizontal=13, vertical=7),
                on_click=on_click,
            )

        # Collapse/expand toggle: the chevron points at what a tap will reveal —
        # UP brings the graph back (it lives above), DOWN focuses the list (below).
        self._net_collapse_icon = ft.Icon(
            ft.Icons.KEYBOARD_ARROW_UP_ROUNDED if self._net_graph_collapsed
            else ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED,
            color=CYAN, size=22,
        )
        collapse_btn = ft.Container(
            content=self._net_collapse_icon,
            tooltip="Show graph" if self._net_graph_collapsed else "Hide graph — focus list",
            on_click=self._toggle_net_graph_focus,
            padding=ft.Padding.all(2),
            border_radius=8,
        )

        header = ft.Container(
            content=ft.Row(
                [
                    ft.Row(
                        [
                            collapse_btn,
                            ft.Column(
                                [
                                    ft.Text(title_txt.upper(), size=11, weight=ft.FontWeight.W_800, color=CYAN),
                                    ft.Text(f"{count} track{'s' if count != 1 else ''}",
                                            size=9, color=DIM),
                                ],
                                spacing=0, tight=True,
                            ),
                        ],
                        spacing=4, tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            _batch_btn("Play", ft.Icons.PLAY_ARROW_ROUNDED, _play_all, primary=True),
                            _batch_btn("Add", ft.Icons.PLAYLIST_ADD_ROUNDED, _add_all),
                        ],
                        spacing=6, tight=True,
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.only(left=4, right=6, top=6, bottom=8),
        )

        self._net_list_view = ft.ListView(
            controls=[self._build_net_list_row(i, nd, is_walk) for i, nd in enumerate(nodes)],
            spacing=4,
            expand=True,
            padding=ft.Padding.only(left=4, right=4, bottom=12),
        )

        # NB: the ListView must be a DIRECT child of an expanding Column so it
        # gets a tightly-bounded height — wrapping it in an expand Container
        # leaves it effectively unbounded (blank subtree + no scroll).
        return ft.Column(
            [header, self._net_list_view],
            spacing=2,
            expand=True,
        )

    def _build_net_list_row(self, i: int, nd: dict, is_walk: bool) -> ft.Control:
        path = nd.get("path") or ""
        title = nd.get("title") or os.path.basename(path) or "Unknown"
        artist = nd.get("artist") or "Unknown Artist"
        color = nd.get("color") or CYAN
        is_sel = (path == self._net_selected_path)
        is_now = bool(path) and (path == (audio_engine.current_path or ""))
        is_seed = bool(nd.get("is_seed"))
        lead_txt = str(i) if is_walk else ("◆" if is_seed else "")

        def _focus(_e):
            # Highlight the node on the graph (halo) + this row. Panning to it is
            # the user's job via the InteractiveViewer; we don't move nodes.
            self.app.trigger_haptic("network_tap")
            self._net_selected_path = path
            self._redraw_net_canvas()
            self._refresh_net_list_selection()

        def _add_one(_e):
            self.app.trigger_haptic("swipe_queue")
            audio_engine.queue_last(self._node_to_track(nd))
            self.app.show_snackbar(f"'{title}' added to queue",
                                   icon=ft.Icons.PLAYLIST_ADD_ROUNDED, color=CYAN)

        lead = ft.Container(
            content=ft.Text(lead_txt, size=10, weight=ft.FontWeight.W_800,
                            color=CYAN if (is_sel or is_now) else DIM),
            width=20, alignment=ft.Alignment(0, 0),
        )
        dot = ft.Container(width=9, height=9, border_radius=5, bgcolor=color)
        text_col = ft.Column(
            [
                ft.Text(title, size=12.5,
                        weight=ft.FontWeight.W_700 if (is_sel or is_now) else ft.FontWeight.W_500,
                        color=CYAN if is_now else TEXT, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Text(artist, size=10, color=DIM, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ],
            spacing=1, expand=True, tight=True,
        )
        trailing = (
            ft.Icon(ft.Icons.GRAPHIC_EQ_ROUNDED, size=16, color=CYAN) if is_now
            else ft.IconButton(
                icon=ft.Icons.ADD_ROUNDED, icon_size=18, icon_color=CYAN,
                tooltip="Add to queue", on_click=_add_one,
                style=ft.ButtonStyle(padding=ft.Padding.all(2)),
            )
        )

        row = ft.Container(
            content=ft.Row([lead, dot, text_col, trailing], spacing=8,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=(apply_opacity(0.14, CYAN) if is_sel
                     else apply_opacity(0.07, CYAN) if is_now else SURFACE),
            border=ft.Border.all(1, apply_opacity(0.4, CYAN) if is_sel
                                 else apply_opacity(0.16, CYAN) if is_now else BORDER),
            border_radius=10,
            padding=ft.Padding.only(left=6, right=2, top=5, bottom=5),
            on_click=_focus,
        )
        return ft.GestureDetector(
            content=row,
            key=f"netrow_{i}",
            on_long_press_start=lambda e: self._net_row_context_menu(i, nd),
        )

    def _refresh_net_list_selection(self):
        """Rebuild the track-list rows to reflect a new selection / now-playing
        row. Cheap: the list is bounded by walk length / neighbour density
        (≤48 rows), so a full rebuild is well under a frame."""
        if self._net_list_view is None:
            return
        is_walk = (self._net_mode == 1)
        self._net_list_view.controls = [
            self._build_net_list_row(i, nd, is_walk) for i, nd in enumerate(self._net_nodes)
        ]
        self.try_update(self._net_list_view)

    def _net_row_context_menu(self, index: int, nd: dict):
        """Long-press menu for a track row: reseed the graph here, start a walk
        here, play the sequence from this point on, or add to a playlist."""
        self.app.trigger_haptic("long_press")
        path = nd.get("path") or ""
        title = nd.get("title") or "this track"
        meta = self._node_to_track(nd)
        bs_holder = [None]

        def _close():
            if bs_holder[0]:
                bs_holder[0].open = False
                bs_holder[0].update()
                self.page.update()

        def _play_from_here(_e):
            _close()
            self.app.trigger_haptic("network_walk")
            tail = [self._node_to_track(n) for n in self._net_nodes[index:]]
            n = self._enqueue_network_tracks(tail, replace=True)
            if n:
                self.app.show_snackbar(f"Playing {n} tracks from '{title}'",
                                       icon=ft.Icons.PLAY_ARROW_ROUNDED, color=CYAN)

        def _reseed(_e):
            _close()
            self.app.trigger_haptic("network_reseed")
            self._network_seed_path = path
            self.page.run_task(self.load_library)

        def _walk_here(_e):
            _close()
            self.app.trigger_haptic("network_walk")
            self._network_seed_path = path
            self.selected_network_index = 1
            self.page.run_task(self.load_library)

        def _add_to_playlist(_e):
            self.page.run_task(self._open_add_to_playlist_sheet, meta, bs_holder[0])

        bs = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(title, color=CYAN, weight=ft.FontWeight.W_800, size=14,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Divider(color=BORDER),
                        ft.ListTile(
                            leading=ft.Icon(ft.Icons.PLAY_ARROW_ROUNDED, color=CYAN),
                            title=ft.Text("Play from here", color=TEXT),
                            on_click=_play_from_here,
                        ),
                        ft.ListTile(
                            leading=ft.Icon(ft.Icons.CENTER_FOCUS_WEAK_ROUNDED, color=CYAN),
                            title=ft.Text("Reseed graph here", color=TEXT),
                            on_click=_reseed,
                        ),
                        ft.ListTile(
                            leading=ft.Icon(ft.Icons.SHUFFLE_ROUNDED, color=CYAN),
                            title=ft.Text("Start a walk here", color=TEXT),
                            on_click=_walk_here,
                        ),
                        ft.ListTile(
                            leading=ft.Icon(ft.Icons.PLAYLIST_ADD_ROUNDED, color=LIB_PLAYLIST_COLOR),
                            title=ft.Text("Add to Playlist", color=TEXT),
                            on_click=_add_to_playlist,
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

    def _redraw_net_canvas(self):
        """Regenerate the canvas shape list in place. Only called when geometry
        actually changes — node selection, walk stepping — i.e. discrete user
        actions, never a per-pointer-move loop."""
        if self._net_canvas_obj is None:
            return
        self._net_canvas_obj.shapes = self._emit_net_shapes()
        self.try_update(self._net_canvas_obj)

    def _reset_net_view(self, _e=None):
        """Snap the graph's pan/zoom back to the seed-centred fit via the
        InteractiveViewer's native reset() — no rebuild, no DB, no bridge churn."""
        self.app.trigger_haptic("network_tap")
        viewer = self._net_viewer
        if viewer is None:
            return
        try:
            viewer.reset()
        except Exception as exc:
            logger.debug("net view reset failed: %s", exc)

    def _toggle_net_graph_focus(self, _e=None):
        """Collapse/expand the graph so the track list can take the whole pane.
        Just flips the canvas's visibility — the list Column's expand child then
        fills the freed space. No rebuild, no DB, no graph re-layout; the state
        persists across rebuilds via _net_graph_collapsed."""
        self.app.trigger_haptic("network_tap")
        self._net_graph_collapsed = not self._net_graph_collapsed
        collapsed = self._net_graph_collapsed
        if self._net_canvas is not None:
            self._net_canvas.visible = not collapsed
        if self._net_collapse_icon is not None:
            self._net_collapse_icon.name = (
                ft.Icons.KEYBOARD_ARROW_UP_ROUNDED if collapsed
                else ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED
            )
            self._net_collapse_icon.tooltip = (
                "Show graph" if collapsed else "Hide graph — focus list"
            )
        # Re-render the whole network subtree so the canvas drop and the list's
        # re-expansion both flush in one coalesced update.
        self.try_update(self._animated_list_wrapper)

    def _net_set_pulse_node(self, nd: dict | None):
        """Move the now-playing ring onto `nd` (or hide it when None). The
        overlay is already mounted, so a bare update flushes it; animate_position
        glides it from the old node to the new one."""
        ov = self._net_pulse_overlay
        if ov is None:
            return
        if nd is None:
            if ov.visible:
                ov.visible = False
                self.try_update(ov)
            return
        d = (nd["radius"] + 9) * 2
        ov.width = d
        ov.height = d
        ov.border_radius = d / 2
        ov.left = nd["px"] - d / 2
        ov.top = nd["py"] - d / 2
        ov.scale = 1.0
        ov.opacity = 0.9
        ov.visible = True
        self.try_update(ov)

    def _sync_network_now_playing(self, prev_path, current_path):
        """React to a track change while the Network view is live, without a
        full rebuild when possible. If the new track is already a node we just
        move the pulse ring onto it in place (covers Walk auto-advance and
        playing a visible Local neighbour). If it has fallen off the graph and
        follow-current is on, we reseed so the graph recenters on it."""
        nbp = self._net_node_by_path
        if not nbp:
            # No graph mounted yet (empty/setup state) — seed it from the live
            # track if we're meant to follow.
            if self._net_follow_current and current_path:
                self._schedule_net_reseed()
            return

        if prev_path:
            old = nbp.get(prev_path)
            if old is not None:
                old["is_now_playing"] = False

        new = nbp.get(current_path) if current_path else None
        if new is not None:
            new["is_now_playing"] = True
            self._net_set_pulse_node(new)
            self._refresh_net_list_selection()  # move the ♫ marker to the live row
            return

        # The active track isn't on the current graph.
        if self._net_follow_current and current_path:
            self._schedule_net_reseed()         # recenter on the live track
        else:
            self._net_set_pulse_node(None)      # not following → just hide it

    def _schedule_net_reseed(self, delay: float = 0.35):
        """Debounced rebuild of the network around the live track. Collapses a
        burst of rapid skips into a single DB+canvas rebuild instead of one per
        skipped track; the latest call wins (older pending rebuilds cancelled)."""
        self._network_seed_path = None
        task = self._net_reseed_task
        if task is not None and not task.done():
            task.cancel()

        async def _run():
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                await self.load_library()
            except asyncio.CancelledError:
                pass

        self._net_reseed_task = asyncio.create_task(_run())

    def _toggle_net_follow(self, _e=None):
        self._net_follow_current = not self._net_follow_current
        on = self._net_follow_current
        icon = self._net_follow_icon
        if icon is not None:
            icon.name = ft.Icons.MY_LOCATION if on else ft.Icons.LOCATION_SEARCHING
            icon.color = CYAN if on else DIM
        btn = self._net_follow_btn
        if btn is not None:
            btn.border = ft.Border.all(
                1, apply_opacity(0.22, CYAN) if on else apply_opacity(0.14, TEXT)
            )
        self.try_update(icon, btn)
        # Confirm the toggle so its effect is legible even when no track is
        # currently advancing (otherwise it only shows on the next track change).
        try:
            self.app.show_snackbar(
                "Following the playing track" if on
                else "Free exploration — graph stays put",
                icon=ft.Icons.MY_LOCATION if on else ft.Icons.LOCATION_SEARCHING,
                color=CYAN,
            )
        except Exception:
            pass
        # Turning follow back on snaps straight to the live track.
        if on:
            cur = audio_engine.current_path or ""
            if cur and (self._network_seed_path or self._net_node_by_path.get(cur) is None):
                self._schedule_net_reseed(delay=0.0)  # explicit tap → no debounce
            elif cur:
                self._net_set_pulse_node(self._net_node_by_path.get(cur))

    def _show_tuning_popup(self, e):
        from utils.streamrip_api import get_walk_params, update_config_params
        temp_val, mmr_val = get_walk_params()

        # Labels for display
        mmr_label = ft.Text(f"Avoid Duplicates (MMR): {mmr_val:.2f}", color=TEXT, size=11, weight=ft.FontWeight.W_700)
        temp_label = ft.Text(f"Variety (Temperature): {temp_val:.2f}", color=TEXT, size=11, weight=ft.FontWeight.W_700)

        def _get_slider_color(val, max_val):
            ratio = min(1.0, max(0.0, val / max_val))
            # Green (rgb(34, 197, 94)) to Red (rgb(239, 68, 68))
            r = int(34 + (239 - 34) * ratio)
            g = int(197 + (68 - 197) * ratio)
            b = int(94 + (68 - 94) * ratio)
            return f"#{r:02x}{g:02x}{b:02x}"

        dlg_holder = [None]

        # Action handlers
        def _on_mmr_change(e):
            val = float(e.control.value)
            mmr_label.value = f"Avoid Duplicates (MMR): {val:.2f}"
            c = _get_slider_color(val, 0.4)
            e.control.active_color = c
            e.control.thumb_color = c
            mmr_label.update()
            e.control.update()

        def _on_mmr_change_end(e):
            val = float(e.control.value)
            update_config_params({"general": {"walk_mmr_lambda": val}})
            self.page.run_task(self.load_library)

        def _on_temp_change(e):
            val = float(e.control.value)
            temp_label.value = f"Variety (Temperature): {val:.2f}"
            c = _get_slider_color(val, 0.8)
            e.control.active_color = c
            e.control.thumb_color = c
            temp_label.update()
            e.control.update()

        def _on_temp_change_end(e):
            val = float(e.control.value)
            update_config_params({"general": {"walk_temperature": val}})
            self.page.run_task(self.load_library)

        mmr_slider = ft.Slider(
            min=0.0, max=0.4, value=mmr_val,
            divisions=40,
            active_color=_get_slider_color(mmr_val, 0.4),
            thumb_color=_get_slider_color(mmr_val, 0.4),
            on_change=_on_mmr_change,
            on_change_end=_on_mmr_change_end,
        )

        temp_slider = ft.Slider(
            min=0.0, max=0.8, value=temp_val,
            divisions=80,
            active_color=_get_slider_color(temp_val, 0.8),
            thumb_color=_get_slider_color(temp_val, 0.8),
            on_change=_on_temp_change,
            on_change_end=_on_temp_change_end,
        )

        def _close_dialog(_e):
            if dlg_holder[0]:
                dlg_holder[0].open = False
                dlg_holder[0].update()

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Icon(ft.Icons.TUNE_ROUNDED, color=CYAN, size=18),
                    ft.Text("Walk Parameters", size=14, weight=ft.FontWeight.W_800, color=TEXT),
                ],
                spacing=8,
            ),
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text("Fine-tune how walk paths are selected. Changes take effect immediately.", color=DIM, size=10),
                        ft.Container(height=4),
                        mmr_label,
                        mmr_slider,
                        ft.Text("Higher values penalize similarity to already-played tracks to prevent duplicate tracks and alternate mixes.", color=DIM, size=9),
                        ft.Container(height=6),
                        temp_label,
                        temp_slider,
                        ft.Text("Higher values introduce random variety in neighbor selections. Low values keep choices deterministic.", color=DIM, size=9),
                    ],
                    spacing=2,
                    tight=True,
                ),
                width=260,
            ),
            actions=[
                ft.TextButton(
                    "Dismiss",
                    style=ft.ButtonStyle(color=CYAN),
                    on_click=_close_dialog,
                )
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=SURFACE2,
        )
        dlg_holder[0] = dlg
        self.page.overlay.append(dlg)
        dlg.open = True
        self.page.update()

    async def _run_net_pulse(self, token: int):
        """Pulse the now-playing ring until a newer build supersedes this token.
        The scale/opacity transitions are animated client-side; we just toggle
        the targets each beat. Tokened so only one loop runs at a time.

        Battery: while the app is backgrounded or the Network view isn't the
        active library mode, we stop pushing updates (and idle at a slow tick),
        so an off-screen pulse never drives the Flet→Flutter bridge. The loop
        resumes pushing the moment the view is foregrounded again."""
        await asyncio.sleep(1.0)  # let finalize mount the overlay first
        big = True
        settled = False
        while token == self._net_pulse_token:
            ov = self._net_pulse_overlay
            if ov is None:
                return
            # Only animate (and drive the bridge) when the ring is actually on
            # screen AND the track is advancing. Off-screen / backgrounded /
            # ring-hidden / paused all idle without pushing updates — the pulse
            # animation is this view's one continuous draw, so freezing it while
            # a track is paused is the main remaining battery saving.
            on_screen = (
                not getattr(self.app, "is_background", False)
                and self.view_mode == "network"
                and getattr(ov, "visible", True)
            )
            if not on_screen or not getattr(audio_engine, "is_playing", True):
                # Settle the ring to a calm, fully-formed state once so a paused
                # track doesn't freeze mid-fade; then idle without pushing.
                if not settled and on_screen:
                    try:
                        ov.scale = 1.0
                        ov.opacity = 0.9
                        ov.update()
                    except Exception:
                        return
                    settled = True
                await asyncio.sleep(0.6)
                continue
            settled = False
            try:
                ov.scale = 1.4 if big else 1.0
                ov.opacity = 0.25 if big else 0.9
                ov.update()
            except Exception:
                return
            big = not big
            await asyncio.sleep(0.7)

    def _update_view_tabs(self):
        from utils.streamrip_api import load_config
        try:
            cfg = load_config()
            appearance = cfg.get("appearance", {})
        except:
            appearance = {}

        show_playlists = bool(appearance.get("show_playlists", True))
        show_artists = bool(appearance.get("show_artists", True))
        show_albums = bool(appearance.get("show_albums", True))
        show_tracks = bool(appearance.get("show_tracks", True))
        show_network = bool(appearance.get("show_network", False))

        visible_modes = []
        if show_network: visible_modes.append("network")
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
            "network":   ft.Icons.HUB_ROUNDED,
        }
        accents = {
            "playlists": LIB_PLAYLIST_COLOR,
            "artists":   LIB_ARTIST_COLOR,
            "albums":    LIB_ALBUM_COLOR,
            "tracks":    LIB_TRACK_COLOR,
            "network":   CYAN,
        }
        tabs = []

        all_modes = [
            ("network", "Network", show_network),
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
        self._next_page_btn.icon_color = DIM if self.current_page >= self.total_pages - 1 else CYAN

        self._pagination_bar.visible = self.total_pages > 1 and (
            self.view_mode in ("tracks", "albums", "artists")
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
            elif self.view_mode == "network":
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

        is_dual_pane = self.view_mode == "network"
        if not is_dual_pane:
            old_content = self._animated_list_wrapper.content
            self._animated_list_wrapper.content = self._library_list
            if old_content != self._library_list:
                self.try_update(self._animated_list_wrapper)

        self._last_highlighted_path = audio_engine.current_path or None

        self._search_spinner.visible = True
        self._library_list.controls.clear()
        self._path_to_controls.clear()
        self._empty_label.visible = False
        self._pagination_bar.visible = False

        self.try_update(
            self._search_spinner,
            self._library_list,
            self._empty_label,
            self._pagination_bar,
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

            elif self.view_mode == "network":
                first_chunk = []

                # Fast existence check — no blob deserialization
                has_pca = await self.app.db_manager.has_pca_coords()

                if not has_pca:
                    setup_card = ft.Container(
                        content=ft.Column(
                            [
                                ft.Container(
                                    content=ft.Icon(ft.Icons.HUB_ROUNDED, color=CYAN, size=40),
                                    bgcolor=apply_opacity(0.1, CYAN),
                                    border_radius=20,
                                    padding=16,
                                ),
                                ft.Text("Acoustic Network Graph", color=TEXT, size=18, weight=ft.FontWeight.W_700, text_align=ft.TextAlign.CENTER),
                                ft.Text(
                                    "No PCA coordinates found. Please analyze your library first to construct the network.",
                                    color=DIM, size=13, text_align=ft.TextAlign.CENTER, max_lines=3,
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
                    self._flat_rows = []
                    stats_text = "0 TRACKS"
                else:
                    neighbors_list = []
                    walk_paths = []

                    # Determine seed path and fetch graph data FIRST,
                    # then only request PCA coords for the relevant paths.
                    if self.selected_network_index == 0:  # Local
                        current_path = self._network_seed_path or audio_engine.current_path or ""
                        if not current_path:
                            # Grab any single path that has PCA coords
                            conn = await self.app.db_manager.get_connection()
                            async with conn.execute(
                                "SELECT track_path FROM play_counts WHERE pca_coords IS NOT NULL LIMIT 1"
                            ) as cur:
                                row = await cur.fetchone()
                                current_path = row[0] if row else ""

                        if current_path:
                            from utils import track_graph as tg
                            neighbors_list = await tg.neighbors(self.app.db_manager, current_path, k=self._net_k_neighbors)

                        # Collect only the paths we need PCA coords for
                        needed_paths = [current_path] if current_path else []
                        needed_paths.extend(n["path"] for n in neighbors_list)
                        pca_rows = await self.app.db_manager.get_tracks_pca_coords_for_paths(needed_paths)

                    elif self.selected_network_index == 1:  # Walk
                        current_path = self._network_seed_path or audio_engine.current_path or ""
                        if not current_path:
                            conn = await self.app.db_manager.get_connection()
                            async with conn.execute(
                                "SELECT track_path FROM play_counts WHERE pca_coords IS NOT NULL LIMIT 1"
                            ) as cur:
                                row = await cur.fetchone()
                                current_path = row[0] if row else ""

                        if current_path:
                            from utils import track_graph as tg
                            from utils.streamrip_api import get_walk_params
                            temp, mmr = get_walk_params()
                            walk_paths = await tg.walk(
                                self.app.db_manager,
                                current_path,
                                length=self._net_walk_length,
                                mmr_lambda=mmr,
                                temperature=temp,
                            )

                        needed_paths = [current_path] if current_path else []
                        needed_paths.extend(walk_paths)
                        pca_rows = await self.app.db_manager.get_tracks_pca_coords_for_paths(needed_paths)

                    else:
                        current_path = audio_engine.current_path or ""
                        pca_rows = []

                    stats_text = f"{len(pca_rows)} TRACKS"
                    
                    self._tracks_cache = pca_rows
                    self._tracks_cache_key = ("network", self.search_query, self.sort_mode)

                    interactive_canvas = self._build_interactive_network_canvas(
                        pca_rows, self.selected_network_index,
                        current_path, neighbors_list, walk_paths,
                    )
                    first_chunk.append(interactive_canvas)
                    self._flat_rows = []
                
                if self._load_token != token:
                    return
                    
                def finalize_network():
                    self._stats_label.text = stats_text
                    self._search_spinner.visible = False
                    
                    is_empty = not first_chunk
                    if is_empty:
                        self._library_list.controls.clear()
                        self._empty_label.visible = True
                        self._empty_label.content.controls[0].name = ft.Icons.HUB_ROUNDED
                        self._empty_label.content.controls[0].color = apply_opacity(0.3, CYAN)
                        self._empty_label.content.controls[1].value = "No network coordinates found."
                        self._empty_label.content.controls[2].value = "Make sure your tracks are analyzed."
                        self._empty_label.content.controls[3].visible = False
                        self._empty_label.content.controls[4].visible = False
                        self._animated_list_wrapper.content = self._library_list
                    else:
                        # Assign the split-layout Column (graph + scrolling list)
                        # DIRECTLY as the wrapper's content — the same relationship
                        # the normal library ListView uses, which reliably gives the
                        # inner ListView a bounded height. An intermediate padded
                        # expand-Container here left the list unbounded, which blanks
                        # the whole pane (no graph, no scroll).
                        self._animated_list_wrapper.content = first_chunk[0]
                    
                    self._update_pagination_ui()
                    # NB: no per-control .update() / page.update() here — dispatch
                    # via safe_update so a single coalesced page.update() flushes
                    # the canvas to the client. Calling finalize directly (the old
                    # behaviour) left the freshly-built canvas unpainted until the
                    # next unrelated safe_update fired, which read as a ~30s "draw"
                    # delay even though the Python build is ~3 ms.

                self.app.safe_update(finalize_network)


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

        # Network view tracks the playing node on the canvas, not list rows.
        if self.view_mode == "network":
            self._sync_network_now_playing(prev_path, current_path)
            self._last_highlighted_path = current_path
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

    def _build_partition_track_row(self, t: dict, partition_tracks: list[dict], depth: int = 0) -> ft.Control:
        res = self._track_row(t, depth=depth)
        path = t.get("path", "")
        tile = res.content.content

        def play_partition_track(_e):
            self._tracks_cache = partition_tracks
            self._tracks_cache_key = ("network", self.search_query, self.sort_mode)
            self.page.run_task(self.app.play_track, path, ("library", None))

        tile.on_click = play_partition_track
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
        self._cached_unanalysed = None
        self._scan_update_count = 0
        self._is_scanning = False
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
