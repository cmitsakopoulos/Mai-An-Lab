import json
import os
import time
import logging
import threading
import random
import pathlib
import signal
import re
import flet as ft
import ffmpeg
from tinytag import TinyTag

logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)


class AudioEngine:
    """
    macOS fallback audio engine using ffmpeg-python.
    """

    def __init__(self):
        self.current_track  = ""
        self.current_artist = ""
        self.current_album  = ""
        self.current_path   = ""
        self.current_art    = ""
        self.position       = 0.0
        self.duration       = 0.0
        self.is_playing     = False
        self.is_shuffle     = False
        self.repeat_mode    = "none"

        self.queue: list[dict] = []
        self.current_index: int = 0

        self._page:  ft.Page | None = None
        self._observers: dict[str, list] = {}
        self._obs_lock = threading.Lock()
        
        self._process = None
        self._monitor_thread = None
        self._stop_event = threading.Event()
        
        self._start_time_offset = 0.0

    def setup(self, page: ft.Page):
        self._page = page

    def bind(self, **kwargs):
        with self._obs_lock:
            for name, fn in kwargs.items():
                lst = self._observers.setdefault(name, [])
                if fn not in lst:
                    lst.append(fn)

    def unbind(self, **kwargs):
        with self._obs_lock:
            for name, fn in kwargs.items():
                try:
                    self._observers.get(name, []).remove(fn)
                except ValueError:
                    pass

    def dispatch(self, name: str, value=None):
        with self._obs_lock:
            fns = list(self._observers.get(name, []))
        for fn in fns:
            try:
                fn(self, value)
            except Exception as exc:
                logger.error("AudioEngine dispatch error (%s): %s", name, exc)

    def _set(self, attr: str, value):
        if getattr(self, attr, None) == value:
            return
        setattr(self, attr, value)
        self.dispatch(attr, value)

    def _sync_metadata_for_current(self):
        if not self.queue or not (0 <= self.current_index < len(self.queue)):
            return
        track = self.queue[self.current_index]
        path = track.get("path") or ""
        title = (track.get("track_title") or os.path.basename(path)) if path else "Unknown"
        self._set("position", 0.0)
        self._set("duration", 0.0)
        self._set("current_artist", track.get("artist_name", "Unknown Artist"))
        self._set("current_album",  track.get("album_title",  "Unknown Album"))
        self._set("current_track",  title)
        self._set("current_path",   path)
        art = track.get("artwork_path") or ""
        if not art and path:
            folder = os.path.dirname(path)
            for name in ("cover.jpg", "folder.jpg", "cover.png", "front.jpg"):
                p = os.path.join(folder, name)
                if os.path.exists(p):
                    art = p
                    break
        self._set("current_art", art)
        
        if path and os.path.exists(path):
            try:
                tag = TinyTag.get(path)
                if tag.duration:
                    self._set("duration", tag.duration)
            except Exception as e:
                logger.error(f"Failed to extract duration: {e}")

    def set_queue(self, tracks: list[dict], start_index: int = 0):
        self.queue = tracks
        self.current_index = min(max(0, start_index), len(self.queue) - 1) if self.queue else 0
        self.dispatch("on_queue_mutated")
        self._sync_metadata_for_current()

    async def play_current(self):
        if not self.queue:
            self.stop()
            return
        now = time.time()
        last_play = getattr(self, "_last_play_time", 0)
        if now - last_play < 0.5:
            return
        self._last_play_time = now
        self._sync_metadata_for_current()
        path = self.queue[self.current_index].get("path")
        if path:
            self._start_playback(path)

    def _start_playback(self, path: str, start_time: float = 0.0):
        self._stop_playback()
        self._stop_event.clear()
        self._start_time_offset = start_time

        try:
            kwargs = {}
            if start_time > 0:
                kwargs['ss'] = start_time
            
            self._process = (
                ffmpeg
                .input(path, **kwargs)
                .output('-', format='audiotoolbox', vn=None)
                .global_args('-nostdin')
                .run_async(pipe_stderr=True)
            )
            self._set("is_playing", True)
            
            self._monitor_thread = threading.Thread(target=self._monitor_ffmpeg, args=(self._process,), daemon=True)
            self._monitor_thread.start()
        except Exception as e:
            logger.error(f"FFmpeg playback error: {e}")
            self._set("is_playing", False)
            self.dispatch("on_playback_error", str(e))

    def _stop_playback(self):
        self._stop_event.set()
        if self._process:
            try:
                self._process.kill()
                self._process.wait(timeout=1.0)
            except Exception:
                pass
            self._process = None
        if self._monitor_thread:
            self._monitor_thread = None

    def _monitor_ffmpeg(self, process):
        time_pattern = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
        for line in process.stderr:
            if self._stop_event.is_set():
                break
            line_str = line.decode('utf-8', errors='ignore')
            match = time_pattern.search(line_str)
            if match:
                h, m, s = match.groups()
                current_time = int(h) * 3600 + int(m) * 60 + float(s) + self._start_time_offset
                now = time.time()
                if not hasattr(self, "_last_pos_time") or now - self._last_pos_time > 0.9:
                    self._set("position", current_time)
                    self._last_pos_time = now

        process.wait()
        if not self._stop_event.is_set() and process.returncode == 0:
            if self._page:
                self._page.run_task(self._on_track_ended)

    async def _on_track_ended(self):
        self.next(auto_advance=True)

    def play(self):
        if not self.is_playing and self.queue:
            path = self.queue[self.current_index].get("path")
            if path:
                # Restart ffmpeg from the current position
                self._start_playback(path, self.position)

    def pause(self):
        if self._process and self.is_playing:
            self._set("is_playing", False)
            # Kill the process entirely to avoid audiotoolbox buffer hang
            self._stop_playback()

    def toggle(self):
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def seek(self, target: float):
        if not self._process and not self.queue:
            return
            
        bounded = max(0.0, float(target))
        if self.duration and self.duration > 0.0:
            bounded = min(bounded, max(0.0, self.duration - 0.5))
            
        self._set("position", bounded)
        
        if self.is_playing:
            path = self.queue[self.current_index].get("path")
            if path:
                self._start_playback(path, bounded)
        else:
            self._start_time_offset = bounded

    def next(self, auto_advance=False):
        if self.repeat_mode == "one" and auto_advance:
            self.seek(0)
            if self._page:
                self._page.run_task(self.play_current)
            return
            
        if self.is_shuffle and len(self.queue) > 1:
            candidates = [i for i in range(len(self.queue)) if i != self.current_index]
            target = random.choice(candidates)
            self.current_index = target
            if self._page:
                self._page.run_task(self.play_current)
            return
            
        if self.current_index < len(self.queue) - 1:
            self.current_index += 1
            if self._page:
                self._page.run_task(self.play_current)
        elif self.repeat_mode == "all":
            self.current_index = 0
            if self._page:
                self._page.run_task(self.play_current)
        else:
            self.stop()

    def previous(self):
        if self.position > 3.0:
            self.seek(0)
            return
        if self.current_index > 0:
            self.current_index -= 1
            if self._page:
                self._page.run_task(self.play_current)

    def queue_next(self, track: dict):
        if not self.queue:
            self.set_queue([track])
            return
        if len(self.queue) >= 25:
            return
        insert_at = min(self.current_index + 1, len(self.queue))
        self.queue.insert(insert_at, track)
        self.dispatch("on_queue_mutated")

    def play_track_at(self, index: int):
        if not (0 <= index < len(self.queue)):
            return
        self.current_index = index
        if self._page:
            self._page.run_task(self.play_current)

    def remove_from_queue(self, index: int):
        if not 0 <= index < len(self.queue):
            return
        removed_active = (index == self.current_index)
        self.queue.pop(index)
        if not self.queue:
            self.stop()
            return
        if index < self.current_index:
            self.current_index -= 1
        elif removed_active:
            self.current_index = min(self.current_index, len(self.queue) - 1)
            if self._page:
                self._page.run_task(self.play_current)
        self.dispatch("on_queue_mutated")

    def move_queue_item(self, old_index: int, new_index: int):
        if not (0 <= old_index < len(self.queue) and 0 <= new_index < len(self.queue)):
            return
        current_obj = self.queue[self.current_index] if self.queue else None
        item = self.queue.pop(old_index)
        self.queue.insert(new_index, item)
        if current_obj is not None and current_obj in self.queue:
            self.current_index = self.queue.index(current_obj)
        self.dispatch("on_queue_mutated")

    def clear_queue(self):
        self.stop()
        self.queue.clear()
        self.current_index = 0
        self.dispatch("on_queue_mutated")

    def restore_queue(self, tracks: list[dict], index: int, position: float = 0.0,
                      duration: float = 0.0):
        if not tracks or not self._page:
            return
        self.queue = tracks[:25]
        self.current_index = min(max(0, index), len(self.queue) - 1)
        track = tracks[self.current_index]
        path  = track.get("path", "")
        title = (track.get("track_title") or os.path.basename(path)) if path else "Unknown"

        self._set("position",       max(0.0, float(position)))
        self._set("duration",       max(0.0, float(duration)))
        self._set("is_playing",     False)
        self._set("current_artist", track.get("artist_name", "Unknown Artist"))
        self._set("current_album",  track.get("album_title",  "Unknown Album"))
        self._set("current_track",  title)
        self._set("current_path",   path)

        if path and os.path.exists(path):
            self._start_time_offset = position
            # We don't start playback, just setup offset
            # When user presses play, it will seek.

    def stop(self):
        self._stop_playback()
        self.current_index = 0
        self._set("is_playing",     False)
        self._set("current_track",  "")
        self._set("current_artist", "")
        self._set("current_album",  "")
        self._set("current_path",   "")
        self._set("duration",       0.0)
        self._set("position",       0.0)

    def shutdown(self):
        self._stop_playback()


audio_engine = AudioEngine()
