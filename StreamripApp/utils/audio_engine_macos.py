import sys
import os
import time
import logging
import threading
import random
import flet as ft
from tinytag import TinyTag

if sys.platform == "darwin":
    try:
        from Foundation import NSURL
        from AVFoundation import AVAudioPlayer
        _AVF_AVAILABLE = True
    except ImportError:
        NSURL = None
        AVAudioPlayer = None
        _AVF_AVAILABLE = False
else:
    NSURL = None
    AVAudioPlayer = None
    _AVF_AVAILABLE = False

logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)


class AudioEngine:
    """macOS audio engine backed by AVAudioPlayer (AVFoundation via pyobjc)."""

    def __init__(self):
        self.current_track  = ""
        self.current_artist = ""
        self.current_album  = ""
        self.current_path   = ""
        self.current_art    = ""
        self.position       = 0.0
        self.duration       = 0.0
        self.is_playing     = False
        self._is_shuffle    = False
        self._shuffle_order: list[int] = []
        self.repeat_mode    = "none"

        self.queue: list[dict] = []
        self.current_index: int = 0
        self.jarvis_controlled = False

        self._page:  ft.Page | None = None
        self._observers: dict[str, list] = {}
        self._obs_lock = threading.Lock()

        self._player = None
        self._poll_thread: threading.Thread | None = None
        self._poll_stop: threading.Event | None = None
        self._lock = threading.RLock()

    def setup(self, page: ft.Page):
        self._page = page

    @property
    def audio_service(self):
        """macOS: no native MethodChannel bridge."""
        return None

    @property
    def is_shuffle(self) -> bool:
        return self._is_shuffle

    @is_shuffle.setter
    def is_shuffle(self, val: bool):
        val = bool(val)
        if getattr(self, "_is_shuffle", False) == val:
            return
        self._is_shuffle = val
        self._on_shuffle_changed()

    def _on_shuffle_changed(self):
        if self._is_shuffle:
            indices = list(range(len(self.queue)))
            if 0 <= self.current_index < len(self.queue):
                indices.remove(self.current_index)
                random.shuffle(indices)
                self._shuffle_order = [self.current_index] + indices
            else:
                random.shuffle(indices)
                self._shuffle_order = indices
        else:
            self._shuffle_order = []

    def _on_track_added_to_shuffle(self, insert_at: int, play_next: bool = True):
        if not getattr(self, "_shuffle_order", None):
            self._shuffle_order = list(range(len(self.queue)))
            return
        # Adjust indices greater than or equal to the inserted index
        self._shuffle_order = [
            (x + 1 if x >= insert_at else x) for x in self._shuffle_order
        ]
        # Insert the new index into shuffle order
        if play_next:
            try:
                curr_pos = self._shuffle_order.index(self.current_index)
                self._shuffle_order.insert(curr_pos + 1, insert_at)
            except ValueError:
                self._shuffle_order.append(insert_at)
        else:
            # Append randomly to the remaining unplayed portion or just the end
            try:
                curr_pos = self._shuffle_order.index(self.current_index)
                if curr_pos < len(self._shuffle_order) - 1:
                    insert_pos = random.randint(curr_pos + 1, len(self._shuffle_order))
                    self._shuffle_order.insert(insert_pos, insert_at)
                else:
                    self._shuffle_order.append(insert_at)
            except ValueError:
                self._shuffle_order.append(insert_at)

    def _on_track_removed_from_shuffle(self, index: int):
        if not getattr(self, "_shuffle_order", None):
            return
        # Remove the index from shuffle order
        if index in self._shuffle_order:
            self._shuffle_order.remove(index)
        # Adjust indices larger than the removed index
        self._shuffle_order = [
            (x - 1 if x > index else x) for x in self._shuffle_order
        ]

    def _on_track_moved_in_shuffle(self, old_index: int, new_index: int):
        if not getattr(self, "_shuffle_order", None):
            return
        new_order = []
        for x in self._shuffle_order:
            if x == old_index:
                new_order.append(new_index)
            elif old_index < new_index:
                if old_index < x <= new_index:
                    new_order.append(x - 1)
                else:
                    new_order.append(x)
            else:  # new_index < old_index
                if new_index <= x < old_index:
                    new_order.append(x + 1)
                else:
                    new_order.append(x)
        self._shuffle_order = new_order

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
        if self._is_shuffle:
            indices = list(range(len(self.queue)))
            if 0 <= self.current_index < len(self.queue):
                indices.remove(self.current_index)
                random.shuffle(indices)
                self._shuffle_order = [self.current_index] + indices
            else:
                random.shuffle(indices)
                self._shuffle_order = indices
        else:
            self._shuffle_order = []
        self.dispatch("on_queue_mutated")
        self._sync_metadata_for_current()
        # Mirror the Android engine: setting a queue auto-plays from
        # start_index. Without this, clicking a track widget updates the
        # mini-player metadata but the previous AVAudioPlayer keeps going.
        if self.queue:
            path = self.queue[self.current_index].get("path")
            if path:
                self._start_playback(path)

    async def play_current(self):
        with self._lock:
            if not self.queue:
                self.stop()
                return
            self._sync_metadata_for_current()
            path = self.queue[self.current_index].get("path")
            if path:
                self._start_playback(path)

    def _start_playback(self, path: str, start_time: float = 0.0):
        with self._lock:
            self._stop_playback()

            if not _AVF_AVAILABLE:
                err_msg = "AVFoundation/pyobjc not installed. Install pyobjc-framework-AVFoundation."
                logger.error(err_msg)
                self._set("is_playing", False)
                self.dispatch("on_playback_error", err_msg)
                return
            if not path or not os.path.exists(path):
                err_msg = f"File not found: {path}"
                logger.error(err_msg)
                self._set("is_playing", False)
                self.dispatch("on_playback_error", err_msg)
                return

            try:
                url = NSURL.fileURLWithPath_(path)
                player, error = AVAudioPlayer.alloc().initWithContentsOfURL_error_(url, None)
                if player is None:
                    raise RuntimeError(f"AVAudioPlayer init failed: {error}")
                player.prepareToPlay()
                if start_time > 0:
                    player.setCurrentTime_(float(start_time))
                if not player.play():
                    raise RuntimeError("AVAudioPlayer.play() returned False")

                self._player = player
                dur = float(player.duration() or 0.0)
                if dur > 0:
                    self._set("duration", dur)
                self._set("position", float(start_time))
                self._set("is_playing", True)
                self._spawn_poller()
            except Exception as e:
                logger.error(f"AVAudioPlayer error: {e}")
                self._player = None
                self._set("is_playing", False)
                self.dispatch("on_playback_error", str(e))

    def _spawn_poller(self):
        self._poll_stop = threading.Event()
        stop_event = self._poll_stop
        player = self._player
        self._poll_thread = threading.Thread(
            target=self._poll_position,
            args=(player, stop_event),
            daemon=True,
        )
        self._poll_thread.start()

    # Poll cadence — desktop, battery cost is negligible.
    _POLL_INTERVAL = 0.10   # 10 Hz: end-of-track detection latency floor
    _DISPATCH_THROTTLE = 0.20  # 5 Hz: position UI update rate

    def _poll_position(self, player, stop_event):
        # AVAudioPlayer's delegate (audioPlayerDidFinishPlaying:) only fires
        # under a running NSRunLoop, which Flet/Python's main thread doesn't
        # provide. Polling isPlaying() handles end-of-track instead.
        last_dispatch = 0.0
        try:
            time.sleep(0.05)
            while not stop_event.is_set():
                try:
                    pos = float(player.currentTime())
                    playing = bool(player.isPlaying())
                except Exception:
                    break
                self.position = pos
                now = time.time()
                if now - last_dispatch > self._DISPATCH_THROTTLE:
                    self.dispatch("position", pos)
                    last_dispatch = now
                if not playing:
                    if self._page and not stop_event.is_set():
                        self._page.run_task(self._on_track_ended)
                    return
                time.sleep(self._POLL_INTERVAL)
        except Exception as e:
            logger.error(f"Position poller error: {e}")

    def _stop_playback(self):
        with self._lock:
            if self._poll_stop:
                self._poll_stop.set()
                self._poll_stop = None
            if self._player:
                try:
                    self._player.stop()
                except Exception:
                    pass
                self._player = None
            self._poll_thread = None

    async def _on_track_ended(self):
        self.next(auto_advance=True)

    def play(self):
        with self._lock:
            if self.is_playing or not self.queue:
                return
            if self._player is not None:
                try:
                    if self._player.play():
                        self._set("is_playing", True)
                        self._spawn_poller()
                        return
                except Exception as e:
                    logger.error(f"Resume failed, restarting: {e}")
            path = self.queue[self.current_index].get("path")
            if path:
                self._start_playback(path, self.position)

    def pause(self):
        with self._lock:
            if self._player is None or not self.is_playing:
                return
            try:
                self._player.pause()
            except Exception as e:
                logger.error(f"pause error: {e}")
            if self._poll_stop:
                self._poll_stop.set()
                self._poll_stop = None
            self._poll_thread = None
            self._set("is_playing", False)

    def toggle(self):
        with self._lock:
            if self.is_playing:
                self.pause()
            else:
                self.play()

    def seek(self, target: float):
        with self._lock:
            if not self.queue:
                return
            bounded = max(0.0, float(target))
            if self.duration and self.duration > 0.0:
                bounded = min(bounded, max(0.0, self.duration - 0.5))
            self._set("position", bounded)

            if self._player is not None:
                try:
                    self._player.setCurrentTime_(bounded)
                except Exception as e:
                    logger.error(f"seek error: {e}")

    def next(self, auto_advance=False):
        with self._lock:
            if self.repeat_mode == "one" and auto_advance:
                self.seek(0)
                if self._page:
                    self._page.run_task(self.play_current)
                return

            if self._is_shuffle and len(self.queue) > 1 and getattr(self, "_shuffle_order", None):
                try:
                    curr_shuf_idx = self._shuffle_order.index(self.current_index)
                except ValueError:
                    curr_shuf_idx = -1
                
                if curr_shuf_idx != -1 and curr_shuf_idx < len(self._shuffle_order) - 1:
                    target = self._shuffle_order[curr_shuf_idx + 1]
                    self.current_index = target
                    if self._page:
                        self._page.run_task(self.play_current)
                    return
                elif self.repeat_mode == "all":
                    target = self._shuffle_order[0]
                    self.current_index = target
                    if self._page:
                        self._page.run_task(self.play_current)
                    return
                else:
                    if getattr(self, "jarvis_controlled", False):
                        self.dispatch("on_jarvis_continue")
                    else:
                        self.stop()
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
                if getattr(self, "jarvis_controlled", False):
                    self.dispatch("on_jarvis_continue")
                else:
                    self.stop()

    def previous(self):
        with self._lock:
            if self.position > 3.0:
                self.seek(0)
                return
            if self._is_shuffle and len(self.queue) > 1 and getattr(self, "_shuffle_order", None):
                try:
                    curr_shuf_idx = self._shuffle_order.index(self.current_index)
                except ValueError:
                    curr_shuf_idx = -1
                
                if curr_shuf_idx > 0:
                    target = self._shuffle_order[curr_shuf_idx - 1]
                    self.current_index = target
                    if self._page:
                        self._page.run_task(self.play_current)
                    return

            if self.current_index > 0:
                self.current_index -= 1
                if self._page:
                    self._page.run_task(self.play_current)

    def queue_next(self, track: dict):
        with self._lock:
            if not self.queue:
                self.set_queue([track])
                return
            insert_at = min(self.current_index + 1, len(self.queue))
            self.queue.insert(insert_at, track)
            if self._is_shuffle:
                self._on_track_added_to_shuffle(insert_at, play_next=True)
            self.dispatch("on_queue_mutated")

    def queue_last(self, track: dict):
        with self._lock:
            if not self.queue:
                self.set_queue([track])
                return
            self.queue.append(track)
            if self._is_shuffle:
                self._on_track_added_to_shuffle(len(self.queue) - 1, play_next=False)
            self.dispatch("on_queue_mutated")

    def play_track_at(self, index: int):
        with self._lock:
            if not (0 <= index < len(self.queue)):
                return
            self.current_index = index
            self._sync_metadata_for_current()
            path = self.queue[self.current_index].get("path")
            if path:
                self._start_playback(path)

    def remove_from_queue(self, index: int):
        with self._lock:
            if not 0 <= index < len(self.queue):
                return
            removed_active = (index == self.current_index)
            if self._is_shuffle and getattr(self, "_shuffle_order", None):
                self._on_track_removed_from_shuffle(index)
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
        with self._lock:
            if not (0 <= old_index < len(self.queue) and 0 <= new_index < len(self.queue)):
                return
            current_obj = self.queue[self.current_index] if self.queue else None
            if self._is_shuffle and getattr(self, "_shuffle_order", None):
                self._on_track_moved_in_shuffle(old_index, new_index)
            item = self.queue.pop(old_index)
            self.queue.insert(new_index, item)
            if current_obj is not None and current_obj in self.queue:
                self.current_index = self.queue.index(current_obj)
            self.dispatch("on_queue_mutated")

    def clear_queue(self):
        with self._lock:
            self.stop()
            self.queue.clear()
            self.current_index = 0
            self.dispatch("on_queue_mutated")

    def restore_queue(self, tracks: list[dict], index: int, position: float = 0.0,
                      duration: float = 0.0):
        with self._lock:
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
            # Playback not started. Press-play will seek to self.position.

    def stop(self):
        with self._lock:
            self.jarvis_controlled = False
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
        with self._lock:
            self._stop_playback()


audio_engine = AudioEngine()
