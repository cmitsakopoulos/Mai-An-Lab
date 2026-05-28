"""
StreamripApp; Full Fidelity Flet Rewrite.
Replaces all Kivy / KivyMD code while maintaining 1:1 UX parity, 
animations, and functional details from the original.

- [x] Fix SELinux denial by setting `PYTHONMALLOC=malloc` in `main.py`
- [x] Improve `AudioEngine` initialization robustness in `audio_engine.py`
- [/] Add diagnostic logging to `_load_src`
- [ ] Verify build configuration in `pyproject.toml`
"""
import os
import sys

# FIX: Avoid SELinux denial for 'max_map_count' on Android 11+
# This must be set before the Python interpreter fully initializes native allocators.
os.environ["PYTHONMALLOC"] = "malloc"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

import pathlib
import tempfile

def get_app_dir() -> str:
    """Returns the primary writable directory for the app, prioritizing 'files'."""
    # Priority order: Standard Android files dir, then Flet storage, then Home
    for env_var in ("APP_FILES_PATH", "FILES_DIR", "INTERNAL_STORAGE", "FLET_APP_STORAGE_DATA", "HOME"):
        val = os.getenv(env_var)
        if val and os.path.isdir(val):
            return val
    # Fallback to temp dir
    return tempfile.gettempdir()

def get_temp_artwork_dir() -> str:
    """Returns the dedicated directory for temporary artwork, creating it and a .nomedia file if missing."""
    dir_path = os.path.join(get_app_dir(), "temp")
    try:
        os.makedirs(dir_path, exist_ok=True)
        # Create .nomedia file to exclude from Android's Media Store / Gallery
        nomedia_file = os.path.join(dir_path, ".nomedia")
        if not os.path.exists(nomedia_file):
            with open(nomedia_file, "w") as f:
                pass
    except Exception:
        # Fallback to get_app_dir if temp subdirectory creation fails
        return get_app_dir()
    return dir_path

# CRITICAL: SET THESE BEFORE ANY OTHER IMPORTS
DATA_DIR = get_app_dir()
os.environ["HOME"] = DATA_DIR
os.environ["XDG_CONFIG_HOME"] = DATA_DIR
os.environ["XDG_CACHE_HOME"] = os.path.join(DATA_DIR, ".cache")
os.makedirs(os.environ["XDG_CACHE_HOME"], exist_ok=True)

# MONKEYPATCH pathlib.Path.home to prevent it from returning '/data' on Android
def _hijacked_home(cls):
    return pathlib.Path(DATA_DIR)
pathlib.Path.home = classmethod(_hijacked_home)


import time
import logging
import functools
import platform
import re
import json
import asyncio
import math
import shutil
import hashlib
import threading
import urllib.request
import subprocess
from io import BytesIO
from pathlib import Path
from datetime import datetime

# ─── JSON Helpers ─────────────────────────────────────────────────────────────
def json_serial(obj):
    """JSON serializer for objects not serializable by default json code"""
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")

def safe_json_dump(data, fh, indent=None):
    """Safely dump data to a file handle, converting sets to lists."""
    json.dump(data, fh, indent=indent, default=json_serial)

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

@functools.lru_cache(maxsize=128)
def get_asset_path(path: str) -> str:
    """Returns path as-is; desktop Flet loads images directly from disk."""
    return path or ""



import flet as ft
# Hard import required for flet build to include the audioplayers flutter plugin in the APK
import flet_audio
try:
    from flet_audio import AudioContext, AudioContextConfig, AudioContextConfigFocus
except ImportError:
    AudioContext = AudioContextConfig = AudioContextConfigFocus = None

from utils.streamrip_api import (
    load_config, update_config_params, download,
    get_config_path, repair_config, get_default_download_path,
)
if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import ui.tokens as _tokens
from ui.tokens import (
    BG, SURFACE, SURFACE2, CYAN, AMBER, TEXT, DIM, BORDER, SOURCE_COLORS,
    LIB_ARTIST_COLOR, LIB_ALBUM_COLOR, LIB_TRACK_COLOR, LIB_PLAYLIST_COLOR, LIB_PARTITION_COLOR,
    apply_opacity
)

def _apply_accent(color: str) -> None:
    """Propagate a new accent colour to ui.tokens and every module that
    imported CYAN from it via 'from ui.tokens import CYAN'."""
    import sys
    _tokens.CYAN = color
    # Patch every already-imported module that bound its own local 'CYAN'.
    _accent_modules = [
        "ui.tokens",
        "ui.widgets",
        "ui.views.library",
        "ui.views.search",
        "ui.views.settings",
        "ui.views.assistant",
        "ui.player.mini_player",
        "ui.player.now_playing",
        "ui.player.queue_sheet",
        "ui.player.dialogs",
        "ui.player.quality_selector",
        "utils.queue_controller",
        "__main__",
    ]
    for mod_name in _accent_modules:
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "CYAN"):
            mod.CYAN = color
from ui.widgets import (
    _ARTWORK_CACHE, fmt_time, src_color, strip_markup, NotificationSystem,
    AnimatedEntry, ScaleButton, OnyxButton, GlassCard, MenuTextItem, AppSearchBar,
    SourceSegment, SettingsHeader, HubSettingItem, AccordionCard, SkeletonRow
)
from ui.views.search import SearchView
from ui.views.library import LibraryView
from ui.views.settings import SettingsView
from ui.views.assistant import AssistantView
from ui.player.mini_player import MiniPlayerBar
from ui.player.now_playing import NowPlayingSheet
from ui.player.queue_sheet import QueueSheet
from ui.player.quality_selector import QualitySelectorSheet
from ui.player.dialogs import PlaylistEditorDialog, MetadataEditorDialog
from utils.queue_controller import QueueController
from utils.error_boundary import ErrorBoundary

# ─── Main App Coordinator ──────────────────────────────────────────────────────
class StreamripFletApp:
    def __init__(self, page: ft.Page):
        self.page   = page
        self.error_boundary = ErrorBoundary(page, on_restart=self.initialize)
        self.is_scrubbing     = False
        self.target_folder    = ""
        self.library_folder   = ""
        self.download_history_list: list[dict] = []
        self._prefs_path      = os.path.join(DATA_DIR, "flet_prefs.json")
        self._prefs: dict = {}

        # Batched safe_update state
        self._pending_fns: list = []
        self._update_lock  = asyncio.Lock()
        self._flush_pending = False
        self._is_restarting = False
        self.is_background  = False
        self.is_restoring_session = False

        # Taste-model / playback-event tracking. _last_played_path holds the
        # path that was most recently *playing* so we can fire a single
        # record_play_event when playback transitions away from it (via skip,
        # natural end, or stop). _last_play_position is the latest reported
        # position on that track. _last_play_duration is its total duration.
        # _explicit_feedback_cache is a per-process map of path -> True/False
        # used to keep the playback-bar like/dislike buttons in sync without
        # re-querying the DB on every track change.
        self._last_played_path: str = ""
        self._last_play_position: float = 0.0
        self._last_play_duration: float = 0.0
        self._explicit_feedback_cache: dict[str, bool] = {}
        self.play_similar_mode: bool = False
        self._play_similar_gen: int = 0

        # ── Session-scoped negative centroid ─────────────────────────────
        # Transient signal that powers the "this chain went bad" guardrail
        # on top of the global taste model. None of this is persisted —
        # the goal is to react inside one listening session, then reset.
        #   * _session_bad_paths: paths the user just rejected (skip/dislike).
        #     Walk() penalises candidates whose timbre is close to any of
        #     these, similar to the existing MMR diversity term.
        #   * _session_last_liked_path: most recent track that earned a
        #     positive signal in this session. Used as a fallback seed
        #     when the trip-wire trips, so we anchor back to something
        #     known-good instead of the bad track currently playing.
        #   * _session_recent_outcomes: rolling label for the last few
        #     continuation picks (True = kept/engaged, False = rejected).
        #     Trip-wire fires when ≥2 of the last 3 went bad.
        from collections import deque
        self._session_bad_paths: deque[str] = deque(maxlen=10)
        self._session_last_liked_path: str | None = None
        self._session_recent_outcomes: deque[bool] = deque(maxlen=3)

    def _show_error(self, e=None):
        """Surfaces critical errors to the full-screen ErrorBoundary."""
        if hasattr(self, "error_boundary"):
            self.error_boundary._show_error(e)
        else:
            self.show_snackbar(f"Critical Error: {e}")

    def _on_lifecycle(self, e):
        # e.data can be: "resumed", "inactive", "hidden", "detached"
        # We suspend UI updates when hidden or inactive (background/multitasking)
        was_bg = self.is_background
        self.is_background = e.data in ("hidden", "inactive", "detached")
        
        if self.is_background != was_bg:
            if self.is_background:
                logger.info(f"App lifecycle: {e.data} - Suspending UI updates")
                if hasattr(self, "assistant_view") and self.assistant_view:
                    self.assistant_view.handle_app_background()
            else:
                logger.info(f"App lifecycle: {e.data} - Resuming UI updates")
                if hasattr(self, "assistant_view") and self.assistant_view:
                    self.assistant_view.handle_app_resume()
                # Force-sync current highlights when returning to foreground
                if hasattr(self, "library_view"):
                    self.library_view.refresh_now_playing()
                if hasattr(self, "search_view"):
                    self.search_view.refresh_now_playing()
                # When returning to foreground, force a single update to sync state
                self.safe_update(lambda: None)

    async def initialize(self):
        # PHASE 1: Immediate Splash Render (< 50ms)
        self.page.clean()
        self.page.on_app_lifecycle_state_change = self._on_lifecycle
        self._splash = self._build_splash()
        self.page.add(self._splash)
        self.page.update()
        
        # Start the GPU-accelerated pulse immediately
        asyncio.create_task(self._pulse_splash())
        
        # PHASE 2: Deferred Heavy Initialization
        # Use a small delay to ensure the splash screen is painted before I/O starts
        asyncio.create_task(self.error_boundary.capture(self._heavy_init)())

    def _build_splash(self):
        # A static, low-overhead container that pulses its opacity natively on the GPU
        self._splash_logo = ft.Container(
            content=ft.Icon(ft.Icons.AUTO_AWESOME, size=80, color=CYAN),
            padding=20,
            opacity=1.0,
            animate_opacity=ft.Animation(800, ft.AnimationCurve.EASE_IN_OUT),
        )

        return ft.Container(
            content=ft.Column(
                [
                    self._splash_logo,
                    ft.Text("Mai An Lab", size=28, weight=ft.FontWeight.W_900, color=TEXT),
                    ft.Container(height=20),
                    # A static, low-fidelity line instead of an active Ticker widget
                    ft.Container(width=180, height=2, bgcolor=SURFACE2, border_radius=1),
                    ft.Text("Loading UI & Database", size=10, weight=ft.FontWeight.W_600, color=DIM),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            expand=True,
            alignment=ft.Alignment(0, 0),
            bgcolor=BG, # Use solid colour instead of gradient to reduce overdraw
        )

    async def _pulse_splash(self):
        """Triggers a smooth opacity pulse on the splash logo."""
        while hasattr(self, '_splash_logo') and self._splash_logo.page:
            try:
                self._splash_logo.opacity = 0.3 if self._splash_logo.opacity == 1.0 else 1.0
                self._splash_logo.update()
                await asyncio.sleep(0.8)
            except Exception:
                break

    def restart_ui(self, target_tab=None):
        """Soft-restarts the app UI to apply theme changes immediately."""
        if self._is_restarting:
            logger.warning("Restart already in progress, ignoring.")
            return
            
        self._forced_tab = target_tab
        self.page.clean()
        self._splash = self._build_splash()
        self.page.add(self._splash)
        self.page.update()
        
        # Trigger re-init
        asyncio.create_task(self.error_boundary.capture(self._heavy_init)())

    async def _heavy_init(self):
        if self._is_restarting: return
        self._is_restarting = True
        
        # Load and apply theme/appearance first
        try:
            # PHASE 1: Cleanup old DB connection if it exists
            if hasattr(self, "db_manager") and self.db_manager:
                await self.db_manager.close()
                
            cfg = load_config()
            appearance = cfg.get("appearance", {})
            acc_color = appearance.get("accent_color", "#00BFFF")
            self.nav_indicator_color = appearance.get("nav_indicator_color", acc_color + "33")
            _apply_accent(acc_color)
        except: pass

        page = self.page
        page.bgcolor      = BG
        page.theme_mode   = ft.ThemeMode.DARK
        page.padding      = 0

        # sub-systems
        await asyncio.to_thread(repair_config)
        self.download_history_list = []
        from utils.db_manager import DatabaseManager
        self.db_manager = DatabaseManager(os.path.join(DATA_DIR, "library.db"))
        await self.db_manager.initialize()
        self.queue = QueueController(self)
        self._view_cache: dict[int, ft.Control] = {}
        await asyncio.to_thread(self._load_prefs)
        
        # Check for NO_CACHE environment variable to force a fresh start
        if os.getenv("FLET_NO_CACHE") == "1":
            logger.info("FLET_NO_CACHE is set. Clearing local state.")
            self._prefs = {}
            if os.path.exists(self._prefs_path): os.remove(self._prefs_path)
            # Clear library DB if it exists
            db_path = os.path.join(DATA_DIR, "library.db")
            if os.path.exists(db_path): os.remove(db_path)
            self.show_snackbar("Cache cleared: Starting fresh.")

        self.sync_config_to_ui()
        

        # views
        self.search_view  = SearchView(self)
        self.library_view = LibraryView(self)
        self.settings_view = SettingsView(self)

        # overlays / player
        self.mini_player         = MiniPlayerBar(self)
        self.now_playing         = NowPlayingSheet(self)
        self.queue_sheet         = QueueSheet(self)
        self.quality_selector_sheet = QualitySelectorSheet(self)
        self.metadata_editor     = MetadataEditorDialog(self)
        self.playlist_editor     = PlaylistEditorDialog(self)
        self.notifications       = NotificationSystem(self)
        self.assistant_view      = AssistantView(self)

        # wire ft.Audio into the page
        audio_engine.setup(self.page)

        # Restore saved shuffle, repeat and similar playback preferences
        audio_engine.is_shuffle = bool(self._prefs.get("is_shuffle", False))
        audio_engine.repeat_mode = self._prefs.get("repeat_mode", "none")
        self.play_similar_mode = bool(self._prefs.get("play_similar_mode", False))
        self.auto_dj_mode = bool(self._prefs.get("auto_dj_mode", False))
        self.now_playing.update_shuffle(audio_engine.is_shuffle)
        self.now_playing.update_repeat(audio_engine.repeat_mode)
        self.now_playing.update_play_similar(self.play_similar_mode)
        self.mini_player.update_play_similar(self.play_similar_mode)
        self.now_playing.update_auto_dj(self.auto_dj_mode)
        self.mini_player.update_auto_dj(self.auto_dj_mode)

        # bind audio engine events
        audio_engine.bind(
            current_path=self._on_current_path,
            position=self._on_position,
            is_playing=self._on_is_playing,
            duration=self._on_duration,
        )
        def _on_queue_mutated(_inst, _val):
            self.safe_update(self.queue_sheet.refresh)
            # Persist immediately on mutation so a hard OS kill (Android low-
            # memory reaping or process death) leaves a recoverable snapshot
            # on disk instead of the stale state from the previous launch.
            self._schedule_queue_save()

        audio_engine.bind(
            on_playback_error=lambda _, d: self.show_snackbar(f"Playback error: {d}", icon=ft.Icons.ERROR_OUTLINE, color="#FF4444"),
            on_queue_mutated=_on_queue_mutated,
            on_jarvis_continue=self._on_jarvis_continue,
            on_similar_continue=self._on_similar_continue,
        )

        # build and mount UI
        self._build_ui()

        # Run on the event loop, not a worker thread: restore_queue
        # synchronously dispatches observers (_on_current_path,
        # _on_is_playing, etc.) which schedule UI work via
        # `page.run_task` / `asyncio.create_task`. Those need a running
        # loop in the calling thread, otherwise the dispatches no-op and
        # the mini-player never gets revived.
        await self._restore_queue_state_async()
        await self.check_onboarding()
        self._is_restarting = False
        # Long-running task that snapshots playback position to disk every
        # ~10 s so a hard kill leaves the resume offset close to where the
        # user actually was (queue/index get saved on mutation events
        # already, but position drifts continuously while playing).
        self._position_save_task = asyncio.create_task(self._position_save_loop())


    def open_wipe_confirmation(self):
        """Opens a confirmation dialog before wiping the database."""
        def on_confirm(e):
            self.wipe_dialog.open = False
            self.page.update()
            # The actual wipe happens here (purely SQL, no file deletion)
            self.page.run_task(self.wipe_database)
            
        def on_cancel(e):
            self.wipe_dialog.open = False
            self.page.update()

        self.wipe_dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Wipe Library Database?"),
            content=ft.Text(
                "This will clear the search index and all metadata from the database. \n\n"
                "IMPORTANT: Your local music files will NOT be touched or deleted.",
                size=13
            ),
            actions=[
                ft.TextButton("Cancel", on_click=on_cancel),
                ft.TextButton(
                    content=ft.Text("Wipe Database", weight=ft.FontWeight.BOLD, color="#FF4444"), 
                    on_click=on_confirm
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        
        self.page.overlay.append(self.wipe_dialog)
        self.page.dialog = self.wipe_dialog
        self.wipe_dialog.open = True
        self.page.update()



    async def check_onboarding(self):
        """Detects a fresh install using a marker file."""
        try:
            marker_path = os.path.join(DATA_DIR, ".onboarded")
            if os.path.exists(marker_path):
                logger.info("Onboarding marker found. Skipping guide.")
                return
            
            # No marker? Show the guide.
            self.show_onboarding_guide()
        except Exception as e:
            logger.error(f"Onboarding check failed: {e}")

    def show_onboarding_guide(self):
        """Presents a clean, high-fidelity setup guide to the user."""
        def close_guide(e):
            self.onboarding_dlg.open = False
            self.page.update()
            
            # Write the marker file so they don't see this again
            try:
                marker_path = os.path.join(DATA_DIR, ".onboarded")
                with open(marker_path, "w") as f:
                    f.write("1")
            except Exception as ex:
                logger.error(f"Failed to write onboarding marker: {ex}")

            # Switch to Settings tab to help them start (now index 3)
            self._switch_tab(3)

        self.onboarding_dlg = ft.AlertDialog(
            modal=True,
            bgcolor=SURFACE,
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Icon(ft.Icons.AUTO_AWESOME_ROUNDED, color=CYAN, size=48),
                        ft.Text("Welcome to Mai An Lab", size=22, weight=ft.FontWeight.W_900, color=TEXT, text_align=ft.TextAlign.CENTER),
                        ft.Text("Your high-fidelity music hub is ready.", size=14, color=DIM, text_align=ft.TextAlign.CENTER),
                        ft.Divider(color=BORDER, height=32),
                        
                        ft.Row([
                            ft.Icon(ft.Icons.LOCK_PERSON_ROUNDED, color=CYAN, size=20),
                            ft.Column([
                                ft.Text("1. Authenticate", weight=ft.FontWeight.BOLD, size=13),
                                ft.Text("Add your Qobuz ID & Token in Settings.", size=11, color=DIM),
                            ], spacing=0, expand=True)
                        ]),
                        ft.Container(height=10),
                        ft.Row([
                            ft.Icon(ft.Icons.FOLDER_OPEN_ROUNDED, color=CYAN, size=20),
                            ft.Column([
                                ft.Text("2. Set Folders", weight=ft.FontWeight.BOLD, size=13),
                                ft.Text("Choose where to download and index music.", size=11, color=DIM),
                            ], spacing=0, expand=True)
                        ]),
                        ft.Container(height=10),
                        ft.Row([
                            ft.Icon(ft.Icons.GESTURE_ROUNDED, color=CYAN, size=20),
                            ft.Column([
                                ft.Text("3. Pro Tip", weight=ft.FontWeight.BOLD, size=13),
                                ft.Text("Long-press any item for advanced actions.", size=11, color=DIM),
                            ], spacing=0, expand=True)
                        ]),
                        
                        ft.Container(height=24),
                        ft.Button(
                            content=ft.Text("GET STARTED", weight=ft.FontWeight.W_900, color=BG),
                            style=ft.ButtonStyle(bgcolor=CYAN, shape=ft.RoundedRectangleBorder(radius=12)),
                            on_click=close_guide,
                            height=50,
                            width=240,
                        ),
                    ],
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12,
                ),
                padding=10,
                width=300,
            ),
        )
        
        self.page.overlay.append(self.onboarding_dlg)
        self.onboarding_dlg.open = True
        self.page.update()

    async def wipe_database(self):
        """Clears all indexed data from the local database without deleting the file."""
        try:
            conn = await self.db_manager.get_connection()
            async with self.db_manager._write_lock:
                await conn.execute("PRAGMA foreign_keys = OFF")
                await conn.execute("BEGIN")
                try:
                    await conn.execute("DELETE FROM playlist_tracks")
                    await conn.execute("DELETE FROM playlists")
                    await conn.execute("DELETE FROM track_partitions")
                    await conn.execute("DELETE FROM mood_feedback")
                    await conn.execute("DELETE FROM mood_profiles")
                    await conn.execute("DELETE FROM playback_history")
                    await conn.execute("DELETE FROM track_neighbors")
                    await conn.execute("DELETE FROM play_counts")
                    await conn.execute("DELETE FROM tracks")
                    await conn.execute("DELETE FROM albums")
                    await conn.execute("DELETE FROM artists")
                    try:
                        await conn.execute("DELETE FROM fts_search")
                    except: pass
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
                finally:
                    await conn.execute("PRAGMA foreign_keys = ON")
                
            # Optional: attempt VACUUM outside the lock
            try:
                await conn.execute("VACUUM")
            except: pass
            
            # Clear in-memory caches
            self.db_manager.clear_caches()
            if hasattr(self, "library_view") and self.library_view:
                self.library_view._tracks_cache = None
                self.library_view._tracks_cache_key = None
                self.library_view._cached_moods = None
                self.library_view._cached_islets = None
                self.library_view._cached_unanalysed = None
                self.library_view._mood_feedback_map.clear()
                self.library_view._mood_recalc_pending = False

            # Clear all queue state files
            audio_engine.clear_queue()
            for filename in ("queue_state.json", "queue_regular.json", "queue_shuffle.json", "queue_similar.json"):
                q_path = os.path.join(os.environ["XDG_CACHE_HOME"] if filename != "queue_state.json" else DATA_DIR, filename)
                try:
                    if os.path.exists(q_path):
                        os.remove(q_path)
                except Exception:
                    pass

            # Refresh UI
            await self.library_view.load_library()
            self.show_snackbar("Library database wiped successfully.")
        except Exception as exc:
            self.show_snackbar(f"Wipe failed: {exc}")

    def safe_update(self, fn):
        """Queue a UI mutation and schedule a single coalesced page.update().
        Thread-safe entry point: may be called from any background thread.
        """
        if not self.page: return
        
        # Use run_task to bridge the sync/async gap safely
        self.page.run_task(self._safe_update_handler, fn)

    async def _safe_update_handler(self, fn):
        async with self._update_lock:
            self._pending_fns.append(fn)
            if self._flush_pending:
                # A flush is already scheduled; just queue the fn and return.
                # This is the key coalescing step: multiple safe_update() calls
                # arriving in the same event-loop burst share a single flush.
                return
            self._flush_pending = True
        
        # Yield once to let any other safe_update() calls in this same tick
        # append their fns before we flush, so they all land in one page.update().
        await asyncio.sleep(0)
        await self._flush_updates()

    async def _flush_updates(self):
        async with self._update_lock:
            # Atomic swap to allow new updates to accumulate
            fns = self._pending_fns
            self._pending_fns = []
            self._flush_pending = False
        
        if not fns:
            return

        for fn in fns:
            try:
                if asyncio.iscoroutinefunction(fn):
                    await fn()
                else:
                    fn()
            except Exception:
                logger.exception("safe_update execution error")
        
        # Skip page.update() while the app is backgrounded; pushing diffs to
        # a suspended Flet/Flutter client wastes CPU and can cause UI hangs.
        # _on_lifecycle calls safe_update(lambda: None) on resume to force-sync.
        if self.is_background:
            return
        try:
            self.page.update()
        except Exception:
            pass

    # ── UI construction ──────────────────────────────────────────────────────
    def _build_ui(self):
        # Resolve startup page from config
        if hasattr(self, "_forced_tab") and self._forced_tab is not None:
            self._current_tab = self._forced_tab
            self._forced_tab = None
        else:
            try:
                cfg = load_config()
                startup_name = cfg.get("general", {}).get("startup_page", "Library")
            except:
                startup_name = "Library"
            
            mapping = {"Jarvis": 0, "Search": 1, "Library": 2, "Settings": 3}
            self._current_tab = mapping.get(startup_name, 2) # Default to Library (index 2)

        # Build views (Jarvis, Search, Library, Settings)
        view_builders = [
            self.assistant_view.build,
            self.search_view.build,
            self.library_view.build,
            self.settings_view.build
        ]
        
        # Pre-build the startup view
        startup_view = view_builders[self._current_tab]()
        self._tab_content = ft.Container(
            content=startup_view,
            expand=True,
        )

        # Background Pre-builder: Warm up other tabs while user is looking at the startup page
        def _warmup_tabs():
            try:
                for i, builder in enumerate(view_builders):
                    if i != self._current_tab:
                        # Build in background and store in cache
                        self._view_cache[i] = builder()
            except: pass
        asyncio.create_task(asyncio.to_thread(_warmup_tabs))
        
        # Content area (removed swipe detector GestureDetector to prevent accidental tab switching)
        self._swipe_content = self._tab_content

        # Navigation bar
        self._nav = ft.NavigationBar(
            selected_index=self._current_tab if self._current_tab < 3 else 0,
            bgcolor=SURFACE,
            indicator_color=CYAN + "55",
            label_behavior=ft.NavigationBarLabelBehavior.ALWAYS_SHOW,
            destinations=[
                ft.NavigationBarDestination(
                    icon=ft.Icons.AUTO_AWESOME_ROUNDED,
                    selected_icon=ft.Icons.AUTO_AWESOME_ROUNDED,
                    label="Jarvis",
                ),
                ft.NavigationBarDestination(
                    icon=ft.Icons.SEARCH_OUTLINED,
                    selected_icon=ft.Icons.SEARCH,
                    label="Search",
                ),
                ft.NavigationBarDestination(
                    icon=ft.Icons.LIBRARY_MUSIC_OUTLINED,
                    selected_icon=ft.Icons.LIBRARY_MUSIC,
                    label="Library",
                ),
            ],
            on_change=lambda e: self._switch_tab(e.control.selected_index),
        )

        # Main layout; NO Stack, NO overlay for primary UI
        # Use a simple Column with SafeArea for Android notch/gesture bar handling
        main_layout = ft.Column(
            [
                ft.Container(
                    content=self._swipe_content,
                    expand=True,
                ),
                self.mini_player.build(),
                self._nav,
            ],
            spacing=0,
            expand=True,
        )

        # Wrap in SafeArea to handle Android notch and gesture bar
        safe_root = ft.SafeArea(
            content=main_layout,
            expand=True,
        )

        # Assistant FAB: positioned above the mini-player + nav bar + the
        # Android gesture/3-button system bar. The FAB lives in the root
        # Stack (outside SafeArea) so it's measured from the absolute
        # screen edge; the offset budget below accounts for:
        #   • ~32 px system gesture/nav bar
        #   • ~80 px Flet NavigationBar
        #   • ~64 px mini-player when visible
        # Total ~176 px — we use 188 to leave breathing room without crowding
        # the mini-player's controls.

        # Add ONLY the root to page; no overlays for main UI
        self._root_stack = ft.Stack(
            [
                safe_root,
                self.error_boundary._error_view,
            ],
            expand=True,
        )
        # Transition from Splash to Main UI: Replace controls
        self.page.controls = [self._root_stack]

        # Sheets are added to overlay BUT must be instantiated before page.update()
        # and MUST NOT block the main UI thread
        self.page.overlay.append(self.quality_selector_sheet.build())
        self.page.overlay.append(self.now_playing.build())
        self.page.overlay.append(self.queue_sheet.build())
        
        # Initial render
        self.page.update()

    def _on_swipe(self, e):
        """Switch tabs on horizontal swipe. Negative velocity = swipe left = next tab."""
        # FIX: Extract primary_velocity natively to prevent AttributeError
        vx = getattr(e, "primary_velocity", 0) or 0
        
        # Increased threshold to 1000 to make swiping less aggressive
        if abs(vx) < 1000:
            return
            
        new_tab = self._current_tab
        if vx < 0 and self._current_tab < 1:
            new_tab += 1
        elif vx > 0 and self._current_tab > 0:
            new_tab -= 1
        
        if new_tab != self._current_tab:
            self._switch_tab(new_tab)
            # Let safe_update handle the page.update()
            self.safe_update(lambda: setattr(self._nav, 'selected_index', new_tab))

    def _switch_tab(self, index: int):
        self._current_tab = index
        
        # Reset labels to standard text
        labels = ["Jarvis", "Search", "Library"]
        for i, dest in enumerate(self._nav.destinations):
            if i < len(labels):
                dest.label = labels[i]

        if index == 0:
            # Jarvis / Assistant View
            content = self._view_cache.get(0) or self.assistant_view.build()
            self._view_cache[0] = content
            # Ensure assistant initialisation is triggered
            self.page.run_task(self.assistant_view._init_assistant)
        elif index == 1:
            content = self._view_cache.get(1) or self.search_view.build()
            self._view_cache[1] = content
        elif index == 2:
            # Library View
            content = self._view_cache.get(2)
            if content is None:
                content = self.library_view.build()
                self._view_cache[2] = content
            elif not self.library_view._library_list.controls:
                self.page.run_task(self.library_view.load_library)
        else:
            # Settings View
            self.settings_view.refresh()
            content = self._view_cache.get(3) or self.settings_view.build()
            self._view_cache[3] = content
            
        def _mutate():
            self._tab_content.content = content
            # If in Settings (index 3), deselect the bar or default to index 0 visually
            is_nav_tab = index < len(self._nav.destinations)
            self._nav.selected_index = index if is_nav_tab else 0
            self._nav.indicator_color = (CYAN + "55" if is_nav_tab else "transparent")

        self.safe_update(_mutate)

    # ── audio engine callbacks ───────────────────────────────────────────────
    def _on_current_path(self, _instance, path: str):
        if path and not getattr(self, "is_restoring_session", False):
            # Increment play count in background
            asyncio.create_task(self.db_manager.increment_play_count(path))

        # Track changed (manual skip or auto-advance); flush queue state so
        # the persisted current_index points at the right slot if the OS
        # kills us before the next mutation event.
        self._schedule_queue_save()

        # ── Implicit play-event capture ──────────────────────────────────
        # Every transition of `current_path` (skip, natural end via
        # _on_track_ended → next(), or stop which sets path to "") fires
        # exactly once for the outgoing track. The classifier inside
        # taste_model discards skips < 5s, so we forward unconditionally
        # whenever we actually had a previous track.
        prev_path = self._last_played_path
        prev_pos  = self._last_play_position
        prev_dur  = self._last_play_duration
        if (
            prev_path
            and prev_path != path
            and not getattr(self, "is_restoring_session", False)
        ):
            self.page.run_task(
                self._record_play_event_safe, prev_path, prev_pos, prev_dur
            )

        # Reset trackers for the incoming track. Duration may arrive a beat
        # later via _on_duration; that's fine — _on_position will overwrite
        # _last_play_duration as soon as a valid duration is reported.
        self._last_played_path    = path or ""
        self._last_play_position  = 0.0
        self._last_play_duration  = float(audio_engine.duration or 0.0)

        # Refresh playback-bar like/dislike state for the new track.
        self._refresh_feedback_buttons(path)

        # Play Similar dynamic queue replenishment hook
        if self.play_similar_mode and path and not getattr(self, "is_restoring_session", False):
            if audio_engine.current_index >= len(audio_engine.queue) - 1:
                self.page.run_task(self._recommend_similar_async, path, self._play_similar_gen)

        def _atomic_update():
            track  = audio_engine.current_track  or ""
            artist = audio_engine.current_artist or ""
            album  = audio_engine.current_album  or ""

            self.mini_player.update_meta(track, artist)
            self.now_playing.update_meta(track, artist, album)
            
            is_playing = audio_engine.is_playing
            self.mini_player.update_state(is_playing)
            self.now_playing.update_state(is_playing)

            img_url = ""
            if audio_engine.queue and audio_engine.current_index < len(audio_engine.queue):
                img_url = audio_engine.queue[audio_engine.current_index].get("image_url", "")
            
            # Use cached local artwork if available, avoiding redundant background extraction
            art_val = audio_engine.current_art or img_url
            self.mini_player.update_artwork(art_val)
            self.now_playing.update_artwork(art_val)

            # Highlight the currently-playing row in both views
            self.search_view.refresh_now_playing()
            self.library_view.refresh_now_playing()

            if isinstance(img_url, str) and img_url.startswith("http"):
                self._fetch_artwork_url_async(img_url)
            elif not audio_engine.current_art and track and path:
                self._extract_artwork_async(path)

        self.safe_update(_atomic_update)

    async def _record_play_event_safe(self, path: str, played: float, duration: float):
        """Background-safe wrapper around tg.record_play_event so the engine's
        sync dispatch doesn't get coupled to import-time / DB errors. Also
        feeds the session-scoped negative centroid / trip-wire so consecutive
        bad continuations get steered away from in the next walk."""
        from utils import track_graph as tg
        # from utils import taste_model as _tm  # Commented out to improve loading times / regressor dead code
        try:
            await tg.record_play_event(
                self.db_manager,
                path,
                float(played or 0.0),
                float(duration or 0.0),
            )
        except Exception as exc:
            logger.debug("record_play_event failed for %s: %s", path, exc)

        # Session signal mirrors the same classifier the taste model uses,
        # so "what the model considers a skip" and "what the centroid
        # treats as bad" can't drift apart.
        try:
            played_seconds = float(played or 0.0)
            duration_seconds = float(duration or 0.0)
            y = None
            if played_seconds >= 5.0:
                if played_seconds >= 45.0 or (duration_seconds > 0.0 and (played_seconds / duration_seconds) >= 0.30):
                    y = 1
                else:
                    y = 0
        except Exception:
            y = None
        if y == 1:
            self._session_last_liked_path = path
            self._session_recent_outcomes.append(True)
        elif y == 0:
            if path and path not in self._session_bad_paths:
                self._session_bad_paths.append(path)
            self._session_recent_outcomes.append(False)

    def _refresh_feedback_buttons(self, path: str):
        """Sync the playback-bar like/dislike icons to whatever explicit
        feedback we have cached for `path`. Hollow = neutral, filled = active."""
        like_state = self._explicit_feedback_cache.get(path) if path else None

        def _apply(btn_like, btn_dislike):
            if btn_like is None or btn_dislike is None:
                return
            if like_state is True:
                btn_like.icon       = ft.Icons.THUMB_UP_ROUNDED
                btn_like.icon_color = CYAN
                btn_dislike.icon       = ft.Icons.THUMB_DOWN_OUTLINED
                btn_dislike.icon_color = DIM
            elif like_state is False:
                btn_like.icon       = ft.Icons.THUMB_UP_OUTLINED
                btn_like.icon_color = DIM
                btn_dislike.icon       = ft.Icons.THUMB_DOWN_ROUNDED
                btn_dislike.icon_color = CYAN
            else:
                btn_like.icon       = ft.Icons.THUMB_UP_OUTLINED
                btn_like.icon_color = DIM
                btn_dislike.icon       = ft.Icons.THUMB_DOWN_OUTLINED
                btn_dislike.icon_color = DIM

        try:
            _apply(getattr(self.mini_player, "_like_btn", None),
                   getattr(self.mini_player, "_dislike_btn", None))
            if getattr(self.now_playing, "_initialized", False):
                _apply(getattr(self.now_playing, "_like_btn", None),
                       getattr(self.now_playing, "_dislike_btn", None))
        except Exception as exc:
            logger.debug("Failed to refresh feedback buttons: %s", exc)

        def _push():
            for btn in (
                getattr(self.mini_player, "_like_btn", None),
                getattr(self.mini_player, "_dislike_btn", None),
                getattr(self.now_playing, "_like_btn", None),
                getattr(self.now_playing, "_dislike_btn", None),
            ):
                if btn is not None and getattr(btn, "page", None):
                    try:
                        btn.update()
                    except Exception:
                        pass
        try:
            _push()
        except Exception:
            pass

    def _on_feedback_click(self, like: bool):
        """Click handler for the playback-bar like/dislike buttons. Mirrors
        the library-tile mood like/dislike behaviour when the user is viewing
        a mood partition."""
        if self.play_similar_mode:
            return
        current_path = audio_engine.current_path
        if not current_path:
            return

        # Toggle off if the user clicks the same state again, otherwise flip
        # to the new state. Cache update is optimistic — the DB call below
        # is the source of truth.
        prev_state = self._explicit_feedback_cache.get(current_path)
        if prev_state == like:
            self._explicit_feedback_cache.pop(current_path, None)
            new_state = None
        else:
            self._explicit_feedback_cache[current_path] = like
            new_state = like

        # Explicit click is a stronger signal than an implicit skip — feed
        # it straight into the session centroid / last-liked anchor.
        if new_state is True:
            self._session_last_liked_path = current_path
        elif new_state is False:
            if current_path not in self._session_bad_paths:
                self._session_bad_paths.append(current_path)

        self._refresh_feedback_buttons(current_path)

        if not like and getattr(self, "auto_dj_mode", False):
            audio_engine.next()

        async def _persist():
            from utils import track_graph as tg
            try:
                # Always record the explicit signal: even an "unlike" carries
                # information (the user actively walked back a prior like).
                await tg.record_explicit_feedback(
                    self.db_manager, current_path, like=like
                )
            except Exception as exc:
                logger.debug("record_explicit_feedback failed: %s", exc)

            # Mirror to the active mood partition, if the user is in one.
            try:
                lib = getattr(self, "library_view", None)
                if (
                    lib is not None
                    and getattr(lib, "partition_sub_mode", "") == "moods"
                    and getattr(lib, "_mood_label", None) is not None
                ):
                    label_value = (lib._mood_label.value or "").strip()
                    canonical = tg.mood_canonical(label_value)
                    if canonical:
                        delta = 1 if like else -1
                        await tg.adjust_mood_profile(
                            self.db_manager, canonical, current_path, delta
                        )
                        # Refresh the partition view so the tile state reflects
                        # the new partition membership.
                        try:
                            await lib.load_library()
                        except Exception as refresh_exc:
                            logger.debug(
                                "Feedback click: partition refresh failed: %s",
                                refresh_exc,
                            )
            except Exception as exc:
                logger.debug("Feedback click: mood mirror failed: %s", exc)

        self.page.run_task(_persist)

        verb = ("Liked" if like else "Disliked") if new_state is not None else "Cleared"
        try:
            self.show_snackbar(
                f"{verb}: {audio_engine.current_track or 'track'}",
                icon=(ft.Icons.THUMB_UP if like else ft.Icons.THUMB_DOWN)
                if new_state is not None else ft.Icons.REMOVE_CIRCLE_OUTLINE,
            )
        except Exception:
            pass

    async def _initiate_play_similar_queue_async(self, path: str, gen: int = 0):
        """Cheap acoustic-only initial fill for Play Similar.

        No taste model, no percentile matrix, no negative-embedding load.
        The avoid set already blocks session-disliked tracks; the graph walk
        is pure cosine + artist edges at low temperature for a tight,
        predictable first queue.
        """
        import os
        from utils import track_graph as tg
        try:
            # Race guard: bail if the mode was toggled while we were awaiting
            if gen != self._play_similar_gen or not self.play_similar_mode:
                return

            avoid = {path}
            # Session-rejected paths go straight into the avoid set — no
            # embedding fetch needed; the graph won't visit them at all.
            avoid.update(self._session_bad_paths)
            try:
                recent = await self.db_manager.recent_played_paths(window_seconds=7 * 86400)
                avoid.update(recent)
            except Exception:
                pass

            from utils.streamrip_api import load_config
            cfg = load_config()
            temp = float(cfg.get("general", {}).get("play_similar_temperature", 0.05))

            walk_paths = await tg.walk(
                self.db_manager,
                path,
                length=12,
                edge_kinds=(tg.KIND_ACOUSTIC, tg.KIND_ARTIST),
                teleport_path=path,
                avoid=avoid,
                restart_prob=0.15,
                diversity_lambda=0.15,   # lighter MMR — no embedding fetch
                temperature=temp,
                taste_weight=0.0,        # acoustic-only, regressor reserved for DJ
                negative_embs=None,      # no negative centroid — avoid set handles it
                prefetch_k=20,           # half the DJ/Jarvis k — cheaper DB fetch
            )

            # Re-check after the await — user may have toggled off mid-walk
            if gen != self._play_similar_gen or not self.play_similar_mode:
                return

            if walk_paths:
                engine_tracks = []
                for p in walk_paths:
                    row = await self.db_manager.get_track_full(p)
                    if not row:
                        continue
                    engine_tracks.append({
                        "path":        row.get("path"),
                        "track_title": row.get("title") or row.get("track_title") or os.path.basename(p),
                        "artist_name": row.get("artist") or row.get("artist_name") or "Unknown Artist",
                        "album_title": row.get("album")  or row.get("album_title")  or "Unknown Album",
                        "duration":    row.get("duration", 0.0) or 0.0,
                        "image_url":   row.get("image_url", "") or "",
                    })
                if engine_tracks:
                    # Final race check before mutating queue
                    if gen != self._play_similar_gen or not self.play_similar_mode:
                        return
                    cur_idx = audio_engine.current_index
                    if 0 <= cur_idx < len(audio_engine.queue):
                        new_q = list(audio_engine.queue[:cur_idx + 1])
                        existing_paths = {t.get("path") for t in new_q if t.get("path")}
                        for et in engine_tracks:
                            if et.get("path") not in existing_paths:
                                new_q.append(et)
                        audio_engine.queue = new_q
                    else:
                        audio_engine.queue = engine_tracks
                        audio_engine.current_index = 0
                    
                    audio_engine.jarvis_controlled = False
                    audio_engine._sync_metadata_for_current()
                    audio_engine.dispatch("on_queue_mutated")
                    if hasattr(audio_engine, "_push_queue_native") and audio_engine._page:
                        if hasattr(audio_engine, "_arm_queue_gate"):
                            audio_engine._arm_queue_gate()
                        audio_engine._page.run_task(
                            audio_engine._push_queue_native,
                            audio_engine.current_index,
                            audio_engine.is_playing
                        )
                    if hasattr(self, "queue_sheet") and self.queue_sheet and self.queue_sheet._initialized:
                        self.queue_sheet.refresh()
                    logger.info("Play Similar: Successfully appended %d similar tracks to the queue.", len(engine_tracks))
        except Exception as exc:
            logger.exception("Play Similar: Failed to initiate similar queue: %s", exc)

    async def _recommend_similar_async(self, path: str, gen: int = 0):
        """Lightweight per-track recommendation for Play Similar.

        Runs a minimal acoustic walk (length=3, k=20 prefetch) with no taste
        model and no embedding fetch — just graph topology + avoid set.
        """
        import os
        from utils import track_graph as tg
        try:
            # Race guard: bail if the mode was toggled while we were awaiting
            if gen != self._play_similar_gen or not self.play_similar_mode:
                return

            # Build avoid set from queued paths + session rejects + current
            avoid = {t["path"] for t in audio_engine.queue if t.get("path")}
            avoid.add(path)
            avoid.update(self._session_bad_paths)
            try:
                recent = await self.db_manager.recent_played_paths(window_seconds=7 * 86400)
                avoid.update(recent)
            except Exception:
                pass

            from utils.streamrip_api import load_config
            cfg = load_config()
            temp = float(cfg.get("general", {}).get("play_similar_temperature", 0.05))

            walk_tracks = await tg.walk(
                self.db_manager,
                path,
                length=3,
                edge_kinds=(tg.KIND_ACOUSTIC, tg.KIND_ARTIST),
                teleport_path=teleport,
                avoid=avoid,
                restart_prob=0.15,
                diversity_lambda=0.0,   # single-track pick — diversity unused
                temperature=temp,
                taste_weight=0.0,
                negative_embs=None,
                prefetch_k=20,
            )

            # Re-check after the await
            if gen != self._play_similar_gen or not self.play_similar_mode:
                return

            if walk_tracks:
                queue_paths = {t["path"] for t in audio_engine.queue}
                next_track_path = next(
                    (wt for wt in walk_tracks if wt not in queue_paths),
                    walk_tracks[0],
                )
                row = await self.db_manager.get_track_full(next_track_path)
                if row:
                    if gen != self._play_similar_gen or not self.play_similar_mode:
                        return
                    track_dict = {
                        "path":        row.get("path"),
                        "track_title": row.get("title") or row.get("track_title") or os.path.basename(next_track_path),
                        "artist_name": row.get("artist") or row.get("artist_name") or "Unknown Artist",
                        "album_title": row.get("album")  or row.get("album_title")  or "Unknown Album",
                        "duration":    row.get("duration", 0.0) or 0.0,
                        "image_url":   row.get("image_url", "") or "",
                    }
                    audio_engine.queue_last(track_dict)
                    logger.info("Play Similar: Appended recommended track '%s' to queue.", next_track_path)
        except Exception as exc:
            logger.exception("Play Similar: Failed to generate dynamic recommendation: %s", exc)

    def _on_similar_continue(self, _inst, _val=None):
        """Sync callback dispatched by AudioEngine when the manually-initiated
        Play Similar or Auto-DJ queue runs dry. Bridges into the async continuation coroutine safely."""
        if self.page:
            if self.play_similar_mode:
                self.page.run_task(self._similar_auto_continue_queue)
            elif getattr(self, "auto_dj_mode", False):
                self.page.run_task(self._auto_dj_auto_continue_queue)

    async def _similar_auto_continue_queue(self):
        """Silently extend the Play Similar queue when it runs dry.

        Cheap path: pure acoustic graph walk, no taste model, no embedding
        fetch. Session-rejected paths are in the avoid set so they won't be
        visited regardless of the negative centroid term.
        """
        import asyncio
        import os
        from utils import track_graph as tg

        seed_path = audio_engine.current_path
        if not seed_path and audio_engine.queue:
            seed_path = audio_engine.queue[-1].get("path", "")
        if not seed_path:
            logger.warning("Play Similar continuation: no seed path found; skipping.")
            audio_engine.stop()
            return

        # Build avoid set — session rejects go in here, not as embeddings
        avoid: set[str] = set()
        avoid.add(seed_path)
        for t in audio_engine.queue:
            if t.get("path"):
                avoid.add(t["path"])
        avoid.update(self._session_bad_paths)
        try:
            recent = await self.db_manager.recent_played_paths(window_seconds=7 * 86400)
            avoid.update(recent)
        except Exception:
            pass

        from utils.streamrip_api import load_config
        cfg = load_config()
        temp = float(cfg.get("general", {}).get("play_similar_temperature", 0.05))

        try:
            teleport = getattr(audio_engine, "play_similar_seed_path", "") or seed_path
            walk_paths = await tg.walk(
                self.db_manager,
                seed_path,
                length=5,
                edge_kinds=(tg.KIND_ACOUSTIC, tg.KIND_ARTIST),
                avoid=avoid,
                restart_prob=0.15,
                diversity_lambda=0.15,
                temperature=temp,
                taste_weight=0.0,
                negative_embs=None,
                teleport_path=teleport,
                prefetch_k=20,
            )
        except Exception as exc:
            logger.warning("Play Similar continuation: graph walk failed: %s", exc)
            walk_paths = []

        if not walk_paths:
            logger.info("Play Similar continuation: no neighbours found for seed %s", seed_path)
            audio_engine.stop()
            return

        first_new_index = len(audio_engine.queue)
        appended_tracks: list[dict] = []
        for p in walk_paths:
            try:
                row = await self.db_manager.get_track_full(p)
            except Exception:
                row = None
            if not row:
                continue
            track_dict = {
                "path":        row.get("path"),
                "track_title": row.get("title") or row.get("track_title") or os.path.basename(p),
                "artist_name": row.get("artist") or row.get("artist_name") or "Unknown Artist",
                "album_title": row.get("album")  or row.get("album_title")  or "Unknown Album",
                "duration":    row.get("duration", 0.0) or 0.0,
                "image_url":   row.get("image_url", "") or "",
            }
            audio_engine.queue_last(track_dict)
            appended_tracks.append(track_dict)

        if len(appended_tracks) == 0:
            logger.info("Play Similar continuation: metadata lookup failed for all neighbours.")
            audio_engine.stop()
            return

        # Resume playback at the first newly appended slot
        audio_engine.play_track_at(first_new_index)

    def _on_jarvis_continue(self, _inst, _val=None):
        """Sync callback dispatched by AudioEngine when the Jarvis-controlled
        queue runs dry. Bridges into the async continuation coroutine safely."""
        if self.page:
            self.page.run_task(self._jarvis_auto_continue_queue)

    async def _jarvis_auto_continue_queue(self):
        """Automatically extend a Jarvis-managed queue with 5 acoustically
        similar tracks when playback reaches the end of the current list.

        Walk order:
          1. Determine seed from the last-played track (current_path or last
             queue entry so the seed is valid even after engine state resets).
          2. Build an avoid set from the runner's recent-play history.
          3. Walk the acoustic+artist graph for up to 5 new paths.
          4. Fetch full metadata from DB and append tracks to the live queue.
          5. Post a premium Jarvis chat bubble and speak the announcement.
          6. Kick off playback at the first newly-appended slot.
        """
        import asyncio
        import random
        from utils import track_graph as tg

        # ── Seed resolution ──────────────────────────────────────────────────
        # Default seed is the currently-playing track. When the trip-wire
        # fires (≥2 of the last 3 continuation picks rejected) we re-anchor
        # to the last track that earned a positive signal this session, so
        # restarts pull back to something known-good instead of compounding
        # a bad chain. Falls through to current_path when there's nothing
        # liked yet this session.
        recent = list(self._session_recent_outcomes)
        tripwire = len(recent) >= 2 and recent.count(False) >= 2
        seed_path = ""
        if tripwire and self._session_last_liked_path:
            seed_path = self._session_last_liked_path
            logger.info(
                "Jarvis continuation: trip-wire fired, re-seeding from "
                "last-liked path %s", seed_path,
            )
        if not seed_path:
            seed_path = audio_engine.current_path
        if not seed_path and audio_engine.queue:
            seed_path = audio_engine.queue[-1].get("path", "")
        if not seed_path:
            logger.warning("Jarvis continuation: no seed path found; skipping.")
            return

        # ── Avoid set ────────────────────────────────────────────────────────
        avoid: set[str] = set()
        runner = getattr(self.assistant_view, "_runner", None)
        if runner is not None:
            try:
                avoid = await runner._avoid_set()
            except Exception:
                pass
        avoid.add(seed_path)
        # Add all currently queued tracks to avoid set to prevent duplicate recommendations
        for t in audio_engine.queue:
            if t.get("path"):
                avoid.add(t["path"])
        # Bad paths from this session are always avoided outright — the
        # centroid penalises *similar* tracks, the avoid set blocks the
        # exact ones.
        for bad in self._session_bad_paths:
            avoid.add(bad)

        # ── Session negative centroid ────────────────────────────────────────
        # Fetch embeddings for the session's rejected tracks once per
        # continuation. Bounded by `_session_bad_paths.maxlen`, so this is
        # ≤10 rows and a single batched query.
        negative_embs: list = []
        if self._session_bad_paths:
            try:
                blobs = await self.db_manager.get_embeddings_for_paths(
                    list(self._session_bad_paths)
                )
                for blob in blobs.values():
                    v = tg._unpack_embedding(blob)
                    if v is not None:
                        negative_embs.append(v)
            except Exception as exc:
                logger.debug("Jarvis continuation: negative emb load failed: %s", exc)

        # ── Acoustic graph walk ───────────────────────────────────────────────
        # On the trip-wire path, drop taste exploration so the walk leans
        # fully on the (now-anchored) seed plus the negative centroid.
        from utils.streamrip_api import load_config
        cfg = load_config()
        temp = float(cfg.get("general", {}).get("play_similar_temperature", 0.05))

        try:
            # Anchored PageRank: teleport back to original seed track to prevent genre drift
            teleport = getattr(audio_engine, "play_similar_seed_path", "") or seed_path
            walk_paths = await tg.walk(
                self.db_manager,
                seed_path,
                length=5,
                edge_kinds=(tg.KIND_ACOUSTIC, tg.KIND_ARTIST),
                avoid=avoid,
                restart_prob=0.15,
                diversity_lambda=0.3,
                temperature=temp,
                taste_explore=0.0 if tripwire else 0.05,
                negative_embs=negative_embs or None,
                teleport_path=teleport,
            )
        except Exception as exc:
            logger.warning("Jarvis continuation: graph walk failed: %s", exc)
            walk_paths = []

        if not walk_paths:
            logger.info("Jarvis continuation: no neighbours found for seed %s", seed_path)
            # Nothing to queue — stop cleanly so the engine doesn't hang.
            audio_engine.stop()
            return

        # ── Fetch metadata and append to queue ───────────────────────────────
        first_new_index = len(audio_engine.queue)
        appended_tracks: list[dict] = []
        for p in walk_paths:
            try:
                row = await self.db_manager.get_track_full(p)
            except Exception:
                row = None
            if not row:
                continue
            track_dict = {
                "path":        row.get("path"),
                "track_title": row.get("title") or row.get("track_title") or os.path.basename(p),
                "artist_name": row.get("artist") or row.get("artist_name") or "Unknown Artist",
                "album_title": row.get("album")  or row.get("album_title")  or "Unknown Album",
                "duration":    row.get("duration", 0.0) or 0.0,
                "image_url":   row.get("image_url", "") or "",
            }
            audio_engine.queue_last(track_dict)
            if runner is not None:
                runner._remember(p, seed_path=seed_path)
            appended_tracks.append(track_dict)

        appended = len(appended_tracks)
        if appended == 0:
            logger.info("Jarvis continuation: metadata lookup failed for all neighbours.")
            audio_engine.stop()
            return

        # ── Honour shuffle state when picking the first continuation track ────
        # If the user is in shuffle (e.g. play_random), starting deterministically
        # at first_new_index breaks the shuffle illusion for one track. For
        # ordered modes (play_similar, sequential queues) we preserve the
        # DSP-derived walk order.
        if getattr(audio_engine, "is_shuffle", False) and appended > 1:
            offset = random.randint(0, appended - 1)
        else:
            offset = 0
        start_index = first_new_index + offset
        first_track = appended_tracks[offset]
        first_track_name = (
            f"{first_track['track_title']} — {first_track['artist_name']}"
        )

        # ── Speak and post bubble ─────────────────────────────────────────────
        spoken_msg = (
            f"The queue has ended, sir. I've automatically continued with "
            f"{appended} similar track{'s' if appended != 1 else ''}. "
            f"Starting with {first_track_name}."
        )
        displayed_msg = (
            f"Queue ended — automatically continued with **{appended}** "
            f"acoustically similar track{'s' if appended != 1 else ''}. "
            f"Now playing: **{first_track_name}**."
        )

        av = self.assistant_view
        av._ensure_initialized()
        try:
            await av._append_bubble(
                "assistant",
                displayed_msg,
                speak=True,
                speak_text=spoken_msg,
            )
        except Exception as exc:
            logger.warning("Jarvis continuation: bubble failed: %s", exc)

        # ── Resume playback at chosen new track (shuffle-aware) ──────────────
        audio_engine.play_track_at(start_index)

    def _fetch_artwork_url_async(self, img_url: str):
        # Check in-memory cache first; avoids any disk/network I/O
        cached = _ARTWORK_CACHE.get(img_url)
        if cached:
            self.safe_update(lambda p=cached: (
                self.mini_player.update_artwork(p),
                self.now_playing.update_artwork(p),
            ))
            return

        def _worker():
            try:
                ph  = hashlib.md5(img_url.encode()).hexdigest()
                tmp = os.path.join(get_temp_artwork_dir(), f"streamrip_art_{ph}.jpg")
                if not os.path.exists(tmp):
                    urllib.request.urlretrieve(img_url, tmp)
                _ARTWORK_CACHE.put(img_url, tmp)
                self.safe_update(lambda p=tmp: (
                    self.mini_player.update_artwork(p),
                    self.now_playing.update_artwork(p),
                ))
            except Exception as exc:
                logger.error("Artwork URL fetch failed: %s", exc)
        asyncio.create_task(asyncio.to_thread(_worker))

    def _extract_artwork_async(self, path: str):
        # Check in-memory cache; avoids PIL decode + disk write on repeat plays
        cached = _ARTWORK_CACHE.get(path)
        if cached:
            self.safe_update(lambda p=cached: (
                self.mini_player.update_artwork(p),
                self.now_playing.update_artwork(p),
            ))
            return

        # Debounce: cancel any pending extraction for rapid track switches
        if hasattr(self, '_artwork_timer') and self._artwork_timer:
            self._artwork_timer.cancel()

        def _worker():
            raw_bytes = None
            if os.path.exists(path):
                dir_path = os.path.dirname(path)
                for name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png", "Artwork.jpg"):
                    candidate = os.path.join(dir_path, name)
                    if os.path.exists(candidate):
                        try:
                            with open(candidate, "rb") as fh:
                                raw_bytes = fh.read()
                        except Exception:
                            pass
                        break
                if not raw_bytes:
                    try:
                        from utils.metadata_editor import extract_artwork
                        raw_bytes = extract_artwork(path)
                    except Exception as exc:
                        logger.error("Artwork extraction failed: %s", exc)

            art_path = ""
            if raw_bytes:
                try:
                    from PIL import Image as _PIL
                    img = _PIL.open(BytesIO(raw_bytes))
                    img.thumbnail((512, 512))
                    buf = BytesIO()
                    img.save(buf, format="JPEG", quality=85)
                    data = buf.getvalue()
                except Exception:
                    data = raw_bytes
                ph = hashlib.md5(path.encode()).hexdigest()
                art_path = os.path.join(get_temp_artwork_dir(), f"streamrip_art_{ph}.jpg")
                try:
                    with open(art_path, "wb") as fh:
                        fh.write(data)
                    _ARTWORK_CACHE.put(path, art_path)  # cache for next play
                except Exception as exc:
                    logger.error("Artwork write failed: %s", exc)
                    art_path = ""

            self.safe_update(lambda p=art_path: (
                self.mini_player.update_artwork(p),
                self.now_playing.update_artwork(p),
            ))

        # Debounce: cancel any pending extraction for rapid track switches
        if hasattr(self, '_artwork_task') and self._artwork_task:
            self._artwork_task.cancel()

        async def _delayed_extract():
            await asyncio.sleep(0.2)
            await asyncio.to_thread(_worker)
        
        self._artwork_task = asyncio.create_task(_delayed_extract())

    def _on_position(self, _instance, position: float):
        # Pacing is set in Dart (flet_audio_service.dart) and dirty-checked in
        # the engine's _set(). No throttle needed here — every dispatch reflects
        # a visible (≥1 s) change at the slider granularity.
        try:
            pos_f = float(position or 0.0)
        except (TypeError, ValueError):
            pos_f = 0.0
        # Only capture position for the currently-active track. Skip the
        # reset-to-zero pulse emitted by _sync_metadata_for_current immediately
        # before the engine swaps current_path — otherwise we'd record every
        # skip as a 0-second play and lose the implicit signal.
        if pos_f > 0.0 and audio_engine.current_path == self._last_played_path:
            self._last_play_position = pos_f
        if self.is_background or self.is_scrubbing:
            return

        dur = audio_engine.duration
        pct = (position / dur * 100) if dur > 0 else 0

        if self.mini_player.container and self.mini_player.container.page:
            self.mini_player.update_progress(pct)
            self.mini_player.container.update()

        if self.now_playing.container and self.now_playing.container.open:
            self.now_playing.update_progress(position, dur)
            if self.now_playing.container.page:
                self.now_playing.container.update()

    def _on_duration(self, _instance, duration: float):
        # Duration is invariant during playback of a single track, so the
        # total-time label is set once per track-change instead of per tick.
        try:
            dur_f = float(duration or 0.0)
        except (TypeError, ValueError):
            dur_f = 0.0
        # Same guard as _on_position: ignore the reset-to-zero pulse during
        # track transitions and only update when the duration belongs to the
        # currently-active track.
        if dur_f > 0.0 and audio_engine.current_path == self._last_played_path:
            self._last_play_duration = dur_f
        if self.is_background:
            return
        self.now_playing.update_duration(duration)
        if self.now_playing.container and self.now_playing.container.open and self.now_playing.container.page:
            self.now_playing.container.update()

    def _on_is_playing(self, _instance, is_playing: bool):
        if self.is_background:
            return
        def _update():
            self.mini_player.update_state(is_playing)
            self.now_playing.update_state(is_playing)
        self.safe_update(_update)

    # ── queue state persistence ───────────────────────────────────────────────
    async def _restore_queue_state_async(self):
        state_path = os.path.join(DATA_DIR, "queue_state.json")
        if not os.path.exists(state_path):
            return
        try:
            # File read is the only blocking bit; keep it off the loop.
            state = await asyncio.to_thread(self._read_queue_state, state_path)
            if not state:
                return
            queue = state.get("queue", [])
            index = state.get("current_index", 0)
            pos   = state.get("position", 0.0)
            dur   = state.get("duration", 0.0)
            if not queue:
                return

            # Issue A: Restore "Play Similar" saved queue and index
            self.play_similar_saved_queue = state.get("play_similar_saved_queue", None)
            self.play_similar_saved_index = state.get("play_similar_saved_index", None)
            self.play_similar_saved_shuffle = state.get("play_similar_saved_shuffle", False)

            if self.play_similar_mode and not self.play_similar_saved_queue:
                fallback_state = self._load_queue_from_file("queue_shuffle.json" if self.play_similar_saved_shuffle else "queue_regular.json")
                if fallback_state:
                    self.play_similar_saved_queue = fallback_state.get("queue", None)
                    self.play_similar_saved_index = fallback_state.get("current_index", None)

            # Bound `index` defensively; a stale snapshot may reference a
            # row that no longer exists in the persisted queue.
            index = max(0, min(int(index or 0), len(queue) - 1))

            # restore_queue must run on the event loop so the synchronous
            # observer dispatches it triggers (current_path, is_playing,
            # etc.) reach safe_update / page.run_task in a thread that
            # actually has a running loop. Without this, the mini-player
            # never gets revived.
            try:
                self.is_restoring_session = True
                audio_engine.restore_queue(queue, index, pos, dur)
            finally:
                self.is_restoring_session = False

            # Pre-extract artwork for the restored track so the mini-
            # player and now-playing screen don't render a blank tile
            # while waiting for first playback.
            restored_path = queue[index].get("path")
            if restored_path:
                self._extract_artwork_async(restored_path)

            # Manually drive the now-playing UI since restore_queue runs
            # entirely synchronously. _set("current_path", ...) dispatches
            # _on_current_path which updates the mini-player meta, but
            # this belt-and-braces call ensures the mini-player is forced
            # visible even on edge cases (e.g. if current_path equalled
            # the cached value because of a prior partial init).
            track  = audio_engine.current_track  or ""
            artist = audio_engine.current_artist or ""
            album  = audio_engine.current_album  or ""
            if track:
                self.mini_player.update_meta(track, artist)
                self.now_playing.update_meta(track, artist, album)
                self.mini_player.update_state(False)
                self.now_playing.update_state(False)

            # Show the saved progress on the bar; duration is unknown
            # until the source loads, so leave the percent at 0 and let
            # _on_position fix it once the engine reports duration.

            async def _delayed_refresh():
                # 300 ms is long enough for restore_queue's _push_queue_native
                # to have reached Dart and produced a queue list.
                await asyncio.sleep(0.3)
                self.safe_update(self.queue_sheet.refresh)
            asyncio.create_task(_delayed_refresh())
        except Exception as exc:
            logger.warning("Could not restore queue state: %s", exc)

    def _read_queue_state(self, state_path: str):
        try:
            with open(state_path) as fh:
                return json.load(fh)
        except Exception:
            return None

    def _save_queue_to_file(self, filename: str, queue_list: list[dict], current_index: int, position: float, duration: float):
        path = os.path.join(os.environ["XDG_CACHE_HOME"], filename)
        if not queue_list:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            return

        state = {
            "queue":         queue_list,
            "current_index": current_index,
            "position":      position,
            "duration":      duration,
            "play_similar_saved_queue": getattr(self, "play_similar_saved_queue", None),
            "play_similar_saved_index": getattr(self, "play_similar_saved_index", None),
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                safe_json_dump(state, fh)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("Could not save context queue to %s: %s", filename, exc)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _load_queue_from_file(self, filename: str) -> dict | None:
        path = os.path.join(os.environ["XDG_CACHE_HOME"], filename)
        if not os.path.exists(path):
            return None
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception as exc:
            logger.warning("Could not load context queue from %s: %s", filename, exc)
            return None

    def _restore_queue_state(self):
        """Synchronous compat wrapper kept for any external callers; the
        runtime path now uses _restore_queue_state_async on the event loop
        so observer dispatches actually update the UI."""
        try:
            asyncio.get_event_loop().run_until_complete(
                self._restore_queue_state_async()
            )
        except RuntimeError:
            pass

    def _save_queue_state(self):
        """Write the current queue snapshot to disk atomically.

        Atomicity matters because Android can SIGKILL the process between
        the open() and the close(); without the tmp+rename dance we'd
        leave a half-written JSON file that breaks the next restore.

        Empty queue ⇒ delete the file rather than write `{"queue": []}` so
        that an explicit `stop()` doesn't leave a phantom session for the
        next launch to "restore" into nothing.
        """
        path = os.path.join(DATA_DIR, "queue_state.json")
        if not audio_engine.queue:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            # Also clear the specific partitioned files
            if self.play_similar_mode:
                self._save_queue_to_file("queue_similar.json", [], 0, 0.0, 0.0)
            elif audio_engine.is_shuffle:
                self._save_queue_to_file("queue_shuffle.json", [], 0, 0.0, 0.0)
            else:
                self._save_queue_to_file("queue_regular.json", [], 0, 0.0, 0.0)
            return

        state = {
            "queue":         audio_engine.queue,
            "current_index": audio_engine.current_index,
            "position":      audio_engine.position,
            # Persist duration so the slider has a correct max value during
            # the brief window between restore and the Dart side reporting
            # duration_ms. Without this the slider's max defaults to 0 and
            # any pre-load scrub computes a meaningless target.
            "duration":      audio_engine.duration,
            # Issue A: Persist "Play Similar" saved queue and index
            "play_similar_saved_queue": getattr(self, "play_similar_saved_queue", None),
            "play_similar_saved_index": getattr(self, "play_similar_saved_index", None),
            "play_similar_saved_shuffle": getattr(self, "play_similar_saved_shuffle", False),
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                safe_json_dump(state, fh)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    # fsync isn't supported on every Android FS layer;
                    # rename still gives us all-or-nothing semantics.
                    pass
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("Could not save queue state: %s", exc)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

        # Also mirror to context-specific queue partition in XDG_CACHE_HOME
        if self.play_similar_mode:
            self._save_queue_to_file("queue_similar.json", audio_engine.queue, audio_engine.current_index, audio_engine.position, audio_engine.duration)
        elif audio_engine.is_shuffle:
            self._save_queue_to_file("queue_shuffle.json", audio_engine.queue, audio_engine.current_index, audio_engine.position, audio_engine.duration)
        else:
            self._save_queue_to_file("queue_regular.json", audio_engine.queue, audio_engine.current_index, audio_engine.position, audio_engine.duration)

    def _schedule_queue_save(self, delay: float = 0.4):
        """Coalesce save requests over a short window. A burst of mutations
        (e.g. enqueueing an album of 12 tracks) triggers one disk write
        instead of twelve."""
        existing = getattr(self, "_queue_save_task", None)
        if existing is not None and not existing.done():
            existing.cancel()

        async def _do_save():
            try:
                await asyncio.sleep(delay)
                await asyncio.to_thread(self._save_queue_state)
            except asyncio.CancelledError:
                pass

        try:
            self._queue_save_task = asyncio.create_task(_do_save())
        except RuntimeError:
            # Called before the loop is running (e.g. during shutdown);
            # fall back to a synchronous write so we don't lose the snapshot.
            self._save_queue_state()

    async def _position_save_loop(self):
        """Periodically flush position so the restored offset is close to
        where the user actually was when the OS killed the process. We
        only write when something has actually changed (track or position
        delta > 5 s) to avoid pointless disk traffic while paused."""
        last_path = None
        last_pos = -10.0
        while True:
            try:
                await asyncio.sleep(10.0)
                if not audio_engine.queue or not audio_engine.is_playing:
                    continue
                pos = audio_engine.position or 0.0
                cur_path = audio_engine.current_path or ""
                if cur_path == last_path and abs(pos - last_pos) < 5.0:
                    continue
                last_path = cur_path
                last_pos = pos
                await asyncio.to_thread(self._save_queue_state)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("position-save loop error: %s", exc)

    # ── playback helpers ─────────────────────────────────────────────────────
    async def play_track(self, target_path: str, source: tuple | None = None):
        await self.error_boundary.capture(self._play_track_core)(target_path, source)

    async def _play_track_core(self, target_path: str, source: tuple | None = None):
        # Yield to event loop to keep UI responsive
        await asyncio.sleep(0)

        # Skip ahead to target track if it already exists in the queue, rather than rebuilding it,
        # but only if we are in the general view (not a playlist, album, or search context).
        is_general_view = False
        if source is None or (isinstance(source, tuple) and len(source) > 0 and source[0] == "library"):
            if not self.library_view or not getattr(self.library_view, "search_query", None):
                is_general_view = True

        if is_general_view and audio_engine.queue:
            existing_idx = -1
            for i, t in enumerate(audio_engine.queue):
                if t.get("path") == target_path:
                    existing_idx = i
                    break
            if existing_idx != -1:
                audio_engine.play_track_at(existing_idx)
                return

        db = self.db_manager

        if audio_engine.is_shuffle:
            # Physical playback in shuffle mode shuffles from the ENTIRE library
            all_tracks = await db.get_all_tracks()
            library_tracks = []
            target_item = None
            for t in all_tracks:
                p = t.get("path", "")
                if p:
                    library_tracks.append({
                        "path":        p,
                        "track_title": t.get("title") or os.path.basename(p),
                        "artist_name": t.get("artist") or "Unknown",
                        "album_title": t.get("album")  or "Unknown",
                    })

            # Find target index
            target_idx = -1
            for i, t in enumerate(library_tracks):
                if t.get("path") == target_path:
                    target_idx = i
                    break

            if target_idx == -1:
                # clicked track is not in library (e.g. streaming search result). Resolve from active view items.
                view_items = []
                if source and source[0] == "album":
                    _, arti, alb = source
                    view_items = await db.get_tracks_by_album(alb, arti)
                elif source and source[0] == "playlist":
                    _, pl_id = source
                    view_items = await db.get_tracks_in_playlist(pl_id)
                else:
                    lv = self.library_view
                    want_key = (lv.view_mode, lv.search_query, lv.sort_mode)
                    if lv._tracks_cache is not None and lv._tracks_cache_key == want_key:
                        view_items = lv._tracks_cache
                    else:
                        view_items = await db.get_all_tracks(search_query=lv.search_query, sort_mode=lv.sort_mode)

                for t in view_items:
                    if t.get("path") == target_path:
                        target_item = {
                            "path":        target_path,
                            "track_title": t.get("title") or os.path.basename(target_path),
                            "artist_name": t.get("artist") or "Unknown",
                            "album_title": t.get("album")  or "Unknown",
                        }
                        break
                if not target_item:
                    target_item = {
                        "path":        target_path,
                        "track_title": os.path.basename(target_path),
                        "artist_name": "Unknown",
                        "album_title": "Unknown",
                    }
                library_tracks.insert(0, target_item)
                target_idx = 0

            audio_engine.jarvis_controlled = False
            audio_engine.set_queue(library_tracks, start_index=target_idx)
            return

        # ── Resolve the candidate item list based on tap context ──────────
        # Album / playlist taps query a small, scoped set directly; far
        # cheaper than re-fetching every track in the library. Library taps
        # reuse the in-memory list captured by LibraryView on the last
        # tracks-view render; we fall back to a fresh query only if that
        # cache is missing or stale (sort/search/view changed since).
        if source and source[0] == "album":
            _, arti, alb = source
            items = await db.get_tracks_by_album(alb, arti)
        elif source and source[0] == "playlist":
            _, pl_id = source
            items = await db.get_tracks_in_playlist(pl_id)
        else:
            lv = self.library_view
            want_key = (lv.view_mode, lv.search_query, lv.sort_mode)
            if lv._tracks_cache is not None and lv._tracks_cache_key == want_key:
                items = lv._tracks_cache
            else:
                items = await db.get_all_tracks(
                    search_query=lv.search_query, sort_mode=lv.sort_mode
                )
                # Populate the cache so subsequent taps in the same view
                # are instant even before the user has scrolled.
                lv._tracks_cache = items
                lv._tracks_cache_key = want_key

        if not items:
            self.safe_update(lambda: self.show_snackbar("No playable media found."))
            return

        # Find target index
        target_idx = -1
        for i, t in enumerate(items):
            if t.get("path") == target_path:
                target_idx = i
                break

        if target_idx == -1:
            self.safe_update(lambda: self.show_snackbar("Track not found in current view."))
            return

        # Physical playback queue extends to the end of the library/search results,
        # fully decoupled from the UI rendering/pagination limits.
        tracks = []
        for t in items:
            p = t.get("path", "")
            if p:
                tracks.append({
                    "path":        p,
                    "track_title": t.get("title") or os.path.basename(p),
                    "artist_name": t.get("artist") or "Unknown",
                    "album_title": t.get("album")  or "Unknown",
                })

        if self.play_similar_mode:
            target_track = tracks[target_idx]
            
            # 1. Update pre-similar backup queues in memory
            self.play_similar_saved_queue = list(tracks)
            self.play_similar_saved_index = target_idx
            
            # 2. Partition Cache: Write this new context queue to disk immediately
            if getattr(self, "play_similar_saved_shuffle", False):
                self._save_queue_to_file("queue_shuffle.json", tracks, target_idx, 0.0, 0.0)
            else:
                self._save_queue_to_file("queue_regular.json", tracks, target_idx, 0.0, 0.0)
            
            # 3. Set active queue to just the clicked track and start play
            audio_engine.jarvis_controlled = False
            audio_engine.set_queue([target_track], start_index=0)
            
            # 4. Trigger new similarity walk starting from this track path
            self._play_similar_gen += 1
            audio_engine.play_similar_seed_path = target_track.get("path") or ""
            self.page.run_task(self._initiate_play_similar_queue_async, target_track.get("path"), self._play_similar_gen)
        elif getattr(self, "auto_dj_mode", False):
            target_track = tracks[target_idx]
            
            # 1. Update pre-similar backup queues in memory
            self.play_similar_saved_queue = list(tracks)
            self.play_similar_saved_index = target_idx
            
            # 2. Partition Cache: Write this new context queue to disk immediately
            if getattr(self, "play_similar_saved_shuffle", False):
                self._save_queue_to_file("queue_shuffle.json", tracks, target_idx, 0.0, 0.0)
            else:
                self._save_queue_to_file("queue_regular.json", tracks, target_idx, 0.0, 0.0)
            
            # 3. Set active queue to just the clicked track and start play
            audio_engine.jarvis_controlled = False
            audio_engine.set_queue([target_track], start_index=0)
            
            # 4. Trigger new Auto-DJ curation starting with this track
            self.page.run_task(self._initiate_auto_dj_queue_async)
        else:
            audio_engine.jarvis_controlled = False
            audio_engine.set_queue(tracks, start_index=target_idx)

    def set_play_similar_mode(self, enabled: bool, transitioning_to_shuffle: bool = False):
        if self.play_similar_mode == enabled:
            return

        # Turn off auto_dj if active
        if enabled and getattr(self, "auto_dj_mode", False):
            self.set_auto_dj_mode(False)

        # Bump the generation counter FIRST so any in-flight async tasks
        # from the previous session see a stale gen and bail out before
        # they mutate the queue.
        self._play_similar_gen += 1
        gen = self._play_similar_gen

        self.play_similar_mode = enabled
        self._save_pref("play_similar_mode", enabled)
        
        self.now_playing.update_play_similar(enabled)
        self.mini_player.update_play_similar(enabled)
        
        verb = "Enabled" if enabled else "Disabled"
        self.show_snackbar(
            f"Play Similar: {verb}",
            icon=ft.Icons.LINK_ROUNDED if enabled else ft.Icons.LINK_OFF_ROUNDED
        )
        
        if enabled:
            # 1. Mutual exclusivity: turn off shuffle, but remember if it was ON
            was_shuffle = bool(audio_engine.is_shuffle)
            self.play_similar_saved_shuffle = was_shuffle
            if was_shuffle:
                audio_engine.is_shuffle = False
                self.now_playing.update_shuffle(False)
                self._save_pref("is_shuffle", False)
            
            # 2. Save original queue before modifying it
            self.play_similar_saved_queue = list(audio_engine.queue)
            self.play_similar_saved_index = audio_engine.current_index
            
            # Save it to appropriate partition file immediately for session recovery
            if was_shuffle:
                self._save_queue_to_file("queue_shuffle.json", self.play_similar_saved_queue, self.play_similar_saved_index, audio_engine.position, audio_engine.duration)
            else:
                self._save_queue_to_file("queue_regular.json", self.play_similar_saved_queue, self.play_similar_saved_index, audio_engine.position, audio_engine.duration)
            
            # 3. Initiate similar tracks walk starting from currently playing song
            path = audio_engine.current_path
            audio_engine.play_similar_seed_path = path or ""
            if path:
                self.page.run_task(self._initiate_play_similar_queue_async, path, gen)
        else:
            # Save the current similar queue to its partitioned file
            self._save_queue_to_file("queue_similar.json", audio_engine.queue, audio_engine.current_index, audio_engine.position, audio_engine.duration)

            # 1. Clear seed path so stale replenishment hooks don't fire
            audio_engine.play_similar_seed_path = ""

            # 2. Restore original queue if saved
            saved_q = getattr(self, "play_similar_saved_queue", None)
            saved_idx = getattr(self, "play_similar_saved_index", 0)
            saved_shuf = getattr(self, "play_similar_saved_shuffle", False)
            
            # Fallback to files if memory variables are empty
            if not saved_q:
                if saved_shuf:
                    state = self._load_queue_from_file("queue_shuffle.json")
                else:
                    state = self._load_queue_from_file("queue_regular.json")
                if state:
                    saved_q = state.get("queue", [])
                    saved_idx = state.get("current_index", 0)
            
            if saved_q:
                cur_path = audio_engine.current_path
                orig_idx = -1
                if cur_path:
                    for idx, t in enumerate(saved_q):
                        if t.get("path") == cur_path:
                            orig_idx = idx
                            break
                
                if orig_idx != -1:
                    # Current track exists in the original queue — splice:
                    # keep the live queue up to the current track, then
                    # append the remainder of the saved queue after it.
                    new_q = list(audio_engine.queue[:audio_engine.current_index + 1])
                    new_q.extend(saved_q[orig_idx + 1:])
                    audio_engine.queue = new_q
                    audio_engine.current_index = audio_engine.current_index  # unchanged
                else:
                    # Current track was injected by the walk and is not in
                    # the saved queue. Keep playing it, but restore the
                    # original queue behind it by inserting it at position 0.
                    cur_track = None
                    ci = audio_engine.current_index
                    if 0 <= ci < len(audio_engine.queue):
                        cur_track = audio_engine.queue[ci]
                    if cur_track:
                        new_q = [cur_track] + list(saved_q)
                        audio_engine.queue = new_q
                        audio_engine.current_index = 0
                    else:
                        audio_engine.queue = list(saved_q)
                        audio_engine.current_index = max(0, min(int(saved_idx or 0), len(saved_q) - 1))
                
                # Restore shuffle state if it was shuffle before
                if saved_shuf or transitioning_to_shuffle:
                    audio_engine.is_shuffle = True
                    self.now_playing.update_shuffle(True)
                    self._save_pref("is_shuffle", True)
                else:
                    audio_engine.is_shuffle = False
                    self.now_playing.update_shuffle(False)
                    self._save_pref("is_shuffle", False)
                
                # Clear saved queue to avoid memory leaks
                self.play_similar_saved_queue = None
                self.play_similar_saved_index = None
                self.play_similar_saved_shuffle = False
                
                # Force sync visual metadata and notify UI
                audio_engine._sync_metadata_for_current()
                audio_engine.dispatch("on_queue_mutated")
                if hasattr(audio_engine, "_push_queue_native") and audio_engine._page:
                    if hasattr(audio_engine, "_arm_queue_gate"):
                        audio_engine._arm_queue_gate()
                    audio_engine._page.run_task(
                        audio_engine._push_queue_native,
                        audio_engine.current_index,
                        audio_engine.is_playing
                    )
        
        if hasattr(self, "queue_sheet") and self.queue_sheet and self.queue_sheet._initialized:
            self.queue_sheet.refresh()

    def set_auto_dj_mode(self, enabled: bool):
        if self.auto_dj_mode == enabled:
            return

        # Turn off play_similar if active
        if enabled and self.play_similar_mode:
            self.set_play_similar_mode(False)

        self.auto_dj_mode = enabled
        self._save_pref("auto_dj_mode", enabled)

        self.now_playing.update_auto_dj(enabled)
        self.mini_player.update_auto_dj(enabled)

        verb = "Enabled" if enabled else "Disabled"
        self.show_snackbar(
            f"Auto-DJ: {verb}",
            icon=ft.Icons.AUTO_AWESOME_ROUNDED if enabled else ft.Icons.AUTO_AWESOME_OUTLINED
        )

        if enabled:
            # 1. Mutual exclusivity: turn off shuffle
            was_shuffle = bool(audio_engine.is_shuffle)
            self.play_similar_saved_shuffle = was_shuffle
            if was_shuffle:
                audio_engine.is_shuffle = False
                self.now_playing.update_shuffle(False)
                self._save_pref("is_shuffle", False)

            # 2. Save original queue
            self.play_similar_saved_queue = list(audio_engine.queue)
            self.play_similar_saved_index = audio_engine.current_index

            # Save to partitioned files for recovery
            if was_shuffle:
                self._save_queue_to_file("queue_shuffle.json", self.play_similar_saved_queue, self.play_similar_saved_index, audio_engine.position, audio_engine.duration)
            else:
                self._save_queue_to_file("queue_regular.json", self.play_similar_saved_queue, self.play_similar_saved_index, audio_engine.position, audio_engine.duration)

            # 3. Initiate Auto-DJ curation
            self.page.run_task(self._initiate_auto_dj_queue_async)
        else:
            # Save current Auto-DJ queue
            self._save_queue_to_file("queue_similar.json", audio_engine.queue, audio_engine.current_index, audio_engine.position, audio_engine.duration)

            # Restore original queue
            saved_q = getattr(self, "play_similar_saved_queue", None)
            saved_idx = getattr(self, "play_similar_saved_index", 0)
            saved_shuf = getattr(self, "play_similar_saved_shuffle", False)

            if not saved_q:
                if saved_shuf:
                    state = self._load_queue_from_file("queue_shuffle.json")
                else:
                    state = self._load_queue_from_file("queue_regular.json")
                if state:
                    saved_q = state.get("queue", [])
                    saved_idx = state.get("current_index", 0)

            if saved_q:
                cur_path = audio_engine.current_path
                orig_idx = -1
                if cur_path:
                    for idx, t in enumerate(saved_q):
                        if t.get("path") == cur_path:
                            orig_idx = idx
                            break
                if orig_idx != -1:
                    saved_idx = orig_idx

                # Preserve current track and position
                pos = audio_engine.position
                is_p = audio_engine.is_playing
                audio_engine.set_queue(saved_q, start_index=saved_idx)
                if is_p:
                    audio_engine.play()
                    if pos > 0:
                        audio_engine.seek(pos)

            if saved_shuf:
                audio_engine.is_shuffle = True
                self.now_playing.update_shuffle(True)
                self._save_pref("is_shuffle", True)

            # Clear memory state
            self.play_similar_saved_queue = None
            self.play_similar_saved_index = None
            self.play_similar_saved_shuffle = False

            # Notify UI
            audio_engine._sync_metadata_for_current()
            audio_engine.dispatch("on_queue_mutated")

        if hasattr(self, "queue_sheet") and self.queue_sheet and self.queue_sheet._initialized:
            self.queue_sheet.refresh()

    async def _initiate_auto_dj_queue_async(self):
        """Build the initial Auto-DJ queue of curated tracks and start playing."""
        import os
        from utils import track_graph as tg
        import numpy as np
        try:
            if not self.auto_dj_mode:
                return

            # Score library
            w, b, ne, ni = await tg._load_taste_model(self.db_manager)
            rows, pca_matrix = await tg._load_percentile_matrix(self.db_manager, tg.FEATURES_VERSION)

            # Preserve current playing track if any
            cur_path = audio_engine.current_path
            cur_track_dict = None
            if cur_path:
                cur_row = await self.db_manager.get_track_full(cur_path)
                if cur_row:
                    cur_track_dict = {
                        "path":        cur_path,
                        "track_title": cur_row.get("title") or cur_row.get("track_title") or os.path.basename(cur_path),
                        "artist_name": cur_row.get("artist") or cur_row.get("artist_name") or "Unknown Artist",
                        "album_title": cur_row.get("album")  or cur_row.get("album_title")  or "Unknown Album",
                        "duration":    cur_row.get("duration", 0.0) or 0.0,
                        "image_url":   cur_row.get("image_url", "") or "",
                    }

            avoid = set()
            if cur_path:
                avoid.add(cur_path)
            for bad in self._session_bad_paths:
                avoid.add(bad)
            try:
                recent = await self.db_manager.recent_played_paths(window_seconds=7 * 86400)
                avoid.update(recent)
            except Exception:
                pass

            candidates = []
            if rows:
                if ne + ni > 0:
                    # Warm model: score
                    for i, r in enumerate(rows):
                        p = r["path"]
                        if p in avoid:
                            continue
                        pc = pca_matrix[i]
                        z = float(np.dot(w, pc)) + b
                        z = np.clip(z, -30.0, 30.0)
                        p_like = 1.0 / (1.0 + np.exp(-z))
                        candidates.append((r, p_like))
                    
                    # Sort and select top 20
                    candidates.sort(key=lambda x: x[1], reverse=True)
                    top_candidates = candidates[:20]
                    import random
                    selected = random.sample(top_candidates, min(10, len(top_candidates))) if top_candidates else []
                else:
                    # Cold model: pick random tracks
                    import random
                    pool = [r for r in rows if r["path"] not in avoid]
                    selected = random.sample(pool, min(10, len(pool))) if pool else []

                engine_tracks = []
                # Place current track first if any
                if cur_track_dict:
                    engine_tracks.append(cur_track_dict)
                
                # Check for cold start message
                if ne + ni == 0:
                    self.show_snackbar("Auto-DJ learning mode active. Like/play tracks to customize!", icon=ft.Icons.INFO_ROUNDED)

                # Fill remaining spots with recommendations
                for item in selected:
                    r = item[0] if isinstance(item, tuple) else item
                    engine_tracks.append({
                        "path":        r.get("path"),
                        "track_title": r.get("title") or r.get("track_title") or os.path.basename(r.get("path")),
                        "artist_name": r.get("artist") or r.get("artist_name") or "Unknown Artist",
                        "album_title": r.get("album")  or r.get("album_title")  or "Unknown Album",
                        "duration":    r.get("duration", 0.0) or 0.0,
                        "image_url":   r.get("image_url", "") or "",
                    })

                if engine_tracks:
                    # Race check
                    if not self.auto_dj_mode:
                        return
                    
                    pos = audio_engine.position if cur_track_dict else 0
                    audio_engine.set_queue(engine_tracks, start_index=0)
                    if cur_track_dict:
                        audio_engine.play()
                        if pos > 0:
                            audio_engine.seek(pos)
                    else:
                        audio_engine.play()
                    
                    logger.info("Auto-DJ: Successfully built initial queue with %d curated tracks.", len(engine_tracks))
                    
                    if hasattr(self, "queue_sheet") and self.queue_sheet and self.queue_sheet._initialized:
                        self.queue_sheet.refresh()
        except Exception as exc:
            logger.exception("Auto-DJ: Failed to build initial queue: %s", exc)

    async def _auto_dj_auto_continue_queue(self):
        """Automatically extend the Auto-DJ queue with 5 tracks optimized by the taste regressor."""
        import os
        from utils import track_graph as tg
        import numpy as np
        try:
            if not self.auto_dj_mode:
                return

            avoid = {t["path"] for t in audio_engine.queue if t.get("path")}
            for bad in self._session_bad_paths:
                avoid.add(bad)
            try:
                recent = await self.db_manager.recent_played_paths(window_seconds=7 * 86400)
                avoid.update(recent)
            except Exception:
                pass

            # Score library
            w, b, ne, ni = await tg._load_taste_model(self.db_manager)
            rows, pca_matrix = await tg._load_percentile_matrix(self.db_manager, tg.FEATURES_VERSION)
            
            candidates = []
            if rows:
                if ne + ni > 0:
                    # Warm model
                    for i, r in enumerate(rows):
                        p = r["path"]
                        if p in avoid:
                            continue
                        pc = pca_matrix[i]
                        z = float(np.dot(w, pc)) + b
                        z = np.clip(z, -30.0, 30.0)
                        p_like = 1.0 / (1.0 + np.exp(-z))
                        candidates.append((r, p_like))
                    
                    # Sort by p_like descending, and take the top 20
                    candidates.sort(key=lambda x: x[1], reverse=True)
                    top_candidates = candidates[:20]
                    # Randomly sample up to 5 tracks from the top candidates to keep it fresh
                    import random
                    selected = random.sample(top_candidates, min(5, len(top_candidates))) if top_candidates else []
                    # Sort them just in case or shuffle
                    engine_tracks = []
                    for r, _ in selected:
                        engine_tracks.append({
                            "path":        r.get("path"),
                            "track_title": r.get("title") or r.get("track_title") or os.path.basename(r.get("path")),
                            "artist_name": r.get("artist") or r.get("artist_name") or "Unknown Artist",
                            "album_title": r.get("album")  or r.get("album_title")  or "Unknown Album",
                            "duration":    r.get("duration", 0.0) or 0.0,
                            "image_url":   r.get("image_url", "") or "",
                        })
                else:
                    # Cold model: pick random tracks
                    import random
                    pool = [r for r in rows if r["path"] not in avoid]
                    selected = random.sample(pool, min(5, len(pool))) if pool else []
                    engine_tracks = []
                    for r in selected:
                        engine_tracks.append({
                            "path":        r.get("path"),
                            "track_title": r.get("title") or r.get("track_title") or os.path.basename(r.get("path")),
                            "artist_name": r.get("artist") or r.get("artist_name") or "Unknown Artist",
                            "album_title": r.get("album")  or r.get("album_title")  or "Unknown Album",
                            "duration":    r.get("duration", 0.0) or 0.0,
                            "image_url":   r.get("image_url", "") or "",
                        })

                if engine_tracks:
                    # Double check mode wasn't toggled off
                    if not self.auto_dj_mode:
                        return
                    for track in engine_tracks:
                        audio_engine.queue_last(track)
                    logger.info("Auto-DJ: Automatically appended %d curated tracks to the queue.", len(engine_tracks))
                    
                    if hasattr(self, "queue_sheet") and self.queue_sheet and self.queue_sheet._initialized:
                        self.queue_sheet.refresh()
        except Exception as exc:
            logger.exception("Auto-DJ: Failed to auto continue queue: %s", exc)

    def toggle_shuffle(self):
        self.page.run_task(self._toggle_shuffle_async)

    async def _toggle_shuffle_async(self):
        new_shuffle = not audio_engine.is_shuffle
        self.now_playing.update_shuffle(new_shuffle)
        self._save_pref("is_shuffle", new_shuffle)
        if new_shuffle:
            if self.play_similar_mode:
                self.set_play_similar_mode(False, transitioning_to_shuffle=True)
            if getattr(self, "auto_dj_mode", False):
                self.set_auto_dj_mode(False)

        # ── Toggle ON: regular queue -> entire library shuffle queue ─────────
        if new_shuffle:
            # 1. Save currently playing/active queue to regular cache
            self._save_queue_to_file("queue_regular.json", audio_engine.queue, audio_engine.current_index, audio_engine.position, audio_engine.duration)
            
            # 2. Fetch all tracks from the entire library
            all_tracks = await self.db_manager.get_all_tracks()
            library_tracks = []
            for t in all_tracks:
                p = t.get("path", "")
                if p:
                    library_tracks.append({
                        "path":        p,
                        "track_title": t.get("title") or os.path.basename(p),
                        "artist_name": t.get("artist") or "Unknown",
                        "album_title": t.get("album")  or "Unknown",
                    })

            if library_tracks:
                current_track = audio_engine.queue[audio_engine.current_index] if audio_engine.queue else None
                found_idx = -1
                if current_track:
                    # Find currently playing track in the library
                    for i, t in enumerate(library_tracks):
                        if t.get("path") == current_track.get("path"):
                            found_idx = i
                            break
                    if found_idx == -1:
                        # Append currently playing track if it is not in the library database
                        library_tracks.insert(0, current_track)
                        found_idx = 0
                else:
                    found_idx = 0

                # 3. Transition to shuffle queue without redundant early pushes
                audio_engine._is_shuffle = True
                audio_engine.set_queue(library_tracks, start_index=found_idx)
                # Save the new shuffle queue state
                self._save_queue_to_file("queue_shuffle.json", audio_engine.queue, audio_engine.current_index, audio_engine.position, audio_engine.duration)
            else:
                audio_engine._is_shuffle = True
                audio_engine._on_shuffle_changed()

        # ── Toggle OFF: shuffle queue -> restore regular queue ───────────────
        else:
            # 1. Save currently playing shuffle queue
            self._save_queue_to_file("queue_shuffle.json", audio_engine.queue, audio_engine.current_index, audio_engine.position, audio_engine.duration)
            
            # 2. Read regular queue from cache
            state = await asyncio.to_thread(self._load_queue_from_file, "queue_regular.json")
            if state:
                regular_queue = state.get("queue", [])
                regular_index = state.get("current_index", 0)
                regular_pos   = state.get("position", 0.0)
                regular_dur   = state.get("duration", 0.0)
                
                if regular_queue:
                    current_track = audio_engine.queue[audio_engine.current_index] if audio_engine.queue else None
                    found_idx = -1
                    if current_track:
                        for i, t in enumerate(regular_queue):
                            if t.get("path") == current_track.get("path"):
                                found_idx = i
                                break
                    
                    audio_engine._is_shuffle = False
                    if found_idx != -1:
                        # Seamless transition: continue playing current track at its regular queue position
                        audio_engine.set_queue(regular_queue, start_index=found_idx)
                    else:
                        # Insert current track at regular's index to prevent audio interruption
                        regular_index = max(0, min(regular_index, len(regular_queue)))
                        if current_track:
                            regular_queue.insert(regular_index, current_track)
                        audio_engine.set_queue(regular_queue, start_index=regular_index)
                else:
                    audio_engine.is_shuffle = False
            else:
                audio_engine.is_shuffle = False

        if hasattr(self, "queue_sheet") and self.queue_sheet and self.queue_sheet._initialized:
            self.queue_sheet.refresh()
        self.page.update()

    def cycle_repeat(self):
        modes = ["none", "one", "all"]
        mode  = modes[(modes.index(audio_engine.repeat_mode) + 1) % 3]
        audio_engine.repeat_mode = mode
        self.now_playing.update_repeat(mode)
        self._save_pref("repeat_mode", mode)
        if hasattr(self, "queue_sheet") and self.queue_sheet and self.queue_sheet._initialized:
            self.queue_sheet.refresh()
        self.page.update()

    # ── download queue UI relay ───────────────────────────────────────────────
    def refresh_queue_ui(self):
        self.search_view.refresh_queue_ui(self.queue.download_queue)

    # ── metadata editor ──────────────────────────────────────────────────────
    def open_metadata_editor(self, edit_type: str, meta: dict):
        self.metadata_editor.open(edit_type, meta)

    async def apply_metadata_edit(self, edit_type: str, meta: dict,
                              new_title: str, new_artist: str, new_album: str):
        self.show_snackbar("Saving metadata…")

        tag_data = {"artist": new_artist, "album": new_album}
        path     = meta.get("path", "")
        if edit_type == "track":
            tag_data["title"] = new_title
            paths = [path] if path else []
        else:
            # bulk-edit all tracks for this album
            items = await self.db_manager.get_tracks_by_album(
                meta.get("album_title", ""), meta.get("artist_name", ""))
            paths = [t.get("path") for t in items if t.get("path")]

        from utils.metadata_editor import update_physical_metadata
        count = 0
        for p in paths:
            # Update physical tags in a thread to avoid blocking the event loop
            success = await asyncio.to_thread(update_physical_metadata, p, tag_data)
            if success is True:
                await self.db_manager.update_track_metadata(p, tag_data)
                count += 1
            elif success == "PERMISSION_DENIED":
                self.show_snackbar("Permission denied: Android requires 'Manage All Files' access to modify tags.", color="#FF4444")
                return

        if count > 0:
            self.show_snackbar(f"Successfully updated {count} tracks.")
        else:
            self.show_snackbar("Failed to update metadata. Check file permissions.")
        
        await self.library_view.load_library()

    def show_play_similar_dialog(self):
        def close_dialog(e):
            dlg.open = False
            self.page.update()

        dlg = ft.AlertDialog(
            modal=False,
            bgcolor="transparent",
            content_padding=0,
            content=ft.Container(
                content=ft.Column(
                    [
                        # Pulsing/Glowing Icon container
                        ft.Container(
                            content=ft.Icon(
                                ft.Icons.ALL_INCLUSIVE_ROUNDED,
                                color=CYAN,
                                size=44,
                            ),
                            alignment=ft.Alignment(0, 0),
                            padding=18,
                            border_radius=26,
                            bgcolor=apply_opacity(0.1, CYAN),
                            margin=ft.margin.only(bottom=16),
                        ),
                        # Title
                        ft.Text(
                            "Similarity Walk Active",
                            color=TEXT,
                            size=18,
                            weight=ft.FontWeight.W_800,
                            text_align=ft.TextAlign.CENTER,
                        ),
                        ft.Container(height=10),
                        # Description with text wrapping enabled
                        ft.Text(
                            "Jarvis has initiated an acoustic similarity walk. "
                            "We will dynamically analyze acoustic features and append recommended, "
                            "acoustically matching tracks to keep your playback going indefinitely, sir.",
                            color=DIM,
                            size=13,
                            text_align=ft.TextAlign.CENTER,
                            max_lines=6,
                            expand=True,
                        ),
                        ft.Divider(color=BORDER, height=24),
                        # Action button
                        ft.Container(
                            content=ft.Button(
                                content=ft.Text("EXCELLENT, JARVIS", weight=ft.FontWeight.BOLD, color=BG),
                                style=ft.ButtonStyle(
                                    bgcolor=CYAN,
                                    color=BG,
                                    padding=ft.Padding.symmetric(vertical=12, horizontal=24),
                                    shape=ft.RoundedRectangleBorder(radius=18),
                                ),
                                on_click=close_dialog,
                            ),
                            alignment=ft.Alignment(0, 0),
                        ),
                    ],
                    spacing=0,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    tight=True,
                ),
                bgcolor=SURFACE,
                border_radius=20,
                border=ft.Border.all(1, apply_opacity(0.25, CYAN)),
                padding=24,
                width=320,
            ),
        )
        self.page.overlay.append(dlg)
        dlg.open = True
        self.page.update()

    def confirm_delete_track(self, path: str, title: str):
        def execute(_e):
            dlg.open = False
            self.page.update()
            self.page.run_task(self._delete_track, path)

        dlg = ft.AlertDialog(
            title=ft.Text("Delete Track?", color=TEXT),
            bgcolor=SURFACE,
            content=ft.Text(
                f"Permanently delete '{title}' from your device?",
                color=DIM, size=13,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _e: setattr(dlg, "open", False) or self.page.update()),
                ft.Button(
                    content=ft.Text("Delete"),
                    style=ft.ButtonStyle(bgcolor="#FF2222", color=TEXT),
                    on_click=execute,
                ),
            ],
        )
        self.page.overlay.append(dlg)
        dlg.open = True
        self.page.update()


    async def _delete_track(self, path: str):
        # Normalize and resolve path to ensure we're using the absolute physical location
        abs_path = os.path.abspath(os.path.expanduser(path))
        logger.warning(f"DEBUG: Attempting to delete file: {abs_path}")

        # Stop playback if the track is currently playing
        if audio_engine.current_path == path or audio_engine.current_path == abs_path:
            audio_engine.stop()

        # Remove from queue if present to prevent playback errors
        # We iterate backwards to safely remove while iterating
        for i in range(len(audio_engine.queue) - 1, -1, -1):
            t = audio_engine.queue[i]
            if t.get("path") == path or t.get("path") == abs_path:
                audio_engine.remove_from_queue(i)

        try:
            if os.path.exists(abs_path):
                await asyncio.to_thread(os.remove, abs_path)
            elif os.path.exists(path):
                await asyncio.to_thread(os.remove, path)
            else:
                logger.warning(f"DEBUG: File not found for deletion on disk: {abs_path}")
                # Still proceed to DB deletion if the file is gone from disk
        except PermissionError as pe:
            logger.error(f"Permission denied deleting {abs_path}: {pe}")
            self.show_snackbar("Permission denied: Android requires 'Manage All Files' access to delete files.", color="#FF4444")
            return
        except Exception as exc:
            logger.error(f"Deletion failed for {abs_path}: {exc}")
            self.show_snackbar(f"Could not delete: {exc}")
            return

        try:
            # Delete both the original and normalized path from DB to be safe
            await self.db_manager.delete_tracks_by_paths([path])
            if abs_path != path:
                await self.db_manager.delete_tracks_by_paths([abs_path])
            
            await self.library_view.load_library()
            self.show_snackbar("Track deleted permanently.")
        except Exception as exc:
            logger.error("DB deletion failed: %s", exc)
            self.show_snackbar(f"File deleted but database update failed: {exc}")

        self.page.update()

    

    # ── cache helpers ────────────────────────────────────────────────────────
    def clear_preview_cache(self):
        preview_dir = os.path.join(get_app_dir(), "previews")
        try:
            if os.path.exists(preview_dir):
                shutil.rmtree(preview_dir)
            self.show_snackbar("Preview cache cleared.")
        except Exception as exc:
            self.show_snackbar(f"Could not clear cache: {exc}")

    def clear_album_artwork_cache(self):
        temp_dir = get_temp_artwork_dir()
        try:
            if os.path.exists(temp_dir):
                for name in os.listdir(temp_dir):
                    if name == ".nomedia":
                        continue
                    p = os.path.join(temp_dir, name)
                    try:
                        if os.path.isfile(p):
                            os.remove(p)
                        elif os.path.isdir(p):
                            shutil.rmtree(p)
                    except:
                        pass
            _ARTWORK_CACHE.clear()
            self.show_snackbar("Album artwork cache cleared.")
        except Exception as exc:
            self.show_snackbar(f"Failed to clear album cache: {exc}")

    async def clear_library_index(self):
        try:
            conn = await self.db_manager.get_connection()
            async with self.db_manager._write_lock:
                await conn.execute("PRAGMA foreign_keys = OFF")
                await conn.execute("BEGIN")
                try:
                    await conn.execute("DELETE FROM playlist_tracks")
                    await conn.execute("DELETE FROM playlists")
                    await conn.execute("DELETE FROM track_neighbors")
                    await conn.execute("DELETE FROM tracks")
                    await conn.execute("DELETE FROM albums")
                    await conn.execute("DELETE FROM artists")
                    try:
                        await conn.execute("DELETE FROM fts_search")
                    except: pass
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
                finally:
                    await conn.execute("PRAGMA foreign_keys = ON")
            try:
                await conn.execute("VACUUM")
            except: pass
            
            self.db_manager.clear_caches()
            if hasattr(self, "library_view") and self.library_view:
                self.library_view._tracks_cache = None
                self.library_view._tracks_cache_key = None
                self.library_view._cached_moods = None
                self.library_view._cached_islets = None
                self.library_view._cached_unanalysed = None
                self.library_view._mood_feedback_map.clear()
                self.library_view._mood_recalc_pending = False
            
            # Clear all queue state files
            audio_engine.clear_queue()
            for filename in ("queue_state.json", "queue_regular.json", "queue_shuffle.json", "queue_similar.json"):
                q_path = os.path.join(os.environ["XDG_CACHE_HOME"] if filename != "queue_state.json" else DATA_DIR, filename)
                try:
                    if os.path.exists(q_path):
                        os.remove(q_path)
                except Exception:
                    pass

            await self.library_view.load_library()
            self.show_snackbar("Library index cleared successfully.")
        except Exception as exc:
            self.show_snackbar(f"Failed to clear library index: {exc}")

    async def clear_dsp_features(self):
        try:
            conn = await self.db_manager.get_connection()
            async with self.db_manager._write_lock:
                await conn.execute("BEGIN")
                try:
                    await conn.execute("UPDATE play_counts SET timbre = NULL, features_version = 0, pca_coords = NULL")
                    await conn.execute("DELETE FROM track_neighbors WHERE edge_kind = 'acoustic'")
                    await conn.execute("DELETE FROM pca_space")
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
            
            # Flush in-memory percentile & mood caches
            from utils import track_graph as tg
            tg.invalidate_mood_cache()
            
            if hasattr(self, "library_view") and self.library_view:
                self.library_view._tracks_cache = None
                self.library_view._tracks_cache_key = None
                self.library_view._cached_moods = None
                self.library_view._cached_islets = None
                self.library_view._cached_unanalysed = None
                await self.library_view.load_library()
                
            self.show_snackbar("Acoustic DSP features cleared successfully.")
        except Exception as exc:
            self.show_snackbar(f"Failed to clear DSP features: {exc}")

    async def clear_taste_model_weights(self):
        try:
            await self.db_manager.clear_taste_model()
            from utils import track_graph as tg
            tg.invalidate_taste_cache()
            self.show_snackbar("User taste model weights reset successfully.")
        except Exception as exc:
            self.show_snackbar(f"Failed to reset taste model: {exc}")

    def open_maintenance_confirmation(self, title: str, description: str, button_text: str, action_coro):
        """Generic confirmation dialog for maintenance tasks."""
        def on_confirm(e):
            dialog.open = False
            self.page.update()
            self.page.run_task(action_coro)
            
        def on_cancel(e):
            dialog.open = False
            self.page.update()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(title),
            content=ft.Text(description, size=13),
            actions=[
                ft.TextButton("Cancel", on_click=on_cancel),
                ft.TextButton(
                    content=ft.Text(button_text, weight=ft.FontWeight.BOLD, color="#FF4444"), 
                    on_click=on_confirm
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.overlay.append(dialog)
        self.page.dialog = dialog
        dialog.open = True
        self.page.update()

    # ── config / prefs ───────────────────────────────────────────────────────
    def sync_config_to_ui(self):
        try:
            cfg = load_config()
            self.target_folder = cfg.get("downloads", {}).get("folder", "") or ""
        except Exception:
            self.target_folder = ""
        self.library_folder = self._prefs.get("library_path", "") or ""

    def _load_prefs(self):
        try:
            with open(self._prefs_path) as fh:
                self._prefs = json.load(fh)
        except Exception:
            self._prefs = {}

    def _save_pref(self, key: str, value):
        self._prefs[key] = value
        def _save_task():
            try:
                with open(self._prefs_path, "w") as fh:
                    safe_json_dump(self._prefs, fh)
            except Exception:
                pass
        asyncio.create_task(asyncio.to_thread(_save_task))

    # ── snackbar ─────────────────────────────────────────────────────────────
    def show_snackbar(self, text: str, icon=ft.Icons.NOTIFICATIONS_ROUNDED, color=CYAN):
        self.notifications.show(text, icon=icon, color=color)

    # ── shutdown ──────────────────────────────────────────────────────────────
    def on_disconnect(self):
        try:
            # Cancel the periodic position writer so it doesn't race with
            # this final synchronous flush.
            task = getattr(self, "_position_save_task", None)
            if task is not None and not task.done():
                task.cancel()
            self._save_queue_state()
            # Flush DB to disk on disconnect
            if hasattr(self, "db_manager"):
                self.db_manager.checkpoint()
        except Exception as exc:
            logger.error("Failed to save queue state or checkpoint: %s", exc)
        # cleanup preview cache
        try:
            preview_dir = os.path.join(get_app_dir(), "previews")
            shutil.rmtree(preview_dir, ignore_errors=True)
        except Exception:
            pass


# ─── Entry point ───────────────────────────────────────────────────────────────
async def main(page: ft.Page):
    page.title = "Mai An Lab"
    
    # Force phone aspect ratio on macOS / desktop
    if platform.system() == "Darwin":
        page.window.width = 390
        page.window.height = 844
        page.window.min_width = 320
        page.window.min_height = 640
        
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG
    page.scrollbar_theme = ft.ScrollbarTheme(thickness=1.5, radius=1)
    
    # Configure audio to allow mixing with other apps' sounds (Fixes concurrency)
    if AudioContext:
        try:
            audio_context = AudioContext(
                android=AudioContextConfig(
                    focus=AudioContextConfigFocus.MIX_WITH_OTHERS
                )
            )
            await page.set_audio_context(audio_context)
        except Exception as e:
            logger.warning(f"Failed to set audio context: {e}")
    
    # Configure custom fonts
    font_path = "assets/Outfit-Regular.ttf"
    if os.path.exists(font_path):
        page.fonts = {"Outfit": font_path}
        page.theme = ft.Theme(
            font_family="Outfit",
            navigation_bar_theme=ft.NavigationBarTheme(
                label_text_style=ft.TextStyle(size=12),
            )
        )
    else:
        logger.warning("Font asset missing, using system default")
        page.theme = ft.Theme(
            navigation_bar_theme=ft.NavigationBarTheme(
                label_text_style=ft.TextStyle(size=12),
            )
        )
    
    try:
        app = StreamripFletApp(page)
        await app.initialize()
        page.on_disconnect = lambda _e: app.on_disconnect()
    except Exception as e:
        import traceback
        page.add(ft.Text(f"Startup crash: {e}", color="red", size=14, selectable=True))
        page.add(ft.Text(traceback.format_exc(), color="white", size=10, selectable=True))
        page.update()


if __name__ == "__main__":
    ft.run(main, assets_dir="assets")
