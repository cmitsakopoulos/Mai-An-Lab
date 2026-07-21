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
import time

def debug_log(msg):
    try:
        log_dir = "/sdcard/Download"
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "mai_an_lab_debug.txt"), "a") as f:
            f.write(f"{time.time()} - {msg}\n")
    except Exception as e:
        pass

debug_log("main.py started")

# FIX: Avoid SELinux denial for 'max_map_count' on Android 11+
# This must be set before the Python interpreter fully initializes native allocators.
os.environ["PYTHONMALLOC"] = "malloc"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

import pathlib
debug_log("importing get_app_dir")
from utils.filepath_utils import get_app_dir, get_temp_artwork_dir

# CRITICAL: SET THESE BEFORE ANY OTHER IMPORTS
DATA_DIR = get_app_dir()
debug_log(f"DATA_DIR: {DATA_DIR}")
try:
    os.environ["HOME"] = DATA_DIR
    os.environ["XDG_CONFIG_HOME"] = DATA_DIR
    os.environ["XDG_CACHE_HOME"] = os.path.join(DATA_DIR, ".cache")
    debug_log(f"creating directory: {os.environ['XDG_CACHE_HOME']}")
    os.makedirs(os.environ["XDG_CACHE_HOME"], exist_ok=True)
    debug_log("directory created, patching pathlib.Path.home")
    
    # MONKEYPATCH pathlib.Path.home to prevent it from returning '/data' on Android
    def _hijacked_home(cls):
        return pathlib.Path(DATA_DIR)
    pathlib.Path.home = classmethod(_hijacked_home)
    debug_log("pathlib.Path.home patched")
except Exception as e:
    import traceback
    debug_log(f"CRITICAL EXCEPTION in startup block: {e}\n{traceback.format_exc()}")


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



debug_log("importing flet and flet_audio")
import flet as ft
# Hard import required for flet build to include the audioplayers flutter plugin in the APK
import flet_audio
try:
    from flet_audio import AudioContext, AudioContextConfig, AudioContextConfigFocus
except ImportError:
    AudioContext = AudioContextConfig = AudioContextConfigFocus = None

debug_log("importing streamrip_api")
from utils.streamrip_api import (
    load_config, update_config_params, download,
    get_config_path, repair_config, get_default_download_path,
    get_walk_params,
)

debug_log("importing audio_engine")
if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

debug_log("audio_engine imported successfully")
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
debug_log("importing ui.widgets")
from ui.widgets import (
    _ARTWORK_CACHE, fmt_time, src_color, strip_markup, NotificationSystem,
    AnimatedEntry, ScaleButton, OnyxButton, GlassCard, MenuTextItem, AppSearchBar,
    SourceSegment, SettingsHeader, HubSettingItem, AccordionCard, SkeletonRow
)
debug_log("importing ui views and player components")
from ui.views.search import SearchView
from ui.views.library import LibraryView
from ui.views.settings import SettingsView
from ui.views.assistant import AssistantView
from ui.player.mini_player import MiniPlayerBar
from ui.player.now_playing import NowPlayingSheet
from ui.player.queue_sheet import QueueSheet
from ui.player.quality_selector import QualitySelectorSheet
from ui.player.dialogs import PlaylistEditorDialog, MetadataEditorDialog
debug_log("importing queue_controller and error_boundary")
from utils.queue_controller import QueueController
from utils.error_boundary import ErrorBoundary
debug_log("all main.py imports completed successfully")

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
        self._play_similar_recommendation_in_progress: bool = False

        # ── Session-scoped negative centroid ─────────────────────────────
        # DORMANT BY DESIGN — collected, not consumed. Keep it that way unless
        # you are deliberately reviving the taste model.
        #
        # `_record_play_event_safe` still fills these on every track transition,
        # but nothing reads them: the trip-wire and the Jarvis continuation that
        # used them were removed in the Jarvis debloat (8ddff64). That was a
        # deliberate simplification — a slim, debuggable walk beat a taste model
        # that was hard to reason about — and the capture is retained so the
        # signal is there if we choose to wire it back up.
        #
        # So this is NOT a bug to "fix" by hunting down the missing consumer.
        # The cost is three small in-memory deques per session.
        #
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
                # Prune caches asynchronously when returning to foreground
                self.page.run_task(self._prune_caches_async)

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
        while getattr(self, '_splash_logo', None) is not None and self._splash_logo.page:
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
            acc_color = appearance.get("accent_color", "#FFD600")
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
        db_path = os.path.join(DATA_DIR, "library.db")

        # Check for auto-import state ZIP on startup (e.g. from auto_offload.sh)
        try:
            import_zip = "/sdcard/Download/mai_an_lab_state_import.zip"
            if not os.path.exists(import_zip):
                import_zip = "/storage/emulated/0/Download/mai_an_lab_state_import.zip"

            if os.path.exists(import_zip) and os.path.getsize(import_zip) > 0:
                logger.info(f"Auto-import state zip found at {import_zip}. Ingesting...")
                from utils import state_export
                from utils import track_graph as tg
                from utils.streamrip_api import get_config_path
                from utils.search_history import get_search_history_path

                # Perform the import (replaces library.db on disk)
                await asyncio.to_thread(
                    state_export.import_state,
                    import_zip,
                    db_path,
                    get_config_path(),
                    get_search_history_path(),
                )

                # Delete the import ZIP
                os.remove(import_zip)
                logger.info("Auto-import state ZIP processed and deleted.")

                # Re-initialize DB manager and rebuild graph and PCA space immediately
                self.db_manager = DatabaseManager(db_path)
                await self.db_manager.initialize()

                logger.info("Auto-import: Rebuilding similarity graph (edges + Zr geometry + communities)...")
                await tg.build_metadata_edges(self.db_manager)
                await tg.build_acoustic_edges(self.db_manager)
                logger.info("Auto-import: Rebuild completed successfully.")
            else:
                self.db_manager = DatabaseManager(db_path)
                await self.db_manager.initialize()
        except Exception as auto_imp_err:
            logger.error(f"Auto-import startup hook failed: {auto_imp_err}", exc_info=True)
            self.db_manager = DatabaseManager(db_path)
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
        audio_engine.setup(self.page, self.db_manager)

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
            loudness_boost_db=self._on_loudness_boost_change,
            on_custom_action=self._on_media_custom_action,
        )
        def _on_queue_mutated(_inst, _val):
            self.safe_update(self.queue_sheet.refresh)
            # Persist immediately on mutation so a hard OS kill (Android low-
            # memory reaping or process death) leaves a recoverable snapshot
            # on disk instead of the stale state from the previous launch.
            self._schedule_queue_save()
            self._replenish_similar_queue_if_needed()

        audio_engine.bind(
            on_playback_error=lambda _, d: self._on_playback_error_toast(d),
            on_queue_mutated=_on_queue_mutated,
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
        # Prime availability checks so the UI reflects DSP readiness immediately,
        # before the user opens Now Playing or Settings for the first time.
        self.page.run_task(self.now_playing._check_play_similar_availability)

        # Long-running task that snapshots playback position to disk every
        # ~10 s so a hard kill leaves the resume offset close to where the
        # user actually was (queue/index get saved on mutation events
        # already, but position drifts continuously while playing).
        self._position_save_task = asyncio.create_task(self._position_save_loop())

        # Auto-export a state snapshot to the user's library folder on every
        # boot. The offload script can always find a fresh bundle at
        # <library>/mai_an_lab_state_latest.zip without the user manually
        # exporting first. Fire-and-forget: failures are logged but never
        # block startup.
        asyncio.create_task(self._auto_export_state_snapshot())

        # Incremental artist-metadata enrichment (MusicBrainz country + genres),
        # off the hot path. This is the ONLY automatic enrichment trigger now
        # that build_acoustic_edges no longer blocks on it: the task self-guards
        # (config opt-out, re-entrancy flag, 3 s settle delay) and refreshes the
        # NPMI genre model as provenance lands, so the walk's metadata gate stays
        # current without stalling the graph rebuild on a 1 req/s network cap.
        asyncio.create_task(self._enrich_metadata_async())

        # Similarity-graph readiness check, off the hot path. Without this the
        # ONLY automatic build was the state-ZIP auto-import branch above, so a
        # library that was analysed but never had its graph built — or whose
        # build failed silently — left the walk with no coordinates and
        # `tg.walk()` returning [] for every seed. Auto-play then queued nothing
        # and quietly fell through to the library tail, which reads as "bad
        # recommendations" when it is actually no recommendations at all.
        asyncio.create_task(self._ensure_graph_built_async())

        # Prune caches asynchronously to keep disk footprint bounded
        self.page.run_task(self._prune_caches_async)

    async def _ensure_graph_built_async(self):
        """Build the similarity graph if it is MISSING — never on a schedule.

        Guarded on the real artifact (`coord_tracks`, i.e. persisted Zr
        coordinates), not on an edge table nobody writes and not on a sidecar
        count file, so this fires exactly when the walk would otherwise be dead
        and stays quiet on every subsequent boot. Requires already-extracted DSP
        features — it never triggers analysis, so the cost is one SVD over
        existing vectors, not a network or decode pass."""
        if getattr(self, "_ensuring_graph", False):
            return
        self._ensuring_graph = True
        try:
            # Let startup settle; this is deliberately behind the UI.
            await asyncio.sleep(5)
            from utils import track_graph as tg
            status = await tg.graph_status(self.db_manager)
            if status["total_tracks"] < 2 or status["coord_tracks"] > 0:
                return  # nothing to do, or geometry already present
            analysed = len(await self.db_manager.get_tracks_with_features(tg.FEATURES_VERSION))
            if analysed < 2:
                logger.info(
                    "Graph readiness: %d tracks but only %d analysed — deferring "
                    "to the analyser sweep.", status["total_tracks"], analysed,
                )
                return
            logger.info(
                "Graph readiness: %d analysed tracks but NO persisted coordinates "
                "— building the similarity graph so auto-play can walk it.",
                analysed,
            )
            # Prefer a scroll-quiet window. The builders now yield cooperatively,
            # but holding the heaviest first-load work until the user pauses
            # keeps the very first post-import scroll fully smooth. Bounded, so a
            # user who never stops scrolling still gets the graph eventually.
            await self._await_scroll_quiet()
            await tg.build_metadata_edges(self.db_manager)
            await tg.build_acoustic_edges(self.db_manager)
            logger.info("Graph readiness: similarity graph built.")
        except Exception as exc:
            logger.error("Graph readiness build failed: %s", exc, exc_info=True)
        finally:
            self._ensuring_graph = False

    def note_scroll_activity(self):
        """Record that the user is actively scrolling. Read by
        _await_scroll_quiet so a one-time first-load graph build can defer its
        heaviest work out of an active fling. Cheap enough to call every tick."""
        self._last_scroll_ts = time.monotonic()

    async def _await_scroll_quiet(self, quiet: float = 1.0, max_wait: float = 20.0):
        """Block until the user has not scrolled for `quiet` seconds, or until
        `max_wait` elapses — whichever comes first. Lets the graph build wait for
        a lull instead of contending with a live scroll, without ever deferring
        indefinitely."""
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            idle = time.monotonic() - getattr(self, "_last_scroll_ts", 0.0)
            if idle >= quiet:
                return
            await asyncio.sleep(min(quiet - idle, 0.5))

    async def _enrich_metadata_async(self):
        """Background, incremental artist-metadata enrichment (MusicBrainz
        country + genres). Only fetches artists with no cached enrichment, so
        each library top-up enriches just the new artists, then refreshes the
        NPMI genre model. Rate-limited, offline-safe, never blocks the UI."""
        if getattr(self, "_enriching_metadata", False):
            return
        try:
            from utils.streamrip_api import load_config
            if not load_config().get("general", {}).get("auto_enrich_metadata", True):
                return
        except Exception:
            pass
        self._enriching_metadata = True
        try:
            await asyncio.sleep(3)  # let startup / the scan settle before network
            from utils.metadata_enrich import enrich_library
            summary = await enrich_library(self.db_manager, with_genres=True)
            if summary.get("enriched"):
                logger.info("Metadata enrichment complete: %s", summary)
        except Exception as exc:
            logger.warning("Metadata enrichment failed: %s", exc)
        finally:
            self._enriching_metadata = False

    async def _auto_export_state_snapshot(self):
        """Background task: write a deterministic state bundle to the standard
        Downloads folder (and library folder if configured) so the desktop offload
        pipeline always has a fresh starting point. Fire-and-forget."""
        try:
            from utils import state_export
            from utils import track_graph as tg
            from utils.streamrip_api import get_config_path
            from utils.search_history import get_search_history_path
            from utils.state_export import _default_bundle_dir

            # Write to default Downloads folder first (always accessible and standard)
            target_dir = _default_bundle_dir()
            if not os.path.isdir(target_dir):
                target_dir = DATA_DIR

            out = await asyncio.to_thread(
                state_export.export_state_snapshot,
                self.db_manager.db_path,
                get_config_path(),
                target_dir,
                get_search_history_path(),
            )
            logger.info("Auto-export state snapshot: %s", out)

            # Optionally also copy to user's library folder if configured
            lib_dir = getattr(self, "library_folder", "") or ""
            if lib_dir and os.path.isdir(lib_dir) and os.path.abspath(lib_dir) != os.path.abspath(target_dir):
                lib_out = os.path.join(lib_dir, "mai_an_lab_state_latest.zip")
                try:
                    import shutil
                    await asyncio.to_thread(shutil.copy2, out, lib_out)
                    logger.info("Auto-export state snapshot copied to library: %s", lib_out)
                except Exception as cp_err:
                    logger.warning("Failed to copy state snapshot to library: %s", cp_err)
        except Exception as exc:
            logger.warning("Auto-export state snapshot failed (non-fatal): %s", exc)


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
                        await conn.execute("DELETE FROM artist_enrichment")
                    except: pass
                    try:
                        await conn.execute("DELETE FROM genre_affinity")
                    except: pass
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
                self.library_view._cached_unanalysed = None

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
            if hasattr(self, "library_view") and self.library_view:
                await self.library_view.load_library()
            if hasattr(self, "settings_view") and getattr(self.settings_view, "_metadata_workbench_pane", None):
                self.settings_view._metadata_workbench_pane._reload()
            self.show_snackbar("Library database wiped successfully.")
        except Exception as exc:
            self.show_snackbar(f"Wipe failed: {exc}")

    def trigger_haptic(self, action: str):
        """Fire-and-forget sync wrapper — schedules the async haptic call on the page event loop.
        All HapticFeedback methods are async coroutines in Flet; calling them without await
        silently creates coroutines that are never executed. This wrapper ensures they actually run.
        """
        if sys.platform == "darwin":
            return
        self.page.run_task(self._trigger_haptic_async, action)

    def play_success_notification(self):
        """Trigger vibration and play the success sound notification."""
        self.trigger_haptic("vibrate")
        if sys.platform == "darwin":
            try:
                import subprocess
                # Use macOS built-in system sound for native offline playback
                sound_path = "/System/Library/Sounds/Glass.aiff"
                if os.path.exists(sound_path):
                    subprocess.Popen(["afplay", sound_path])
                else:
                    logger.warning(f"System sound not found at: {sound_path}")
            except Exception as e:
                logger.warning("Failed to play success sound on macOS: %s", e)
        else:
            # Android native notification sound via Pyjnius
            try:
                from jnius import autoclass
                ActivityThread = autoclass("android.app.ActivityThread")
                context = ActivityThread.currentApplication().getApplicationContext()
                RingtoneManager = autoclass("android.media.RingtoneManager")
                Uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
                ringtone = RingtoneManager.getRingtone(context, Uri)
                ringtone.play()
            except Exception as e:
                logger.warning("Failed to play Android native notification sound: %s", e)

    async def _trigger_haptic_async(self, action: str):
        """Async implementation of haptic feedback triggering."""
        try:
            from utils.streamrip_api import load_config
            cfg = load_config()
            haptics_cfg = cfg.get("haptics", {})
            enabled = bool(haptics_cfg.get("haptic_feedback_enabled", True))
            if not enabled:
                return

            # If a direct intensity name was passed (light, medium, heavy, selection, vibrate), use it as fallback.
            defaults = {
                "eq_drag": "light",
                "swipe_queue": "medium",
                "swipe_dismiss": "medium",
                "long_press": "heavy",
                "network_tap": "selection",
                "network_reseed": "medium",
                "network_walk": "light",
            }
            intensity = haptics_cfg.get(f"{action}_intensity", action if action in ["light", "medium", "heavy", "selection", "vibrate", "none"] else defaults.get(action, "light"))

            if intensity == "none" or not intensity:
                return
        except Exception:
            intensity = "light"

        if not hasattr(self, "haptic") or self.haptic is None:
            logger.debug("Haptic: service not initialized, skipping.")
            return

        try:
            logger.debug("Haptic: triggering '%s' (resolved intensity=%s)", action, intensity)
            if intensity == "light":
                await self.haptic.light_impact()
            elif intensity == "medium":
                await self.haptic.medium_impact()
            elif intensity == "heavy":
                await self.haptic.heavy_impact()
            elif intensity == "selection":
                await self.haptic.selection_click()
            elif intensity == "vibrate":
                await self.haptic.vibrate()
            logger.debug("Haptic: '%s' dispatched successfully.", intensity)
        except Exception as e:
            logger.debug("Haptic trigger failed: %s", e)

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
        # Load show_jarvis from config
        try:
            cfg = load_config()
            self._show_jarvis = bool(cfg.get("appearance", {}).get("show_jarvis", True))
        except:
            self._show_jarvis = True

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
            if not self._show_jarvis and self._current_tab == 0:
                self._current_tab = 2 # Fallback to Library if Jarvis is disabled

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

        destinations = []
        if self._show_jarvis:
            destinations.append(
                ft.NavigationBarDestination(
                    icon=ft.Icons.AUTO_AWESOME_ROUNDED,
                    selected_icon=ft.Icons.AUTO_AWESOME_ROUNDED,
                    label="Jarvis",
                )
            )
        destinations.extend([
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
        ])

        # Navigation bar
        self._nav = ft.NavigationBar(
            selected_index=self._get_nav_index(self._current_tab),
            bgcolor=SURFACE,
            indicator_color=CYAN + "55",
            label_behavior=ft.NavigationBarLabelBehavior.ALWAYS_SHOW,
            destinations=destinations,
            on_change=self._on_nav_change,
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
        
        # Initialize haptic feedback overlay (Android only)
        if sys.platform != "darwin":
            try:
                self.haptic = ft.HapticFeedback()
                self.page.services.append(self.haptic)
            except Exception as e:
                self.haptic = None
                logger.warning("Failed to initialize haptic feedback: %s", e)
        else:
            self.haptic = None
            
        # Clean up splash logo reference so the background pulsing task exits immediately
        self._splash_logo = None
        
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
        if self._show_jarvis:
            if vx < 0 and self._current_tab < 1:
                new_tab += 1
            elif vx > 0 and self._current_tab > 0:
                new_tab -= 1
        else:
            if vx < 0 and self._current_tab == 1:
                new_tab = 2
            elif vx > 0 and self._current_tab == 2:
                new_tab = 1
        
        if new_tab != self._current_tab:
            self._switch_tab(new_tab)
            # Let safe_update handle the page.update()
            self.safe_update(lambda: setattr(self._nav, 'selected_index', self._get_nav_index(new_tab)))

    def _get_nav_index(self, tab_index: int) -> int:
        if self._show_jarvis:
            return tab_index if tab_index < 3 else 0
        else:
            if tab_index == 1:
                return 0
            elif tab_index == 2:
                return 1
            else:
                return 0

    def _get_absolute_tab_index(self, nav_index: int) -> int:
        if self._show_jarvis:
            return nav_index
        else:
            if nav_index == 0:
                return 1
            elif nav_index == 1:
                return 2
            else:
                return 2

    def _on_nav_change(self, e):
        abs_index = self._get_absolute_tab_index(e.control.selected_index)
        self._switch_tab(abs_index)

    def _switch_tab(self, index: int):
        self._current_tab = index

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
            nav_idx = self._get_nav_index(index)
            is_nav_tab = index < 3
            if is_nav_tab:
                self._nav.selected_index = nav_idx
            self._nav.indicator_color = (CYAN + "55" if is_nav_tab else "transparent")

        self.safe_update(_mutate)

    # ── audio engine callbacks ───────────────────────────────────────────────
    def _on_loudness_boost_change(self, _instance, value: float):
        self.now_playing.update_loudness_boost(value)
        if hasattr(self, "settings_view") and self.settings_view:
            self.settings_view.update_loudness_boost(value)

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
        # exactly once for the outgoing track; forward unconditionally
        # whenever we had a previous track.
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
            self._replenish_similar_queue_if_needed()

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

    def _on_media_custom_action(self, _instance, data: dict):
        """Called when the user clicks a custom button in the media notification."""
        name = data.get("name")
        if name == "replenish_queue":
            self.page.run_task(self._force_replenish_similar_queue)

    async def _record_play_event_safe(self, path: str, played: float, duration: float):
        """Background-safe tracker updates that feed the session-scoped
        negative centroid / trip-wire so consecutive bad continuations
        get steered away from in the next walk."""
        # Session signal tracks engagement (similar to skip detection)
        # to identify what the centroid treats as bad.
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
            # Play Similar leans purely on graph topology + DSP similarity +
            # metadata. The 7-day recent-played window is deliberately kept
            # out of the avoid set so a large library's natural listening
            # history doesn't strip the seed's top-K neighbours mid-walk.
            # `recent_played_paths` stays in db_manager for future features.

            # Seed-anchored smooth walk: the 0.3·seed term + metadata/cluster
            # penalties keep the queue in the seed's genre/community, replacing
            # the old restart-probability band-aid for two-hop genre drift.
            temp, mmr = get_walk_params()
            walk_paths = await tg.walk(
                self.db_manager,
                path,
                length=8,
                avoid=avoid,
                mmr_lambda=mmr,   # suppress remix / alt-mix chaining
                temperature=temp,   # vary the queue across repeat requests
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
                    # NON-DESTRUCTIVE: drop the similar block in right AFTER the
                    # current track via the native insert — the current source is
                    # NOT reloaded, so playback is never cut. The queue tail (e.g.
                    # the rest of the library) is preserved below the block. Dedup
                    # only against the current track + this block; NOT the tail, or
                    # nothing from the library could ever be recommended.
                    seen = {audio_engine.current_path}
                    block = []
                    for et in engine_tracks:
                        p = et.get("path")
                        if p and p not in seen:
                            et["_autoplay"] = True
                            block.append(et)
                            seen.add(p)
                    if block:
                        audio_engine.queue_after_current(block)
                        if hasattr(self, "queue_sheet") and self.queue_sheet and self.queue_sheet._initialized:
                            self.queue_sheet.refresh()
                        logger.info("Auto-play: inserted %d similar tracks after the current song.", len(block))
        except Exception as exc:
            logger.exception("Play Similar: Failed to initiate similar queue: %s", exc)

    async def _recommend_similar_async(self, path: str, count: int = 1, gen: int = 0):
        """Lightweight block recommendation for Play Similar.

        Runs a minimal acoustic walk with no taste model and no embedding fetch.
        Appends up to `count` new unique similar tracks.
        """
        import os
        from utils import track_graph as tg
        try:
            # Race guard: bail if the mode was toggled while we were awaiting
            if gen != self._play_similar_gen or not self.play_similar_mode:
                return

            # Avoid the current track + tracks already in the auto-play buffer +
            # session rejects — NOT the whole queue. The queue holds the entire
            # library tail; avoiding all of it would leave the walk with zero
            # candidates. Library tracks MUST stay eligible (they're what gets
            # promoted into the buffer).
            avoid = {t["path"] for t in audio_engine.queue if t.get("_autoplay") and t.get("path")}
            avoid.add(path)
            if audio_engine.current_path:
                avoid.add(audio_engine.current_path)
            avoid.update(self._session_bad_paths)
            # Play Similar leans purely on graph topology + DSP similarity +
            # metadata. The 7-day recent-played window is deliberately kept
            # out of the avoid set so a large library's natural listening
            # history doesn't strip the seed's top-K neighbours mid-walk.
            # `recent_played_paths` stays in db_manager for future features.

            walk_len = max(count + 4, count * 2)
            temp, mmr = get_walk_params()
            walk_tracks = await tg.walk(
                self.db_manager,
                path,
                length=walk_len,
                avoid=avoid,
                mmr_lambda=mmr,   # suppress remix / alt-mix chaining
                temperature=temp,   # vary the queue across repeat requests
            )

            # Re-check after the await
            if gen != self._play_similar_gen or not self.play_similar_mode:
                return

            if walk_tracks:
                # Dedup against the current track + the existing buffer only.
                queued = {t["path"] for t in audio_engine.queue if t.get("_autoplay") and t.get("path")}
                if audio_engine.current_path:
                    queued.add(audio_engine.current_path)
                batch: list[dict] = []
                for wt in walk_tracks:
                    if wt not in queued:
                        row = await self.db_manager.get_track_full(wt)
                        if row:
                            if gen != self._play_similar_gen or not self.play_similar_mode:
                                return
                            track_dict = {
                                "path":        row.get("path"),
                                "track_title": row.get("title") or row.get("track_title") or os.path.basename(wt),
                                "artist_name": row.get("artist") or row.get("artist_name") or "Unknown Artist",
                                "album_title": row.get("album")  or row.get("album_title")  or "Unknown Album",
                                "duration":    row.get("duration", 0.0) or 0.0,
                                "image_url":   row.get("image_url", "") or "",
                                "_autoplay":   True,
                            }
                            batch.append(track_dict)
                            queued.add(wt)
                            if len(batch) >= count:
                                break
                # Insert right AFTER the existing auto-play buffer (keeps ordering,
                # stays ahead of the library tail) via the non-destructive native
                # insert — no source reload, no playback cut.
                if batch and gen == self._play_similar_gen and self.play_similar_mode:
                    # Insert after the LAST buffered track anywhere ahead of
                    # current (robust to a manual "Play Next" splitting the run;
                    # that untagged track stays put and plays before the buffer).
                    ci = audio_engine.current_index
                    q = audio_engine.queue
                    after = ci
                    for idx in range(ci + 1, len(q)):
                        if q[idx].get("_autoplay"):
                            after = idx
                    audio_engine.queue_after_current(batch, after_index=after)
                    logger.info("Auto-play: queued %d more similar tracks (buffer refill).", len(batch))
        except Exception as exc:
            logger.exception("Play Similar: Failed to generate dynamic recommendations: %s", exc)
        finally:
            self._play_similar_recommendation_in_progress = False

    def _replenish_similar_queue_if_needed(self):
        """Keep an ~8-track auto-play buffer of similar songs queued right after
        the current track. The buffer is the RUN of _autoplay-tagged tracks after
        current; the library tail below it is ignored (and preserved), so a full
        library queue no longer masks an empty buffer."""
        if not self.play_similar_mode or getattr(self, "is_restoring_session", False):
            return
        if getattr(self, "_play_similar_recommendation_in_progress", False):
            return
        q = audio_engine.queue
        ci = audio_engine.current_index
        # Count EVERY _autoplay track ahead of current — do NOT stop at the first
        # non-buffer track: a manual "Play Next" inserts an untagged track at
        # current+1, which just plays before the similars and must not be read as
        # an empty buffer.
        buffer = 0
        last_buf_path = None
        for t in q[ci + 1:]:
            if t.get("_autoplay"):
                buffer += 1
                last_buf_path = t.get("path") or last_buf_path
        if buffer < 4:
            needed = 8 - buffer
            # Continue the walk from the end of the buffer (or the current track
            # when the buffer is empty).
            seed = last_buf_path or audio_engine.current_path
            if seed:
                self._play_similar_recommendation_in_progress = True
                self.page.run_task(self._recommend_similar_async, seed, needed, self._play_similar_gen)

    async def _force_replenish_similar_queue(self):
        """Force replenish / extend the queue using the graph walk, waking up in the background."""
        try:
            self.show_snackbar("Replenishing queue...", icon=ft.Icons.AUTO_AWESOME)
        except Exception:
            pass

        if self.play_similar_mode:
            path = None
            if audio_engine.queue:
                path = audio_engine.queue[-1].get("path")
            if not path:
                path = audio_engine.current_path
            if path:
                self._play_similar_recommendation_in_progress = True
                try:
                    await self._recommend_similar_async(path, 8, self._play_similar_gen)
                finally:
                    self._play_similar_recommendation_in_progress = False
        elif getattr(self, "auto_dj_mode", False):
            await self._auto_dj_auto_continue_queue()
        else:
            path = audio_engine.current_path
            if path:
                self.play_similar_mode = True
                audio_engine.play_similar_seed_path = path
                self.now_playing.update_play_similar(True)
                self.mini_player.update_play_similar(True)
                self._play_similar_recommendation_in_progress = True
                try:
                    await self._recommend_similar_async(path, 8, self._play_similar_gen)
                finally:
                    self._play_similar_recommendation_in_progress = False

    async def _run_continuation(self, coro):
        """Wrap a dry-queue continuation so the in-progress flag is always
        cleared, even on early return / exception."""
        try:
            await coro()
        finally:
            self._continuation_in_progress = False

    def _on_similar_continue(self, _inst, _val=None):
        """Sync callback dispatched by AudioEngine when the manually-initiated
        Play Similar or Auto-DJ queue runs dry. Bridges into the async
        continuation coroutine safely.

        Guarded against double-dispatch: at end-of-queue BOTH next() and the
        `completed` state event can fire on_similar_continue; a second concurrent
        continuation would double-append and double-skip the queue."""
        if not self.page:
            return
        if getattr(self, "_continuation_in_progress", False):
            return
        if self.play_similar_mode:
            self._continuation_in_progress = True
            self.page.run_task(self._run_continuation, self._similar_auto_continue_queue)
        elif getattr(self, "auto_dj_mode", False):
            self._continuation_in_progress = True
            self.page.run_task(self._run_continuation, self._auto_dj_auto_continue_queue)

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
        # Play Similar continuation also skips the recent-played avoid window
        # (see _initiate_play_similar_queue_async for rationale).

        try:
            temp, mmr = get_walk_params()
            walk_paths = await tg.walk(
                self.db_manager,
                seed_path,
                length=8,
                avoid=avoid,
                mmr_lambda=mmr,   # suppress remix / alt-mix chaining
                temperature=temp,   # vary the queue across repeat continuations
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
            appended_tracks.append(track_dict)

        if len(appended_tracks) == 0:
            logger.info("Play Similar continuation: metadata lookup failed for all neighbours.")
            audio_engine.stop()
            return

        # Single batched append (one queue-sheet rebuild) before resuming.
        audio_engine.queue_extend(appended_tracks)

        # Resume playback at the first newly appended slot
        audio_engine.play_track_at(first_new_index)



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

        Partition files (queue_regular/shuffle/similar.json) are refreshed
        only at mode-transition points — mirroring them on every save just
        duplicates the I/O without buying any extra recoverability.
        """
        path = os.path.join(DATA_DIR, "queue_state.json")
        if not audio_engine.queue:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
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
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("Could not save queue state: %s", exc)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

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

    def _schedule_partition_save(self, filename: str, queue_list: list[dict], current_index: int, position: float, duration: float):
        # Snapshot the queue list now so a later mutation on the event loop
        # can't corrupt the bytes we're about to serialise on the worker thread.
        snapshot = list(queue_list) if queue_list else []
        try:
            self.page.run_task(
                self._partition_save_async,
                filename, snapshot, current_index, position, duration,
            )
        except Exception:
            # No loop available (shutdown); fall back to a blocking write so
            # we don't lose the partition snapshot.
            self._save_queue_to_file(filename, snapshot, current_index, position, duration)

    async def _partition_save_async(self, filename: str, queue_list: list[dict], current_index: int, position: float, duration: float):
        await asyncio.to_thread(
            self._save_queue_to_file, filename, queue_list, current_index, position, duration
        )

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
            # NON-DESTRUCTIVE: play the tapped song within the FULL library queue
            # (starting a chosen song is expected), then insert similar tracks
            # right after it. No queue wipe, no saved-queue bookkeeping.
            audio_engine.set_queue(tracks, start_index=target_idx)
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
                self._schedule_partition_save("queue_shuffle.json", tracks, target_idx, 0.0, 0.0)
            else:
                self._schedule_partition_save("queue_regular.json", tracks, target_idx, 0.0, 0.0)
            
            # 3. Set active queue to just the clicked track and start play
            audio_engine.set_queue([target_track], start_index=0)
            
            # 4. Trigger new Auto-DJ curation starting with this track
            self.page.run_task(self._initiate_auto_dj_queue_async)
        else:
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
        self._play_similar_recommendation_in_progress = False
        gen = self._play_similar_gen

        self.play_similar_mode = enabled
        self._save_pref("play_similar_mode", enabled)
        
        self.now_playing.update_play_similar(enabled)
        self.mini_player.update_play_similar(enabled)
        
        verb = "Enabled" if enabled else "Disabled"
        self.show_snackbar(
            f"Auto-play: {verb}",
            icon=ft.Icons.ALL_INCLUSIVE_ROUNDED if enabled else ft.Icons.LINK_OFF_ROUNDED,
        )

        if enabled:
            # NON-DESTRUCTIVE model: no save / replace / restore of the queue.
            # Similar tracks are inserted right after the current song (see
            # _initiate_play_similar_queue_async) and the library tail is left in
            # place, so turning Auto-play off later needs no restore — the library
            # simply resumes below the buffer. Shuffle must be off, though:
            # similars play in walk order right after the current track, which
            # Dart's shuffle order would otherwise scatter.
            if audio_engine.is_shuffle:
                audio_engine.is_shuffle = False
                self.now_playing.update_shuffle(False)
                self._save_pref("is_shuffle", False)
            path = audio_engine.current_path
            audio_engine.play_similar_seed_path = path or ""
            if path:
                self.page.run_task(self._initiate_play_similar_queue_async, path, gen)
        else:
            # Nothing to restore — the library was never removed; it resumes
            # below the current similar buffer once that plays out. Just stop
            # replenishing (the gen bump above already cancels in-flight fills).
            audio_engine.play_similar_seed_path = ""

        if hasattr(self, "queue_sheet") and self.queue_sheet and self.queue_sheet._initialized:
            self.safe_update(self.queue_sheet.refresh)

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
                self._schedule_partition_save("queue_shuffle.json", self.play_similar_saved_queue, self.play_similar_saved_index, audio_engine.position, audio_engine.duration)
            else:
                self._schedule_partition_save("queue_regular.json", self.play_similar_saved_queue, self.play_similar_saved_index, audio_engine.position, audio_engine.duration)

            # 3. Initiate Auto-DJ curation
            self.page.run_task(self._initiate_auto_dj_queue_async)
        else:
            # Save current Auto-DJ queue
            self._schedule_partition_save("queue_similar.json", audio_engine.queue, audio_engine.current_index, audio_engine.position, audio_engine.duration)

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
            self.safe_update(self.queue_sheet.refresh)

    async def _initiate_auto_dj_queue_async(self):
        """Build the initial Auto-DJ queue of curated tracks and start playing."""
        import os
        from utils import track_graph as tg
        try:
            if not self.auto_dj_mode:
                return

            rows = await self.db_manager.get_tracks_with_features(tg.FEATURES_VERSION)

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

            if rows:
                # Pick random tracks (unbiased, taste model removed)
                import random
                pool = [r for r in rows if r["path"] not in avoid]
                selected = random.sample(pool, min(10, len(pool))) if pool else []

                engine_tracks = []
                # Place current track first if any
                if cur_track_dict:
                    engine_tracks.append(cur_track_dict)
                
                # Fill remaining spots with recommendations
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
        """Automatically extend the Auto-DJ queue with 5 random tracks."""
        import os
        from utils import track_graph as tg
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

            rows = await self.db_manager.get_tracks_with_features(tg.FEATURES_VERSION)
            
            if rows:
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
            self._schedule_partition_save("queue_regular.json", audio_engine.queue, audio_engine.current_index, audio_engine.position, audio_engine.duration)
            
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
                self._schedule_partition_save("queue_shuffle.json", audio_engine.queue, audio_engine.current_index, audio_engine.position, audio_engine.duration)
            else:
                audio_engine._is_shuffle = True
                audio_engine._on_shuffle_changed()

        # ── Toggle OFF: shuffle queue -> restore regular queue ───────────────
        else:
            # 1. Save currently playing shuffle queue
            self._schedule_partition_save("queue_shuffle.json", audio_engine.queue, audio_engine.current_index, audio_engine.position, audio_engine.duration)
            
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
            self.safe_update(self.queue_sheet.refresh)
        self.page.update()

    def cycle_repeat(self):
        modes = ["none", "one", "all"]
        mode  = modes[(modes.index(audio_engine.repeat_mode) + 1) % 3]
        audio_engine.repeat_mode = mode
        self.now_playing.update_repeat(mode)
        self._save_pref("repeat_mode", mode)
        if hasattr(self, "queue_sheet") and self.queue_sheet and self.queue_sheet._initialized:
            self.safe_update(self.queue_sheet.refresh)
        self.page.update()

    # ── download queue UI relay ───────────────────────────────────────────────
    def refresh_queue_ui(self):
        self.search_view.refresh_queue_ui(self.queue.download_queue)

    # ── metadata editor ──────────────────────────────────────────────────────
    def open_metadata_editor(self, edit_type: str, meta: dict):
        self.metadata_editor.open(edit_type, meta)

    def open_artist_metadata_editor(self, artist_name: str, on_saved=None):
        from ui.player.dialogs import ArtistMetadataDialog
        dlg = ArtistMetadataDialog(self)
        dlg.open(artist_name, on_saved=on_saved)

    def open_metadata_enrichment_wizard(self):
        self.switch_tab(3)
        self.settings_view._on_launch_enrichment_wizard_click()

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
    async def _prune_caches_async(self):
        """Asynchronously prunes the artwork and search preview caches to prevent disk ballooning."""
        now = time.time()
        last_run = getattr(self, "_last_cache_prune_time", 0.0)
        if now - last_run < 21600:  # 6 hours
            return
        self._last_cache_prune_time = now

        def _do_prune():
            try:
                # 1. Prune Artwork Cache
                temp_dir = get_temp_artwork_dir()
                if os.path.exists(temp_dir):
                    files = []
                    for name in os.listdir(temp_dir):
                        if name == ".nomedia":
                            continue
                        p = os.path.join(temp_dir, name)
                        if os.path.isfile(p):
                            try:
                                files.append((p, os.path.getmtime(p)))
                            except Exception:
                                pass
                    # Keep the 100 most recently modified artwork files, delete the rest
                    if len(files) > 100:
                        files.sort(key=lambda x: x[1])  # oldest first
                        for p, _ in files[:-100]:
                            try:
                                os.remove(p)
                            except Exception:
                                pass

                # 2. Prune Preview Cache
                preview_dir = os.path.join(get_app_dir(), "previews")
                if os.path.exists(preview_dir):
                    cutoff = now - 86400  # 24 hours
                    for name in os.listdir(preview_dir):
                        p = os.path.join(preview_dir, name)
                        try:
                            if os.path.isdir(p):
                                mtime = os.path.getmtime(p)
                                if mtime < cutoff:
                                    shutil.rmtree(p)
                            elif os.path.isfile(p):
                                mtime = os.path.getmtime(p)
                                if mtime < cutoff:
                                    os.remove(p)
                        except Exception:
                            pass
            except Exception as exc:
                logger.warning("Cache pruning failed: %s", exc)

        await asyncio.to_thread(_do_prune)

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
                self.library_view._cached_unanalysed = None

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
            
            if hasattr(self, "library_view") and self.library_view:
                self.library_view._tracks_cache = None
                self.library_view._tracks_cache_key = None
                self.library_view._cached_unanalysed = None
                await self.library_view.load_library()
                
            self.show_snackbar("Acoustic DSP features cleared successfully.")
        except Exception as exc:
            self.show_snackbar(f"Failed to clear DSP features: {exc}")



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
            from utils.streamrip_api import load_config, get_default_download_path
            cfg = load_config()
            self.target_folder = cfg.get("downloads", {}).get("folder", "") or get_default_download_path()
        except Exception:
            from utils.streamrip_api import get_default_download_path
            self.target_folder = get_default_download_path()
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
    def _on_playback_error_toast(self, detail: str):
        # Swallow transient just_audio load/abort races that fire during rapid
        # skip/mutation; they self-recover and shouldn't alarm the user. Only
        # surface genuine, sticky failures.
        benign = ("abort", "interrupted", "Source error", "Loading interrupted",
                  "Connection", "setAudioSource")
        if any(b.lower() in str(detail).lower() for b in benign):
            logger.warning("Suppressed transient playback error: %s", detail)
            return
        self.show_snackbar(f"Playback error: {detail}",
                           icon=ft.Icons.ERROR_OUTLINE, color="#FF4444")

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
            # Shut down the audio engine (stops playback and background pollers)
            try:
                audio_engine.shutdown()
            except Exception as ae_exc:
                logger.error("Failed to shutdown audio engine: %s", ae_exc)
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
    debug_log("main function entered")
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
            debug_log("setting audio context")
            audio_context = AudioContext(
                android=AudioContextConfig(
                    focus=AudioContextConfigFocus.MIX_WITH_OTHERS
                )
            )
            await page.set_audio_context(audio_context)
            debug_log("audio context set successfully")
        except Exception as e:
            debug_log(f"Failed to set audio context: {e}")
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
        debug_log("creating StreamripFletApp instance")
        app = StreamripFletApp(page)
        debug_log("initializing StreamripFletApp")
        await app.initialize()
        debug_log("StreamripFletApp initialized successfully")
        page.on_disconnect = lambda _e: app.on_disconnect()
    except Exception as e:
        import traceback
        debug_log(f"exception in main: {e}\n{traceback.format_exc()}")
        page.add(ft.Text(f"Startup crash: {e}", color="red", size=14, selectable=True))
        page.add(ft.Text(traceback.format_exc(), color="white", size=10, selectable=True))
        page.update()


if __name__ == "__main__":
    import os

    # Diagnostic logging — do NOT modify FLET_SERVER_UDS_PATH; it must stay
    # relative so that Python and Dart resolve it from the same CWD.
    uds_path = os.getenv("FLET_SERVER_UDS_PATH", "")
    cwd = os.getcwd()
    debug_log(f"CWD: {cwd}")
    debug_log(f"FLET_SERVER_UDS_PATH (raw): {uds_path}")
    debug_log(f"Resolved UDS path: {os.path.join(cwd, uds_path)}")

    # Check if a stale socket file exists and report
    resolved = os.path.join(cwd, uds_path)
    if os.path.exists(resolved):
        debug_log(f"WARNING: stale socket file exists at {resolved}, Flet will remove it")

    # List CWD contents for diagnostics
    try:
        cwd_files = os.listdir(cwd)
        debug_log(f"CWD files: {cwd_files}")
    except Exception as e:
        debug_log(f"CWD listing error: {e}")

    # Background thread: monitor the UDS socket file creation
    def socket_monitor():
        import time
        waited = 0
        while waited < 30:
            if os.path.exists(resolved):
                debug_log(f"Socket file {resolved} appeared after {waited}s")
                break
            time.sleep(0.5)
            waited += 0.5
        else:
            debug_log(f"Socket file {resolved} did NOT appear after 30s — Flet server may have failed")

    import threading
    threading.Thread(target=socket_monitor, daemon=True).start()

    debug_log(f"__main__ block: calling ft.run")
    try:
        ft.run(main, assets_dir="assets")
    except Exception as run_err:
        import traceback
        debug_log(f"ft.run crashed: {run_err}\n{traceback.format_exc()}")

