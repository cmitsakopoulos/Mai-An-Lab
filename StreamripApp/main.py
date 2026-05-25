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

# ─── Design tokens (mirrors style.kv) ─────────────────────────────────────────
BG       = "#08080A"
SURFACE  = "#0D0D12"
SURFACE2 = "#111116"

try:
    _cfg = load_config()
    _accent_hex = _cfg.get("appearance", {}).get("accent_color", "#00BFFF")
except Exception:
    _accent_hex = "#00BFFF"

CYAN     = _accent_hex
TEXT     = "#FFFFFF"
DIM      = "#A0A0A0"
BORDER   = "#262626"

SOURCE_COLORS = {
    "qobuz":      "#00E5FF",
    "tidal":      "#0088FF",
    "deezer":     "#CC00FF",
    "soundcloud": "#FF5500",
}

def apply_opacity(opacity: float, hex_color: str) -> str:
    if hex_color == "white": hex_color = "#FFFFFF"
    if hex_color.startswith("#"):
        # Convert hex to ARGB (Flet format) or RGBA-like string
        # Actually, if we use #RRGGBB, opacity can be prepended as #AARRGGBB
        alpha = int(opacity * 255)
        return f"#{alpha:02X}{hex_color[1:]}"
    return hex_color



LIB_ARTIST_COLOR   = "#CC00FF"
LIB_ALBUM_COLOR    = "#00E3FF"
LIB_TRACK_COLOR    = "#D4B038"
LIB_PLAYLIST_COLOR = "#9B59B6"
LIB_PARTITION_COLOR = "#00FF88"

# In-memory artwork path cache: {url-or-path-hash → local tmp path}
class ArtworkCache:
    def __init__(self, max_size=50):
        self._cache = {}
        self._access_order = []
        self._max_size = max_size
        self._lock = threading.Lock()
        
    def get(self, key: str) -> str | None:
        with self._lock:
            if key in self._cache:
                self._access_order.remove(key)
                self._access_order.append(key)
                return self._cache[key]
            return None
        
    def put(self, key: str, path: str):
        with self._lock:
            if key in self._cache:
                self._access_order.remove(key)
            elif len(self._cache) >= self._max_size:
                oldest = self._access_order.pop(0)
                evicted_path = self._cache[oldest]
                del self._cache[oldest]
                try:
                    if evicted_path and os.path.exists(evicted_path):
                        os.remove(evicted_path)
                except Exception as exc:
                    logger.warning("Failed to delete evicted artwork: %s", exc)
            
            self._cache[key] = path
            self._access_order.append(key)
        
    def clear(self):
        with self._lock:
            for path in self._cache.values():
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
            self._cache.clear()
            self._access_order.clear()

_ARTWORK_CACHE = ArtworkCache(max_size=50)

def src_color(source: str) -> str:
    return SOURCE_COLORS.get((source or "").lower(), "#FFFFFF")

def fmt_time(s: float) -> str:
    m, s = divmod(int(s), 60)
    return f"{m}:{s:02d}"

def pick_folder(title="Select Folder") -> str | None:
    """Native folder picker fallback for desktop platforms."""
    system = platform.system()
    
    if system == "Darwin":  # macOS
        script = f'''
        tell application "System Events" to activate
        set f to choose folder with prompt "{title}"
        return POSIX path of f
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return result.stdout.strip() or None
        except Exception:
            pass
        return None
        
    elif system == "Linux":
        # Try zenity first, then kdialog
        for cmd in [
            ["zenity", "--file-selection", "--directory", f"--title={title}"],
            ["kdialog", "--getexistingdirectory", "."],
        ]:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    return result.stdout.strip() or None
            except Exception:
                continue
        return None
        
    return None # Fallback to Flet FilePicker for Windows/Mobile

def strip_markup(text: str) -> str:
    """Remove Kivy-style [b]…[/b] markup tags that streamrip_search pre-computes."""
    return re.sub(r"\[/?[^\]]*\]", "", str(text))

class NotificationSystem:
    def __init__(self, app: "StreamripFletApp"):
        self.app = app
        self.page = app.page
        self._initialized = False
        self.container = None
        self.wrapper = None
        self._active_notifications = []

    def _ensure_initialized(self):
        if self._initialized:
            return
        self.container = ft.Column(
            tight=True,
            spacing=10,
            width=320,
        )
        self.wrapper = ft.Container(
            content=self.container,
            top=40,
            right=20,
        )
        self.page.overlay.append(self.wrapper)
        self._initialized = True

    def show(self, text: str, icon=ft.Icons.NOTIFICATIONS_ROUNDED, color=CYAN):
        # Don't try to animate UI elements into a suspended/hidden client;
        # it causes buffer back-pressure and spurious 120 Hz wakeups.
        if self.app.is_background:
            return
        self._ensure_initialized()

        # Limit to at most 3 active notifications
        while len(self._active_notifications) >= 3:
            oldest = self._active_notifications.pop(0)
            if oldest.data and callable(oldest.data):
                oldest.data()

        # Create a sleek notification card
        notification = ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Icon(icon, color=color, size=20),
                        bgcolor=apply_opacity(0.1, color),
                        padding=10,
                        border_radius=8,
                    ),
                    ft.Text(
                        text, 
                        color=TEXT, 
                        size=13, 
                        weight=ft.FontWeight.W_500, 
                        expand=True,
                        no_wrap=False,
                        max_lines=3,
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=SURFACE2,
            border=ft.Border.all(1, apply_opacity(0.1, TEXT)),
            border_radius=12,
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            shadow=ft.BoxShadow(
                blur_radius=20,
                color=apply_opacity(0.3, BG),
                offset=ft.Offset(0, 10),
            ),
            animate_opacity=300,
            opacity=0,
            offset=ft.Offset(0.3, 0),
            animate_offset=ft.Animation(400, ft.AnimationCurve.DECELERATE),
        )

        dismissed = [False]

        def _do_dismiss_immediate():
            if dismissed[0]: return
            dismissed[0] = True
            if dismissible in self._active_notifications:
                self._active_notifications.remove(dismissible)
            def _remove():
                if dismissible in self.container.controls:
                    self.container.controls.remove(dismissible)
            self.app.safe_update(_remove)

        def _do_dismiss():
            if dismissed[0]: return
            dismissed[0] = True
            if dismissible in self._active_notifications:
                self._active_notifications.remove(dismissible)
            # If the app is in the background we can't drive animations;
            # skip straight to an immediate removal instead.
            if self.app.is_background:
                _do_dismiss_immediate()
                return
            def _fade_out():
                notification.opacity = 0
                notification.offset = ft.Offset(0.3, 0)
            self.app.safe_update(_fade_out)

            async def _remove_after():
                await asyncio.sleep(0.4)
                def _remove():
                    if dismissible in self.container.controls:
                        self.container.controls.remove(dismissible)
                self.app.safe_update(_remove)
            asyncio.create_task(_remove_after())

        dismissible = ft.Dismissible(
            content=notification,
            dismiss_direction=ft.DismissDirection.HORIZONTAL,
            on_dismiss=lambda e: _do_dismiss_immediate(),
        )
        dismissible.data = _do_dismiss
        self._active_notifications.append(dismissible)

        def _add():
            self.container.controls.insert(0, dismissible)
            # Trigger slide-in animation in the same update as the insertion.
            # Do NOT call page.update() twice; the second bare call was the
            # source of spurious 120 Hz updates during notification display.
            notification.opacity = 1
            notification.offset = ft.Offset(0, 0)

        self.app.safe_update(_add)

        async def _dismiss():
            await asyncio.sleep(4)
            _do_dismiss()

        notification.on_click = lambda _: _do_dismiss()

        asyncio.create_task(_dismiss())

# ─── High Fidelity UI Components ──────────────────────────────────────────────


# NOTE: An MCL-based AutoPlaylistEngine used to live here. It was removed
# when playlist generation moved to the KNN selector in
# `utils/auto_playlist.py` — KNN over the hot-set's weighted feature
# vectors gives the same coherent groupings without the matrix-power
# convergence loop or the artist/album string-similarity blending, and
# loads on demand instead of at app start. See git history if you need
# the old implementation.


class AnimatedEntry(ft.Container):
    def __init__(self, content, target_height=56, depth=0, **kwargs):
        super().__init__(
            content=content,
            height=target_height,
            opacity=1.0,
            animate=ft.Animation(200, ft.AnimationCurve.EASE_OUT_EXPO),
            animate_opacity=ft.Animation(200, ft.AnimationCurve.EASE_OUT_EXPO),
            **kwargs
        )
        self.target_height = target_height
        self.depth = depth

    def hide(self):
        """Trigger the slide-out animation."""
        self.height = 0
        self.opacity = 0
        self.update()

class ScaleButton(ft.GestureDetector):
    """Wraps content to provide 0.96 scale-down feedback on tap."""
    def __init__(self, content, on_tap=None, scale_to=0.96, **kwargs):
        # The container that will be scaled
        self._inner = ft.Container(
            content=content,
            scale=ft.Scale(1.0),
            animate_scale=ft.Animation(50, ft.AnimationCurve.EASE_OUT_QUAD),
            expand_loose=False,   # prevent size collapsing
        )
        super().__init__(
            content=self._inner,
            **kwargs
        )
        self.on_tap = on_tap
        self.scale_to = scale_to
        self.on_tap_down = self._press
        self.on_tap_up = self._release
        self.on_tap_cancel = self._release

    def _press(self, e):
        self._inner.scale = ft.Scale(self.scale_to)
        if self.page:
            self.page.update()

    def _release(self, e):
        self._inner.scale = ft.Scale(1.0)
        if self.page:
            self.page.update()




class OnyxButton(ScaleButton):
    def __init__(self, text: str, icon: str = None, on_tap=None, height=50, **kwargs):
        content_row = ft.Row(
            [
                ft.Icon(icon, color=BG, size=20) if icon else ft.Container(),
                ft.Text(text, color=BG, weight=ft.FontWeight.W_700, size=14),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=10,
        )
        super().__init__(
            content=ft.Container(
                content=content_row,
                bgcolor=CYAN,
                height=height,
                border_radius=12,
                alignment=ft.Alignment(0, 0),
            ),
            on_tap=on_tap,
            **kwargs
        )

class GlassCard(ft.Container):
    def __init__(self, content, **kwargs):
        super().__init__(
            content=content,
            bgcolor="#0DFFFFFF",
            border_radius=16,
            padding=20,
            border=ft.Border.all(1, "#1AFFFFFF"),
            **kwargs
        )

class MenuTextItem(ft.Container):
    def __init__(self, text: str, on_click=None, icon: str = None):
        super().__init__(
            content=ft.Row([
                ft.Icon(icon, color=DIM, size=20) if icon else ft.Container(),
                ft.Text(text, color=TEXT, size=14),
            ], spacing=12),
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            on_click=on_click,
        )

class AppSearchBar(ft.Container):
    def __init__(self, hint: str, on_submit=None, on_change=None, on_clear=None):
        self._input = ft.TextField(
            hint_text=hint,
            hint_style=ft.TextStyle(color=DIM),
            text_style=ft.TextStyle(color=TEXT),
            border=ft.InputBorder.NONE,
            on_submit=on_submit,
            on_change=on_change,
            expand=True,
            content_padding=ft.Padding.only(left=10, right=10),
        )
        self._clear_btn = ft.IconButton(
            icon=ft.Icons.CLOSE, icon_color=DIM, icon_size=18,
            on_click=lambda _: self._clear(on_clear),
            visible=False
        )
        super().__init__(
            content=ft.Row([
                ft.Icon(ft.Icons.SEARCH, color=CYAN, size=20),
                self._input,
                self._clear_btn,
            ], spacing=0),
            bgcolor=SURFACE2,
            border_radius=12,
            padding=ft.Padding.symmetric(horizontal=12),
            border=ft.Border.all(1, BORDER),
            animate=ft.Animation(200, ft.AnimationCurve.EASE_OUT),
        )
    def _clear(self, callback):
        self._input.value = ""
        self._clear_btn.visible = False
        self.update()
        if callback: callback()
    @property
    def value(self): return self._input.value
    @value.setter
    def value(self, val): 
        self._input.value = val
        self._clear_btn.visible = bool(val)

class SourceSegment(ScaleButton):
    def __init__(self, text: str, selected=False, on_tap=None, **kwargs):
        self.selected = selected
        self.text_control = ft.Text(
            text.upper(), color=BG if selected else TEXT, weight=ft.FontWeight.W_700, size=11
        )
        super().__init__(
            content=ft.Container(
                content=self.text_control,
                bgcolor=CYAN if selected else "transparent",
                border=ft.Border.all(1, CYAN if selected else BORDER),
                border_radius=8,
                padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                alignment=ft.Alignment(0, 0),
            ),
            on_tap=on_tap,
            **kwargs
        )
    def update_state(self, selected: bool):
        self.selected = selected
        self.content.bgcolor = CYAN if selected else "transparent"
        self.content.border = ft.Border.all(1, CYAN if selected else BORDER)
        self.text_control.color = BG if selected else TEXT
        self.update()

class SettingsHeader(ft.Row):
    def __init__(self, title: str, on_back=None):
        super().__init__(
            controls=[
                ft.IconButton(icon=ft.Icons.ARROW_BACK, icon_color=CYAN, on_click=on_back),
                ft.Text(title, size=24, weight=ft.FontWeight.W_700, color=TEXT),
            ],
            spacing=12,
        )

class HubSettingItem(ScaleButton):
    def __init__(self, icon: str, title: str, subtitle: str, on_tap=None):
        super().__init__(
            content=ft.Container(
                content=ft.Row([
                    ft.Icon(icon, color=CYAN, size=22),
                    ft.Column([
                        ft.Text(title, color=TEXT, size=15, weight=ft.FontWeight.W_700),
                        ft.Text(subtitle, color=DIM, size=12),
                    ], spacing=2, expand=True),
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, color=DIM, opacity=0.25, size=18),
                ], spacing=16),
                bgcolor="#0DFFFFFF",
                padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                border_radius=14,
            ),
            on_tap=on_tap,
        )

class AccordionCard(ft.Column):
    def __init__(self, icon: str, title: str, subtitle: str, content_controls: list,
                 header_actions: list | None = None, initially_open: bool = False,
                 on_toggle: callable = None):
        self.is_open = initially_open
        self.on_toggle = on_toggle
        self.content_area = ft.Container(
            content=ft.Column(content_controls, spacing=6),
            visible=initially_open,
            padding=ft.Padding.only(left=16, right=16, bottom=14, top=4),
        )
        self.chevron = ft.Icon(
            ft.Icons.KEYBOARD_ARROW_DOWN if initially_open else ft.Icons.CHEVRON_RIGHT,
            color=DIM, opacity=0.35, size=18
        )
        # Click target is the toggle_zone only; header_actions live outside it
        # so clicking edit/delete doesn't also toggle the accordion.
        toggle_zone = ft.Container(
            content=ft.Row([
                ft.Icon(icon, color=CYAN, size=22),
                ft.Column([
                    ft.Text(title, color=TEXT, size=15, weight=ft.FontWeight.W_700),
                    ft.Text(subtitle, color=DIM, size=12),
                ], spacing=2, expand=True),
                self.chevron,
            ], spacing=16),
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            on_click=self.toggle,
            expand=True,
        )
        if header_actions:
            header = ft.Row(
                [toggle_zone, ft.Container(
                    content=ft.Row(header_actions, spacing=2, tight=True),
                    padding=ft.Padding.only(right=8),
                )],
                spacing=0,
            )
        else:
            header = toggle_zone
        super().__init__(
            controls=[ft.Container(content=ft.Column([header, self.content_area], spacing=0), bgcolor="#0DFFFFFF", border_radius=14)],
            spacing=0,
        )
    def toggle(self, _e):
        self.is_open = not self.is_open
        self.content_area.visible = self.is_open
        self.chevron.icon = ft.Icons.KEYBOARD_ARROW_DOWN if self.is_open else ft.Icons.CHEVRON_RIGHT
        if self.on_toggle:
            self.on_toggle(self.is_open)
        self.update()

# AnimatedLibraryNode removed; library uses flat row list rebuilt by load_library()


# ─── Download Queue Controller ─────────────────────────────────────────────────
class JobCancelledException(Exception):
    pass


class QueueController:
    def __init__(self, app: "StreamripFletApp"):
        self.app = app
        self._queue = asyncio.Queue()
        self._pending_items = [] 
        self.is_processing = False
        self._cancel_event = asyncio.Event()
        self.current_job: dict | None = None
        self._job_lock = asyncio.Lock()
        self._status_chips: list[ft.Control] = []
        self._worker_task: asyncio.Task | None = None

    @property
    def download_queue(self) -> list[dict]:
        """Compatibility property for UI rendering."""
        return self._pending_items

    # ── quality resolution ──────────────────────────────────────────────────
    def _quality_int(self, source: str, tier: str) -> int | None:
        src = source.lower()
        if tier == "mp3":   return 1
        if tier == "cd":    return 2
        if tier == "hires":
            if src == "qobuz":  return 4
            if src == "tidal":  return 3
            if src == "deezer": return 2
        return None

    # ── public API ──────────────────────────────────────────────────────────
    def enqueue(self, item_data: dict, quality_tier: str = "mp3"):
        source     = item_data.get("source", "qobuz")
        media_type = item_data.get("media_type", "track")
        item_id    = item_data.get("id", "")
        url        = item_data.get("url") or f"https://www.{source}.com/{media_type}/{item_id}"

        meta = item_data.copy()
        meta["quality"]       = self._quality_int(source, quality_tier)
        meta["quality_label"] = quality_tier.upper()

        chip = ft.Container(
            width=12, height=12,
            border_radius=6,
            bgcolor=DIM + "44",
            animate_opacity=300,
        )
        self._status_chips.append(chip)
        job = {"url": url, "metadata": meta, "chip": chip}
        
        self._pending_items.append(job)
        self._queue.put_nowait(job)
        
        self.app.search_view.update_chips(self._status_chips)
        self.app.refresh_queue_ui()

        if not self.is_processing:
            if not self._worker_task or self._worker_task.done():
                self._worker_task = asyncio.create_task(self._worker_loop())
        else:
            self.app.show_snackbar(f"Added to queue ({quality_tier.upper()}).")

    def clear(self):
        count = len(self._pending_items)
        # Drain queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

        self._pending_items.clear()
        self._status_chips.clear()
        self._cancel_event.set()
        
        self.app.refresh_queue_ui()
        self.app.search_view.update_chips([])
        self.app.search_view.hide_progress_card()
        self.app.show_snackbar(f"Queue cleared ({count} items).")

    def cancel_current(self):
        self._cancel_event.set()
        self.app.show_snackbar("Cancellation requested…")

    async def _worker_loop(self):
        self.is_processing = True
        while not self._queue.empty():
            self.current_job = await self._queue.get()
            if self.current_job in self._pending_items:
                self._pending_items.remove(self.current_job)
            
            self._cancel_event.clear()
            
            chip = self.current_job.get("chip")
            if chip: chip.bgcolor = CYAN
                
            self.app.refresh_queue_ui()
            self.app.search_view.show_progress_card()

            await self._workflow()
            
            self._queue.task_done()
            
            # Transition delay
            delay = 1 if self._cancel_event.is_set() else 3
            await asyncio.sleep(delay)
            self._ui(self.app.search_view.hide_progress_card)
            self.current_job = None
            self.app.refresh_queue_ui()

        self.is_processing = False
        self._worker_task = None

    # ── background workflow ─────────────────────────────────────────────────
    async def _workflow(self):
        url      = self.current_job.get("url")
        metadata = self.current_job.get("metadata", {})
        target   = self.app.target_folder or get_default_download_path()
        last_update = [0.0]

        def progress_hook(data):
            now = time.time()
            pct = data.get("percent")
            if pct is None or pct >= 100 or (now - last_update[0] > 0.25):
                last_update[0] = now
                status  = data.get("status", "")
                message = data.get("message", "")
                self._ui(lambda s=status, p=pct, m=message:
                         self.app.search_view.update_progress(s.capitalize(), p, m))

        try:
            await asyncio.to_thread(os.makedirs, target, exist_ok=True)
            for attempt in range(3):
                try:
                    async with self._job_lock:
                        if self._cancel_event.is_set():
                            raise JobCancelledException()
                    self._ui(lambda a=attempt: self.app.search_view.update_progress(
                        "Initializing…", 5, f"Connecting to Qobuz API… (Attempt {a + 1})"))
                    
                    await download(
                        url, target,
                        progress_callback=progress_hook,
                        quality=metadata.get("quality"),
                        stop_event=self._cancel_event,
                    )
                    break
                except JobCancelledException:
                    raise
                except Exception as exc:
                    if attempt < 2:
                        wait = 5 * (2 ** attempt)
                        self._ui(lambda w=wait, e=str(exc): self.app.search_view.update_progress(
                            "Retrying", 0, f"Error: {e}. Retrying in {w}s…"))
                        for _ in range(wait * 2):
                            if self._cancel_event.is_set():
                                raise JobCancelledException()
                            await asyncio.sleep(0.5)
                    else:
                        raise Exception(f"Failed after 3 attempts: {exc}") from exc

            chip = self.current_job.get("chip")
            if chip: chip.bgcolor = "#4CAF50" # Green
            
            self._ui(lambda: self.app.search_view.update_progress(
                "Finished", 100, "Download completed successfully!"))

            # Automatically trigger library scan ~1s after download completes to import the new song
            if hasattr(self.app, "library_view") and self.app.library_view:
                async def _deferred_scan():
                    await asyncio.sleep(1.0)
                    self.app.library_view.start_scan()
                asyncio.create_task(_deferred_scan())

        except JobCancelledException:
            self._ui(lambda: self.app.search_view.update_progress(
                "Cancelled", 0, "Download aborted by user."))
            chip = self.current_job.get("chip")
            if chip: chip.bgcolor = "#F44336" # Red
        except Exception as exc:
            self._ui(lambda e=exc: self.app.search_view.update_progress(
                "Failed", 0, str(e)))
            chip = self.current_job.get("chip")
            if chip: chip.bgcolor = "#F44336"

    def _ui(self, fn):
        try:
            self.app.safe_update(fn)
        except Exception:
            logger.exception("UI update error")


# ─── Skeleton Pulse Component ──────────────────────────────────────────────────
class SkeletonRow(ft.Container):
    def __init__(self, delay: float = 0):
        # The delay argument can be ignored; Shimmer handles its own render lifecycle
        super().__init__(
            content=ft.Shimmer(
                base_color=apply_opacity(0.15, DIM),
                highlight_color=apply_opacity(0.4, CYAN),
                content=ft.Row(
                    [
                        ft.Container(width=52, height=52, bgcolor=SURFACE2, border_radius=10),
                        ft.Column(
                            [
                                ft.Container(width=200, height=13, bgcolor=SURFACE2, border_radius=6),
                                ft.Container(width=140, height=11, bgcolor=SURFACE2, border_radius=6),
                                ft.Container(width=90,  height=9,  bgcolor=SURFACE2, border_radius=6),
                            ],
                            spacing=6,
                            expand=True,
                            tight=True,
                        ),
                    ],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ),
            bgcolor=SURFACE,
            border_radius=12,
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            height=64,
        )

# ─── Search View ───────────────────────────────────────────────────────────────
class SearchView:
    def __init__(self, app: "StreamripFletApp"):
        from utils.streamrip_search import StreamripSearcher
        self.app             = app
        self.page            = app.page
        self.searcher        = StreamripSearcher()
        self.current_search_id = 0
        self.selected_source = "qobuz"
        # Unified pre-fetch cache: all three types are fetched in one search call.
        # Keyed by media_type singular ("track", "album", "artist").
        self.cached_results: dict[str, list[dict]] = {"track": [], "album": [], "artist": []}
        self._active_preview_data: dict | None = None
        self.expanded_nodes: set[str] = set() # Track IDs/Artist IDs of expanded items
        self.node_cache: dict[str, list[dict]] = {} # Cache for expanded node children
        self.view_mode = "tracks" # artist, album, track (plural, matches tab labels)

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

        # pending queue list
        self._pending_list = ft.ListView(spacing=6, padding=ft.Padding.symmetric(horizontal=12))

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

        # ── Up Next Container ──
        self._up_next_container = ft.Container(
            content=ft.Column([
                ft.Text("  Up Next", color=DIM, size=11, weight=ft.FontWeight.W_700),
                self._pending_list,
            ], spacing=4),
            visible=False,
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
                                            ft.Text("Qobuz", size=14, color=CYAN, weight=ft.FontWeight.W_500),
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
                
                # History / Up Next Section
                ft.Container(
                    content=ft.Column(
                        [
                            self._up_next_container,
                        ],
                        spacing=0,
                    ),
                ),
            ],
            expand=True,
            spacing=0,
        )


    def try_update(self, *controls):
        for c in controls:
            try: c.update()
            except: pass

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
        # Without this, an in-flight callback that resolves *after* the user
        # taps X will repopulate cached_results and the list — effectively
        # re-running the search the user just dismissed.
        self.current_search_id += 1

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

        # Skeleton rows
        cards = [SkeletonRow(delay=i * 0.08) for i in range(8)]
        def _mutate_start():
            self._results_list.controls = cards
            self._results_list.opacity = 1.0
            self._results_list.offset = ft.Offset(0, 0)
        self.app.safe_update(_mutate_start)

        def results_callback(results):
            if self.current_search_id == search_id:
                self._on_results(results)

        # Always fetch all three types at once. The concurrent gather in the
        # searcher means this costs no more latency than fetching a single type.
        asyncio.create_task(asyncio.to_thread(
            self.searcher.search,
            query, self.selected_source, results_callback,
            media_types=["track", "album", "artist"],
            limit=250, offset=0
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
        # Always render from the local typed cache for the active tab
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
            # Re-render instantly from the local pre-fetched cache; zero network.
            # Only trigger a fresh search if the cache is entirely empty (first load
            # for this query somehow missed all types, which should not happen).
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
        
        # Highlight if currently playing
        expected_preview_title = f"(Preview) {title}"
        is_playing = (
            (audio_engine.current_track == title or audio_engine.current_track == expected_preview_title)
            and audio_engine.current_artist == subtitle
        )

        # In Library Awareness
        is_in_library = r.get("is_in_library", False)
        download_icon = ft.Icons.CHECK_CIRCLE if is_in_library else ft.Icons.DOWNLOAD_OUTLINED
        download_color = CYAN if is_in_library else DIM
             
        # Expand Icon for Artists/Albums
        expand_icon = ft.Icon(
            ft.Icons.KEYBOARD_ARROW_DOWN if is_expanded else ft.Icons.KEYBOARD_ARROW_RIGHT,
            color=accent if is_expanded else DIM,
            size=20,
        ) if m_type in ("artist", "album") else None

        _prev_state = r.get("preview_state", "idle")
        _prev_icon  = ft.Icons.PAUSE_CIRCLE if _prev_state == "playing" else (
                      ft.Icons.SYNC if _prev_state == "loading" else ft.Icons.PLAY_CIRCLE_OUTLINE)
        
        # --- Handlers defined before usage ---
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
        
        # Find index in flat list
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
                # Replace expansion icon with a spinner for feedback
                tile_ctrl.trailing.controls[2] = ft.Container(
                    content=ft.ProgressRing(width=16, height=16, stroke_width=2, color=accent),
                    width=20, height=20, alignment=ft.Alignment(0, 0)
                )
            self.app.page.update()

            def children_callback(children):
                self.node_cache[node_id] = children
                if node_id not in self.expanded_nodes: return
                async def _process():
                    # Process and insert children...
                    # (Logic remains same, just ensuring we swap the icon back)
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
                            # Swap spinner back to down arrow
                            if isinstance(tile_ctrl.trailing, ft.Row):
                                tile_ctrl.trailing.controls[2] = ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN, color=accent, size=20)
                            
                            # Re-find index
                            curr_idx = -1
                            for j, entry in enumerate(parent_list):
                                if getattr(entry, "content", None) == tile_ctrl:
                                    curr_idx = j; break
                            if curr_idx == -1: return

                            # Get current depth
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
                # Ensure we replace the control entirely to avoid type mismatch (Container vs Icon)
                tile_ctrl.trailing.controls[2] = ft.Icon(ft.Icons.KEYBOARD_ARROW_RIGHT, color=DIM, size=20)
            
            # Remove children with greater depth
            depth = 0
            if isinstance(tile_ctrl.leading, ft.Row):
                depth = int(tile_ctrl.leading.controls[0].width / 20)
            
            idx = node_idx + 1
            while idx < len(parent_list):
                child_entry = parent_list[idx]
                if not isinstance(child_entry, AnimatedEntry): break
                child_depth = getattr(child_entry, "depth", 0)
                if child_depth > depth:
                    # Also discard child's expanded state if any
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

            # Library Aesthetic: Use bgcolor
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
            
            # Update Icon
            _prev_icon = ft.Icons.PAUSE_CIRCLE if state == "playing" else (
                         ft.Icons.SYNC if state == "loading" else ft.Icons.PLAY_CIRCLE_OUTLINE)
            
            if isinstance(card.trailing, ft.Row) and card.trailing.controls:
                # preview_btn is at index 0 for tracks
                if m_type == "track":
                    p_btn = card.trailing.controls[0]
                    if isinstance(p_btn, ft.Container):
                        if state == "loading":
                            p_btn.content = ft.ProgressRing(width=16, height=16, stroke_width=2, color=CYAN)
                        else:
                            p_btn.content = ft.Icon(_prev_icon, color=CYAN if state != "idle" else DIM, size=20)
                
                # expand_icon is at index 2 (or last)
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

    # ── preview ─────────────────────────────────────────────────────────────
    def _start_preview(self, index: int, data: dict, icon_ctrl: ft.Icon, container_ctrl: ft.Container):
        async def _worker():
            try:
                from utils.streamrip_api import download as _do_download
                url       = data.get("url", "")
                title     = re.sub(r"\[.*?\]", "", data.get("ui_title", data.get("name", ""))).strip()
                safe_name = "".join(c if c.isalnum() else "_" for c in title[:20])
                pdir      = os.path.join(get_app_dir(), "previews", f"{index}_{safe_name}")
                await asyncio.to_thread(os.makedirs, pdir, exist_ok=True)

                audio_file = await self._find_audio(pdir)
                if not audio_file and url:
                    # STRUCTURAL FIX: Offload the blocking download to a background thread.
                    # This prevents Android's Flet engine from timing out and aborting the socket.
                    if asyncio.iscoroutinefunction(_do_download):
                        await _do_download(url, pdir, quality=1) 
                    else:
                        await asyncio.to_thread(_do_download, url, pdir, quality=1)
                    
                    audio_file = await self._find_audio(pdir)

                if audio_file:
                    meta = {
                        "path":         audio_file,
                        "track_title":  f"(Preview) {title}",
                        "artist_name":  data.get("ui_subtitle", data.get("artist", "")),
                        "album_title":  "Streamrip Search",
                        "image_url":    data.get("image_url", data.get("image", "")),
                    }
                    def _play_success():
                        data["preview_state"] = "playing"
                        icon_ctrl.content = ft.Icon(ft.Icons.STOP_CIRCLE_OUTLINED, color=CYAN, size=20)
                        container_ctrl.shadow = ft.BoxShadow(blur_radius=8, color=apply_opacity(0.15, CYAN))
                        audio_engine.jarvis_controlled = False
                        audio_engine.set_queue([meta], start_index=0)
                        self.app.show_snackbar(f"Playing preview: {title}")
                        icon_ctrl.update()
                        container_ctrl.update()
                        
                    self.app.safe_update(_play_success)
                else:
                    # Detailed diagnostic for the "not found" error
                    files_found = os.listdir(pdir) if os.path.exists(pdir) else "Directory Missing"
                    logger.error("Audio not found in %s. Found instead: %s", pdir, files_found)
                    raise Exception(f"Audio not found. Content: {files_found}")

            except Exception as exc:
                logger.error("Preview failed: %s", exc)
                _exc = exc  # rebind: Python 3 deletes 'exc' when the except block exits
                def _play_fail():
                    data["preview_state"] = "idle"
                    icon_ctrl.content = ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINE, color=DIM, size=20)
                    container_ctrl.shadow = ft.BoxShadow(blur_radius=0, color=ft.Colors.TRANSPARENT, spread_radius=0)
                    self.app.show_snackbar("Preview failed.")
                    self.app._show_error(_exc)
                    icon_ctrl.update()
                    container_ctrl.update()
                self.app.safe_update(_play_fail)

        asyncio.create_task(_worker())

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
                await asyncio.sleep(0.5) # Wait for filesystem to settle
        return None

    # ── progress card ────────────────────────────────────────────────────────
    def show_progress_card(self):
        def _mutate():
            self._progress_status.value    = "Connecting…"
            self._progress_pct.value       = ""
            self._progress_detail.value    = ""
            self._progress_bar.value       = None   # indeterminate until first real update
            self._progress_spinner.visible = True
            self._progress_card.visible    = True
            self._progress_card.opacity    = 1
            self._progress_card.offset     = ft.Offset(0, 0)
        self.app.safe_update(_mutate)

    def hide_progress_card(self):
        def _mutate():
            self._progress_spinner.visible = False
            self._progress_card.opacity    = 0
            self._progress_card.offset     = ft.Offset(0, 0.4)
        self.app.safe_update(_mutate)
        async def _delayed_hide():
            await asyncio.sleep(0.3)
            self._hide_card_done()
        asyncio.create_task(_delayed_hide())

    def _hide_card_done(self):
        def _mutate():
            self._progress_card.visible = False
            self._progress_bar.value    = 0
        self.app.safe_update(_mutate)

    def update_progress(self, status: str, pct: float | None, detail: str = ""):
        # Improved stage-based display
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
            active_job = self.app.queue.current_job
            self._pending_list.controls = [
                self._pending_card(item, is_active=(item == active_job))
                for item in queue
            ]
            # Dynamically show/hide "Up Next" container
            self._up_next_container.visible = bool(queue)
        self.app.safe_update(_mutate)

    def _pending_card(self, item: dict, is_active: bool = False) -> ft.Control:
        meta   = item.get("metadata", {})
        source = meta.get("source", "qobuz")
        title  = meta.get("name", "Unknown")
        artist = meta.get("artist", "Unknown Artist")
        qlabel = meta.get("quality_label", "MP3")
        scolor = src_color(source)

        card = ft.Container(
            content=ft.Row(
                [
                    # Placeholder Circle
                    ft.Container(
                        content=ft.Icon(ft.Icons.MUSIC_NOTE, color=scolor, size=24),
                        width=56, height=56,
                        bgcolor=apply_opacity(0.15, scolor),
                        shape=ft.BoxShape.CIRCLE,
                        alignment=ft.Alignment(0, 0),
                    ),
                    # Text content
                    ft.Column(
                        [
                            ft.Text(title, color=TEXT, size=15, weight=ft.FontWeight.W_700,
                                    overflow=ft.TextOverflow.ELLIPSIS, max_lines=1),
                            ft.Text(artist, color=DIM, size=13,
                                    overflow=ft.TextOverflow.ELLIPSIS, max_lines=1),
                            ft.Text("PENDING", color=CYAN, size=10, weight=ft.FontWeight.W_700,
                                    opacity=0.7),
                        ],
                        spacing=1,
                        expand=True,
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                    # Quality tag
                    ft.Container(
                        content=ft.Text(qlabel, color=BG, size=9, weight=ft.FontWeight.W_700),
                        bgcolor=CYAN,
                        padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                        border_radius=4,
                    ),
                ],
                spacing=16,
            ),
            height=72,
            bgcolor=apply_opacity(0.04, "#FFFFFF") if is_active else SURFACE,
            border=ft.Border.all(1, apply_opacity(0.3, CYAN) if is_active else BORDER),
            border_radius=12,
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            # Glow effect for active item
            shadow=ft.BoxShadow(
                spread_radius=1, blur_radius=8,
                color=apply_opacity(0.15, CYAN),
            ) if is_active else None,
        )
        return AnimatedEntry(card, target_height=72)

    def refresh_now_playing(self):
        """Update shadows on all visible cards to reflect the currently playing track."""
        # This is a lightweight refresh that doesn't rebuild the controls
        self.refresh_results_only()



    def _remove_history_item(self, item: dict):
        pass


# ─── Library View ──────────────────────────────────────────────────────────────
class LibraryView:
    def try_update(self, *controls):
        for c in controls:
            if c is not None:
                try:
                    c.update()
                except Exception:
                    pass

    def __init__(self, app: "StreamripFletApp"):
        self.app            = app
        self.page           = app.page
        self.view_mode      = "tracks"
        # Resolve default sort from config
        try:
            cfg = load_config()
            self.sort_mode = cfg.get("general", {}).get("library_sort", "date")
        except:
            self.sort_mode = "date"

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

        # Animating carousel wrapper for the track list (optimized snappy transition)
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

        # Load SVD projection space and eigenvalues for dynamic raw feature mapping and variance calculations
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
        
        projection = None
        eigenvalues = None
        try:
            conn = await db.get_connection()
            async with conn.execute("SELECT projection, eigenvalues FROM pca_space WHERE id = 1") as cursor:
                row = await cursor.fetchone()
            
            # If not optimized or empty, compute SVD on-the-fly to get eigenvalues/projection
            if row is None or not row['eigenvalues']:
                logger.info("EQ dialog: PCA space empty, optimizing on-the-fly...")
                await tg.optimize_pca_spacing(db, tg.FEATURES_VERSION)
                async with conn.execute("SELECT projection, eigenvalues FROM pca_space WHERE id = 1") as cursor:
                    row = await cursor.fetchone()
            
            if row:
                import numpy as np
                projection = np.frombuffer(row['projection'], dtype=np.float32).reshape(8, 3)
                if row['eigenvalues']:
                    eigenvalues = np.frombuffer(row['eigenvalues'], dtype=np.float32)
        except Exception as exc:
            logger.exception("EQ dialog: failed to load SVD space for dynamic mapping: %s", exc)

        most_variance_text = ""
        least_variance_text = ""
        
        feature_pc = {f: 0 for f in raw_features}
        
        if projection is not None:
            import numpy as np
            overall_weights = []
            for feat_idx, feat in enumerate(raw_features):
                if eigenvalues is not None and len(eigenvalues) >= 3:
                    weight = float(
                        (projection[feat_idx, 0] ** 2) * eigenvalues[0] +
                        (projection[feat_idx, 1] ** 2) * eigenvalues[1] +
                        (projection[feat_idx, 2] ** 2) * eigenvalues[2]
                    )
                else:
                    weight = float(
                        (projection[feat_idx, 0] ** 2) +
                        (projection[feat_idx, 1] ** 2) +
                        (projection[feat_idx, 2] ** 2)
                    )
                overall_weights.append((feat, weight))
                
                # Determine dominant PC for this feature
                loadings = [abs(projection[feat_idx, 0]), abs(projection[feat_idx, 1]), abs(projection[feat_idx, 2])]
                feature_pc[feat] = int(np.argmax(loadings))
                
            sorted_overall = sorted(overall_weights, key=lambda x: x[1], reverse=True)
            most_variance_features = [friendly_names[x[0]] for x in sorted_overall[:3]]
            most_variance_text = ", ".join(most_variance_features)
            
            least_variance_features = [friendly_names[x[0]] for x in sorted_overall[-2:]]
            least_variance_text = ", ".join(least_variance_features)
            
            # Sort raw_features by descending variance contribution
            raw_features = [x[0] for x in sorted_overall]

        header_controls = []
        if most_variance_text and least_variance_text:
            header_controls.extend([
                ft.Text("Sonic Variance Profile", color=CYAN, size=11, weight=ft.FontWeight.BOLD),
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Text(f"Most Active: {most_variance_text}", color=TEXT, size=11, weight=ft.FontWeight.W_600),
                            ft.Text(f"Least Active: {least_variance_text}", color=DIM, size=10),
                        ],
                        spacing=2,
                    ),
                    bgcolor=apply_opacity(0.04, TEXT),
                    border_radius=8,
                    padding=10,
                    margin=ft.Margin.only(bottom=8),
                    border=ft.Border.all(1, apply_opacity(0.08, TEXT)),
                ),
                ft.Text("Feature Dimension Coding", color=CYAN, size=11, weight=ft.FontWeight.BOLD),
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Row(
                                [
                                    ft.Container(
                                        width=8,
                                        height=8,
                                        border_radius=4,
                                        bgcolor=ft.Colors.CYAN,
                                    ),
                                    ft.Text("Timbre", color=TEXT, size=10, weight=ft.FontWeight.W_600),
                                ],
                                spacing=4,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            ft.Row(
                                [
                                    ft.Container(
                                        width=8,
                                        height=8,
                                        border_radius=4,
                                        bgcolor=ft.Colors.PURPLE,
                                    ),
                                    ft.Text("Tempo", color=TEXT, size=10, weight=ft.FontWeight.W_600),
                                ],
                                spacing=4,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            ft.Row(
                                [
                                    ft.Container(
                                        width=8,
                                        height=8,
                                        border_radius=4,
                                        bgcolor=ft.Colors.AMBER_500,
                                    ),
                                    ft.Text("Harmonic", color=TEXT, size=10, weight=ft.FontWeight.W_600),
                                ],
                                spacing=4,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_AROUND,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bgcolor=apply_opacity(0.04, TEXT),
                    border_radius=8,
                    padding=10,
                    margin=ft.Margin.only(bottom=8),
                    border=ft.Border.all(1, apply_opacity(0.08, TEXT)),
                )
            ])

        def _weight_for(feature: str) -> float:
            if not definition:
                return 0.0
            entry = definition.get(feature)
            if not entry:
                return 0.0
            try:
                # Target stores the quartile (1.0 to 4.0), weight stores 1.0 (active) or 0.0 (neutral)
                target_val, weight_val = entry
                if weight_val == 0.0:
                    return 0.0
                return float(target_val)
            except (TypeError, ValueError, IndexError):
                return 0.0

        sliders: dict[str, ft.Slider] = {}
        
        pc_metadata = {
            0: {"label": "Timbre (PC1)", "color": ft.Colors.CYAN},
            1: {"label": "Tempo (PC2)", "color": ft.Colors.PURPLE},
            2: {"label": "Harmonic (PC3)", "color": ft.Colors.AMBER_500},
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
            pc_label = meta["label"]
            
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
                        # Explanatory subtext with text wrapping instead of technical code columns
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

        rows = []
        if header_controls:
            rows.extend(header_controls)
        rows.extend([_make_row(f) for f in raw_features])

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
            weights_to_save = {
                f: float(sliders[f].value) for f in raw_features
            }
            
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
            self.app.show_snackbar(f"EQ update failed: {exc}",
                                    icon=ft.Icons.ERROR_OUTLINE)
            return
        self.app.show_snackbar(
            f"EQ updated for {canonical_mood.capitalize()}.",
            icon=ft.Icons.CHECK_CIRCLE_OUTLINE,
        )
        # Refresh the partition view if the user is still viewing this mood.
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
            
            # Toggle feedback
            new_fb = 0 if is_liked else 1
            await db.save_mood_feedback(track_path, mood, new_fb)
            self._mood_feedback_map.setdefault(track_path, {})[mood] = new_fb
            
            feedback_str = "Like removed" if is_liked else "Track pinned to mood"
            
            # Optimistic UI update for the clicked button
            if btn:
                btn.icon = ft.Icons.THUMB_UP_OUTLINED if is_liked else ft.Icons.THUMB_UP_ROUNDED
                btn.icon_color = DIM if is_liked else CYAN
                self.try_update(btn)
            
            # Optimistic UI update for any duplicate controls of this track
            for ctrl in self._path_to_controls.get(track_path, []):
                try:
                    tile = ctrl.content.content
                    if isinstance(tile.trailing, ft.Row) and len(tile.trailing.controls) >= 2:
                        l_btn = tile.trailing.controls[0]
                        l_btn.icon = ft.Icons.THUMB_UP_OUTLINED if is_liked else ft.Icons.THUMB_UP_ROUNDED
                        l_btn.icon_color = DIM if is_liked else CYAN
                        self.try_update(l_btn)
                except Exception:
                    pass

            if not is_liked:
                # Run learning shift & random walk in background
                async def run_learning_and_walk():
                    try:
                        walk_tracks = await tg.walk(db, track_path, length=5, edge_kinds=(tg.KIND_ACOUSTIC,))
                        for wt in walk_tracks:
                            await db.save_mood_feedback(wt, mood, 1)
                            self._mood_feedback_map.setdefault(wt, {})[mood] = 1
                            for ctrl in self._path_to_controls.get(wt, []):
                                try:
                                    tile = ctrl.content.content
                                    if isinstance(tile.trailing, ft.Row) and len(tile.trailing.controls) >= 2:
                                        l_btn = tile.trailing.controls[0]
                                        l_btn.icon = ft.Icons.THUMB_UP_ROUNDED
                                        l_btn.icon_color = CYAN
                                        self.try_update(l_btn)
                                except Exception:
                                    pass
                    except Exception as walk_err:
                        logger.exception("Random walk failed during like toggle: %s", walk_err)

                    try:
                        await tg.adjust_mood_profile(db, mood, track_path, 1)
                    except Exception as shift_err:
                        logger.exception("Online learning shift failed during like: %s", shift_err)

                self.page.run_task(run_learning_and_walk)
            else:
                # Run random walk in background to revert propagated likes
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
                                        tile = ctrl.content.content
                                        if isinstance(tile.trailing, ft.Row) and len(tile.trailing.controls) >= 2:
                                            l_btn = tile.trailing.controls[0]
                                            l_btn.icon = ft.Icons.THUMB_UP_OUTLINED
                                            l_btn.icon_color = DIM
                                            self.try_update(l_btn)
                                    except Exception:
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

            # Save negative feedback
            await db.save_mood_feedback(track_path, mood, -1)
            self._mood_feedback_map.setdefault(track_path, {})[mood] = -1

            # Instantly re-calculate the next best matching mood for this track
            try:
                rows, percentile_matrix = await tg._load_percentile_matrix(db, tg.FEATURES_VERSION)
                track_idx = None
                for idx, r in enumerate(rows):
                    if r["path"] == track_path:
                        track_idx = idx
                        break

                if track_idx is not None:
                    adjusted_profiles = await db.get_all_adjusted_mood_profiles()
                    
                    # Score the track using the uncompressed raw physical quartile distance system
                    raw_mood_scores = tg.score_tracks_for_repartition(rows, adjusted_profiles)
                    mood_scores = {}
                    for m in tg.MOODS.keys():
                        if m in raw_mood_scores:
                            mood_scores[m] = float(raw_mood_scores[m][track_idx])
                        else:
                            mood_scores[m] = -np.inf

                    # Find the next best matching mood that is NOT disliked
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
                        # Write the updated partition assignment to SQLite immediately
                        await db.save_partitions([(track_path, best_mood, None)])
                        logger.info("register_mood_dislike: Re-routed track '%s' from '%s' to next-best mood '%s' (score: %.3f)",
                                    track_path, mood, best_mood, best_score)
                    else:
                        # If the track is disliked across all moods or does not meet the specificity threshold, remove it from track_partitions
                        async with db._write_lock:
                            conn = await db.get_connection()
                            await conn.execute("DELETE FROM track_partitions WHERE track_path = ?", (track_path,))
                            await conn.commit()
                        best_mood = None
                        logger.info("register_mood_dislike: Track '%s' has no compatible default partition, removed from partitions",
                                    track_path)

                    # Update in-memory cache directly to ensure UI and cache are perfectly aligned
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

            # Optimistic row deletion
            controls = self._path_to_controls.get(track_path, [])
            removed_any = False
            for ctrl in controls:
                if ctrl in self._library_list.controls:
                    self._library_list.controls.remove(ctrl)
                    removed_any = True
            
            if removed_any:
                self.try_update(self._library_list)
            
            # Decrement search stats label
            try:
                if self._stats_label.text:
                    parts = self._stats_label.text.split()
                    if parts and parts[0].isdigit():
                        cnt = max(0, int(parts[0]) - 1)
                        self._stats_label.text = f"{cnt} {'TRACK' if cnt == 1 else 'TRACKS'}"
                        self.try_update(self._stats_label)
            except Exception:
                pass

            # Run learning shift & unlike walk in background
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
                                        tile = ctrl.content.content
                                        if isinstance(tile.trailing, ft.Row) and len(tile.trailing.controls) >= 2:
                                            l_btn = tile.trailing.controls[0]
                                            l_btn.icon = ft.Icons.THUMB_UP_OUTLINED
                                            l_btn.icon_color = DIM
                                            self.try_update(l_btn)
                                    except Exception:
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
            
            # Reset memory caches so library view updates
            self._cached_moods = None
            self._cached_islets = None
            self._cached_unanalysed = None
            
            # Invalidate cache of percentile matrix
            from utils import track_graph as tg
            tg.invalidate_mood_cache()
            
            # Re-load the library to show the setup card (Generate Moods / Run PCA) from scratch
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
            
            # 1. Re-optimize PCA projection space and fetch coordinates
            await tg.optimize_pca_spacing(db, tg.FEATURES_VERSION)
            rows, percentile_matrix = await tg._load_percentile_matrix(db, tg.FEATURES_VERSION)
            
            # 2. Get all tracks
            all_tracks = await db.get_all_tracks()
            all_paths_to_track = {t["path"]: t for t in all_tracks}
            
            if not all_tracks:
                self.app.show_snackbar("No tracks found in library. Scan your music folder first.")
                return
            
            # Separate into analysed and unanalysed
            analysed_rows = []
            analysed_indices = []
            for idx, r in enumerate(rows):
                if r["path"] in all_paths_to_track:
                    analysed_rows.append(r)
                    analysed_indices.append(idx)
            
            analysed_paths_all = {r["path"] for r in rows}
            
            # --- Compute Mood Assignments ---
            mood_assignments = {}
            if analysed_rows and len(analysed_indices) > 0:
                filtered_percentiles = percentile_matrix[analysed_indices]
                
                # Load feedback and customized profiles from database
                feedback_map = await db.get_mood_feedback()
                self._mood_feedback_map = feedback_map
                adjusted_profiles = await db.get_all_adjusted_mood_profiles()
                
                # If no profiles exist in the database (e.g. cold start onboarding or after a wipe/reset),
                # seed the database with the preconfigured default target quartiles to initialize the scoring scaffold.
                if not adjusted_profiles:
                    for mood in tg.MOODS.keys():
                        default_profile = tg._get_default_quartiles(mood)
                        await db.save_adjusted_mood_profile(mood, default_profile)
                    adjusted_profiles = await db.get_all_adjusted_mood_profiles()
                
                # Score all tracks against all adjusted mood profiles using the raw-feature quartile system
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
                    
                    # 1. Check if the track has any liked feedback (fb == 1)
                    track_likes = [m for m, fb in track_feedback.items() if fb == 1]
                    if track_likes:
                        # Pin track to the highest-scoring liked mood
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
                        # 2. Exclude disliked moods (fb == -1)
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
                                
                        # Sonic Specificity Threshold: only assign to default partition if it genuinely matches
                        if best_score is not None and best_score < -2.0:
                            best_mood = None
                                
                    if best_mood is not None:
                        mood_assignments[path] = best_mood
            
            # Islets are user-driven now (created from the Library "New Islet"
            # button or via Jarvis "save this as <name>"). Bulk graph-component
            # clustering removed — track_partitions.islet_id stays NULL.
            assignments = [
                (path, mood_assignments.get(path), None)
                for path in all_paths_to_track.keys()
                if mood_assignments.get(path) is not None
            ]

            await db.save_partitions(assignments)

            # Reset memory caches so next library load pulls fresh DB entries
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
        tabs = []
        for mode, label, icon in [
            ("moods", "Default", ft.Icons.EMOJI_EMOTIONS_ROUNDED),
            ("islets", "Custom", ft.Icons.DIVERSITY_3_ROUNDED),
        ]:
            is_active = (self.partition_sub_mode == mode)
            active_col = LIB_PARTITION_COLOR
            
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
        # "New Islet" only shown when the user is on the islets sub-tab.
        if self.partition_sub_mode == "islets":
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
        # Reset mood feedback button (mood subsets only)
        if self.partition_sub_mode == "moods":
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
        # Refresh partitions button (default moods only — custom islets are user-driven)
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
            # Best-effort: feed the negative into the islet's regressor too.
            # Quiet on failure — the JSON blacklist is the source of truth
            # for membership; this is a refinement on top.
            try:
                await tg.record_islet_negative(self.db, islet_name, track_path)
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
            self.app.show_snackbar(f"Islet '{name}' no longer exists.",
                                   icon=ft.Icons.ERROR_OUTLINE)
            return
        current_threshold = float(entry.get("threshold", tg.ISLET_THRESHOLD))

        name_field = ft.TextField(
            label="Name",
            value=name,
            autofocus=True,
            border_color=LIB_PARTITION_COLOR,
            cursor_color=LIB_PARTITION_COLOR,
        )
        threshold_label = ft.Text(
            f"Threshold: {current_threshold:.2f}",
            color=DIM, size=12,
        )
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
            self.app.show_snackbar(f"Islet '{raw_new}' updated.",
                                   icon=ft.Icons.CHECK_CIRCLE_OUTLINE)
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
                self.app.show_snackbar(f"Islet '{name}' deleted.",
                                       icon=ft.Icons.DELETE_OUTLINE)
                self.page.run_task(self.load_library)
            else:
                self.app.show_snackbar(f"Islet '{name}' was already gone.",
                                       icon=ft.Icons.ERROR_OUTLINE)

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
            self._cached_islets = None  # force rebuild on next load
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
        for mode, label in [
            ("partitions", "Moods"),
            ("playlists", "Playlists"),
            ("artists", "Artists"),
            ("albums", "Albums"),
            ("tracks", "Tracks"),
        ]:
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
        """Yields fresh control objects from the DB result set.
        Flattened tree structure: children are yielded immediately after expanded parents.
        """
        db = self.app.db_manager

        if self.view_mode == "artists":
            artists = await db.get_all_artists(search_query=self.search_query, sort_mode=self.sort_mode)
            stats_text = f"{len(artists)} {'ARTIST' if len(artists) == 1 else 'ARTISTS'}"
            
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
                                    yield self._track_row(
                                        t, depth=2,
                                        album_context=(al['artist'], al['album']),
                                    )
            return _gen(), stats_text

        elif self.view_mode == "albums":
            albums = await db.get_all_albums(search_query=self.search_query, sort_mode=self.sort_mode)
            stats_text = f"{len(albums)} {'ALBUM' if len(albums) == 1 else 'ALBUMS'}"
            
            async def _gen():
                for a in albums:
                    node_id = f"album_{a['artist']}_{a['album']}"
                    expanded = node_id in self.expanded_nodes
                    yield self._album_row(a, node_id, expanded, depth=0)
                    if expanded:
                        for t in await db.get_tracks_by_album(a['album'], a['artist']):
                            yield self._track_row(
                                t, depth=1,
                                album_context=(a['artist'], a['album']),
                            )
            return _gen(), stats_text

        elif self.view_mode == "playlists":
            playlists = await db.get_all_playlists(search_query=self.search_query, sort_mode=self.sort_mode)
            stats_text = f"{len(playlists)} {'PLAYLIST' if len(playlists) == 1 else 'PLAYLISTS'}"

            async def _gen():
                for pl in playlists:
                    node_id  = f"playlist_{pl['id']}"
                    expanded = node_id in self.expanded_nodes
                    yield self._playlist_row(pl, node_id, expanded)
                    if expanded:
                        for t in await db.get_tracks_in_playlist(pl['id']):
                            yield self._track_row(t, depth=1)
                
                # Always yield the "Add Playlist" button at the end
                if not self.search_query:
                    yield self._new_playlist_row()

            return _gen(), stats_text

        else:  # tracks
            tracks = await db.get_all_tracks(search_query=self.search_query, sort_mode=self.sort_mode)
            stats_text = f"{len(tracks)} {'TRACK' if len(tracks) == 1 else 'TRACKS'}"
            # Stash the in-memory list so play_track() taps can reuse it
            # instead of re-running get_all_tracks() (which dominates
            # initialisation time when the result set is large; a few
            # thousand rows of dict materialisation per tap).
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
        if abs(vx) < 300:  # Deliberate swipe threshold
            return
            
        if vx < 0: # Swipe Left = Go Forward
            if self.current_page < self.total_pages - 1:
                self.page.run_task(self.change_page, self.current_page + 1)
        elif vx > 0: # Swipe Right = Go Backward
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

        # Staggered result rendering via asyncio
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
            # 1. Slide Out active view (left if going forward, right if going backward)
            is_forward = new_page > self.current_page
            exit_offset = ft.Offset(-0.15, 0) if is_forward else ft.Offset(0.15, 0)
            entry_offset = ft.Offset(0.15, 0) if is_forward else ft.Offset(-0.15, 0)
            
            self._animated_list_wrapper.offset = exit_offset
            self._animated_list_wrapper.opacity = 0.0
            self.try_update(self._animated_list_wrapper)
            
            # Wait for transition animation to finish (snappy 80ms)
            await asyncio.sleep(0.08)
            
            # 2. Update page index and instantiate new controls
            self.current_page = new_page
            
            # Re-slice items
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
                    
            # Update controls
            self._library_list.controls = controls
            self._update_pagination_ui()
            
            # 3. Teleport off-screen to the other side instantly
            self._animated_list_wrapper.offset = entry_offset
            self.try_update(self._animated_list_wrapper, self._library_list)
            
            # Wait a tiny tick for the layout to render and compute heights in client (optimized 40ms)
            await asyncio.sleep(0.04)
            
            # 4. Programmatically scroll to the correct position (locked out of scroll listener)
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
                
            # 5. Slide in and fade back to center
            self._animated_list_wrapper.offset = ft.Offset(0, 0)
            self._animated_list_wrapper.opacity = 1.0
            self.try_update(self._animated_list_wrapper)
        finally:
            # Cooldown to let scroll physics settle fully in Flutter client (optimized 150ms)
            await asyncio.sleep(0.15)
            self._is_changing_page = False

    async def load_library(self):
        """Rebuild the library list using an async generator."""
        self._load_token += 1
        token = self._load_token
        self._current_gen = None
        self._is_loading_chunk = False
        
        # Reset current page index on fresh loads
        self.current_page = 0

        # Drop the tracks cache; any sort/search/view change forces a
        # fresh fetch, and the cached list would otherwise be stale.
        self._tracks_cache = None
        self._tracks_cache_key = None

        # Reset the content of the list wrapper to the flat list view unless we are in default moods mode
        if not (self.view_mode == "partitions" and self.partition_sub_mode == "moods"):
            old_content = self._animated_list_wrapper.content
            self._animated_list_wrapper.content = self._library_list
            if old_content != self._library_list:
                self.try_update(self._animated_list_wrapper)

        # Force fresh reload of partitions and islets to prevent race conditions
        # and ensure changes in database/metadata reflect instantly
        if self.view_mode == "partitions":
            self._cached_islets = None
            self._cached_moods = None
            self._cached_unanalysed = None

        # Sync the highlight cache to whatever path the rows are about to
        # be *built* against.
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
            # Check if we are in tracks, albums, or artists mode to use the paginated flat row setup
            if self.view_mode in ("tracks", "albums", "artists"):
                db = self.app.db_manager
                
                if self.view_mode == "tracks":
                    tracks = await db.get_all_tracks(search_query=self.search_query, sort_mode=self.sort_mode)
                    
                    # Cache the list so play_track taps don't issue get_all_tracks queries
                    self._tracks_cache = tracks
                    self._tracks_cache_key = (self.view_mode, self.search_query, self.sort_mode)
                    
                    self._flat_rows = [{"type": "track", "data": t, "depth": 0} for t in tracks]
                    stats_text = f"{len(tracks)} {'TRACK' if len(tracks) == 1 else 'TRACKS'}"
                elif self.view_mode == "albums":
                    albums = await db.get_all_albums(search_query=self.search_query, sort_mode=self.sort_mode)
                    self._flat_rows = [{"type": "album", "data": a, "depth": 0} for a in albums]
                    stats_text = f"{len(albums)} {'ALBUM' if len(albums) == 1 else 'ALBUMS'}"
                else:
                    artists = await db.get_all_artists(search_query=self.search_query, sort_mode=self.sort_mode)
                    self._flat_rows = [{"type": "artist", "data": a, "depth": 0} for a in artists]
                    stats_text = f"{len(artists)} {'ARTIST' if len(artists) == 1 else 'ARTISTS'}"
                
                # Calculate total pages
                self.total_pages = math.ceil(len(self._flat_rows) / self.items_per_page)
                
                if self._load_token != token:
                    return

                # Instantiate only the 50 rows for the first page
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
                    
                    # Ensure vertical scroll wheel (left dial) is hidden and wrapper content is reset
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
                from collections import Counter
                
                # Fetch saved partitions from SQLite database to avoid expensive recalculation on startup
                saved_partitions = await db.get_saved_partitions()
                self._mood_feedback_map = await db.get_mood_feedback()
                
                # Populate caches if not already warm. Moods come from the
                # saved track_partitions table (computed by recalculate);
                # islets come from custom_moods.json + on-demand membership
                # against each saved centroid.
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
                        # No mood partitions saved yet; every track is "unanalysed"
                        # from the partition view's standpoint.
                        cached_unanalysed = list(all_tracks)

                    # Islets: enumerate user-named islets from custom_moods.json
                    # and resolve members against the current library state.
                    # min_count=0 so an islet whose threshold was tightened
                    # below the floor still renders (with an empty/sparse
                    # subtitle and a hint to loosen) rather than vanishing.
                    cached_islets = {}
                    for islet_name in tg.list_islets():
                        members = await tg.tracks_in_islet(db, islet_name, min_count=0)
                        members = [m for m in members if m.get("path") in all_paths_to_track]
                        cached_islets[islet_name] = members

                    # Thread-safe/load-token check before mutating shared instance state
                    # to prevent concurrent/aborted loads from writing stale data.
                    if self._load_token != token:
                        return

                    self._cached_moods = cached_moods
                    self._cached_unanalysed = cached_unanalysed
                    self._cached_islets = cached_islets

                # Filter pre-computed partitions using the search_query (if active)
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
                
                # If partitions have not been generated yet, show the premium glassmorphic setup card
                if not saved_partitions:
                    setup_card = ft.Container(
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
                                    "Sonic Library Moods",
                                    color=TEXT,
                                    size=18,
                                    weight=ft.FontWeight.W_700,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                                ft.Text(
                                    "Analyze your library's DSP features to segment your music "
                                    "collection into cohesive Default and Custom moods.",
                                    color=DIM,
                                    size=13,
                                    text_align=ft.TextAlign.CENTER,
                                    max_lines=3,
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
                        # Default moods partition
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
                            
                        # Build all active sections
                        active_sections = []
                        for mood, tracks in active_moods:
                            icon = mood_icons.get(mood.lower(), ft.Icons.EMOJI_EMOTIONS_ROUNDED)
                            active_sections.append((mood.capitalize(), tracks, icon))
                        if unanalysed_searched:
                            active_sections.append(("Unanalysed Tracks", unanalysed_searched, ft.Icons.HELP_OUTLINE_ROUNDED))
                            
                        if active_sections:
                            # Clamp mood index
                            if self.selected_mood_index >= len(active_sections):
                                self.selected_mood_index = 0
                            elif self.selected_mood_index < 0:
                                self.selected_mood_index = max(0, len(active_sections) - 1)
                                
                            sec_title, sec_tracks, sec_icon = active_sections[self.selected_mood_index]
                            self._mood_label.value = sec_title
                            
                            # For default moods, disable pagination completely to make it frictionless
                            # Cap rendering to 35 tracks for optimal performance
                            self._flat_rows = [{"type": "partition_track", "data": t, "tracks": sec_tracks, "depth": 0} for t in sec_tracks][:35]
                            self.total_pages = 1
                            self.current_page = 0
                            
                            # Hide the old horizontal pagination slider
                            self._mood_pagination_bar.visible = False
                            
                            # Build or update the premium vertical scrollwheel controls in-place
                            if hasattr(self, "_mood_wheel_list") and self._mood_wheel_list is not None and len(self._mood_wheel_list.controls) == len(active_sections) + 1:
                                for idx, (title, tracks, icon) in enumerate(active_sections):
                                    is_selected = (idx == self.selected_mood_index)
                                    accent = LIB_PARTITION_COLOR if is_selected else DIM
                                    
                                    chip = self._mood_wheel_list.controls[idx + 1]
                                    container = chip.content
                                    column = container.content
                                    
                                    # Update Icon
                                    column.controls[0].name = icon
                                    column.controls[0].color = accent
                                    
                                    # Update Text
                                    short_title = title.split()[0]
                                    column.controls[1].value = short_title
                                    column.controls[1].color = accent
                                    
                                    # Update Container Styling
                                    container.bgcolor = apply_opacity(0.12, LIB_PARTITION_COLOR) if is_selected else "transparent"
                                    container.border = ft.Border.all(1.5, LIB_PARTITION_COLOR if is_selected else apply_opacity(0.15, TEXT))
                                    chip.on_tap = lambda _e, index=idx: self._select_mood_index(index)
                            else:
                                wheel_controls = []
                                
                                # Prepend the beautifully styled circular TUNE / EQ dial button
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
                                        width=58,
                                        height=58,
                                        border_radius=29,
                                        bgcolor="transparent",
                                        border=ft.Border.all(1.5, apply_opacity(0.4, CYAN)),
                                        padding=4,
                                        animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
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
                                            width=58,
                                            height=58,
                                            border_radius=29,
                                            bgcolor=apply_opacity(0.12, LIB_PARTITION_COLOR) if is_selected else "transparent",
                                            border=ft.Border.all(1.5, LIB_PARTITION_COLOR if is_selected else apply_opacity(0.15, TEXT)),
                                            padding=4,
                                            animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
                                        ),
                                        on_tap=lambda _e, index=idx: self._select_mood_index(index),
                                    )
                                    wheel_controls.append(chip)
                                    
                                if not hasattr(self, "_mood_wheel_list") or self._mood_wheel_list is None:
                                    self._mood_wheel_list = ft.ListView(
                                        spacing=12,
                                        width=68,
                                        padding=ft.Padding.only(left=2, right=2, top=6, bottom=20),
                                    )
                                self._mood_wheel_list.controls = wheel_controls
                            
                            # Render all tracks in the active mood
                            if self._flat_rows:
                                for item in self._flat_rows:
                                    first_chunk.append(self._build_partition_track_row(item["data"], item["tracks"], depth=0))
                            else:
                                # Show a beautiful empty state inside the tracks list for this empty mood
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
                                        padding=32,
                                        alignment=ft.Alignment(0, 0),
                                        expand=True,
                                    )
                                )
                        else:
                            self._flat_rows = []
                            self.total_pages = 1
                            self._mood_pagination_bar.visible = False
                            
                    else:
                        # Acoustic islets sub-mode: hide the mood pagination toggle
                        self._mood_pagination_bar.visible = False
                        
                        # User-named islets. Each one is a centroid (the
                        # exemplar track's timbre) with membership computed
                        # by cosine >= threshold via tg.tracks_in_islet.
                        named_islets = []
                        for name, member_tracks in (self._cached_islets or {}).items():
                            filtered = [t for t in member_tracks if matches_query(t)]
                            # Search-mode hides islets with zero matches; in
                            # no-search mode, keep every saved islet visible so
                            # over-tightened ones can still be edited.
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
                            
                            # Cap rendering to 35 tracks for optimal performance
                            rendered_tracks = member_tracks[:35]
                            content_controls = [
                                self._build_partition_track_row(t, member_tracks, depth=1, islet_name=name)
                                for t in rendered_tracks
                            ]
                            if len(member_tracks) > 35:
                                remaining = len(member_tracks) - 35
                                content_controls.append(
                                    ft.Container(
                                        content=ft.Text(
                                            f"+ {remaining} more tracks in this custom islet",
                                            size=11,
                                            color=DIM,
                                            italic=True,
                                        ),
                                        padding=ft.Padding.only(left=24, top=6, bottom=6),
                                    )
                                )
                                
                            edit_btn = ft.IconButton(
                                icon=ft.Icons.EDIT_OUTLINED,
                                icon_color=DIM,
                                icon_size=18,
                                tooltip="Edit islet",
                                on_click=lambda _e, n=name: self._open_edit_islet_dialog(n),
                            )
                            del_btn = ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                icon_color=DIM,
                                icon_size=18,
                                tooltip="Delete islet",
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

                        # Empty-state card when no islets exist yet.
                        if not (self._cached_islets or {}):
                            first_chunk.append(self._build_islet_empty_state())
                            
                    # Stats label update
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
                    
                    # Update layout: if default moods sub-mode, embed the vertical dial wheel
                    if self.partition_sub_mode == "moods" and not is_empty:
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
                                spacing=6,
                                expand=True,
                            )
                            self._animated_list_wrapper.update()
                        else:
                            # Update only the changed circular chips in place—no list replacement!
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
                # Expandable view modes (artists, playlists) use the original lazy chunk scroll generator
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
                    
                    # Check for empty state, excluding the "New Playlist" row if in playlists mode
                    is_empty = not first_chunk or (self.view_mode == "playlists" and len(first_chunk) == 1)
                    
                    # Customise empty message based on view mode
                    if is_empty:
                        self._empty_label.visible = True
                        if self.view_mode == "playlists":
                            self._empty_label.content.controls[0].name = ft.Icons.QUEUE_MUSIC_ROUNDED
                            self._empty_label.content.controls[0].color = apply_opacity(0.3, LIB_PLAYLIST_COLOR)
                            self._empty_label.content.controls[1].value = "No playlists yet."
                            self._empty_label.content.controls[2].value = "Create your first playlist below."
                        else:
                            self._empty_label.content.controls[0].name = ft.Icons.LIBRARY_MUSIC_OUTLINED
                            self._empty_label.content.controls[0].color = apply_opacity(0.3, CYAN)
                            self._empty_label.content.controls[1].value = "It's empty in here."
                            self._empty_label.content.controls[2].value = "Index your folders to start listening."
                    
                    # Ensure vertical scroll wheel (left dial) is hidden and wrapper content is reset
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
                    # Safety check: if children already exist (e.g. collapse failed), don't duplicate
                    if idx + 1 < len(controls):
                        next_child = controls[idx + 1]
                        next_data = getattr(next_child, "data", {}) or {}
                        if next_data.get("depth", 0) > depth:
                            self.expanded_nodes.add(nid) # Keep it marked as expanded
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
                    
                    # Direct reference access (O(1))
                    icon = getattr(ctrl, "_chevron", None)
                    if icon:
                        icon.rotate = ft.Rotate(1.57) # Rotate 90 degrees down
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
                    
                    # Direct reference access (O(1))
                    icon = getattr(ctrl, "_chevron", None)
                    if icon:
                        icon.rotate = ft.Rotate(0) # Rotate back to right
                        icon.color = DIM
                        try: icon.update()
                        except: pass
                
                # Explicitly update both the tile and the list to force refresh
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

        chevron = ft.Icon(
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
                    chevron,
                ],
                tight=True, spacing=0,
            ),
            bgcolor=apply_opacity(0.07, accent) if expanded else "transparent",
        )
        tile._chevron = chevron # Direct reference for O(1) access
        tile.on_click = lambda e: self.page.run_task(self._toggle_node, node_id, tile)
        return tile

    def _album_row(self, a: dict, node_id: str, expanded: bool, depth: int = 0) -> ft.Control:
        album  = a.get("album") or "Unknown Album"
        artist = a.get("artist") or "Unknown Artist"
        tc     = a.get("track_count", "?")
        accent = LIB_ALBUM_COLOR

        chevron = ft.Icon(
            ft.Icons.KEYBOARD_ARROW_RIGHT, 
            color=accent if expanded else DIM,
            rotate=ft.Rotate(1.57) if expanded else ft.Rotate(0),
            animate_rotation=ft.Animation(200, ft.AnimationCurve.DECELERATE),
            data="chevron"
        )
        meta = {"artist_name": artist, "album_title": album}
        tile = ft.ListTile(
            data={"node_id": node_id, "depth": depth, "type": "album",
                  "album": album, "artist": artist},
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
                    chevron,
                ],
                tight=True, spacing=0,
            ),
            bgcolor=apply_opacity(0.06, accent) if expanded else "transparent",
        )
        tile._chevron = chevron # Direct reference for O(1) access
        tile.on_click = lambda e: self.page.run_task(self._toggle_node, node_id, tile)
        return tile

    def _new_playlist_row(self) -> ft.Control:
        accent = LIB_PLAYLIST_COLOR
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.ADD_CIRCLE_OUTLINE_ROUNDED, color=accent),
            title=ft.Text("Create New Playlist", color=accent, weight=ft.FontWeight.W_800, size=14),
            subtitle=ft.Text("Organize your music collection", color=DIM, size=12),
            on_click=lambda e: self._create_playlist_dialog(),
            bgcolor=apply_opacity(0.05, accent),
            visual_density=ft.VisualDensity.COMPACT,
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
        """Expandable playlist row; mirrors _album_row but for user playlists."""
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

        chevron = ft.Icon(
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
                    chevron,
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
        tile._chevron = chevron # Direct reference for O(1) access
        tile.on_click = lambda e: self.page.run_task(self._toggle_node, node_id, tile)
        return tile

    def _update_row_highlight(self, ctrl: ft.Control, is_current: bool) -> bool:
        """Atomic update of a single track row's visual state."""
        try:
            # Structure: GestureDetector -> Dismissible -> ListTile
            tile = ctrl.content.content
            
            active_color = apply_opacity(0.1, CYAN)
            
            # Update Icon
            icon = tile.leading.controls[1]
            icon.name = ft.Icons.EQUALIZER if is_current else ft.Icons.MUSIC_NOTE_ROUNDED
            icon.color = CYAN if is_current else LIB_TRACK_COLOR
            
            # Update Title Text
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

        # 1. Clear old highlights using the map
        if prev_path:
            for ctrl in self._path_to_controls.get(prev_path, []):
                self._update_row_highlight(ctrl, is_current=False)
        
        # 2. Set new highlights using the map
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
        """Return the list of (global_idx, playlist_relative_idx) tuples for
        every visible track row that belongs to `playlist_id`. Used by the
        in-place reorder/remove helpers so we don't have to rebuild the
        whole library tree to mutate one playlist's tail."""
        out = []
        rel = 0
        for gi, c in enumerate(self._library_list.controls):
            d = getattr(c, "data", None) or {}
            if d.get("type") == "track" and d.get("playlist_id") == playlist_id:
                out.append((gi, rel))
                rel += 1
        return out

    def _move_playlist_track_in_place(self, playlist_id, path, direction):
        """Optimistic move: swap two adjacent ListTiles in
        _library_list.controls and update only the ListView, then commit to
        the DB in the background. Mirrors the queue's _move pattern so
        repeated taps on Move Up/Down feel instant instead of triggering a
        full library reload between every step.
        """
        entries = self._find_playlist_track_indices(playlist_id)
        if not entries:
            return
        target = next(
            (e for e in entries
             if (getattr(self._library_list.controls[e[0]], "data", None) or {}).get("path") == path),
            None,
        )
        if target is None:
            return
        target_gi, target_rel = target
        new_rel = target_rel + direction
        if new_rel < 0 or new_rel >= len(entries):
            return  # already at the edge

        # Swap_gi is the global index of the neighbour we're swapping with.
        # Adjacent in playlist-relative space ⇒ adjacent in global space too,
        # because the playlist's children sit in a contiguous range under
        # the parent node.
        swap_gi = entries[new_rel][0]
        controls = self._library_list.controls
        controls[target_gi], controls[swap_gi] = controls[swap_gi], controls[target_gi]
        self._library_list.update()

        async def _commit():
            await self.app.db_manager.move_playlist_track(
                playlist_id, target_rel, new_rel,
            )
        self.page.run_task(_commit)

    def _remove_playlist_track_in_place(self, playlist_id, path, title):
        """Optimistic remove: drop the matching ListTile from the ListView
        immediately, then delete the row in the DB. Same motivation as
        _move_playlist_track_in_place; avoids a full library reload."""
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
            trailing_controls.insert(0, ft.Row(
                [
                    ft.IconButton(ft.Icons.ARROW_UPWARD, icon_size=18, icon_color=DIM, on_click=move_up, tooltip="Move Up"),
                    ft.IconButton(ft.Icons.ARROW_DOWNWARD, icon_size=18, icon_color=DIM, on_click=move_down, tooltip="Move Down"),
                    ft.IconButton(ft.Icons.REMOVE_CIRCLE_OUTLINE, icon_size=18, icon_color="#FF4444", on_click=remove_from_pl, tooltip="Remove from Playlist"),
                ],
                spacing=4, tight=True
            ))

        tile = ft.ListTile(
            # `data` lets _move_playlist_track_in_place locate the tile in
            # the flat _library_list.controls without rebuilding the whole
            # tree; same in-place pattern the queue uses.
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
                    # expand=True bounds the Text to the remaining width so
                    # long titles wrap onto additional lines instead of
                    # pushing the format badge past the right edge of the
                    # tile (which is what was happening with tight=True +
                    # ELLIPSIS; the badge clipped off-screen on long names).
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
                # Tell play_track which set to queue. Album / playlist
                # contexts skip the expensive full-library query and use
                # the already-small per-album / per-playlist track set.
                ("playlist", playlist_id) if playlist_id is not None else
                ("album", album_context[0], album_context[1]) if album_context else
                ("library", None),
            ),
        )

        async def _on_swipe_right(e):
            # Swipe Right = Play Next (Right after current)
            audio_engine.queue_next({
                "path":        path,
                "track_title": title,
                "artist_name": artist,
                "album_title": album,
            })
            await e.control.confirm_dismiss(False)
            self.app.show_snackbar(
                f"'{title}' will play next",
                icon=ft.Icons.QUEUE_MUSIC_ROUNDED,
                color=CYAN,
            )

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
                        ft.Text(
                            "Next Up",
                            color=ft.Colors.WHITE,
                            size=13,
                            weight=ft.FontWeight.W_700,
                        ),
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
            on_long_press_start=lambda e: self._open_track_context_menu(meta),
        )
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
            self._tracks_cache_key = ("tracks", self.search_query, self.sort_mode)
            self.page.run_task(self.app.play_track, path, ("library", None))
            
        tile.on_click = play_partition_track

        # Determine the active canonical mood name
        from utils import track_graph as tg
        mood = tg.mood_canonical(self._mood_label.value) if self.partition_sub_mode == "moods" and hasattr(self, "_mood_label") and self._mood_label else None

        if mood:
            # We are in mood partition mode. Add Like / Dislike buttons to the subtitle
            feedback_map = getattr(self, "_mood_feedback_map", {})
            track_feedback = feedback_map.get(path, {}).get(mood, 0)

            is_liked = track_feedback == 1
            is_disliked = track_feedback == -1

            like_btn = ft.IconButton(
                icon=ft.Icons.THUMB_UP_ROUNDED if is_liked else ft.Icons.THUMB_UP_OUTLINED,
                icon_color=CYAN if is_liked else DIM,
                icon_size=16,
                tooltip="Like track in this mood (pin & random walk)",
                on_click=lambda e, p=path, m=mood: self.page.run_task(self._toggle_mood_like, p, m, e.control),
            )

            dislike_btn = ft.IconButton(
                icon=ft.Icons.THUMB_DOWN_ROUNDED if is_disliked else ft.Icons.THUMB_DOWN_OUTLINED,
                icon_color=CYAN if is_disliked else DIM,
                icon_size=16,
                tooltip="Dislike track in this mood (exclude)",
                on_click=lambda e, p=path, m=mood: self.page.run_task(self._register_mood_dislike, p, m, e.control),
            )

            artist = t.get("artist") or "Unknown"
            tnum = t.get("track_num")
            is_current = (path == audio_engine.current_path and bool(path))

            tile.subtitle = ft.Column(
                [
                    ft.Text(
                        f"Track {tnum}  ·  {artist}" if tnum else artist,
                        color=CYAN if is_current else DIM,
                        size=11,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    ft.Row(
                        [
                            like_btn,
                            ft.Container(width=4), # subtle separator
                            dislike_btn,
                        ],
                        spacing=0,
                        tight=True,
                    ),
                ],
                spacing=2,
                tight=True,
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
            _close()
            self.page.run_task(self._open_add_to_playlist_sheet, meta)

        def _edit_meta(_e):
            _close()
            self.app.open_metadata_editor("track", meta)

        def _redownload(_e):
            _close()
            title = meta.get("track_title", "")
            artist = meta.get("artist_name", "")
            
            # Construct a clean search query using title and artist for maximum match rate
            query_parts = []
            if title:
                query_parts.append(title)
            if artist and artist != "Unknown":
                query_parts.append(artist)
                
            query = " ".join(query_parts).strip()
            if not query: return
            
            # Switch to Search tab (index 1)
            self.app._switch_tab(1)
            
            # Set search query, set view mode to "tracks"
            self.app.search_view._search_field.value = query
            self.app.search_view.view_mode = "tracks"
            self.app.search_view._update_view_tabs()
            
            # Start search using standard async task runner on the main thread
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

    async def _open_add_to_playlist_sheet(self, meta: dict):
        playlists = await self.app.db_manager.get_all_playlists(sort_mode="name")
        bs_holder = [None]
        
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
                    # Refresh library if currently in playlists mode to show the new playlist
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
                    # Refresh library if currently in playlists mode and this playlist is expanded
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

        bs = ft.BottomSheet(
            content=ft.Container(
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
            ),
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
        self._toggling_nodes = set() # Track nodes currently being expanded/collapsed
        self.page.run_task(self.load_library)

        def _hide_scanner():
            self._scan_progress_container.visible = False
            self._scan_btn.disabled = False
        self.app.safe_update(_hide_scanner)

        # Re-arm the assistant's greeting so the next time the user opens
        # Jarvis, the init flow re-runs and surfaces a confirmation prompt
        # for any newly-scanned tracks that need DSP analysis. Without this
        # reset, _init_greeted stays True for the whole session and Jarvis
        # silently skips the "X tracks need analysis" prompt.
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


# ─── Settings View ─────────────────────────────────────────────────────────────
class SettingsView:
    def __init__(self, app: "StreamripFletApp"):
        self.app = app
        self.page = app.page
        self._picking_target = None # "download" or "library"

        # File Picker: Windows only. macOS/Linux use native subprocess picker.
        # Android uses _browse_android_paths(); no FilePicker (separate Flet extension).
        self._file_picker = None
        if platform.system() not in ["Darwin", "Linux"]:
            try:
                self._file_picker = ft.FilePicker()
                self._file_picker.on_result = self._on_file_picked
                self.page.overlay.append(self._file_picker)
            except Exception as exc:
                self._file_picker = None
                logger.warning(f"FilePicker fallback initialization failed: {exc}")

        self._init_widgets() 

    def _init_widgets(self):
        """Initializes the functional controls (keeps original logic)."""
        # Path TextFields
        self._dl_path_field = ft.TextField(
            label="Download Path",
            hint_text="e.g. C:\\Music\\Downloads",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
        )
        self._lib_path_field = ft.TextField(
            label="Library Path",
            hint_text="e.g. C:\\Music\\Library",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
        )

        self._selected_accent_color = CYAN

        # Qobuz Credentials
        self._qobuz_user_id_field = ft.TextField(
            label="Qobuz User ID",
            hint_text="e.g. 1234567",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
        )
        self._qobuz_token_field = ft.TextField(
            label="Auth Token / Password Hash",
            hint_text="Enter token or MD5 of password",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
            password=True,
            can_reveal_password=True,
        )
        self._qobuz_use_token_switch = ft.Switch(
            value=True,
            active_color=CYAN
        )

        # Config Editor
        self._config_editor = ft.TextField(
            multiline=True,
            min_lines=15,
            max_lines=25,
            text_style=ft.TextStyle(color=TEXT, font_family="monospace", size=11),
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER),
            border_radius=8,
            content_padding=12,
        )

        # Dropdowns for General Preferences
        common_style = dict(
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
        )
        self._startup_page_dropdown = ft.Dropdown(
            label="Startup Page",
            options=[
                ft.dropdown.Option("Search"),
                ft.dropdown.Option("Library"),
            ],
            on_select=lambda _e: self._save_general_settings(),
            **common_style
        )
        self._default_sort_dropdown = ft.Dropdown(
            label="Default Library Sort",
            options=[
                ft.dropdown.Option(key="date", text="Date Added"),
                ft.dropdown.Option(key="artist", text="Artist (A–Z)"),
                ft.dropdown.Option(key="album", text="Album (A–Z)"),
                ft.dropdown.Option(key="track", text="Track (A–Z)"),
            ],
            on_select=lambda _e: self._save_general_settings(),
            **common_style
        )

        # Landing Page Customization
        self._show_most_listened_switch = ft.Switch(value=True, active_color=CYAN)
        self._show_library_stats_switch  = ft.Switch(value=True, active_color=CYAN)

        # We wrap the main content in a container for easy swapping
        self._scroll_column = ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=12,
            animate_opacity=300
        )
        self.main_content = ft.Container(
            content=self._scroll_column,
            expand=True, 
            padding=ft.Padding.symmetric(horizontal=28, vertical=20),
            animate=ft.Animation(300, ft.AnimationCurve.DECELERATE)
        )

    def build(self) -> ft.Control:
        self.refresh()
        if getattr(self, "initial_subpage", None) == "Storage":
            self.initial_subpage = None
            self._scroll_column.controls = [
                ft.Row([
                    ft.IconButton(ft.Icons.ARROW_BACK_IOS_NEW_ROUNDED, icon_color=CYAN, icon_size=16, 
                                  on_click=lambda _: self._show_hub()),
                    ft.Text("Storage", size=24, weight=ft.FontWeight.W_700, color=TEXT),
                ], spacing=10),
                ft.Container(height=20),
                self._build_storage_group()
            ]
        else:
            self._show_hub() # Start at the Hub
        return self.main_content

    def _show_hub(self):
        """Displays the main settings menu (the 'hub')."""
        self._scroll_column.controls = [
            ft.Text("Settings", size=32, weight=ft.FontWeight.W_900, color=TEXT),
            ft.Text("Configure your high-fidelity experience", color=DIM, size=14),
            ft.Container(height=24),
            
            # Thematic Tiles
            HubSettingItem(ft.Icons.LOCK_PERSON_ROUNDED, "Authentication", "Qobuz credentials & tokens", 
                           on_tap=lambda _: self._show_sub_page("Account", self._build_auth_group())),
            
            HubSettingItem(ft.Icons.STORAGE_ROUNDED, "Storage & Paths", "Library and download locations", 
                           on_tap=lambda _: self._show_sub_page("Storage", self._build_storage_group())),
            
            HubSettingItem(ft.Icons.PALETTE_ROUNDED, "Appearance", "Accent colors and UI behavior",
                           on_tap=lambda _: self._show_sub_page("Appearance", self._build_appearance_group())),

            HubSettingItem(ft.Icons.SHIELD_OUTLINED, "Permissions", "Notifications, audio, and file access",
                           on_tap=lambda _: self._show_sub_page("Permissions", self._build_permissions_group())),

            ft.Divider(color=BORDER, height=40),
            
            HubSettingItem(ft.Icons.TERMINAL_ROUNDED, "Advanced", "Edit TOML config and data maintenance", 
                           on_tap=lambda _: self._show_sub_page("Advanced", self._build_advanced_group())),
            
            HubSettingItem(ft.Icons.INFO_OUTLINE_ROUNDED, "About", "App version and developer info", 
                           on_tap=lambda _: self._show_sub_page("About", self._build_about_group())),
        ]
        self.app.safe_update(lambda: None)

    def _show_sub_page(self, title: str, content_control: ft.Control):
        """Swaps the hub for a specific settings group."""
        self._scroll_column.controls = [
            ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK_IOS_NEW_ROUNDED, icon_color=CYAN, icon_size=16, 
                              on_click=lambda _: self._show_hub()),
                ft.Text(title, size=24, weight=ft.FontWeight.W_700, color=TEXT),
            ], spacing=10),
            ft.Container(height=20),
            content_control
        ]
        self.app.safe_update(lambda: None)

    # --- Sub-Page Builders ---

    def _build_auth_group(self):
        return ft.Column([
            ft.Text("Enter your Qobuz credentials to enable search and preview.", color=DIM, size=12),
            self._qobuz_user_id_field,
            self._qobuz_token_field,
            ft.Row([self._qobuz_use_token_switch, ft.Text("Use Auth Token", color=TEXT, size=12)], spacing=10),
            OnyxButton("SAVE CREDENTIALS", ft.Icons.SAVE, on_tap=lambda _: self._save_qobuz_credentials())
        ], spacing=20)

    def _build_storage_group(self):
        return ft.Column([
            ft.Text("Define where your music is indexed and downloaded.", color=DIM, size=12),
            ft.Row([self._dl_path_field, ft.IconButton(ft.Icons.FOLDER_OPEN, icon_color=CYAN, on_click=self._browse_download_folder)]),
            ft.Row([self._lib_path_field, ft.IconButton(ft.Icons.FOLDER_OPEN, icon_color=CYAN, on_click=self._browse_library_folder)]),
            OnyxButton("SAVE PATHS", ft.Icons.SAVE, on_tap=lambda _: self._save_paths())
        ], spacing=20)

    def _build_appearance_group(self):
        return ft.Column([
            ft.Text("Customize how the app looks and behaves on startup.", color=DIM, size=12),
            self._startup_page_dropdown,
            self._default_sort_dropdown,
            ft.Divider(color=BORDER, height=20),
            ft.Text("Landing Page Sections", color=CYAN, size=12, weight=ft.FontWeight.BOLD),
            ft.Row([self._show_most_listened_switch, ft.Text("Show Most Listened Tracks", color=TEXT, size=12)], spacing=10),
            ft.Row([self._show_library_stats_switch, ft.Text("Show Library Stats", color=TEXT, size=12)], spacing=10),
            ft.Divider(color=BORDER, height=20),
            ft.Text("Accent Color", color=CYAN, size=12, weight=ft.FontWeight.BOLD),
            self._build_color_selector(mode="accent"),
            OnyxButton("APPLY VISUALS", ft.Icons.PALETTE, on_tap=lambda _: self._save_appearance_settings())
        ], spacing=20)


    # ── Permissions ──────────────────────────────────────────────────────────
    _PERMISSION_SPECS = [
        ("notification",            "Notifications",   "Required for media controls on the lock screen"),
        ("audio",                   "Audio Files",     "Read access to music files (Android 13+)"),
        ("storage",                 "Storage",         "Read/write external storage (Android ≤12)"),
        ("manage_external_storage", "All Files Access", "Required to delete or edit songs on Android 11+"),
        ("record_audio",            "Microphone",      "Required for Jarvis voice commands"),
    ]

    def _build_permissions_group(self):
        if "ANDROID_ROOT" not in os.environ and "ANDROID_DATA" not in os.environ:
            return ft.Column([
                ft.Text(
                    "Permissions are managed by the OS on this platform; this panel "
                    "only applies on Android.",
                    color=DIM, size=12,
                ),
            ], spacing=12)

        self._perm_rows: dict[str, dict] = {}
        rows: list[ft.Control] = [
            ft.Text(
                "Some features (delete, tag editing) need elevated access. "
                "Tap GRANT to request, or open Android Settings to manage directly.",
                color=DIM, size=12,
            ),
            ft.Container(height=4),
        ]

        for name, label, desc in self._PERMISSION_SPECS:
            status_text = ft.Text("checking…", color=DIM, size=11)
            grant_btn = ft.TextButton(
                "GRANT",
                icon=ft.Icons.LOCK_OPEN_ROUNDED,
                on_click=lambda _e, n=name: self._on_grant_permission(n),
            )
            row = ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text(label, color=TEXT, size=14, weight=ft.FontWeight.W_600, expand=True),
                        status_text,
                    ]),
                    ft.Text(desc, color=DIM, size=11),
                    ft.Row([grant_btn], alignment=ft.MainAxisAlignment.END),
                ], spacing=4),
                padding=ft.Padding.all(12),
                border=ft.Border.all(1, BORDER),
                border_radius=10,
            )
            self._perm_rows[name] = {"status": status_text, "grant": grant_btn}
            rows.append(row)

        rows.append(ft.Container(height=8))
        rows.append(ft.Row([
            OnyxButton("REFRESH", ft.Icons.REFRESH, on_tap=lambda _: self._refresh_permissions()),
            OnyxButton("OPEN SYSTEM SETTINGS", ft.Icons.LAUNCH, on_tap=lambda _: self._open_app_settings()),
        ], spacing=10))

        # Kick off the initial status query.
        self.page.run_task(self._refresh_permissions_async)
        return ft.Column(rows, spacing=12)

    def _refresh_permissions(self):
        self.page.run_task(self._refresh_permissions_async)

    async def _refresh_permissions_async(self):
        service = getattr(audio_engine, "audio_service", None)
        if service is None:
            return
        try:
            result = await service.query_permissions()
        except Exception as exc:
            logger.warning("query_permissions failed: %s", exc)
            return
        self._apply_perm_status(result)

    def _apply_perm_status(self, result: dict):
        for name, _label, _desc in self._PERMISSION_SPECS:
            row = self._perm_rows.get(name)
            if row is None:
                continue
            status = result.get(name, "unknown")
            granted = status == "granted"
            row["status"].value = status.upper() if status else "UNKNOWN"
            row["status"].color = CYAN if granted else "#FF8866"
            row["grant"].disabled = granted
            row["grant"].text = "GRANTED" if granted else "GRANT"
        self.app.safe_update(lambda: None)

    def _on_grant_permission(self, name: str):
        self.page.run_task(self._grant_permission_async, name)

    async def _grant_permission_async(self, name: str):
        service = getattr(audio_engine, "audio_service", None)
        if service is None:
            return
        try:
            await service.request_permission(name)
        except Exception as exc:
            logger.warning("request_permission(%s) failed: %s", name, exc)
            self.app.show_snackbar(f"Permission request failed: {exc}")
            return
        # Re-query so the row reflects the user's choice (especially for
        # manage_external_storage, where status flips on return from Settings).
        await self._refresh_permissions_async()

    def _open_app_settings(self):
        self.page.run_task(self._open_app_settings_async)

    async def _open_app_settings_async(self):
        service = getattr(audio_engine, "audio_service", None)
        if service is None:
            return
        try:
            await service.open_app_settings()
        except Exception as exc:
            logger.warning("open_app_settings failed: %s", exc)

    def _build_advanced_group(self):
        return ft.Column([
            ft.Text("State Backup & Migration (Import / Export)", weight=ft.FontWeight.BOLD, color=CYAN),
            ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.LOCK_PERSON_ROUNDED, color=CYAN, size=20),
                        ft.Text(
                            "Android Release Build Sandbox Security",
                            weight=ft.FontWeight.BOLD,
                            color=TEXT,
                            size=13,
                        ),
                    ], spacing=6),
                    ft.Text(
                        "Because this application runs as a secure production release build, Android isolates all internal databases, config profiles, and search histories inside a restricted sandbox (/data/data/...). Direct file system access via developer tools like ADB is completely blocked.\n\n"
                        "This Import/Export state system is your exclusive gateway to back up, restore, or migrate your library and custom settings. "
                        "You can also use this system to offload heavy DSP feature computations: export the state ZIP here, run the parallelised desktop offloading script on your PC/Mac, and import the bundle back to instantly sync your track features.",
                        color=DIM,
                        size=12,
                    ),
                ], spacing=6),
                padding=12,
                bgcolor=SURFACE2,
                border_radius=8,
                border=ft.Border.all(1, BORDER),
            ),
            ft.Row([
                ft.TextButton(
                    "Export State",
                    icon=ft.Icons.IOS_SHARE_ROUNDED,
                    on_click=self._on_export_state_click,
                ),
                ft.TextButton(
                    "Import State",
                    icon=ft.Icons.FILE_DOWNLOAD_ROUNDED,
                    on_click=self._on_import_state_click,
                ),
            ]),
            ft.Divider(color=BORDER, height=40),
            ft.Text("Maintenance", weight=ft.FontWeight.BOLD, color=DIM),
            ft.Row([
                ft.TextButton("Clear Cache", icon=ft.Icons.DELETE_SWEEP, on_click=lambda _: self.app.clear_preview_cache()),
                ft.TextButton("Wipe DB", icon=ft.Icons.DELETE_FOREVER, icon_color="#FF4444", on_click=lambda _: self._on_wipe_db_click()),
            ]),
            ft.Divider(color=BORDER, height=40),
            ft.Text("Raw Configuration (TOML)", weight=ft.FontWeight.BOLD, color=DIM),
            ft.Text("Directly edit the Streamrip TOML configuration file for advanced control.", color=DIM, size=12),
            self._config_editor,
            OnyxButton("SAVE CONFIG FILE", ft.Icons.TERMINAL, on_tap=lambda _: self._save_config()),
            ft.Container(height=10),
            ft.TextButton("Debug: Populate Play Counts", icon=ft.Icons.BUG_REPORT_ROUNDED, on_click=self._on_debug_populate_click),
        ], spacing=15)

    def _build_about_group(self):
        return ft.Column([
            ft.Container(
                content=ft.Column([
                    ft.Text("Mai An Lab", size=28, weight=ft.FontWeight.W_900, color=CYAN),
                    ft.Text("Version 1.0.0", color=DIM, size=14),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
                alignment=ft.Alignment(0, 0),
                padding=ft.Padding.only(bottom=20),
            ),
            ft.Text("Summary", weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Text("A deployment friendly restructure of Streamrip (Qobuz only), packaged with Flet alongside custom Flutter (audio engine) extensions.", color=DIM, size=12),
            ft.Divider(color=BORDER, height=30),
            ft.Text("Developer", weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Text("Christophoros Mitsakopoulos", color=DIM, size=13),
            ft.Divider(color=BORDER, height=30),
            ft.Text("Credits", weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Column([
                ft.Text("• Streamrip by nathom and community", color=DIM, size=12),
                ft.Text("• Flet Framework and community", color=DIM, size=12),
            ], spacing=4),
            ft.Divider(color=BORDER, height=40),
            ft.Row([
                ft.TextButton("Project GitHub", icon=ft.Icons.CODE_ROUNDED, on_click=lambda _: asyncio.create_task(self.page.launch_url("CURRENTLY PRIVATE"))),
                ft.TextButton("Developer", icon=ft.Icons.PERSON_ROUNDED, on_click=lambda _: self.app.show_snackbar("Contact: mitsacopoulos@gmail.com")),
            ], alignment=ft.MainAxisAlignment.CENTER),
            ft.Container(height=20),
            ft.Text("2026 Mai An Lab", color=DIM, size=11, italic=True, text_align=ft.TextAlign.CENTER, width=float("inf")),
        ], spacing=15)

    def _on_wipe_db_click(self):
        self.app.open_wipe_confirmation()

    async def _on_debug_populate_click(self, _e):
        self.app.show_snackbar("Populating random play counts...", icon=ft.Icons.STORAGE_ROUNDED)
        await self.app.db_manager.debug_populate_play_counts()
        self.app.show_snackbar("Done! Check your Most Listened Tracks.", icon=ft.Icons.CHECK_CIRCLE, color=CYAN)
        # Refresh UI
        self.app.search_view.refresh_setup_state()

    # ── State bundle (export/import) ────────────────────────────────────────
    def _on_export_state_click(self, _e):
        # Reuse the same Android folder picker as the Download/Library
        # selectors; pick a directory and write the bundle inside it.
        if hasattr(sys, 'getandroidapilevel'):
            self._browse_android_state_bundle(mode="export")
        else:
            path = pick_folder("Choose export folder") or os.path.join(
                os.path.expanduser("~"), "Downloads"
            )
            self.page.run_task(self._do_export_state, path)

    def _on_import_state_click(self, _e):
        if hasattr(sys, 'getandroidapilevel'):
            self._browse_android_state_bundle(mode="import")
        else:
            self.app.show_snackbar(
                "Desktop import: drop a bundle into ~/Downloads and use Android.",
                color="#FF4444",
            )

    async def _do_export_state(self, out_dir: str):
        from utils import state_export
        from utils import track_graph as tg
        from utils.streamrip_api import get_config_path
        from utils.search_history import get_search_history_path

        self.app.show_snackbar("Exporting state...", icon=ft.Icons.IOS_SHARE_ROUNDED)
        try:
            # Run the zip + sqlite-backup in a worker thread so the UI doesn't
            # hitch — backup() on a multi-MB DB can take a few hundred ms.
            out_path = await asyncio.to_thread(
                state_export.export_state,
                self.app.db_manager.db_path,
                get_config_path(),
                get_search_history_path(),
                out_dir,
                tg.CUSTOM_MOODS_PATH,
            )
        except Exception as ex:
            logger.exception("state export failed")
            self.app.show_snackbar(f"Export failed: {ex}", color="#FF4444")
            return
        self.app.show_snackbar(
            f"Exported to {out_path}", icon=ft.Icons.CHECK_CIRCLE, color=CYAN
        )

    async def _do_import_state(self, zip_path: str):
        from utils import state_export
        from utils import track_graph as tg
        from utils.streamrip_api import get_config_path
        from utils.search_history import get_search_history_path

        self.app.show_snackbar("Importing state...", icon=ft.Icons.FILE_DOWNLOAD_ROUNDED)

        # Close the live aiosqlite connection so the .db file isn't locked
        # while we overwrite it. The app will be killed right after.
        try:
            close = getattr(self.app.db_manager, "close", None)
            if close is not None:
                await close()
        except Exception as ex:
            logger.warning(f"db_manager.close() raised before import: {ex}")

        try:
            result = await asyncio.to_thread(
                state_export.import_state,
                zip_path,
                self.app.db_manager.db_path,
                get_config_path(),
                get_search_history_path(),
                tg.CUSTOM_MOODS_PATH,
            )
        except Exception as ex:
            logger.exception("state import failed")
            self.app.show_snackbar(f"Import failed: {ex}", color="#FF4444")
            return

        replaced = ", ".join(result["replaced"].keys()) or "nothing"
        self.app.show_snackbar(
            f"Imported {replaced}. Force-close and relaunch the app.",
            icon=ft.Icons.CHECK_CIRCLE,
            color=CYAN,
        )

    def _browse_android_state_bundle(self, mode: str):
        """Folder/file picker for the debug state-bundle round trip.
        mode='export' picks a destination directory; mode='import' picks a
        `.zip` file. Mirrors the layout of _browse_android_paths so users get
        a consistent navigator across the app."""
        is_import = (mode == "import")
        app_data = os.getenv("FLET_APP_STORAGE_DATA") or ""

        BOOKMARKS = [
            (os.path.abspath("/storage/emulated/0/Download"), "Downloads"),
            (os.path.abspath("/storage/emulated/0"),          "Internal Storage"),
            (os.path.abspath("/sdcard"),                       "SD Card"),
            (os.path.abspath("/storage/emulated/0/Music"),    "Music"),
        ]
        if app_data:
            BOOKMARKS.append((app_data, "App Storage"))

        bs_holder = [None]
        path_state = [None]

        title_text = ft.Text("", color=TEXT, weight=ft.FontWeight.W_700, size=14)
        path_text  = ft.Text("", color=DIM, size=10, italic=True)
        dir_list   = ft.Column(tight=True, spacing=0, scroll=ft.ScrollMode.AUTO)

        def _close():
            if bs_holder[0]:
                bs_holder[0].open = False
                bs_holder[0].update()
                self.page.update()

        def _confirm_dir(path):
            _close()
            self.page.run_task(self._do_export_state, path)

        def _confirm_file(path):
            _close()
            self.page.run_task(self._do_import_state, path)

        def _render(directory):
            path_state[0] = directory
            dir_list.controls.clear()

            if directory is None:
                title_text.value = "Import State Bundle" if is_import else "Export State Bundle"
                path_text.value  = (
                    "Pick a .zip bundle" if is_import else "Pick a destination folder"
                )
                for bpath, bname in BOOKMARKS:
                    exists = os.path.isdir(bpath)
                    dir_list.controls.append(ft.ListTile(
                        leading=ft.Icon(
                            ft.Icons.FOLDER_ROUNDED,
                            color=CYAN if exists else DIM,
                            size=20,
                        ),
                        title=ft.Text(bname, color=TEXT if exists else DIM, size=13),
                        subtitle=ft.Text(bpath, color=DIM, size=10),
                        on_click=_nav_to(bpath),
                    ))
                return

            title_text.value = os.path.basename(directory) or directory
            path_text.value  = directory

            if not is_import:
                # Export: lead with the "use this folder" affordance.
                dir_list.controls.append(
                    ft.Container(
                        content=ft.Button(
                            f"Export here",
                            icon=ft.Icons.IOS_SHARE_ROUNDED,
                            on_click=lambda _: _confirm_dir(directory),
                            bgcolor=CYAN,
                            color=BG,
                            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                        ),
                        padding=ft.Padding.only(bottom=6),
                    )
                )

            try:
                entries = sorted(os.listdir(directory))
            except PermissionError:
                dir_list.controls.append(
                    ft.Text("Permission denied", color="#FF5555", size=12, italic=True)
                )
                if bs_holder[0] and bs_holder[0].open:
                    bs_holder[0].update()
                return

            sub_dirs = [e for e in entries
                        if os.path.isdir(os.path.join(directory, e)) and not e.startswith(".")]
            zip_files = [e for e in entries
                         if is_import
                         and e.lower().endswith(".zip")
                         and os.path.isfile(os.path.join(directory, e))]

            # Import: surface .zip files first so they're easy to tap.
            for entry in zip_files:
                full = os.path.join(directory, entry)
                dir_list.controls.append(ft.ListTile(
                    leading=ft.Icon(ft.Icons.ARCHIVE_ROUNDED, color=CYAN, size=18),
                    title=ft.Text(entry, color=TEXT, size=13),
                    subtitle=ft.Text(
                        f"{os.path.getsize(full)/1024:.0f} KB", color=DIM, size=10,
                    ),
                    on_click=lambda _e, p=full: _confirm_file(p),
                ))

            for entry in sub_dirs:
                full = os.path.join(directory, entry)
                dir_list.controls.append(ft.ListTile(
                    leading=ft.Icon(ft.Icons.FOLDER_OUTLINED, color=CYAN, size=18),
                    title=ft.Text(entry, color=TEXT, size=13),
                    on_click=_nav_to(full),
                ))

            if not sub_dirs and not zip_files:
                dir_list.controls.append(
                    ft.Text(
                        "(no .zip bundles or sub-folders)" if is_import else "(no sub-folders)",
                        color=DIM, size=12, italic=True,
                    )
                )

            if bs_holder[0] and bs_holder[0].open:
                bs_holder[0].update()

        def _nav_to(path):
            def _handler(_e):
                _render(path)
            return _handler

        def _go_up(_e):
            cur = path_state[0]
            if cur is None:
                return
            parent = os.path.dirname(cur)
            if parent == cur:
                _render(None)
            else:
                _render(parent)

        _render(None)

        bs = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.IconButton(
                                    ft.Icons.ARROW_BACK_ROUNDED,
                                    icon_color=CYAN,
                                    on_click=_go_up,
                                    tooltip="Up",
                                ),
                                ft.Column(
                                    [title_text, path_text],
                                    spacing=0,
                                    expand=True,
                                ),
                                ft.TextButton("Cancel", on_click=lambda _: _close()),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=4,
                        ),
                        ft.Divider(color=BORDER),
                        ft.Container(content=dir_list, height=320),
                    ],
                    tight=True,
                    spacing=6,
                ),
                bgcolor=SURFACE,
                padding=ft.Padding.only(left=16, right=16, top=16, bottom=40),
            ),
            use_safe_area=True,
            bgcolor=SURFACE,
        )
        bs_holder[0] = bs
        self.app.page.overlay.append(bs)
        bs.open = True
        self.app.page.update()

    def refresh(self):
        self._dl_path_field.value  = self.app.target_folder
        self._lib_path_field.value = self.app.library_folder
        try:
            cfg = load_config()
            with open(get_config_path(), "r", encoding="utf-8") as f:
                self._config_editor.value = f.read()
            
            # Update general dropdowns
            gen = cfg.get("general", {})
            self._startup_page_dropdown.value = gen.get("startup_page", "Library")
            self._default_sort_dropdown.value = gen.get("library_sort", "date")

            # Update Qobuz fields
            qobuz = cfg.get("qobuz", {})
            self._qobuz_user_id_field.value = str(qobuz.get("email_or_userid", ""))
            self._qobuz_token_field.value   = str(qobuz.get("password_or_token", ""))
            self._qobuz_use_token_switch.value = bool(qobuz.get("use_auth_token", True))

            # Update Landing Page fields
            landing = cfg.get("landing", {})
            self._show_most_listened_switch.value = bool(landing.get("show_search_history", True))
            self._show_library_stats_switch.value  = bool(landing.get("show_library_stats", True))

            # Update Appearance
            appearance = cfg.get("appearance", {})
            self._selected_accent_color = appearance.get("accent_color", "#00BFFF")
        except: pass
        if self.page: self.page.update()

    def _save_landing_settings(self):
        show_history = self._show_search_history_switch.value
        show_stats   = self._show_library_stats_switch.value
        
        update_config_params({
            "landing": {
                "show_search_history": show_history,
                "show_library_stats": show_stats
            }
        })
        self.app.show_snackbar("Landing page settings updated.")
        # Refresh search view to reflect changes if it's already built
        if hasattr(self.app, "search_view"):
            self.app.search_view.refresh_setup_state()

    def _build_color_selector(self, mode="accent"):
        colors = {
            "Cyan": "#00BFFF",
            "Deep Blue": "#2979FF",
            "Purple": "#9B59B6",
            "Lavender": "#B39DDB",
            "Pink": "#E91E63",
            "Red": "#E74C3C",
            "Crimson": "#DC143C",
            "Orange": "#E67E22",
            "Gold": "#FFD700",
            "Yellow": "#FFD600",
            "Green": "#2ECC71",
            "Emerald": "#00FF7F",
            "Mint": "#69F0AE",
            "Slate": "#78909C",
        }
        
        target_color_base = self._selected_accent_color

        circles = []
        for name, hex in colors.items():
            is_selected = (hex.lower() == target_color_base.lower())
            circle = ft.Container(
                width=32, height=32,
                bgcolor=hex,
                border_radius=16,
                border=ft.Border.all(2, TEXT if is_selected else "transparent"),
                on_click=lambda e, h=hex, m=mode: self._on_color_click(h, m),
                tooltip=name
            )
            circles.append(circle)
            
        return ft.Row(circles, spacing=12, alignment=ft.MainAxisAlignment.START, wrap=True)

    def _on_color_click(self, hex, mode):
        self._selected_accent_color = hex
        self.app.safe_update(lambda: None)
        # We need to rebuild the appearance group to show selection
        self._show_sub_page("Appearance", self._build_appearance_group())

    def _save_appearance_settings(self):
        update_config_params({
            "appearance": {
                "accent_color": self._selected_accent_color
            },
            "landing": {
                "show_search_history": self._show_most_listened_switch.value,
                "show_library_stats": self._show_library_stats_switch.value
            }
        })
        self.app.show_snackbar("Appearance and interface settings saved.")
        # Refresh search view to reflect changes
        if hasattr(self.app, "search_view"):
            self.app.search_view.refresh_setup_state()
        # Soft-restart UI to apply colors everywhere
        self.app.restart_ui(target_tab=2)

    def _save_paths(self):
        dl  = self._dl_path_field.value.strip()
        lib = self._lib_path_field.value.strip()
        if not dl or not lib:
            self.app.show_snackbar("Paths cannot be empty.")
            return
        
        self.app.target_folder = dl
        self.app.library_folder = lib
        update_config_params({"downloads": {"folder": dl}})
        self.app._save_pref("folder_path", dl)
        self.app._save_pref("library_path", lib)
        self.app.show_snackbar("Storage paths updated.")
        self.app.library_view.start_scan()

    def _save_general_settings(self):
        startup = self._startup_page_dropdown.value
        sort = self._default_sort_dropdown.value
        if not startup or not sort:
            return

        update_config_params({
            "general": {
                "startup_page": startup,
                "library_sort": sort,
            }
        })

        # Apply the new library sort to the live view so the change is visible
        # without restarting the app. startup_page takes effect on next launch.
        lib_view = getattr(self.app, "library_view", None)
        if lib_view is not None and getattr(lib_view, "sort_mode", None) != sort:
            lib_view.sort_mode = sort
            if hasattr(lib_view, "load_library"):
                self.page.run_task(lib_view.load_library)

    def _save_config(self):
        try:
            with open(get_config_path(), "w", encoding="utf-8") as f:
                f.write(self._config_editor.value or "")
            self.app.show_snackbar("Configuration saved.")
            self.app.sync_config_to_ui()
        except Exception as exc:
            self.app.show_snackbar(f"Save failed: {exc}")

    def _save_qobuz_credentials(self):
        uid   = self._qobuz_user_id_field.value.strip()
        token = self._qobuz_token_field.value.strip()
        if not uid or not token:
            self.app.show_snackbar("Credentials cannot be empty.")
            return
        
        use_token = self._qobuz_use_token_switch.value
        success = update_config_params({
            "qobuz": {
                "use_auth_token": use_token,
                "email_or_userid": uid,
                "password_or_token": token
            }
        })
        if success:
            self.app.show_snackbar("Qobuz credentials updated.")
            self.app.sync_config_to_ui()
        else:
            self.app.show_snackbar("Failed to update credentials.")

    # ── native browsing ──────────────────────────────────────────────────────
    def _browse_android_paths(self, target: str):
        """Interactive directory navigator for Android."""
        app_data = os.getenv("FLET_APP_STORAGE_DATA") or ""
        label    = "Download Folder" if target == "download" else "Library Folder"

        # Root bookmarks; listed at the top level
        BOOKMARKS = [
            (os.path.abspath("/storage/emulated/0"),          "Internal Storage"),
            (os.path.abspath("/sdcard"),                       "SD Card"),
            (os.path.abspath("/storage/emulated/0/Music"),    "Music"),
            (os.path.abspath("/storage/emulated/0/Download"), "Downloads"),
        ]
        if app_data:
            BOOKMARKS.append((app_data, "App Storage"))

        bs_holder  = [None]
        path_state = [None]   # current directory being browsed; None = bookmark list

        # ── widgets that will be mutated on navigation ─────────────────────
        title_text = ft.Text("", color=TEXT, weight=ft.FontWeight.W_700, size=14)
        path_text  = ft.Text("", color=DIM, size=10, italic=True)
        dir_list   = ft.Column(tight=True, spacing=0, scroll=ft.ScrollMode.AUTO)

        def _close():
            if bs_holder[0]:
                bs_holder[0].open = False
                bs_holder[0].update()
                self.page.update()

        def _confirm(path):
            _close()
            self._handle_folder_picked(path, target)


        def _render(directory):
            """Fill dir_list with entries for the given directory (or bookmarks if None)."""
            path_state[0] = directory
            dir_list.controls.clear()

            if directory is None:
                # Bookmark list
                title_text.value = f"Select {label}"
                path_text.value  = "Choose a starting location"
                for bpath, bname in BOOKMARKS:
                    exists = os.path.isdir(bpath)
                    dir_list.controls.append(ft.ListTile(
                        leading=ft.Icon(
                            ft.Icons.FOLDER_ROUNDED,
                            color=CYAN if exists else DIM,
                            size=20,
                        ),
                        title=ft.Text(bname, color=TEXT if exists else DIM, size=13),
                        subtitle=ft.Text(bpath, color=DIM, size=10),
                        on_click=_nav_to(bpath),
                    ))
            else:
                title_text.value = os.path.basename(directory) or directory
                path_text.value  = directory

                # "Use this folder" button
                dir_list.controls.append(
                    ft.Container(
                        content=ft.Button(
                            f"Use \"{os.path.basename(directory) or directory}\"",
                            icon=ft.Icons.CHECK_ROUNDED,
                            on_click=lambda _: _confirm(directory),
                            bgcolor=CYAN,
                            color=BG,
                            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                        ),
                        padding=ft.Padding.only(bottom=6),
                    )
                )

                # Sub-directories
                try:
                    entries = sorted(
                        e for e in os.listdir(directory)
                        if os.path.isdir(os.path.join(directory, e))
                        and not e.startswith(".")
                    )
                except PermissionError:
                    entries = []
                    dir_list.controls.append(
                        ft.Text("Permission denied", color="#FF5555", size=12,
                                italic=True)
                    )

                for entry in entries:
                    full = os.path.join(directory, entry)
                    dir_list.controls.append(ft.ListTile(
                        leading=ft.Icon(ft.Icons.FOLDER_OUTLINED, color=CYAN, size=18),
                        title=ft.Text(entry, color=TEXT, size=13),
                        on_click=_nav_to(full),
                    ))

                if not entries and not any(
                    isinstance(c, ft.Text) for c in dir_list.controls
                ):
                    dir_list.controls.append(
                        ft.Text("(no sub-folders)", color=DIM, size=12, italic=True)
                    )

            if bs_holder[0] and bs_holder[0].open:
                bs_holder[0].update()

        def _nav_to(path):
            def _handler(_e):
                _render(path)
            return _handler

        def _go_up(_e):
            cur = path_state[0]
            if cur is None:
                return
            parent = os.path.dirname(cur)
            if parent == cur:   # filesystem root
                _render(None)
            else:
                _render(parent)

        _render(None)   # start at bookmark list

        bs = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        # header
                        ft.Row(
                            [
                                ft.IconButton(
                                    ft.Icons.ARROW_BACK_ROUNDED,
                                    icon_color=CYAN,
                                    on_click=_go_up,
                                    tooltip="Up",
                                ),
                                ft.Column(
                                    [title_text, path_text],
                                    spacing=0,
                                    expand=True,
                                ),
                                ft.TextButton("Cancel", on_click=lambda _: _close()),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=4,
                        ),
                        ft.Divider(color=BORDER),
                        ft.Container(content=dir_list, height=320),
                    ],
                    tight=True,
                    spacing=6,
                ),
                bgcolor=SURFACE,
                padding=ft.Padding.only(left=16, right=16, top=16, bottom=40),
            ),
            use_safe_area=True,
            bgcolor=SURFACE,
        )
        bs_holder[0] = bs
        self.app.page.overlay.append(bs)
        bs.open = True
        self.app.page.update()

    def _browse_download_folder(self, e):
        if hasattr(sys, 'getandroidapilevel'):
            self._browse_android_paths("download")
            return
        if platform.system() in ["Darwin", "Linux"]:
            path = pick_folder("Select Download Folder")
            if path:
                self._handle_folder_picked(path, "download")
            return
        if self._file_picker:
            self._picking_target = "download"
            self._file_picker.get_directory_path()
        else:
            self.app.show_snackbar("Folder browsing not available")

    def _browse_library_folder(self, e):
        if hasattr(sys, 'getandroidapilevel'):
            self._browse_android_paths("library")
            return
        if platform.system() in ["Darwin", "Linux"]:
            path = pick_folder("Select Library Folder")
            if path:
                self._handle_folder_picked(path, "library")
            return
        if self._file_picker:
            self._picking_target = "library"
            self._file_picker.get_directory_path()
        else:
            self.app.show_snackbar("Folder browsing not available")

    def _on_file_picked(self, e) -> None:
        if hasattr(e, 'path') and e.path:
            self._handle_folder_picked(e.path, self._picking_target)
        self._picking_target = None

    def _handle_folder_picked(self, path: str, target: str):
        if not path:
            return

        # Update app-level state
        if target == "download":
            self.app.target_folder = path
        elif target == "library":
            self.app.library_folder = path

        # Update the UI fields in the Settings tab
        self._dl_path_field.value  = self.app.target_folder
        self._lib_path_field.value = self.app.library_folder

        # Persist to both Streamrip config and Flet preferences
        if target == "download":
            update_config_params({"downloads": {"folder": path}})
            self.app._save_pref("folder_path", path)
        else:
            self.app._save_pref("library_path", path)

        self.refresh()

        label = "Download" if target == "download" else "Library"
        self.app.show_snackbar(f"{label} folder set: {path}")
        self.app.page.update()


# ─── Mini Player Bar ───────────────────────────────────────────────────────────
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
        self._music_icon = ft.Icon(ft.Icons.MUSIC_NOTE, color=CYAN, size=24)
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
                                            ft.Container(
                                                content=self._music_icon,
                                                width=44, height=44,
                                                bgcolor=SURFACE2,
                                                border_radius=6,
                                                alignment=ft.Alignment(0, 0),
                                            ),
                                            self._artwork,
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

    def update_state(self, is_playing: bool):
        self._play_btn.icon = ft.Icons.PAUSE if is_playing else ft.Icons.PLAY_ARROW
        if self._play_btn.page:
            self._play_btn.update()
        
        self.container.border = ft.Border.all(1, apply_opacity(0.7, "#FFFFFF")) if is_playing else ft.Border.all(1, BORDER)
        if self.container.page:
            self.container.update()

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


# ─── Now Playing Sheet ─────────────────────────────────────────────────────────
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
        )
        self._overlay_icon = ft.Icon(ft.Icons.PLAY_ARROW, size=64, color=TEXT, opacity=0, animate_opacity=400)
        
        self._art_stack = ft.GestureDetector(
            content=ft.Stack([
                self._art_placeholder,
                ft.Container(self._artwork, shadow=ft.BoxShadow(blur_radius=30, color=CYAN+"33"), expand=True),
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
        show_feedback = self.app.play_similar_mode or in_mood_partition
        
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
        self._title.value  = title  or "Unknown"
        self._artist.value = artist or "Unknown"
        self._album.value  = album  or "Unknown"
        self._subtitle_text.value = f"{self._artist.value}  ·  {self._album.value}"

    def update_artwork(self, src: str):
        if src:
            self._artwork.src        = src
            self._artwork.src_base64 = ""
            self._artwork.visible    = True
            self._artwork.scale      = ft.Scale(1.0)
        else:
            self._artwork.visible    = False

    def update_state(self, is_playing: bool):
        self._play_btn.icon = ft.Icons.PAUSE if is_playing else ft.Icons.PLAY_ARROW
        if self._play_btn.page:
            self._play_btn.update()

    def update_progress(self, position: float, duration: float):
        if self.app.is_scrubbing:
            return
        pct = (position / duration * 100) if duration > 0 else 0
        self._scrubber.value = pct
        self._time_cur.value = fmt_time(position)

    def update_duration(self, duration: float):
        self._time_tot.value = fmt_time(duration)

    def update_shuffle(self, is_shuffle: bool):
        self._shuffle_btn.icon_color = CYAN if is_shuffle else DIM

    def update_repeat(self, mode: str):
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
        self.app.play_similar_mode = not self.app.play_similar_mode
        self.update_play_similar(self.app.play_similar_mode)
        verb = "Enabled" if self.app.play_similar_mode else "Disabled"
        self.app.show_snackbar(
            f"Play Similar: {verb}",
            icon=ft.Icons.LINK_ROUNDED if self.app.play_similar_mode else ft.Icons.LINK_OFF_ROUNDED
        )
        # If the user just enabled the mode and the queue is at (or past) the
        # last track, kick off a recommendation immediately so playback doesn't
        # stop before the first dynamic replenishment fires via _on_current_path.
        if self.app.play_similar_mode:
            path = audio_engine.current_path
            audio_engine.play_similar_seed_path = path or ""
            if path and audio_engine.current_index >= len(audio_engine.queue) - 1:
                self.app.page.run_task(self.app._recommend_similar_async, path)

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
        show_feedback = enabled or in_mood_partition
        self._like_btn.visible = show_feedback
        self._dislike_btn.visible = show_feedback
        
        if self._play_similar_btn.page:
            self._play_similar_btn.update()
        if self._like_btn.page:
            self._like_btn.update()
        if self._dislike_btn.page:
            self._dislike_btn.update()


# ─── Queue Sheet ───────────────────────────────────────────────────────────────
class QueueSheet:
    def __init__(self, app: "StreamripFletApp"):
        self.app  = app
        self.page = app.page
        self._initialized = False
        self.container = None

    def _ensure_initialized(self):
        if self._initialized:
            return

        self._count_text = ft.Text("", color=DIM, size=11, weight=ft.FontWeight.W_700)
        self._queue_list = ft.ListView(expand=True, spacing=4,
                                        padding=ft.Padding.symmetric(horizontal=12))
        self._empty_label = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.QUEUE_MUSIC, color=DIM, size=48),
                    ft.Text("Queue is empty", color=DIM, size=13, text_align=ft.TextAlign.CENTER),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
            alignment=ft.Alignment(0, 0),
            expand=True,
            visible=False,
        )

        # 1. Migrate to native BottomSheet for reliable mobile expansion
        self.container = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        # Re-add custom visual drag handle since show_drag_handle=False
                        ft.Row([ft.Container(width=40, height=4, bgcolor=BORDER, border_radius=2)],
                               alignment=ft.MainAxisAlignment.CENTER),
                        ft.Container(
                            content=ft.Row(
                                [
                                    ft.Column(
                                        [
                                            ft.Text("UP NEXT", color=TEXT, size=13, weight=ft.FontWeight.W_700),
                                            self._count_text,
                                        ],
                                        spacing=1,
                                    ),
                                    ft.Container(expand=True),
                                    ft.TextButton(
                                        content=ft.Text("CLEAR ALL", color=CYAN, size=11, weight=ft.FontWeight.W_700),
                                        on_click=lambda e: self._clear_all(),
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.CLOSE, icon_color=DIM, icon_size=18,
                                        on_click=lambda e: self.collapse(),
                                    ),
                                ],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            padding=ft.Padding.symmetric(horizontal=20),
                        ),
                        ft.Divider(color=BORDER),
                        self._empty_label,
                        self._queue_list,
                    ],
                    spacing=0,
                    expand=True,
                ),
                bgcolor=SURFACE,
                border_radius=ft.BorderRadius.only(top_left=20, top_right=20),
                expand=True, # FIX: Expand container to fill the screen
            ),
            fullscreen=True,
            scrollable=False, # CRITICAL FIX: Let the ListView scroll, not the sheet
            show_drag_handle=False, # CRITICAL FIX: Prevents scroll controller conflict
            draggable=True,
            use_safe_area=True, 
            bgcolor=SURFACE,
        )
        self._initialized = True

    def build(self) -> ft.Control:
        self._ensure_initialized()
        return self.container

    def expand(self):
        def _mutate():
            self._ensure_initialized()
            self.refresh()
            # FIX: Removed manual height constraints here as well
            self.container.open = True
        self.app.safe_update(_mutate)

    def collapse(self):
        def _mutate():
            self._ensure_initialized()
            self.container.open = False
        self.app.safe_update(_mutate)

    def refresh(self):
        cur_idx    = audio_engine.current_index
        cur_artist = audio_engine.current_artist
        remaining  = max(0, len(audio_engine.queue) - cur_idx - 1)

        self._count_text.value = (
            f"{remaining} track{'s' if remaining != 1 else ''} remaining"
            if audio_engine.queue else "Nothing queued"
        )

        def track_row(i: int, t: dict) -> ft.Control:
            is_active = (i == cur_idx)
            same_art  = (not is_active and bool(cur_artist)
                         and t.get("artist_name", "") == cur_artist)
            position  = i - cur_idx  # 0 = now playing, 1+ = up next

            accent = CYAN if is_active else (LIB_TRACK_COLOR if same_art else "transparent")
            bg     = apply_opacity(0.1, CYAN) if is_active else (
                     apply_opacity(0.05, LIB_TRACK_COLOR) if same_art else SURFACE)

            pos_label = ft.Container(
                content=ft.Text(
                    "▶" if is_active else f"+{position}",
                    color=CYAN if is_active else DIM,
                    size=10, weight=ft.FontWeight.W_700,
                ),
                width=28,
                alignment=ft.Alignment(0, 0),
            )

            card = ft.Container(
                content=ft.Row(
                    [
                        ft.Container(width=3, bgcolor=accent, border_radius=2),
                        pos_label,
                        ft.Column(
                            [
                                ft.Text(t.get("track_title", "Unknown"),
                                        color=CYAN if is_active else TEXT,
                                        size=13,
                                        weight=ft.FontWeight.W_700 if is_active else None,
                                        overflow=ft.TextOverflow.ELLIPSIS, max_lines=1),
                                ft.Text(
                                    t.get("artist_name", "Unknown"),
                                    color=CYAN if is_active else (LIB_TRACK_COLOR if same_art else DIM),
                                    size=11,
                                ),
                            ],
                            spacing=1, expand=True,
                        ),
                        ft.Row(
                            [
                                ft.IconButton(icon=ft.Icons.ARROW_UPWARD, icon_color=DIM, icon_size=16,
                                              visible=not is_active and i > cur_idx + 1,
                                              on_click=lambda e, idx=i: self._move(idx, idx - 1)),
                                ft.IconButton(icon=ft.Icons.ARROW_DOWNWARD, icon_color=DIM, icon_size=16,
                                              visible=not is_active,
                                              on_click=lambda e, idx=i: self._move(idx, idx + 1)),
                                ft.IconButton(icon=ft.Icons.REMOVE_CIRCLE_OUTLINE, icon_color="#FF4444",
                                              icon_size=16,
                                              on_click=lambda e, idx=i: self._remove(idx)),
                            ],
                            spacing=0,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                bgcolor=bg,
                border=ft.Border.all(1, apply_opacity(0.4, CYAN) if is_active else BORDER),
                border_radius=10,
                height=60,
                padding=ft.Padding.only(left=0, right=4, top=4, bottom=4),
            )

            return AnimatedEntry(
                ft.Dismissible(
                    content=card,
                    # Background exposed when swiping RIGHT (START_TO_END)
                    background=ft.Container(
                        content=ft.Row(
                            [ft.Icon(ft.Icons.DELETE_OUTLINE, color=BG, size=20)],
                            alignment=ft.MainAxisAlignment.START,
                        ),
                        bgcolor="#FF4444",
                        border_radius=10,
                        padding=ft.Padding.only(left=20),
                    ),
                    # Background exposed when swiping LEFT (END_TO_START)
                    secondary_background=ft.Container(
                        content=ft.Row(
                            [ft.Icon(ft.Icons.DELETE_OUTLINE, color=BG, size=20)],
                            alignment=ft.MainAxisAlignment.END,
                        ),
                        bgcolor="#FF4444",
                        border_radius=10,
                        padding=ft.Padding.only(right=20),
                    ),
                    dismiss_direction=ft.DismissDirection.HORIZONTAL, # Enables swiping in both directions
                    on_dismiss=lambda e, idx=i: self._remove(idx),
                ),
                target_height=60,
            )

        # Cap at 15 items to avoid DOM explosion and performance drops on mobile
        upcoming = audio_engine.queue[cur_idx:]
        rows = [
            track_row(cur_idx + i, t)
            for i, t in enumerate(upcoming[:15])
        ]

        is_empty = len(rows) == 0
        self._empty_label.visible = is_empty
        # Single synchronous assignment; no async chunking.
        # Chunked async writes race with subsequent refresh() calls and corrupt
        # the Flet control tree (RangeError in Control.applyPatch on Dart side).
        self._queue_list.controls = rows
        self._queue_list.update()

    def _move(self, from_idx: int, to_idx: int):
        audio_engine.move_queue_item(from_idx, to_idx)
        self.app.safe_update(self.refresh)

    def _remove(self, idx: int):
        audio_engine.remove_from_queue(idx)
        self.app.safe_update(self.refresh)

    def _clear_all(self):
        audio_engine.clear_queue()
        self.collapse()
        self.app.show_snackbar("Playback queue cleared.")


# ─── Assistant (chat sheet + TTS) ──────────────────────────────────────────────
class AssistantView:
    """Integrated chat surface for the faux-AI assistant.

    Owns the chat scrollback, the input field, and the initialisation banner
    (which surfaces the DSP analyser sweep + graph-build progress).
    """

    def __init__(self, app: "StreamripFletApp"):
        self.app = app
        self.page = app.page
        self._initialized = False
        self.layout: ft.Column | None = None
        # Runner is created lazily so it picks up the DB + engine after they
        # have themselves finished setting up.
        self._runner = None
        # Concurrency guard: only one init pass at a time. There is NO
        # _init_done flag — init is intentionally re-run on every open so
        # newly-added tracks get surfaced as a confirmation prompt without
        # needing a manual reset.
        self._init_started = False
        # Suppresses the "Hi, ready" greeting on subsequent opens within
        # the same session. Reset to False when LibraryView finishes a scan
        # (so the next open re-greets, surfacing newly-scanned tracks).
        self._init_greeted = False
        self._tts_enabled = True
        # Cancellation hook for the analyser sweep so the user can dismiss
        # the panel without leaving a background analyser running forever.
        self._init_cancel = False
        self._analysing_library = False

        from utils.chat_memory import ChatMemoryManager
        self.chat_memory = ChatMemoryManager()
        self._history_list = []

    def _ensure_initialized(self):
        if self._initialized:
            return

        self._messages = ft.ListView(
            expand=True,
            spacing=8,
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            auto_scroll=False,
        )

        self._input = ft.TextField(
            hint_text="Ask me anything, sir…",
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=14),
            content_padding=ft.Padding.symmetric(horizontal=14, vertical=12),
            border_radius=22,
            multiline=False,
            min_lines=1,
            max_lines=1,
            expand=True,
            on_submit=lambda _e: self._on_send_click(),
        )

        self._send_btn = ft.IconButton(
            icon=ft.Icons.ARROW_UPWARD_ROUNDED,
            icon_color=CYAN,
            on_click=lambda _e: self._on_send_click(),
        )

        # Push-to-talk mic. Pressing and holding starts a listening session via
        # speech_to_text; releasing stops it. While listening, the icon
        # turns red so the user can see we're capturing audio. STT runs
        # through the audio_service bridge.
        self._mic_icon = ft.Icon(ft.Icons.MIC_ROUNDED, color=CYAN)
        self._mic_btn = ft.GestureDetector(
            content=ft.Container(
                content=self._mic_icon,
                padding=10,
                border_radius=20,
            ),
            tooltip="Hold to Speak",
            # Touch-down starts listening immediately. Release stops it.
            #   • short tap   → on_tap_up
            #   • held button → on_long_press_end (Flutter cancels the tap
            #     gesture once long-press wins arbitration, so on_tap_cancel
            #     would fire mid-hold — we deliberately don't wire it).
            on_tap_down=self._on_mic_down,
            on_tap_up=self._on_mic_up,
            on_long_press_end=self._on_mic_up,
        )
        self._stt_listening = False
        self._mic_pressed = False

        self._tts_toggle = ft.IconButton(
            icon=ft.Icons.VOLUME_UP_ROUNDED,
            icon_color=CYAN,
            tooltip="Toggle voice replies",
            on_click=lambda _e: self._toggle_tts(),
        )

        self._clear_btn = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
            icon_color=CYAN,
            tooltip="Clear conversation history",
            on_click=lambda _e: self.clear_chat_manually(),
        )

        # Initialisation banner: hidden once the graph is ready.
        self._init_label = ft.Text(
            "Preparing your music network…", color=DIM, size=12,
        )
        self._init_bar = ft.ProgressBar(value=None, color=CYAN, bgcolor=SURFACE2)
        self._init_banner = ft.Container(
            content=ft.Column([self._init_label, self._init_bar], spacing=6),
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            visible=False,
        )

        # Load session history
        session = self.chat_memory.load_session()
        self._init_greeted = session["init_greeted"]
        self._history_list = session["messages"]

        restored_controls = []
        for msg in self._history_list:
            restored_controls.append(self._build_bubble_row(msg))

        if restored_controls:
            self._messages.controls = restored_controls
        else:
            self._messages.controls = [self._build_empty_state()]

        self.layout = ft.Column(
            [
                # Header
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Text("JARVIS", color=TEXT, size=13,
                                    weight=ft.FontWeight.W_700, expand=True),
                            self._clear_btn,
                            self._tts_toggle,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                ),
                self._init_banner,
                ft.Divider(color=BORDER, height=1),
                # Messages Slot
                ft.Container(content=self._messages, expand=True),
                ft.Divider(color=BORDER, height=1),
                # Footer Input
                ft.Container(
                    content=ft.Row(
                        [self._mic_btn, self._input, self._send_btn],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding.only(
                        left=12, right=8, top=8, bottom=24,
                    ),
                ),
            ],
            spacing=0,
            expand=True,
        )
        self._initialized = True

    def build(self) -> ft.Control:
        self._ensure_initialized()
        return self.layout

    # ── Public lifecycle ───────────────────────────────────────────────────

    def expand(self):
        # In Tab-mode, expand is simply a trigger for lazy initialization.
        self.page.run_task(self._init_assistant)

    def collapse(self):
        # No-op in Tab-mode; visibility is handled by the tab-switcher.
        pass
        # Stop any in-flight TTS so the user isn't talked at after dismissing.
        service = getattr(audio_engine, "audio_service", None)
        if service is not None:
            self.page.run_task(service.tts_stop)

    # ── Initialisation flow ────────────────────────────────────────────────

    async def _init_assistant(self):
        """Inspect graph state and decide what to surface to the user.

        Cheap operations (building metadata edges from existing data,
        rebuilding the acoustic graph when features are already present)
        run silently in the background — they take seconds and don't need
        confirmation. The expensive DSP analyser sweep is *always* offered
        as a confirmation prompt, never auto-run, because it can take hours
        on large libraries and consume battery the user didn't ask for.

        Re-runs are cheap and safe: every open just re-reads status and
        either says "Ready" or asks again about pending analysis work.
        _init_done is intentionally absent — making this stateless means
        new tracks added between opens automatically get surfaced as a new
        prompt without needing a manual reset."""
        if self._init_started:
            return
        self._init_started = True
        try:
            await self._do_init()
        finally:
            self._init_started = False

    async def _do_init(self):
        # Yield to the UI loop for a beat to ensure the Jarvis tab finishes
        # its initial paint before we start the heavy DSP/Graph work.
        await asyncio.sleep(0.2)
        
        # Scroll any existing history to bottom on open
        if hasattr(self, "_messages") and self._messages:
            try:
                await self._messages.scroll_to(offset=-1, duration=0)
            except Exception:
                pass

        if getattr(self, "_analysing_library", False):
            has_busy_msg = any(
                msg["sender"] == "assistant" and "busy analyzing the music library" in msg["text"]
                for msg in self._history_list
            )
            if not has_busy_msg:
                await self._append_bubble(
                    "assistant",
                    "I am currently busy analyzing the music library and rebuilding the DSP graph, sir. I will notify you as soon as I am done.",
                )
            return

        self._set_banner(visible=True, message="Checking your library…", determinate=None)
        logger.info("AssistantView: init flow started")

        # Lazy runner construction.
        from utils.assistant_runner import AssistantRunner, PendingConfirmation
        from utils import track_graph as tg
        if self._runner is None:
            self._runner = AssistantRunner(self.app.db_manager, audio_engine)

        # Voice config — slower pace + lower pitch for the Jarvis persona.
        if audio_engine.audio_service:
            self.page.run_task(
                audio_engine.audio_service.tts_set_voice, pitch=0.75, rate=0.75
            )

        try:
            status = await tg.graph_status(self.app.db_manager)
        except Exception as exc:
            logger.exception("AssistantView: graph_status failed")
            self._set_banner(visible=False)
            await self._append_bubble(
                "assistant",
                f"Couldn't read your library: {exc}",
            )
            return

        logger.info("AssistantView: graph_status = %s", status)

        # Empty library: nothing to do, just inform the user.
        if status["total_tracks"] == 0:
            self._set_banner(visible=False)
            if not self._init_greeted:
                self._init_greeted = True
                self.chat_memory.save_session(self._history_list, self._init_greeted)
                has_empty_msg = any(
                    msg["sender"] == "assistant" and "Your library looks empty" in msg["text"]
                    for msg in self._history_list
                )
                if not has_empty_msg:
                    await self._append_bubble(
                        "assistant",
                        "Your library looks empty. Scan a music folder in "
                        "Library → Scan, then ask me to 'rescan my library'.",
                    )
            return

        # Count tracks that the analyser would touch — drives both the
        # confirmation prompt and the "everything's fine" silent path.
        try:
            missing = await self.app.db_manager.get_tracks_missing_features(
                tg.FEATURES_VERSION
            )
        except Exception as exc:
            logger.warning("AssistantView: missing-features check failed: %s", exc)
            missing = []
        missing_count = len(missing)
        logger.info("AssistantView: %d tracks need DSP features", missing_count)

        needs_metadata = (status["artist_edges"] == 0 and status["album_edges"] == 0)
        needs_acoustic = (status["acoustic_edges"] == 0 and status["total_tracks"] >= 2)

        # Cheap path: rebuild metadata + acoustic edges from already-present
        # features. No DSP analysis required. Run silently.
        if needs_metadata or (needs_acoustic and missing_count == 0):
            self._set_banner(
                visible=True,
                message="Linking your music graph…",
                determinate=None,
            )
            try:
                if needs_metadata:
                    await tg.build_metadata_edges(self.app.db_manager)
                if needs_acoustic:
                    await tg.build_acoustic_edges(self.app.db_manager)
            except Exception as exc:
                logger.warning("AssistantView: edge build failed: %s", exc)
            self._set_banner(visible=False)

        # Now ask the user about any DSP work. Never auto-run.
        if missing_count > 0:
            self._set_banner(visible=False)
            if not self._init_greeted:
                self._init_greeted = True
                self.chat_memory.save_session(self._history_list, self._init_greeted)
                has_dsp_prompt = any(
                    msg["sender"] == "assistant" and any(k in msg["text"] for k in ["DSP", "analys", "unindexed", "profile", "acoustic"])
                    for msg in self._history_list
                )
                if not has_dsp_prompt:
                    self._runner.queue_confirmation(PendingConfirmation(
                        prompt="rescan",
                        on_yes_action="rebuild_graph",
                        on_yes_msg=f"Acknowledged. Analysing {missing_count} tracks now.",
                        on_no_msg="Understood. I'll work with what I have for now.",
                    ))
                    dsp_text = self._runner._say("dsp_prompt", missing=missing_count, total=status['total_tracks'])
                    dsp_speak = self._runner._say("dsp_prompt_speak", missing=missing_count, total=status['total_tracks'])
                    await self._append_bubble(
                        "assistant",
                        dsp_text,
                        speak=True,
                        speak_text=dsp_speak,
                    )
            return

        # Everything's built and analysed. Show a quiet "ready" bubble (no TTS;
        # the assistant only speaks when something actually happened — e.g.
        # after a graph rebuild — not on every reopen of the pane).
        self._set_banner(visible=False)
        if not self._init_greeted:
            self._init_greeted = True
            self.chat_memory.save_session(self._history_list, self._init_greeted)
            if not self._history_list:
                edge_total = (status["acoustic_edges"]
                              + status["artist_edges"] + status["album_edges"])
                greeting_text = self._runner._say("greeting")
                status_line = f"\n\n*System status: {status['total_tracks']} tracks mapped, {edge_total} graph edges active.*"
                await self._append_bubble(
                    "assistant",
                    greeting_text + status_line,
                )

    async def _do_graph_rebuild(self):
        """Long-running operation: run the analyser for any tracks lacking
        features, then rebuild metadata + acoustic edges. Invoked when the
        runner emits action='rebuild_graph' (either from a confirmation
        yes-response or the explicit 'rescan' intent)."""
        import time
        from utils import track_graph as tg
        self._analysing_library = True
        audio_active = bool(audio_engine.audio_service)
        is_desktop = (sys.platform == "darwin")
        can_analyze = audio_active or is_desktop
        start_time = time.time()

        try:
            if not can_analyze:
                await self._append_bubble(
                    "assistant",
                    "*Audio engine not active. Running edge linkage in offline developer mode...*",
                )

            try:
                missing = await self.app.db_manager.get_tracks_missing_features(
                    tg.FEATURES_VERSION
                )
            except Exception as exc:
                logger.warning("AssistantView: missing-features check failed: %s", exc)
                missing = []

            if missing and can_analyze:
                total = len(missing)
                self._init_cancel = False
                if not is_desktop:
                    await self._append_bubble(
                        "assistant",
                        "*Sir, please ensure a song is playing (even at 0 volume) in the background. "
                        "This activates Android's keep-alive service, preventing the OS from killing "
                        "our background DSP worker thread while we work!*",
                        speak=True,
                        speak_text="Sir, please ensure a song is playing in the background. This activates Android's keep-alive service, preventing the OS from killing our background DSP thread.",
                    )
                self._set_banner(
                    visible=True,
                    message=f"Analysing 1 / {total} tracks…",
                    determinate=0.0,
                )

                async def _on_progress(done, total_, current, failures):
                    if self._init_cancel:
                        return
                    
                    # Compute dynamic estimated time remaining (ETA)
                    eta_str = ""
                    if done > 0 and total_ > done:
                        elapsed = time.time() - start_time
                        avg_time_per_track = elapsed / done
                        remaining_tracks = total_ - done
                        eta_seconds = int(avg_time_per_track * remaining_tracks)
                        
                        if eta_seconds < 60:
                            eta_str = f" (~{eta_seconds}s left)"
                        elif eta_seconds < 3600:
                            eta_str = f" (~{eta_seconds // 60}m {eta_seconds % 60}s left)"
                        else:
                            eta_str = f" (~{eta_seconds // 3600}h {(eta_seconds % 3600) // 60}m left)"

                    suffix = f" ({failures} failed)" if failures else ""
                    self._set_banner(
                        visible=True,
                        message=f"Analysing {done} / {total_}{suffix}{eta_str}…",
                        determinate=(done / total_) if total_ else None,
                    )
                    # Keep background process alive and show progress on Android notification
                    if audio_active and audio_engine.audio_service:
                        try:
                            await audio_engine.audio_service.show_progress_notification(
                                title="DSP Analysis Progress",
                                content=f"Analysing {done} / {total_}{suffix}{eta_str}",
                                progress=done,
                                total=total_
                            )
                        except Exception as ex:
                            logger.warning("AssistantView: failed to update notification: %s", ex)

                try:
                    result = await tg.bulk_analyze_library(
                        self.app.db_manager,
                        audio_engine.audio_service,
                        progress_cb=_on_progress,
                        cancel_check=lambda: self._init_cancel,
                    )
                    logger.info("AssistantView: analyser sweep done: %s", result)
                except Exception as exc:
                    logger.warning("AssistantView: analyser sweep failed: %s", exc)
            elif missing and not can_analyze:
                logger.info("AssistantView: skipped bulk feature extraction (no audio service)")

            # Rebuild edges — metadata is fast, acoustic depends on new feature
            # vectors. Run both unconditionally after a sweep so the graph
            # reflects the latest analyser output.
            self._set_banner(visible=True, message="Linking similar tracks…", determinate=None)
            try:
                await tg.build_metadata_edges(self.app.db_manager)
                await tg.build_acoustic_edges(self.app.db_manager)
            except Exception as exc:
                logger.warning("AssistantView: edge rebuild failed: %s", exc)

            self._set_banner(visible=False)
            
            # Invalidate the partitions cache on LibraryView since edges / features changed
            if hasattr(self.app, "library_view") and self.app.library_view:
                self.app.library_view._cached_moods = None
                self.app.library_view._cached_islets = None
                self.app.library_view._cached_unanalysed = None

            await self._append_bubble(
                "assistant",
                "Analysis complete. Mood search and similarity walks are ready.",
                speak=True,
            )
        finally:
            self._analysing_library = False
            # Clear Android system progress notification
            if audio_active:
                try:
                    await audio_engine.audio_service.show_progress_notification(
                        title="",
                        content="",
                        progress=0,
                        total=0,
                        done=True
                    )
                except Exception as ex:
                    logger.warning("AssistantView: failed to cancel notification: %s", ex)

    def _set_banner(self, visible: bool, message: str = "", determinate=None):
        def _mutate():
            self._init_banner.visible = visible
            if message:
                self._init_label.value = message
            self._init_bar.value = determinate
        self.app.safe_update(_mutate)

    # ── Chat plumbing ──────────────────────────────────────────────────────

    def _toggle_tts(self):
        self._tts_enabled = not self._tts_enabled
        self._tts_toggle.icon = (
            ft.Icons.VOLUME_UP_ROUNDED if self._tts_enabled
            else ft.Icons.VOLUME_OFF_ROUNDED
        )
        if not self._tts_enabled:
            service = getattr(audio_engine, "audio_service", None)
            if service is not None:
                self.page.run_task(service.tts_stop)
        self.app.safe_update(lambda: None)

    def _on_send_click(self):
        text = (self._input.value or "").strip()
        if not text:
            return
        self._input.value = ""
        self.app.safe_update(lambda: None)
        self.page.run_task(self._handle_user_text, text)

    def _on_mic_down(self, e):
        self._mic_pressed = True
        self.page.run_task(self._start_listening)

    def _on_mic_up(self, e):
        self._mic_pressed = False
        self.page.run_task(self._stop_listening)

    async def _start_listening(self):
        if getattr(self, "_analysing_library", False):
            self.app.show_snackbar("Please wait until library analysis is complete.")
            self._mic_pressed = False
            return

        if self._stt_listening:
            return

        service = getattr(audio_engine, "audio_service", None)
        is_mock = (service is None)

        # First tap: ensure mic permission (skip if mock mode)
        if not is_mock:
            try:
                perms = await service.query_permissions()
                if perms.get("record_audio") != "granted":
                    res = await service.request_permission("record_audio")
                    if res.get("status") != "granted":
                        self.app.show_snackbar(
                            "Microphone permission is required for voice commands."
                        )
                        self._mic_pressed = False
                        return
            except Exception as ex:
                logger.warning(f"Mic permission check failed: {ex}")
                self.app.show_snackbar("Couldn't verify microphone permission.")
                self._mic_pressed = False
                return

        # Safeguard check: If the user released the button while we were awaiting permissions, abort!
        if not self._mic_pressed:
            logger.info("AssistantView: user released mic before initialization completed. Aborting STT start.")
            return

        self._stt_listening = True
        self._mic_icon.color = "#FF4444"
        self._mic_btn.tooltip = "[MOCK MODE] Release to Send" if is_mock else "Release to Send"
        self._mic_icon.update()
        self._mic_btn.update()

        self._listening_icon = ft.Icon(ft.Icons.MIC_ROUNDED, color="#FF4444", size=18)
        self._listening_text = ft.Text(
            "Listening (mock mode), sir..." if is_mock else "Listening, sir...",
            color=DIM, size=13, italic=True
        )
        self._listening_bubble = ft.Row(
            [
                ft.Container(
                    content=ft.Row(
                        [
                            self._listening_icon,
                            self._listening_text,
                            ft.ProgressRing(width=12, height=12, stroke_width=1.5, color=CYAN),
                        ],
                        spacing=8,
                        alignment=ft.MainAxisAlignment.START,
                    ),
                    padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                    bgcolor=SURFACE2,
                    border_radius=14,
                    border=ft.Border.all(1, apply_opacity(0.1, CYAN)),
                )
            ],
            alignment=ft.MainAxisAlignment.START,
        )

        def _show_listening():
            # Drop the empty-state on first real message.
            if self._messages.controls and isinstance(self._messages.controls[0], ft.Container) \
                    and not isinstance(getattr(self._messages.controls[0], "content", None), ft.Row):
                if len(self._messages.controls) == 1:
                    self._messages.controls = []
            self._messages.controls.append(self._listening_bubble)
            self._messages.update()
        self.app.safe_update(_show_listening)
        
        # Smoothly scroll to the bottom after the listening bubble is added
        await asyncio.sleep(0.06)
        try:
            await self._messages.scroll_to(offset=-1, duration=200)
        except Exception:
            pass

        try:
            if not is_mock:
                # 60 s upper bound on a single hold (the plugin still
                # finalises early when stt_stop() is called on release).
                stt_task = asyncio.create_task(service.stt_listen(timeout=60.0))
                
                released = False
                release_time = None
                while not stt_task.done():
                    if not self._stt_listening and not released:
                        released = True
                        release_time = asyncio.get_running_loop().time()
                    
                    if released:
                        elapsed = asyncio.get_running_loop().time() - release_time
                        if elapsed > 4.0:
                            logger.info("AssistantView: Post-release transcription timeout. Cancelling STT task.")
                            stt_task.cancel()
                            break
                    await asyncio.sleep(0.05)
                
                if stt_task.done() and not stt_task.cancelled():
                    try:
                        res = stt_task.result()
                    except Exception as e:
                        logger.warning(f"stt_task failed with exception: {e}")
                        res = {"ok": False, "error": str(e)}
                else:
                    res = {"ok": False, "error": "cancelled"}
            else:
                # Simulated Speech-to-Text session for offline debugging on macOS
                # Check if self._stt_listening is flipped to False every 0.05 seconds
                timeout_counter = 0.0
                while self._stt_listening and timeout_counter < 15.0:
                    await asyncio.sleep(0.05)
                    timeout_counter += 0.05
                typed = (self._input.value or "").strip()
                res = {"ok": True, "text": typed if typed else "play random"}

            if res.get("ok") and res.get("text"):
                self._input.value = res["text"]
                self.app.safe_update(lambda: None)
                self._on_send_click()
            elif res.get("error") and all(x not in res["error"].lower() for x in ["cancel", "no_match", "no match"]):
                self.app.show_snackbar(f"Speech error: {res['error']}")
        except asyncio.TimeoutError:
            # No utterance recognised in the listen window; silent no-op.
            pass
        except Exception as ex:
            logger.error(f"STT Error: {ex}")
        finally:
            self._stt_listening = False
            self._mic_icon.color = CYAN
            self._mic_btn.tooltip = "Hold to Speak"
            
            def _hide_listening():
                if hasattr(self, "_listening_bubble") and self._listening_bubble in self._messages.controls:
                    self._messages.controls.remove(self._listening_bubble)
                    self._messages.update()
                self._mic_icon.update()
                self._mic_btn.update()
            self.app.safe_update(_hide_listening)

    async def _stop_listening(self):
        if not self._stt_listening:
            return
        
        self._stt_listening = False
        
        # Instantly update UI on finger lift so microphone looks closed
        # and Jarvis shows "Thinking..." state during the transcription delay.
        def _show_processing():
            self._mic_icon.color = CYAN
            self._mic_btn.tooltip = "Hold to Speak"
            self._mic_icon.update()
            self._mic_btn.update()
            
            if hasattr(self, "_listening_icon") and hasattr(self, "_listening_text"):
                self._listening_icon.name = ft.Icons.AUTO_AWESOME_ROUNDED
                self._listening_icon.color = CYAN
                self._listening_text.value = "Thinking..."
                self._listening_icon.update()
                self._listening_text.update()
        
        self.app.safe_update(_show_processing)
        
        service = getattr(audio_engine, "audio_service", None)
        if service is not None:
            try:
                await service.stt_stop()
            except Exception as ex:
                logger.warning(f"stt_stop failed: {ex}")

    async def _handle_user_text(self, text: str):
        await self._append_bubble("user", text)
        if getattr(self, "_analysing_library", False):
            await self._append_bubble(
                "assistant",
                "Please wait, sir. I am currently busy analyzing the music library and rebuilding the DSP graph.",
            )
            return

        if self._runner is None:
            await self._append_bubble(
                "assistant",
                "Hold on — I'm still initialising. Try again in a moment.",
            )
            return

        # Mirror the voice flow: show a "Thinking..." bubble while the runner
        # works so text-only users get the same feedback as voice users.
        thinking_bubble = ft.Row(
            [
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.AUTO_AWESOME_ROUNDED, color=CYAN, size=18),
                            ft.Text("Thinking...", color=DIM, size=13, italic=True),
                            ft.ProgressRing(width=12, height=12, stroke_width=1.5, color=CYAN),
                        ],
                        spacing=8,
                        alignment=ft.MainAxisAlignment.START,
                    ),
                    padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                    bgcolor=SURFACE2,
                    border_radius=14,
                    border=ft.Border.all(1, apply_opacity(0.1, CYAN)),
                )
            ],
            alignment=ft.MainAxisAlignment.START,
        )
        def _show_thinking():
            self._messages.controls.append(thinking_bubble)
            self._messages.update()
        self.app.safe_update(_show_thinking)

        # Hand the runner a live snapshot of the chat history so it can
        # resolve anaphora without round-tripping through chat_history.json
        # on every utterance.
        try:
            response = await self._runner.dispatch_text(
                text, history_provider=lambda: list(self._history_list),
            )
        finally:
            def _hide_thinking():
                if thinking_bubble in self._messages.controls:
                    self._messages.controls.remove(thinking_bubble)
                    self._messages.update()
            self.app.safe_update(_hide_thinking)
        # Build a structured entity dict from the response so future turns
        # can resolve pronouns without any regex or DB round-trip.
        # Non-canonical seeds (play_random, play_mood, play_similar_bulk) carry
        # an entity_intent marker — their "track"/"first" is a system-picked
        # seed, NOT the user's referent, so we don't anchor pronouns on it.
        # Multi-match plays (extras.is_multi) are similar: the opener was
        # randomly picked from many hits, but the artist (if consistent) is
        # still meaningful for "more by them" follow-ups.
        _NON_CANONICAL_SEEDS = {"play_random", "play_mood", "play_similar_bulk"}
        _entity_intent = response.extras.get("entity_intent")
        _is_non_canonical = _entity_intent in _NON_CANONICAL_SEEDS
        _is_multi = bool(response.extras.get("is_multi"))
        _raw_track = response.extras.get("track") or response.extras.get("first")
        if _is_non_canonical:
            _track = None
            _artist = None
        elif _is_multi:
            _track = None        # random opener is not the anchor
            _artist = (_raw_track.get("artist") or _raw_track.get("artist_name")
                       if _raw_track else None)
        else:
            _track = _raw_track
            _artist = (_track.get("artist") or _track.get("artist_name")
                       if _track else response.extras.get("artist"))
        intent = getattr(response, "intent", None)
        _entities = {
            "track":  _track,
            "artist": _artist,
            "playlist": response.extras.get("playlist"),
            "intent": intent.name if intent else getattr(response, "_intent_name", None),
        }

        await self._append_bubble(
            "assistant", response.displayed,
            speak=response.success and bool(response.spoken),
            speak_text=response.spoken,
            entities=_entities if any(_entities.values()) else None,
            intent=intent,
        )
        # Playback intents stage the queue but leave engine.play() to us so
        # Jarvis finishes his sentence before the music starts. _append_bubble
        # awaits the TTS future before returning, so by here it's safe to
        # kick off playback. Guarded so a failed-intent response that still
        # has deferred_play set (shouldn't happen, but defensive) doesn't
        # start audio on top of an error message.
        if response.success and response.deferred_play:
            try:
                audio_engine.play()
            except Exception as exc:
                logger.warning("AssistantView: deferred play failed: %s", exc)

        # Update shuffle button color in Now Playing if state changed in audio_engine.
        # Without page.update() the icon_color assignment doesn't repaint until the
        # next unrelated UI event, so play_random's shuffle activation looked silent.
        try:
            if hasattr(self.app, "now_playing") and self.app.now_playing:
                self.app.now_playing.update_shuffle(audio_engine.is_shuffle)
                self.app.page.update()
        except Exception:
            pass

        # Honour any UI-side action the runner requested. Long-running
        # operations live here, not in the runner, so we own the banner +
        # cancellation state.
        if response.action == "rebuild_graph":
            await self._do_graph_rebuild()

    async def _append_bubble(
        self,
        sender: str,
        text: str,
        speak: bool = False,
        speak_text: str | None = None,
        entities: dict | None = None,
        intent = None,
    ):
        # Update in-memory history list. Persist structured entity data on
        # assistant messages so _resolve_anaphora can do a plain dict lookup
        # instead of regex-parsing markdown bold tags.
        msg: dict = {"sender": sender, "text": text}
        if entities:
            msg["entities"] = entities
        if intent:
            msg["intent"] = {
                "name": intent.name if hasattr(intent, "name") else intent.get("name"),
                "query": intent.query if hasattr(intent, "query") else intent.get("query"),
                "raw": intent.raw if hasattr(intent, "raw") else intent.get("raw"),
                "extras": intent.extras if hasattr(intent, "extras") else intent.get("extras", {}),
            }

        row = self._build_bubble_row(msg)
        self._history_list.append(msg)
        if len(self._history_list) > 50:
            self._history_list.pop(0)

        # Save to disk
        self.chat_memory.save_session(self._history_list, self._init_greeted)

        def _mutate():
            # Drop the empty-state on first real message.
            if self._messages.controls and isinstance(self._messages.controls[0], ft.Container) \
                    and not isinstance(getattr(self._messages.controls[0], "content", None), ft.Row):
                if len(self._messages.controls) == 1:
                    self._messages.controls = []
            self._messages.controls.append(row)
            
            # Proactive performance cap: keep history limited to 50 bubbles maximum
            if len(self._messages.controls) > 50:
                self._messages.controls.pop(0)
        self.app.safe_update(_mutate)

        # Smoothly scroll to the bottom after the UI has flushed and rendered.
        # Tall bubbles (VLM-matched bubbles carry a 3-row diagnostic footer;
        # markdown content lays out asynchronously) finish painting after the
        # first scroll fires, leaving the new content below the fold — so we
        # do a second pass once layout has settled.
        await asyncio.sleep(0.06)
        try:
            await self._messages.scroll_to(offset=-1, duration=200)
        except Exception:
            pass
        await asyncio.sleep(0.25)
        try:
            await self._messages.scroll_to(offset=-1, duration=120)
        except Exception:
            pass

        if speak and self._tts_enabled:
            service = getattr(audio_engine, "audio_service", None)
            if service is not None:
                try:
                    # Pause music briefly so TTS isn't drowned out. Resume
                    # afterwards only if we were the ones who paused.
                    was_playing = bool(getattr(audio_engine, "is_playing", False))
                    if was_playing:
                        audio_engine.pause()
                    await service.tts_speak(speak_text or text, timeout=30.0)
                    if was_playing:
                        audio_engine.play()
                except Exception as exc:
                    logger.warning("AssistantView: TTS speak failed: %s", exc)

    def _build_bubble_row(self, msg: dict) -> ft.Row:
        sender = msg.get("sender", "assistant")
        text = msg.get("text", "")
        is_user = (sender == "user")

        if is_user:
            bubble_content = ft.Text(
                text,
                color="#FFFFFF",
                size=13,
                selectable=True,
            )
        else:
            bubble_content = ft.Markdown(
                value=text,
                selectable=True,
                extension_set=ft.MarkdownExtensionSet.GITHUB_FLAVORED,
                code_theme="github-dark",
                auto_follow_links=True,
            )

        intent = msg.get("intent")
        if not is_user and intent:
            intent_name = intent.get("name", "unknown")
            extras = intent.get("extras") or {}
            is_semantic = extras.get("semantic", False)

            # Stage 1: Regex
            if intent_name != "unknown" and not is_semantic:
                s1_text = f"Stage 1: Regex (Matched: {intent_name})"
                s1_icon = ft.Icons.CHECK_CIRCLE_OUTLINE
                s1_color = "#81C784"
                
                s2_text = "Stage 2: VLM (Skipped)"
                s2_icon = ft.Icons.REMOVE_CIRCLE_OUTLINE
                s2_color = DIM
            elif is_semantic:
                s1_text = "Stage 1: Regex (No Match)"
                s1_icon = ft.Icons.CANCEL_OUTLINED
                s1_color = "#E57373"
                
                score = extras.get("score")
                score_str = f" @ {score:.2f}" if score is not None else ""
                duration = extras.get("compute_time_windows_ms")
                dur_str = f" in {duration:.1f}ms" if duration is not None else ""
                s2_text = f"Stage 2: VLM (Matched: {intent_name}{score_str}{dur_str})"
                s2_icon = ft.Icons.CHECK_CIRCLE_OUTLINE
                s2_color = "#81C784"
            else:
                s1_text = "Stage 1: Regex (No Match)"
                s1_icon = ft.Icons.CANCEL_OUTLINED
                s1_color = "#E57373"
                
                s2_text = "Stage 2: VLM (No Match)"
                s2_icon = ft.Icons.CANCEL_OUTLINED
                s2_color = "#E57373"

            bubble_content = ft.Column(
                [
                    bubble_content,
                    ft.Container(height=1, bgcolor="#262626", margin=ft.Margin.symmetric(vertical=6)),
                    ft.Row(
                        [
                            ft.Icon(s1_icon, color=s1_color, size=11),
                            ft.Text(s1_text, color=s1_color, size=10, weight=ft.FontWeight.W_500)
                        ],
                        spacing=6,
                        alignment=ft.MainAxisAlignment.START,
                    ),
                    ft.Row(
                        [
                            ft.Icon(s2_icon, color=s2_color, size=11),
                            ft.Text(s2_text, color=s2_color, size=10, weight=ft.FontWeight.W_500)
                        ],
                        spacing=6,
                        alignment=ft.MainAxisAlignment.START,
                    )
                ],
                spacing=4,
                tight=True,
            )

        bubble = ft.Container(
            content=bubble_content,
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            bgcolor=CYAN if is_user else SURFACE2,
            border_radius=14,
            width=290 if (len(text) > 25 or intent) else None,
        )
        return ft.Row(
            [bubble],
            alignment=(
                ft.MainAxisAlignment.END if is_user
                else ft.MainAxisAlignment.START
            ),
        )

    def _build_empty_state(self) -> ft.Container:
        return ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.AUTO_AWESOME_ROUNDED, color=CYAN, size=42),
                    ft.Text("Jarvis", color=TEXT, size=20,
                            weight=ft.FontWeight.W_900,
                            text_align=ft.TextAlign.CENTER),
                    ft.Text(
                        "Try: 'play stairway', 'more like this', "
                        "'add radiohead to queue', or 'help'.",
                        color=DIM, size=12, text_align=ft.TextAlign.CENTER,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
            padding=ft.Padding.all(24),
        )

    def clear_chat_manually(self):
        """Wipes the session history on disk and clears it in the UI."""
        self._history_list = []
        self._init_greeted = False
        self.chat_memory.clear_session()
        
        def _mutate():
            self._messages.controls = [self._build_empty_state()]
        self.app.safe_update(_mutate)

    def handle_app_background(self):
        """Invoked when the app is backgrounded to write the latest timestamp."""
        self.chat_memory.touch_session()

    def handle_app_resume(self):
        """Invoked when returning to foreground. If expired, clears UI and re-triggers greet."""
        session = self.chat_memory.load_session()
        # If the session was expired, load_session cleared it, returning empty lists
        if not session["messages"] and self._history_list:
            logger.info("Chat Memory: Inactivity timeout reached. Resetting conversation.")
            self._history_list = []
            self._init_greeted = False
            
            def _mutate():
                self._messages.controls = [self._build_empty_state()]
            self.app.safe_update(_mutate)
            
            # Re-trigger initialisation/greeting flow
            self.page.run_task(self._init_assistant)


# ─── Quality Selector Sheet ────────────────────────────────────────────────────
class QualitySelectorSheet:
    def __init__(self, app: "StreamripFletApp"):
        self.app            = app
        self.page           = app.page
        self._current_track = None
        self._initialized   = False
        self._sheet         = None

    def _ensure_initialized(self):
        if self._initialized:
            return
        self._sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text("Select Quality", color=TEXT, size=15, weight=ft.FontWeight.W_700),
                        ft.Divider(color=BORDER),
                        ft.ListTile(
                            title=ft.Text("High", color=TEXT),
                            subtitle=ft.Text("MP3 / AAC 320kbps", color=DIM),
                            leading=ft.Icon(ft.Icons.MUSIC_NOTE, color=CYAN),
                            on_click=lambda e: self._enqueue("mp3"),
                        ),
                        ft.ListTile(
                            title=ft.Text("CD Quality", color=TEXT),
                            subtitle=ft.Text("16-bit FLAC", color=DIM),
                            leading=ft.Icon(ft.Icons.ALBUM, color=CYAN),
                            on_click=lambda e: self._enqueue("cd"),
                        ),
                        ft.ListTile(
                            title=ft.Text("Hi-Res", color=TEXT),
                            subtitle=ft.Text("24-bit FLAC", color=DIM),
                            leading=ft.Icon(ft.Icons.HIGH_QUALITY, color=CYAN),
                            on_click=lambda e: self._enqueue("hires"),
                        ),
                    ],
                    spacing=0,
                    tight=True,
                ),
                bgcolor=SURFACE,
                padding=20,
            ),
            bgcolor=SURFACE,
        )
        self._initialized = True

    def build(self) -> ft.BottomSheet:
        self._ensure_initialized()
        return self._sheet

    def show(self, track_data: dict):
        self._current_track = track_data
        self.page.bottom_sheet = self._sheet
        self._sheet.open = True
        self.page.update()

    def _close(self):
        self._sheet.open = False
        self.page.update()

    def _enqueue(self, selected_tier: str):
        if not self._current_track:
            return
        self.app.queue.enqueue(self._current_track, quality_tier=selected_tier)
        self._close()


# ─── Metadata Editor Dialog ────────────────────────────────────────────────────
class PlaylistEditorDialog:
    def __init__(self, app: "StreamripFletApp"):
        self.app  = app
        self.page = app.page
        self._dlg = None

    def open(self, pl_id: int, name: str, current_color: str):
        t_name = ft.TextField(value=name, label="Playlist Name", border_color=BORDER, focused_border_color=LIB_PLAYLIST_COLOR, bgcolor=SURFACE)
        
        colors = ["#FF5555", "#55FF55", "#5555FF", "#FFFF55", "#FF55FF", "#55FFFF", "#FFFFFF", "#FF8C00", "#8A2BE2"]
        
        selected_color = [current_color]

        def set_color(c):
            selected_color[0] = c
            for i, circle in enumerate(color_row.controls):
                circle.border = ft.Border.all(2, TEXT if colors[i] == c else "transparent")
            self.page.update()

        color_row = ft.Row(
            [
                ft.Container(
                    width=24, height=24, bgcolor=c, border_radius=12,
                    border=ft.Border.all(2, TEXT if c == current_color else "transparent"),
                    on_click=lambda e, color=c: set_color(color)
                ) for c in colors
            ],
            wrap=True, spacing=10
        )

        def save(e):
            async def _do():
                self._dlg.open = False
                self.page.update()
                await self.app.db_manager.update_playlist(pl_id, name=t_name.value, color=selected_color[0])
                await self.app.library_view.load_library()
                self.app.show_snackbar(f"Playlist '{t_name.value}' updated.")
            self.page.run_task(_do)

        self._dlg = ft.AlertDialog(
            title=ft.Text("Customize Playlist", color=TEXT),
            bgcolor=SURFACE,
            content=ft.Column([
                t_name,
                ft.Text("Accent Color", color=DIM, size=12),
                color_row
            ], spacing=15, tight=True),
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: self._close()),
                ft.Button("Save", bgcolor=LIB_PLAYLIST_COLOR, color=BG, on_click=save)
            ]
        )
        self.page.overlay.append(self._dlg)
        self._dlg.open = True
        self.page.update()

    def _close(self):
        if self._dlg:
            self._dlg.open = False
            self.page.update()

class MetadataEditorDialog:
    def __init__(self, app: "StreamripFletApp"):
        self.app  = app
        self.page = app.page
        self._dlg: ft.AlertDialog | None = None

    def open(self, edit_type: str, meta: dict):
        path        = meta.get("path", "")
        title_val   = meta.get("track_title", "")
        artist_val  = meta.get("artist_name", "")
        album_val   = meta.get("album_title", "")

        t_title  = ft.TextField(value=title_val,  hint_text="Track Title",
                                border_color=BORDER, focused_border_color=CYAN,
                                text_style=ft.TextStyle(color=TEXT), bgcolor=SURFACE)
        t_artist = ft.TextField(value=artist_val, hint_text="Artist",
                                border_color=BORDER, focused_border_color=CYAN,
                                text_style=ft.TextStyle(color=TEXT), bgcolor=SURFACE)
        t_album  = ft.TextField(value=album_val,  hint_text="Album",
                                border_color=BORDER, focused_border_color=CYAN,
                                text_style=ft.TextStyle(color=TEXT), bgcolor=SURFACE)

        # artwork preview (async extraction)
        art_image = ft.Image(src="", width=100, height=100, fit="cover",
                              border_radius=ft.BorderRadius.all(8), visible=False)
        art_box   = ft.Container(
            content=art_image,
            width=100, height=100,
            bgcolor=SURFACE2,
            border_radius=8,
            alignment=ft.Alignment(0, 0),
            padding=ft.Padding.all(0),
        )

        def load_art():
            if path:
                try:
                    from utils.metadata_editor import extract_artwork
                    raw = extract_artwork(path)
                    if raw:
                        ext      = "png" if raw.startswith(b"\x89PNG") else "jpg"
                        ph       = hashlib.md5(path.encode()).hexdigest()
                        tmp_path = os.path.join(get_temp_artwork_dir(), f"meta_art_{ph}.{ext}")
                        with open(tmp_path, "wb") as fh:
                            fh.write(raw)
                        art_image.src     = get_asset_path(tmp_path)
                        def _show_art():
                            art_image.visible = True
                        self.app.safe_update(_show_art)
                except Exception:
                    pass

        asyncio.create_task(asyncio.to_thread(load_art))

        content_cols = [art_box]
        if edit_type == "track":
            content_cols.append(t_title)
        content_cols += [t_artist, t_album]

        def save(e):
            def _mutate():
                self._dlg.open = False
            self.app.safe_update(_mutate)
            self.app.page.run_task(self.app.apply_metadata_edit,
                edit_type, meta,
                t_title.value, t_artist.value, t_album.value,
            )

        def delete(e):
            self._close()
            self.app.confirm_delete_track(path, title_val)

        actions = [
            ft.TextButton("Cancel", on_click=lambda e: self._close()),
            ft.Button(
                content=ft.Text("Save"),
                style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
                on_click=save,
            ),
        ]
        if edit_type == "track" and path:
            actions.insert(0, ft.TextButton(
                content=ft.Text("DELETE TRACK", color="#FF4444", size=11, weight="bold"),
                on_click=delete
            ))

        self._dlg = ft.AlertDialog(
            title=ft.Text("Edit Metadata", color=TEXT),
            bgcolor=SURFACE,
            content=ft.Column(content_cols, spacing=12, tight=True),
            actions=actions,
        )
        self.page.overlay.append(self._dlg)
        self._dlg.open = True
        self.page.update()

    def _close(self):
        if self._dlg:
            self._dlg.open = False
            self.page.update()


class ErrorBoundary:
    def __init__(self, page, on_restart=None):
        self.page = page
        self.on_restart = on_restart
        self._error_view = ft.Container(
            content=ft.Column([
                ft.Icon(ft.Icons.ERROR_OUTLINE, color="#FF4444", size=64),
                ft.Text("Something went wrong", size=20, weight=ft.FontWeight.W_700, color=TEXT),
                ft.Text("Tap to restart", size=14, color=DIM),
            ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            alignment=ft.Alignment(0, 0),
            visible=False,
            bgcolor=BG,
            expand=True,
            on_click=lambda _: self.restart()
        )
        
    def capture(self, fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                if asyncio.iscoroutinefunction(fn):
                    return await fn(*args, **kwargs)
                else:
                    return fn(*args, **kwargs)
            except Exception as e:
                logger.exception("Captured error")
                self._show_error(e)
        return wrapper
        
    def _show_error(self, e=None):
        if e:
            # Add selectable error detail if provided
            detail = ft.Text(f"Error: {e}", color="#FF4444", size=11, selectable=True, text_align=ft.TextAlign.CENTER)
            self._error_view.content.controls.insert(2, detail)
            
        self._error_view.visible = True
        try:
            self.page.update()
        except:
            pass

    def restart(self):
        self._error_view.visible = False
        try:
            self.page.update()
        except:
            pass
        if self.on_restart:
            if asyncio.iscoroutinefunction(self.on_restart):
                asyncio.create_task(self.on_restart())
            else:
                self.on_restart()


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
            global CYAN
            CYAN = acc_color
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
                self.page.run_task(self._recommend_similar_async, path)

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
            
            self.mini_player.update_artwork(img_url)
            self.now_playing.update_artwork(img_url)

            # Highlight the currently-playing row in both views
            self.search_view.refresh_now_playing()
            self.library_view.refresh_now_playing()

            if isinstance(img_url, str) and img_url.startswith("http"):
                self._fetch_artwork_url_async(img_url)
            elif track and path:
                self._extract_artwork_async(path)

        self.safe_update(_atomic_update)

    async def _record_play_event_safe(self, path: str, played: float, duration: float):
        """Background-safe wrapper around tg.record_play_event so the engine's
        sync dispatch doesn't get coupled to import-time / DB errors. Also
        feeds the session-scoped negative centroid / trip-wire so consecutive
        bad continuations get steered away from in the next walk."""
        from utils import track_graph as tg
        from utils import taste_model as _tm
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
            y = _tm.classify_play_event(float(played or 0.0), float(duration or 0.0))
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

        if not like and self.play_similar_mode:
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

    async def _recommend_similar_async(self, path: str):
        import os
        from utils import track_graph as tg
        try:
            # Build comprehensive avoid set (currently queued + session skipped/disliked + 7-day recently played + current path)
            avoid = {t["path"] for t in audio_engine.queue if t.get("path")}
            avoid.add(path)
            for bad in self._session_bad_paths:
                avoid.add(bad)
            try:
                recent = await self.db_manager.recent_played_paths(window_seconds=7 * 86400)
                avoid.update(recent)
            except Exception:
                pass

            # Anchored PageRank: step from the current track, but teleport back to the original seed to prevent genre drift
            teleport = getattr(audio_engine, "play_similar_seed_path", "") or path
            walk_tracks = await tg.walk(
                self.db_manager,
                path,
                length=3,
                edge_kinds=(tg.KIND_ACOUSTIC, tg.KIND_ARTIST),
                teleport_path=teleport,
                avoid=avoid,
            )
            if walk_tracks:
                queue_paths = {t["path"] for t in audio_engine.queue}
                next_track_path = None
                for wt in walk_tracks:
                    if wt not in queue_paths:
                        next_track_path = wt
                        break
                
                if not next_track_path:
                    next_track_path = walk_tracks[0]
                
                row = await self.db_manager.get_track_full(next_track_path)
                if row:
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
                temperature=0.08,
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

        db = self.db_manager

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
            want_key = ("tracks", lv.search_query, lv.sort_mode)
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

        audio_engine.jarvis_controlled = False
        audio_engine.set_queue(tracks, start_index=target_idx)

    def toggle_shuffle(self):
        audio_engine.is_shuffle = not audio_engine.is_shuffle
        self.now_playing.update_shuffle(audio_engine.is_shuffle)
        self.page.update()

    def cycle_repeat(self):
        modes = ["none", "one", "all"]
        mode  = modes[(modes.index(audio_engine.repeat_mode) + 1) % 3]
        audio_engine.repeat_mode = mode
        self.now_playing.update_repeat(mode)
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
