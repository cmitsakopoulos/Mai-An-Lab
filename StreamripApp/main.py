"""
StreamripApp; Full Fidelity Flet Rewrite.
Replaces all Kivy / KivyMD code while maintaining 1:1 UX parity, 
animations, and functional details from the original.
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
    BG, SURFACE, SURFACE2, SURFACE_ELEVATED, CYAN, AMBER, TEXT, DIM, BORDER,
    BORDER_SUBTLE, RADIUS_CARD, RADIUS_PILL, SOURCE_COLORS,
    LIB_ARTIST_COLOR, LIB_ALBUM_COLOR, LIB_TRACK_COLOR, LIB_PLAYLIST_COLOR, LIB_PARTITION_COLOR,
    apply_opacity, LEGACY_ACCENT_MAP
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
from ui.player.download_dock import DownloadDock
from ui.player.dialogs import PlaylistEditorDialog
debug_log("importing queue_controller and error_boundary")
from utils.queue_controller import QueueController
from utils import play_ledger
from utils.autoplay import AutoPlay, VARIETY_MODES, DETERMINISTIC
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
        self._post_restart_message: str | None = None
        self._apply_accent = _apply_accent
        self.is_background  = False
        self.is_restoring_session = False

        # Listen capture for engines WITHOUT a native play ledger (macOS): the
        # path that was last playing and how far into it we got, so the
        # transition away from it can be classified as a listen or a skip.
        self._last_played_path: str = ""
        self._last_play_position: float = 0.0
        self._last_play_duration: float = 0.0
        # Mirrors self.autoplay.enabled (persisted as the "play_similar_mode"
        # pref); the AutoPlay controller is created in _heavy_init.
        self.play_similar_mode: bool = False

    def _show_error(self, e=None):
        """Surfaces critical errors to the full-screen ErrorBoundary."""
        if hasattr(self, "error_boundary"):
            self.error_boundary._show_error(e)
        else:
            self.show_snackbar(f"Critical Error: {e}")

    # Flet 0.86 delivers the state as `e.state` (an AppLifecycleState whose
    # values are "show"/"resume"/"hide"/"inactive"/"pause"/"detach"/"restart")
    # with `e.data` None. The old check compared e.data against "hidden"/
    # "detached", so is_background was never set: UI updates kept flowing with
    # the screen off and the resume path below never ran. "inactive" is NOT
    # background — split-screen keeps a visible app inactive.
    _BACKGROUND_STATES = ("hide", "pause", "detach")

    def _on_lifecycle(self, e):
        state = getattr(e, "state", None)
        state = getattr(state, "value", state) or e.data or ""
        if state in self._BACKGROUND_STATES:
            # The session may be torn down from here on, and nothing refills
            # auto-play without it: queue enough to ride it out. Runs on every
            # background state (hide then pause); a full buffer makes it a no-op.
            self.autoplay.prefill_for_background()
            now_bg = True
        elif state == "resume":
            now_bg = False
        else:
            return  # show / inactive / restart are transitional
        if now_bg == self.is_background:
            return
        self.is_background = now_bg
        if now_bg:
            logger.info("App lifecycle: %s - Suspending UI updates", state)
            if hasattr(self, "assistant_view") and self.assistant_view:
                self.assistant_view.handle_app_background()
            return
        logger.info("App lifecycle: %s - Resuming UI updates", state)
        if hasattr(self, "assistant_view") and self.assistant_view:
            self.assistant_view.handle_app_resume()
        # Player handlers skip UI work while backgrounded, so the mini player
        # and Now Playing may show a stale track, play/pause state or duration.
        # One refresh (also re-highlights the playing rows) and a full flush.
        self.safe_update(self._refresh_now_playing_ui)
        self.page.run_task(self._prune_caches_async)
        # Pick up listens that finished while we were in the background.
        self._schedule_ledger_drain(0.0)

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
        while getattr(self, '_splash_logo', None) is not None:
            try:
                _ = self._splash_logo.page
                self._splash_logo.opacity = 0.3 if self._splash_logo.opacity == 1.0 else 1.0
                self._splash_logo.update()
                await asyncio.sleep(0.8)
            except Exception:
                break

    def restart_ui(self, target_tab=None, post_message=None):
        """Soft-restarts the app UI to apply theme changes immediately."""
        if self._is_restarting:
            logger.warning("Restart already in progress, ignoring.")
            return
            
        self._forced_tab = target_tab
        self._post_restart_message = post_message
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
            raw_color = appearance.get("accent_color", "#FFD60A")
            acc_color = LEGACY_ACCENT_MAP.get(raw_color.upper(), raw_color)
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
        # App-level download surface. Registered as the QueueController's
        # listener so downloads stay visible from every tab, not just Search.
        self.download_dock       = DownloadDock(self)
        self.queue.add_listener(self.download_dock.refresh)
        self.playlist_editor     = PlaylistEditorDialog(self)
        self.notifications       = NotificationSystem(self)
        self.assistant_view      = AssistantView(self)

        # wire ft.Audio into the page
        audio_engine.play_log_path = play_ledger.ledger_path(DATA_DIR)
        audio_engine.setup(self.page, self.db_manager)

        # Restore saved shuffle, repeat and similar playback preferences
        audio_engine.is_shuffle = bool(self._prefs.get("is_shuffle", False))
        audio_engine.repeat_mode = self._prefs.get("repeat_mode", "none")
        self.play_similar_mode = bool(self._prefs.get("play_similar_mode", False))
        # One controller per process: a UI restart re-runs _heavy_init, and the
        # session's history/rejections must survive it.
        if getattr(self, "autoplay", None) is None:
            self.autoplay = AutoPlay(
                audio_engine, self.db_manager,
                run_task=lambda fn, *a: self.page.run_task(fn, *a),
                notify=lambda msg: self.safe_update(lambda: self.show_snackbar(msg)),
            )
        self.autoplay.db = self.db_manager
        variety = self._prefs.get("autoplay_variety", DETERMINISTIC)
        self.autoplay.variety = variety if variety in VARIETY_MODES else DETERMINISTIC
        await self.autoplay.load_feedback()
        # Restored silently: the restored queue already carries its buffer, and
        # refills resume on the next track change.
        self.autoplay.enabled = self.play_similar_mode
        self.now_playing.update_shuffle(audio_engine.is_shuffle)
        self.now_playing.update_repeat(audio_engine.repeat_mode)
        self.now_playing.update_play_similar(self.play_similar_mode)

        # bind audio engine events
        audio_engine.bind(
            current_path=self._on_current_path,
            current_art=self._on_current_art,
            position=self._on_position,
            is_playing=self._on_is_playing,
            duration=self._on_duration,
            loudness_boost_db=self._on_loudness_boost_change,
        )
        def _on_queue_mutated(_inst, _val):
            self.safe_update(self.queue_sheet.refresh)
            # Persist immediately on mutation so a hard OS kill (Android low-
            # memory reaping or process death) leaves a recoverable snapshot
            # on disk instead of the stale state from the previous launch.
            self._schedule_queue_save()

        audio_engine.bind(
            on_playback_error=lambda _, d: self._on_playback_error_toast(d),
            on_queue_mutated=_on_queue_mutated,
            on_queue_end=lambda _i, _v: self.autoplay.on_queue_dry(),
            on_native_ready=lambda _i, _v: self._schedule_ledger_drain(0.0),
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
        """Build the similarity graph if it is MISSING — never on a schedule —
        and rebuild the journey graph if an older builder made it.

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
            if status["coord_tracks"] > 0 and await tg.journey_graph_stale(self.db_manager):
                # Geometry is there but the genre graph predates the current
                # builder (e.g. v2's bridge-artist adjacency): rebuild just it.
                await self._await_scroll_quiet()
                await tg.build_journey_graph(self.db_manager)
                logger.info("Graph readiness: journey graph rebuilt (stale version).")
                return
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
            # Background pass: NEW artists only (no include_failed /
            # retry_incomplete). Re-checking blanks and retrying failures is
            # deliberate work the user asks for with Sync in the workbench, not
            from utils.progress_notify import (
                ProgressNotifier, metadata_progress_cb, metadata_summary_text,
            )
            provider = self._prefs.get("sync_provider", "musicbrainz")
            notifier = ProgressNotifier("metadata", "Fetching artist metadata")
            summary = await enrich_library(
                self.db_manager, provider=provider, with_genres=True,
                progress=metadata_progress_cb(notifier),
            )
            notifier.finish(metadata_summary_text(summary))
            if summary.get("enriched"):
                logger.info("Metadata enrichment complete (%s): %s", provider, summary)
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
            self.dismiss_dialog(self.wipe_dialog)
            self.page.run_task(self.wipe_database)

        def on_cancel(e):
            self.dismiss_dialog(self.wipe_dialog)

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
        
        if self.page:
            self.page.show_dialog(self.wipe_dialog)

    def open_fresh_install_reset_confirmation(self):
        """Opens a confirmation dialog before completely lobotomizing the app state."""
        def on_confirm(e):
            self.dismiss_dialog(self.fresh_install_reset_dialog)
            self.page.run_task(self.reset_to_fresh_install)

        def on_cancel(e):
            self.dismiss_dialog(self.fresh_install_reset_dialog)

        self.fresh_install_reset_dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Are you sure? (Reset to Fresh Install)", weight=ft.FontWeight.BOLD),
            content=ft.Column(
                [
                    ft.Text(
                        "This will completely reset the app to an initial first-run state:",
                        size=13,
                        color=TEXT,
                    ),
                    ft.Text(
                        "- Clears all library databases and metadata\n"
                        "- Wipes search history and cached graph state\n"
                        "- Clears playback queues and stored preferences\n"
                        "- Removes the onboarding marker so the setup sequence appears immediately",
                        size=12,
                        color=DIM,
                    ),
                    ft.Container(height=4),
                    ft.Text(
                        "Your actual music audio files will NOT be touched.",
                        size=12,
                        weight=ft.FontWeight.BOLD,
                        color=TEXT,
                    ),
                ],
                spacing=8,
                tight=True,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=on_cancel),
                ft.TextButton(
                    content=ft.Text("Reset & Lobotomize", weight=ft.FontWeight.BOLD, color="#FF4444"),
                    on_click=on_confirm,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        if self.page:
            self.page.show_dialog(self.fresh_install_reset_dialog)

    async def reset_to_fresh_install(self):
        """Wipes all databases, caches, preferences, queues, and onboarding markers across all storage targets, then restarts the UI."""
        logger.info("Initiating fresh install reset (lobotomy)...")
        try:
            # 1. Close active DB connection if open
            if hasattr(self, "db_manager") and self.db_manager:
                try:
                    await self.db_manager.close()
                except Exception as e:
                    logger.warning(f"Error closing db_manager: {e}")

            # 2. Stop audio engine and clear queue
            try:
                audio_engine.stop()
                audio_engine.queue.clear()
            except Exception:
                pass

            # 3. Collect all candidate directories and files to purge
            import shutil
            from utils.filepath_utils import get_app_dir
            from utils.streamrip_api import get_config_path
            
            app_dir = get_app_dir()
            config_dir = os.path.dirname(get_config_path())
            home = os.path.expanduser("~")
            repo_app_dir = os.path.dirname(os.path.abspath(__file__))
            flet_storage_dir = os.path.join(repo_app_dir, ".flet", "storage", "data")

            # Explicit candidate paths to delete
            paths_to_delete = [
                os.path.join(app_dir, ".onboarded"),
                os.path.join(app_dir, "library.db"),
                os.path.join(app_dir, "library.db-wal"),
                os.path.join(app_dir, "library.db-shm"),
                os.path.join(app_dir, "queue_state.json"),
                os.path.join(app_dir, "queue_pos.json"),
                os.path.join(app_dir, "flet_prefs.json"),
                os.path.join(app_dir, "user_prefs.json"),
                os.path.join(app_dir, "recent_searches.json"),
                os.path.join(app_dir, "graph_state.json"),
                os.path.join(app_dir, "failed_downloads.db"),
                os.path.join(app_dir, "mai_an_lab_state_latest.zip"),
                os.path.join(config_dir, ".onboarded"),
                os.path.join(config_dir, "user_prefs.json"),
                os.path.join(config_dir, "failed_downloads.db"),
                os.path.join(config_dir, "library.db"),
                os.path.join(config_dir, "library.db-wal"),
                os.path.join(config_dir, "library.db-shm"),
                os.path.join(home, ".onboarded"),
                os.path.join(home, "library.db"),
                os.path.join(home, "library.db-wal"),
                os.path.join(home, "library.db-shm"),
                os.path.join(home, "queue_state.json"),
                os.path.join(home, "flet_prefs.json"),
                os.path.join(repo_app_dir, "recent_searches.json"),
                os.path.join(repo_app_dir, "graph_state.json"),
                os.path.join(repo_app_dir, "history.json"),
                os.path.join(repo_app_dir, "chat_history.json"),
                os.path.join(home, "Downloads", "mai_an_lab_state_latest.zip"),
            ]

            if hasattr(self, "library_folder") and self.library_folder:
                paths_to_delete.append(os.path.join(self.library_folder, "mai_an_lab_state_latest.zip"))

            for f_path in paths_to_delete:
                try:
                    if os.path.exists(f_path):
                        if os.path.isdir(f_path):
                            shutil.rmtree(f_path)
                        else:
                            os.remove(f_path)
                except Exception:
                    pass

            # Wipe .flet/storage/data contents
            if os.path.isdir(flet_storage_dir):
                for item in os.listdir(flet_storage_dir):
                    p = os.path.join(flet_storage_dir, item)
                    try:
                        if os.path.isdir(p):
                            shutil.rmtree(p)
                        else:
                            os.remove(p)
                    except Exception:
                        pass

            # Wipe cache queues
            cache_dir = os.path.join(app_dir, ".cache")
            if os.path.isdir(cache_dir):
                for item in os.listdir(cache_dir):
                    if "queue_" in item or "streamrip" in item:
                        try:
                            os.remove(os.path.join(cache_dir, item))
                        except Exception:
                            pass

            # Wipe pca_report directories
            for pca_p in [
                os.path.join(config_dir, "pca_report"),
                os.path.join(app_dir, "pca_report"),
            ]:
                if os.path.isdir(pca_p):
                    try:
                        shutil.rmtree(pca_p)
                    except Exception:
                        pass

            # 4. Reset in-memory preferences and state
            self._prefs = {}
            self.library_folder = ""
            self.target_folder = ""
            self.download_history_list = []

            # 5. Brief delay to ensure I/O settles, then restart UI
            await asyncio.sleep(0.2)
            self.restart_ui()
            self.show_snackbar("App reset to fresh install. Onboarding ready.")
        except Exception as e:
            logger.error(f"Failed to reset to fresh install: {e}", exc_info=True)
            self.show_snackbar("Reset failed. Check logs.")

    async def check_onboarding(self):
        """Detects a fresh install using a marker file."""
        try:
            marker_path = os.path.join(DATA_DIR, ".onboarded")
            from utils.streamrip_api import get_config_path
            marker_path2 = os.path.join(os.path.dirname(get_config_path()), ".onboarded")
            if os.path.exists(marker_path) or os.path.exists(marker_path2):
                logger.info("Onboarding marker found. Skipping guide.")
                return
            
            # No marker? Show the guide.
            self.show_onboarding_guide()
        except Exception as e:
            logger.error(f"Onboarding check failed: {e}")

    def show_onboarding_guide(self):
        """Presents an interactive, high-fidelity setup guide to the user."""
        from ui.player.onboarding_modal import OnboardingWizardModal
        self.onboarding_wizard = OnboardingWizardModal.show(self)

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
            if hasattr(self, "db_manager") and self.db_manager:
                res = self.db_manager.clear_caches()
                if asyncio.iscoroutine(res):
                    await res
            if hasattr(self, "library_view") and self.library_view:
                self.library_view._tracks_cache = None
                self.library_view._tracks_cache_key = None
                self.library_view._cached_unanalysed = None

            # Clear all queue state files
            audio_engine.clear_queue()
            for filename in ("queue_state.json", "queue_pos.json", "queue_regular.json", "queue_shuffle.json", "queue_similar.json"):
                q_path = os.path.join(os.environ["XDG_CACHE_HOME"] if filename not in ("queue_state.json", "queue_pos.json") else DATA_DIR, filename)
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
                "swipe_back": "light",
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

    def safe_update(self, fn, target=None):
        """Queue a UI mutation and schedule a single coalesced update.
        Thread-safe entry point: may be called from any background thread.

        `target` narrows the sync to one control's subtree instead of the whole
        page. A download emits progress about four times a second, and a bare
        page.update() re-syncs every control in every cached tab on each tick —
        which is what made the entire UI visibly churn while downloading. A
        flush uses the narrow path only when EVERY mutation in that flush named
        a target; one untargeted caller in the batch falls back to page.update()
        so nothing can be left unsynced.
        """
        if not self.page: return

        # A UI rebuild (e.g. after a download completes) tears down the Flet
        # session while background workers — the queue controller, notification
        # dismiss timers — may still hold a reference to the old page. Both
        # page.session and run_task then raise RuntimeError("...destroyed
        # session."). Touching the session first lets us bail *before* run_task
        # eagerly builds the coroutine it can't schedule — which is what leaks
        # the "coroutine was never awaited" RuntimeWarning — and the surrounding
        # guard covers the tiny window where the session dies mid-call.
        try:
            # Use run_task to bridge the sync/async gap safely.
            _ = self.page.session
            self.page.run_task(self._safe_update_handler, fn, target)
        except RuntimeError:
            return

    async def _safe_update_handler(self, fn, target=None):
        async with self._update_lock:
            self._pending_fns.append((fn, target))
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

        targets = []
        narrow = True
        for fn, target in fns:
            try:
                if asyncio.iscoroutinefunction(fn):
                    await fn()
                else:
                    fn()
            except Exception:
                logger.exception("safe_update execution error")
            if target is None:
                narrow = False
            elif not any(target is t for t in targets):
                targets.append(target)

        # Skip the sync while the app is backgrounded; pushing diffs to a
        # suspended Flet/Flutter client wastes CPU and can cause UI hangs.
        # _on_lifecycle calls safe_update(lambda: None) on resume to force-sync.
        if self.is_background:
            return

        if narrow and targets:
            for target in targets:
                try:
                    target.update()
                except Exception:
                    # Not mounted yet, or detached mid-flush. A missed narrow
                    # sync is cosmetic; the next full update will catch it.
                    pass
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
            raw_jarvis = cfg.get("appearance", {}).get("show_jarvis", True)
            if isinstance(raw_jarvis, str):
                self._show_jarvis = raw_jarvis.strip().lower() in ("true", "1", "yes")
            else:
                self._show_jarvis = bool(raw_jarvis) if raw_jarvis is not None else True
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

        # Where back-navigation out of Settings returns to. Settings (tab 3) is
        # entered from every other tab but has no NavigationBar destination of
        # its own — _switch_tab even blanks the nav indicator while it's open —
        # so the originating tab has to be remembered or there is no way back.
        # Never point this at Settings itself, or back becomes a no-op.
        self._previous_tab = self._current_tab if self._current_tab != 3 else 2

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
                icon=ft.Icons.SEARCH_ROUNDED,
                selected_icon=ft.Icons.SEARCH_ROUNDED,
                label="Search",
            ),
            ft.NavigationBarDestination(
                icon=ft.Icons.LIBRARY_MUSIC_OUTLINED,
                selected_icon=ft.Icons.LIBRARY_MUSIC_ROUNDED,
                label="Library",
            ),
        ])

        # Navigation bar - Apple style tab bar (no M3 capsule pill, subtle border)
        self._nav = ft.NavigationBar(
            selected_index=self._get_nav_index(self._current_tab),
            bgcolor=SURFACE,
            indicator_color="transparent",
            elevation=0,
            border=ft.Border(top=ft.BorderSide(0.5, BORDER_SUBTLE)),
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
                self.download_dock.build(),
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
        self.page.overlay.append(self.download_dock.build_sheet())
        
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
            
        # ── System back navigation ───────────────────────────────────────────
        # Flet 0.86 wraps EVERY view — the automatic root view included — in a
        # Flutter PopScope. With can_pop left at its default True, Android back
        # pops the only route and kills the app from anywhere, including halfway
        # down the Settings hierarchy. Setting can_pop=False makes Flutter fire
        # on_confirm_pop instead, letting _on_confirm_pop decide whether the
        # gesture means "go up one level" or "leave the app".
        #
        # This is why no page.views migration is needed: the root view already
        # has the hook. Side effect: can_pop=False opts this view out of the
        # Android 14+ predictive-back preview animation.
        try:
            root_view = self.page.views[0]
            root_view.can_pop = False
            root_view.on_confirm_pop = self._on_confirm_pop
        except Exception:
            # Never let back-wiring failure block the UI from rendering.
            logger.exception("Failed to wire system back navigation")

        # Desktop has no back gesture; Escape is the equivalent affordance.
        if sys.platform == "darwin":
            self.page.on_keyboard_event = self._on_keyboard

        # Clean up splash logo reference so the background pulsing task exits immediately
        self._splash_logo = None

        # Initial render
        self.page.update()

        if getattr(self, "_post_restart_message", None):
            msg = self._post_restart_message
            self._post_restart_message = None
            self.show_snackbar(msg, icon=ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED)

    # ── back navigation ──────────────────────────────────────────────────────
    def navigate_back(self) -> bool:
        """Resolve ONE level of back-navigation. Returns True if consumed.

        Single source of truth for "go up one level", so the Android system
        back gesture and the desktop Escape key resolve identically. Any future
        gesture affordance must call this rather than re-deriving the hierarchy.

        Ordered most-nested first. Sheets and dialogs are deliberately absent:
        Flutter already pops its own dialog/bottom-sheet routes on system back,
        and reaching for page.pop_dialog() here would re-introduce the toast-eats
        -the-pop hazard documented on dismiss_dialog().
        """
        # Search selection mode → normal browsing. This is the most nested
        # state in the app: it is a mode inside a tab, so back must resolve it
        # before anything else, the same way a selection mode does elsewhere.
        if self._current_tab == 1:
            sv = getattr(self, "search_view", None)
            if sv is not None and getattr(sv, "selection_mode", False):
                sv.exit_selection()
                self.trigger_haptic("swipe_back")
                return True

        # Settings subpage → hub. The hub is a content swap inside the Settings
        # tab, not a separate tab, so this rung has to come first.
        if self._current_tab == 3:
            sv = getattr(self, "settings_view", None)
            if sv is not None and getattr(sv, "_current_subpage_name", None):
                sv._show_hub()
                self.trigger_haptic("swipe_back")
                return True

            # Settings hub → whichever tab opened it.
            target = getattr(self, "_previous_tab", 2)
            if target == 3:
                target = 2  # never bounce back into Settings
            self._switch_tab(target)
            self.trigger_haptic("swipe_back")
            return True

        # A main tab is the top of the hierarchy — nothing to go back to.
        return False

    async def _on_confirm_pop(self, e):
        """Android system back / gesture, delivered via the root view's PopScope.

        The root view is set can_pop=False so Flutter routes the gesture here
        instead of tearing the app down. confirm_pop(False) keeps the app open
        (we handled it); confirm_pop(True) lets Android background us normally.

        confirm_pop() MUST be answered on every path: the Dart side parks on a
        completer with a 5-minute timeout and cancels the pop if nothing arrives,
        which would leave the user unable to leave the app at all. Hence the
        finally, and hence the broad except.
        """
        consumed = False
        try:
            consumed = self.navigate_back()
        except Exception:
            logger.exception("navigate_back failed; deferring to system back")
        finally:
            try:
                await self.page.views[0].confirm_pop(not consumed)
            except Exception:
                logger.exception("confirm_pop failed")

    def _on_keyboard(self, e):
        """Desktop equivalent of system back. Escape resolves one level."""
        if getattr(e, "key", None) == "Escape":
            self.navigate_back()

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

    # Views that want to save/restore state across a tab switch implement
    # on_hide()/on_show(). Swapping _tab_content.content remounts the subtree,
    # so Flutter-side state — scroll position above all — is discarded even
    # though the Python controls survive in _view_cache.
    _TAB_VIEWS = {0: "assistant_view", 1: "search_view", 2: "library_view", 3: "settings_view"}

    def _tab_lifecycle(self, index: int, hook: str) -> None:
        view = getattr(self, self._TAB_VIEWS.get(index, ""), None)
        fn = getattr(view, hook, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                # A lifecycle hook must never be able to block navigation.
                logger.exception("%s hook failed for tab %s", hook, index)

    def _switch_tab(self, index: int):
        # Remember where we came from *before* the overwrite, so back-navigation
        # out of Settings can return there. Guarded on the outgoing tab not
        # already being Settings: re-entering Settings from Settings (a deep
        # link like library.py's Storage jump) must not make this self-referential.
        if index == 3 and self._current_tab != 3:
            self._previous_tab = self._current_tab

        if self._current_tab != index:
            self._tab_lifecycle(self._current_tab, "on_hide")

        previous_tab = self._current_tab
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

        if previous_tab != index:
            self._tab_lifecycle(index, "on_show")

    def switch_tab(self, index: int):
        """Public tab switch entry point."""
        self._switch_tab(index)

    def switch_to_settings(self, subpage: str = None):
        """Switches to Settings tab and directly opens the target subpage."""
        if subpage and hasattr(self, "settings_view") and self.settings_view:
            self.settings_view.initial_subpage = subpage
        self._switch_tab(3)
        if subpage and hasattr(self, "settings_view") and self.settings_view:
            self.settings_view.open_subpage(subpage)

    # ── audio engine callbacks ───────────────────────────────────────────────
    def _on_loudness_boost_change(self, _instance, value: float):
        self.now_playing.update_loudness_boost(value)
        if hasattr(self, "settings_view") and self.settings_view:
            self.settings_view.update_loudness_boost(value)

    def _on_current_path(self, _instance, path: str):
        native_log = getattr(audio_engine, "has_native_play_log", False)
        restoring = getattr(self, "is_restoring_session", False)
        if native_log:
            # Plays are counted from the native ledger (real listening time,
            # captured even while this session is deaf). The outgoing track's
            # line lands a moment after the index change, hence the delay.
            self._schedule_ledger_drain()
        elif path and not restoring:
            # Increment play count in background
            asyncio.create_task(self.db_manager.increment_play_count(path))

        # Track changed (manual skip or auto-advance): only the index moved,
        # so persist the small position file, not the whole queue.
        self._schedule_position_save()

        if not native_log:
            self._capture_listen_without_ledger(path, restoring)

        if not restoring:
            self.autoplay.on_track_changed(path)

        self.safe_update(self._refresh_now_playing_ui)

    def _on_current_art(self, _instance, art: str):
        """Art can arrive after the track change (the native resolver is
        asynchronous); apply it if it still belongs to the playing track."""
        if art and not self.is_background:
            self._apply_artwork_if_current(audio_engine.current_path, art)

    def _refresh_now_playing_ui(self):
        """Push the engine's current track/state/art into the mini player and
        Now Playing and re-highlight the playing rows. Runs inside a
        safe_update flush."""
        path   = audio_engine.current_path
        track  = audio_engine.current_track  or ""
        artist = audio_engine.current_artist or ""
        album  = audio_engine.current_album  or ""
        self.mini_player.update_meta(track, artist)
        self.now_playing.update_meta(track, artist, album)
        is_playing = audio_engine.is_playing
        self.mini_player.update_state(is_playing)
        self.now_playing.update_state(is_playing)
        try:
            self.now_playing.update_duration(audio_engine.duration)
        except Exception:
            pass

        img_url = ""
        if audio_engine.queue and audio_engine.current_index < len(audio_engine.queue):
            img_url = audio_engine.queue[audio_engine.current_index].get("image_url", "")
        # Use cached local artwork if available, avoiding redundant extraction
        art_val = audio_engine.current_art or img_url
        self.mini_player.update_artwork(art_val)
        self.now_playing.update_artwork(art_val)

        self.search_view.refresh_now_playing()
        self.library_view.refresh_now_playing()

        if isinstance(img_url, str) and img_url.startswith("http"):
            self._fetch_artwork_url_async(img_url)
        elif not audio_engine.current_art and track and path:
            self._extract_artwork_async(path)

    def _capture_listen_without_ledger(self, path: str, restoring: bool):
        """Listen/skip signal for engines with no native play ledger (macOS),
        from the position mirror. On Android the ledger supplies it instead
        (see _schedule_ledger_drain): Dart stops emitting positions while the
        app is backgrounded, so this mirror would misread a song that finished
        with the screen off as a 10-second skip."""
        prev = self._last_played_path
        if prev and prev != path and not restoring:
            self.autoplay.on_listen(prev, self._last_play_position, self._last_play_duration)
        self._last_played_path = path or ""
        self._last_play_position = 0.0
        self._last_play_duration = float(audio_engine.duration or 0.0)

    def _apply_artwork_if_current(self, for_path: str, art: str):
        """Show `art` only if `for_path` is still the playing track when the
        flush runs. Artwork loads finish on worker threads after arbitrary
        delays (and cancelling a to_thread task does not stop its thread), so an
        unguarded apply let a skipped track's cover overwrite the current one."""
        def _apply():
            if audio_engine.current_path != for_path:
                return
            self.mini_player.update_artwork(art)
            self.now_playing.update_artwork(art)
        self.safe_update(_apply)

    def _fetch_artwork_url_async(self, img_url: str):
        for_path = audio_engine.current_path
        # Check in-memory cache first; avoids any disk/network I/O
        cached = _ARTWORK_CACHE.get(img_url)
        if cached:
            self._apply_artwork_if_current(for_path, cached)
            return

        def _worker():
            try:
                ph  = hashlib.md5(img_url.encode()).hexdigest()
                tmp = os.path.join(get_temp_artwork_dir(), f"streamrip_art_{ph}.jpg")
                if not os.path.exists(tmp):
                    urllib.request.urlretrieve(img_url, tmp)
                _ARTWORK_CACHE.put(img_url, tmp)
                self._apply_artwork_if_current(for_path, tmp)
            except Exception as exc:
                logger.error("Artwork URL fetch failed: %s", exc)
        asyncio.create_task(asyncio.to_thread(_worker))

    def _extract_artwork_async(self, path: str):
        # Android: the native resolver already decodes this art for the
        # notification and hands it over as current_art (see _on_current_art).
        # Decoding it again here with PIL was pure duplicate work per track.
        if getattr(audio_engine, "has_native_art", False):
            return
        # Check in-memory cache; avoids PIL decode + disk write on repeat plays
        cached = _ARTWORK_CACHE.get(path)
        if cached:
            self._apply_artwork_if_current(path, cached)
            return

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

            self._apply_artwork_if_current(path, art_path)

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

        try:
            if self.mini_player.container and self.mini_player.container.page:
                self.mini_player.update_progress(pct)
                self.mini_player.container.update()
        except Exception:
            pass

        try:
            if self.now_playing.container and self.now_playing.container.open:
                self.now_playing.update_progress(position, dur)
                if self.now_playing.container.page:
                    self.now_playing.container.update()
        except Exception:
            pass

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
        try:
            self.now_playing.update_duration(duration)
            if self.now_playing.container and self.now_playing.container.open and self.now_playing.container.page:
                self.now_playing.container.update()
        except Exception:
            pass

    def _on_is_playing(self, _instance, is_playing: bool):
        if self.is_background:
            return
        def _update():
            self.mini_player.update_state(is_playing)
            self.now_playing.update_state(is_playing)
            if is_playing:
                self.library_view.kick_net_pulse()
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
            # The position file is written far more often than the queue file
            # (every track change / 10 s of play) and is never older than it.
            # Trust it when its track is still in this queue.
            pstate = await asyncio.to_thread(self._read_queue_state, self._position_state_path())
            if pstate and pstate.get("path"):
                pi = pstate.get("current_index")
                if not (isinstance(pi, int) and 0 <= pi < len(queue)
                        and queue[pi].get("path") == pstate["path"]):
                    pi = next((i for i, t in enumerate(queue) if t.get("path") == pstate["path"]), None)
                if pi is not None:
                    index = pi
                    pos = pstate.get("position", 0.0)
                    dur = pstate.get("duration", 0.0)

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
                self.autoplay.resume(restored_path)

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
                # 300 ms is long enough for restore_queue's _push_native
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

    def _save_queue_state(self):
        """Write the current queue snapshot to disk atomically.

        Atomicity matters because Android can SIGKILL the process between
        the open() and the close(); without the tmp+rename dance we'd
        leave a half-written JSON file that breaks the next restore.

        Empty queue ⇒ delete the file rather than write `{"queue": []}` so
        that an explicit `stop()` doesn't leave a phantom session for the
        next launch to "restore" into nothing.

        Partition files (queue_regular/shuffle.json) are refreshed
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
            self._save_position_state()  # removes it too
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
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                safe_json_dump(state, fh)
            os.replace(tmp, path)
            # Keep the position file at least as new as the queue file, so a
            # restore can always trust it (see _restore_queue_state_async).
            self._save_position_state()
        except Exception as exc:
            logger.warning("Could not save queue state: %s", exc)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _position_state_path(self) -> str:
        return os.path.join(DATA_DIR, "queue_pos.json")

    def _save_position_state(self):
        """Persist where we are in the queue (track, index, offset) without
        rewriting the queue itself. Atomic like _save_queue_state."""
        path = self._position_state_path()
        if not audio_engine.queue:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except Exception:
                pass
            return
        ci = audio_engine.current_index
        q = audio_engine.queue
        state = {
            # The row the index points at — the same key the queue file uses.
            "path":          (q[ci].get("path") or "") if 0 <= ci < len(q) else "",
            "current_index": ci,
            "position":      audio_engine.position,
            "duration":      audio_engine.duration,
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(state, fh)
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("Could not save position state: %s", exc)

    def _schedule_position_save(self, delay: float = 0.4):
        """Coalesced like _schedule_queue_save; a queue save supersedes it."""
        existing = getattr(self, "_pos_save_task", None)
        if existing is not None and not existing.done():
            existing.cancel()

        async def _do_save():
            try:
                await asyncio.sleep(delay)
                await asyncio.to_thread(self._save_position_state)
            except asyncio.CancelledError:
                pass

        try:
            self._pos_save_task = asyncio.create_task(_do_save())
        except RuntimeError:
            self._save_position_state()

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

    def _schedule_ledger_drain(self, delay: float = 3.0):
        """Ingest the native play ledger into the DB. Coalescing: if a drain is
        already pending, it will pick up anything appended before it runs, and
        a later line is caught by the next track change / resume / ready."""
        if not getattr(audio_engine, "has_native_play_log", False):
            return
        existing = getattr(self, "_ledger_drain_task", None)
        if existing is not None and not existing.done():
            return

        async def _do_drain():
            try:
                await asyncio.sleep(delay)
                await play_ledger.drain_ledger(DATA_DIR, self.db_manager,
                                               on_entries=self.autoplay.on_listens)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                # The ledger file is kept on failure and retried next drain.
                logger.error("play ledger drain failed: %s", exc)

        try:
            self._ledger_drain_task = asyncio.create_task(_do_drain())
        except RuntimeError:
            pass

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
                # Only the position changed: a ~150-byte write. This used to
                # rewrite queue_state.json — the WHOLE queue (the entire
                # library tail, ~1.6 MB for 5k tracks) — every 10 s of play.
                await asyncio.to_thread(self._save_position_state)
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
                self.autoplay.on_user_chose(target_path)
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
                elif source and source[0] == "genre":
                    _, genre_name = source
                    view_items = await db.get_tracks_by_genre(genre_name)
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
        elif source and source[0] == "genre":
            _, genre_name = source
            items = await db.get_tracks_by_genre(genre_name)
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

        # NON-DESTRUCTIVE: the tapped song plays within the FULL list; with
        # auto-play on, similar tracks are then inserted right after it.
        audio_engine.set_queue(tracks, start_index=target_idx)
        self.autoplay.on_user_chose(target_path)

    def set_play_similar_mode(self, enabled: bool):
        """Auto-play on/off (UI: the chain icon in Now Playing)."""
        if self.play_similar_mode == enabled:
            return
        self.play_similar_mode = enabled
        self._save_pref("play_similar_mode", enabled)
        self.now_playing.update_play_similar(enabled)
        # Similar tracks play in walk order right after the current one;
        # Dart's shuffle order would scatter them.
        if enabled and audio_engine.is_shuffle:
            audio_engine.is_shuffle = False
            self.now_playing.update_shuffle(False)
            self._save_pref("is_shuffle", False)
        # Non-destructive both ways: on inserts a buffer after the current
        # track; off drops only that buffer (the queue tail was never removed).
        self.autoplay.set_enabled(enabled, audio_engine.current_path)
        if hasattr(self, "queue_sheet") and self.queue_sheet and self.queue_sheet._initialized:
            self.safe_update(self.queue_sheet.refresh)

    def set_autoplay_variety(self, mode: str):
        """UI hook (the Deterministic / Random pills). Switching rebuilds the
        pending buffer as a new station from the playing track."""
        if mode not in VARIETY_MODES:
            return
        self.autoplay.set_variety(mode)
        self._save_pref("autoplay_variety", mode)

    async def start_radio(self, path: str):
        """UI hook (long-press → Start radio): play `path` now and restart
        auto-play from it, turning auto-play on if needed."""
        if not path:
            return
        if audio_engine.current_path != path:
            idx = next((i for i, t in enumerate(audio_engine.queue) if t.get("path") == path), -1)
            if idx == -1:
                row = (await self.db_manager.get_tracks_brief([path])).get(path) or {}
                audio_engine.queue_after_current([{
                    "path":        path,
                    "track_title": row.get("title") or os.path.basename(path),
                    "artist_name": row.get("artist") or "Unknown Artist",
                    "album_title": row.get("album") or "Unknown Album",
                    "duration":    row.get("duration") or 0.0,
                }])
                idx = audio_engine.current_index + 1 if len(audio_engine.queue) > 1 else 0
            audio_engine.play_track_at(idx)
        if self.play_similar_mode:
            self.autoplay.start(path)
        else:
            self.set_play_similar_mode(True)

    def toggle_shuffle(self):
        self.page.run_task(self._toggle_shuffle_async)

    async def _toggle_shuffle_async(self):
        new_shuffle = not audio_engine.is_shuffle
        self.now_playing.update_shuffle(new_shuffle)
        self._save_pref("is_shuffle", new_shuffle)
        if new_shuffle:
            if self.play_similar_mode:
                self.set_play_similar_mode(False)

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
    # ── metadata editor ──────────────────────────────────────────────────────
    def open_artist_metadata_editor(self, artist_name: str, on_saved=None):
        """Route an artist to THE metadata editor — the Settings workbench.

        This used to open a second, separate AlertDialog editor with its own
        affordances and its own save path (which refreshed only some of the
        walk's derived models, so an edit made here behaved differently from the
        identical edit made in the workbench). One surface, one save path.
        """
        self.switch_tab(3)
        self.settings_view._on_open_metadata_workbench_click(focus_artist=artist_name)

    def open_genre_metadata_workbench(self, genre_name: str):
        """Route a genre to the Settings metadata workbench with search filtered."""
        self.switch_tab(3)
        self.settings_view._on_open_metadata_workbench_click(genre_filter=genre_name)

    def dismiss_dialog(self, dialog) -> bool:
        """Close ONE specific dialog. Returns False if it was already closed.

        page.pop_dialog() closes whichever entry in page._dialogs still has
        open=True and sits highest in the stack — and NotificationSystem.show()
        pushes every toast into that same stack (ft.SnackBar is a DialogControl
        on Flet 0.86). Background work here raises toasts at arbitrary times, so
        a toast alive when the user taps a dialog button eats the pop: the
        dialog stays up, and tapping again just eats the next toast. Naming the
        dialog runs the identical teardown — Flutter pops the route and reports
        `dismiss`, whose wrapper unmounts the stack entry — without the guess.
        """
        if not self.page or dialog is None or not getattr(dialog, "open", False):
            return False
        dialog.open = False
        try:
            dialog.update()
        except Exception:
            logger.exception("Failed to dismiss dialog")
            return False
        return True

    def confirm_delete_track(self, path: str, title: str):
        def execute(_e):
            self.dismiss_dialog(dlg)
            self.page.run_task(self._delete_track, path)

        def cancel(_e):
            self.dismiss_dialog(dlg)

        # Deliberately left non-modal: the barrier stays tappable so there is
        # always a way out of a destructive confirmation even if a button
        # handler misfires.
        dlg = ft.AlertDialog(
            title=ft.Text("Delete Track?", color=TEXT),
            bgcolor=SURFACE,
            content=ft.Text(
                f"Permanently delete '{title}' from your device?",
                color=DIM, size=13,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=cancel),
                ft.Button(
                    content=ft.Text("Delete"),
                    style=ft.ButtonStyle(bgcolor="#FF2222", color=TEXT),
                    on_click=execute,
                ),
            ],
        )
        if self.page:
            self.page.show_dialog(dlg)


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

    def confirm_delete_playlist(self, pl_id: int, name: str):
        def execute(_e):
            self.dismiss_dialog(dlg)
            self.page.run_task(self._delete_playlist, pl_id)

        def cancel(_e):
            self.dismiss_dialog(dlg)

        # Deliberately left non-modal: the barrier stays tappable so there is
        # always a way out of a destructive confirmation even if a button
        # handler misfires.
        dlg = ft.AlertDialog(
            title=ft.Text("Delete Playlist?", color=TEXT),
            bgcolor=SURFACE,
            content=ft.Text(
                f"Permanently delete playlist '{name}'? Tracks will remain in your library.",
                color=DIM, size=13,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=cancel),
                ft.Button(
                    content=ft.Text("Delete"),
                    style=ft.ButtonStyle(bgcolor="#FF2222", color=TEXT),
                    on_click=execute,
                ),
            ],
        )
        if self.page:
            self.page.show_dialog(dlg)

    async def _delete_playlist(self, pl_id: int):
        try:
            await self.db_manager.delete_playlist(pl_id)
            if hasattr(self, "library_view") and self.library_view:
                await self.library_view.load_library()
            self.show_snackbar("Playlist deleted.")
        except Exception as exc:
            logger.error("Playlist deletion failed: %s", exc)
            self.show_snackbar(f"Could not delete playlist: {exc}")

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
            for filename in ("queue_state.json", "queue_pos.json", "queue_regular.json", "queue_shuffle.json", "queue_similar.json"):
                q_path = os.path.join(os.environ["XDG_CACHE_HOME"] if filename not in ("queue_state.json", "queue_pos.json") else DATA_DIR, filename)
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
            self.dismiss_dialog(dialog)
            self.page.run_task(action_coro)

        def on_cancel(e):
            self.dismiss_dialog(dialog)

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
        if self.page:
            self.page.show_dialog(dialog)

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
    # Scrollbar theme MUST live inside page.theme (below) — ft.Page has no
    # scrollbar_theme field, so assigning page.scrollbar_theme is a dead no-op
    # Flet never serialises. Material scrollbars are also hidden on mobile unless
    # thumb_visibility is forced on, and made grabbable (thicker + interactive)
    # so a long fling can be dragged back up. _ideal_window already resolves a
    # thousand-row thumb drag in one slide, so scrubbing the tracks list is cheap.
    #
    # CAVEAT: this theme only styles a Scrollbar that actually EXISTS. Flet wraps
    # a scrollable in its own Scrollbar only when the control's `scroll` prop is
    # set (flet ScrollableControl.build); otherwise the bare list falls to
    # Flutter's MaterialScrollBehavior, which adds a Scrollbar on desktop but NOT
    # on Android/iOS. So every ListView that needs a visible bar on the phone must
    # also pass scroll=ft.ScrollMode.ALWAYS — the theme alone is not enough.
    scrollbar_theme = ft.ScrollbarTheme(
        thumb_visibility=True,
        interactive=True,
        thickness=5,
        radius=4,
        main_axis_margin=4,
        cross_axis_margin=2,
        thumb_color=apply_opacity(0.55, DIM),
    )
    
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
    nav_bar_theme = ft.NavigationBarTheme(
        bgcolor=SURFACE,
        indicator_color="transparent",
        elevation=0,
        label_text_style=ft.TextStyle(size=11, weight=ft.FontWeight.W_500),
    )
    if os.path.exists(font_path):
        page.fonts = {"Outfit": font_path}
        page.theme = ft.Theme(
            font_family="Outfit",
            scrollbar_theme=scrollbar_theme,
            navigation_bar_theme=nav_bar_theme,
        )
    else:
        logger.warning("Font asset missing, using system default")
        page.theme = ft.Theme(
            scrollbar_theme=scrollbar_theme,
            navigation_bar_theme=nav_bar_theme,
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

