"""
StreamripApp Audio Engine; flet_audio backend (audioplayers; Android native).

A single Audio service is created on first play and kept alive for the session.
Subsequent tracks update Audio.src in-place; AudioService.update() on the Dart
side detects the change and calls setSourceDeviceFile() on the still-active
AudioPlayer, then fires the 'loaded' event. This avoids the timing window
("Calling resume method of inexistent control") that occurs when a new
AudioService is created per track; the invokeMethod listener registered once
in AudioService.init() stays valid for the entire session.

Creating Audio without a src is still forbidden: AudioService.update() throws
synchronously ("Audio must have src specified"), breaking the ChangeNotifier
listener and preventing all future events from reaching the Dart side.
"""
import json
import os
import time
import logging
import threading
import random
import pathlib
import flet as ft
from flet_audio_service import AudioServiceControl

logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)



def _dur_secs(d: ft.Duration) -> float:
    return (d.days * 86400 + d.hours * 3600 + d.minutes * 60
            + d.seconds + d.milliseconds / 1000.0 + d.microseconds / 1_000_000.0)


class AudioEngine:
    """
    Cross-platform audio engine backed by flet_audio.Audio (audioplayers).
    State changes dispatch named callbacks registered via bind().
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
        # Mirror of the Dart player's just_audio processing state
        # ("idle"/"loading"/"buffering"/"ready"/"completed"). Lets Python-side
        # callers (e.g. the search preview wait loop) query load/buffer
        # progress directly instead of inferring it from is_playing+position.
        self.processing_state = "idle"
        self._is_shuffle    = False
        self._shuffle_order: list[int] = []
        self._repeat_mode   = "none"

        self.queue: list[dict] = []
        self.current_index: int = 0
        self.jarvis_controlled = False
        self.play_similar_seed_path = ""

        self._audio: AudioServiceControl | None = None
        self._is_loaded: bool = False
        self._page:  ft.Page | None = None
        self._db_manager = None
        self._restore_position: float = 0.0
        self._load_gen: int = 0

        # Wall-clock cutoff: after a user-initiated queue change, ignore
        # Dart-side queue_index mirroring until this time. Prevents stale
        # events from the previously-playing track flipping current_index
        # into a wrong row of the new queue while Dart is still catching up.
        self._queue_change_until: float = 0.0

        self._observers: dict[str, list] = {}
        self._obs_lock = threading.Lock()
        self._native_lock = None
        self._eq_gains: list[float] = [0.0] * 5
        self._loudness_boost_db: float = 0.0
        self.loudness_boost_db: float = 0.0
        self._base_eq: list[float] = [0.0] * 5
        self._dyn_offsets: list[float] = [0.0] * 5

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

        if self._page and self.queue:
            self._arm_queue_gate()
            self._page.run_task(self._push_queue_native, self.current_index, self.is_playing)

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

    @property
    def repeat_mode(self) -> str:
        return self._repeat_mode

    @repeat_mode.setter
    def repeat_mode(self, mode: str):
        if mode not in ("none", "one", "all"):
            return
        self._repeat_mode = mode
        self.dispatch("repeat_mode", mode)
        if self._audio and self._page:
            self._page.run_task(self._audio.set_repeat_mode, mode)

    # ── Initialisation ────────────────────────────────────────────────────────

    def setup(self, page: ft.Page, db_manager=None):
        """Call from Flet main() after page is ready. Safe to call again on
        session re-entry; the existing AudioServiceControl is bound to the
        previous page's session, which becomes invalid when the OS suspends
        the app long enough. We detect a new page object and rebind."""
        logger.warning("ADB_AUDIO: setup() called")
        self._db_manager = db_manager
        if self._page is page and self._audio is not None:
            return  # already set up on this page
        if self._page is not page:
            logger.warning("ADB_AUDIO: page changed; clearing stale observers and discarding stale audio control")
            self.clear_observers()
            self._audio = None
            self._is_loaded = False
        self._page = page
        self._ensure_audio()

    def _ensure_audio(self):
        """Lazy initialization of the AudioServiceControl."""
        if self._audio is None and self._page:
            self._audio = AudioServiceControl(
                on_state_change=self._on_state_change,
                on_position_change=self._on_position_change,
                on_error=self._on_error,
                on_ready=self._on_ready,
            )
            # Service must live in page.services
            self._page.services.append(self._audio)
            self._page.update()
            logger.warning("ADB_AUDIO: AudioServiceControl added to services")
            if self._repeat_mode != "none":
                self._page.run_task(self._audio.set_repeat_mode, self._repeat_mode)

    @property
    def audio_service(self) -> "AudioServiceControl | None":
        """Exposes the underlying AudioServiceControl so other subsystems
        (e.g. the DSP analyser, which calls `decode_pcm`) can reuse the same
        Dart bridge instead of standing up a second one."""
        return self._audio

    def _on_ready(self, e):
        """Called by the native side once the audio handler is fully initialized."""
        print("FLET_AUDIO_SERVICE: Native side is READY")
        logger.warning("ADB_AUDIO: Native audio service is READY")
        # Unblock every pending _wait_ready() inside AudioServiceControl.
        if self._audio:
            self._audio.mark_ready()

    @staticmethod
    def _to_uri(p: str) -> str:
        if not p:
            return ""
        if p.startswith(("http://", "https://", "file://")):
            return p
        try:
            return pathlib.Path(p).as_uri()
        except Exception:
            return p

    def _get_artwork_path(self, track: dict) -> str:
        art = track.get("artwork_path") or track.get("image_url") or track.get("_resolved_artwork_path") or ""
        if not art:
            path = track.get("path") or ""
            if path and not path.startswith(("http://", "https://")):
                folder = os.path.dirname(path)
                if not hasattr(self, "_folder_artwork_cache"):
                    self._folder_artwork_cache = {}
                if folder in self._folder_artwork_cache:
                    art = self._folder_artwork_cache[folder]
                else:
                    for name in ("cover.jpg", "folder.jpg", "cover.png", "front.jpg"):
                        p = os.path.join(folder, name)
                        if os.path.exists(p):
                            art = p
                            break
                    self._folder_artwork_cache[folder] = art
                track["_resolved_artwork_path"] = art
        return art

    def _track_to_playlist_item(self, track: dict) -> dict | None:
        """Convert a queue track dict into the payload set_playlist expects.
        Returns None if the track has no playable source."""
        path = track.get("path") or ""
        if not path:
            return None
        title = track.get("track_title") or os.path.basename(path) or "Unknown"
        artist = track.get("artist_name", "Unknown Artist")
        art = self._get_artwork_path(track)
        return {
            "src": self._to_uri(path),
            "title": title,
            "artist": artist,
            "album_art": self._to_uri(art),
        }

    def _build_playlist_payload(self) -> list[dict]:
        items = []
        order = self._shuffle_order if (self._is_shuffle and getattr(self, "_shuffle_order", None)) else range(len(self.queue))
        for idx in order:
            if 0 <= idx < len(self.queue):
                track = self.queue[idx]
                item = self._track_to_playlist_item(track)
                if item is not None:
                    items.append(item)
        return items

    async def _push_queue_native(self, start_index: int = 0, autoplay: bool = True):
        """Push the current Python queue to Dart's ConcatenatingAudioSource so
        the native player owns advancement, notification skip, and background
        continuation. Atomic: set_playlist takes start_index, so there's no
        race between source-load and a follow-up skip."""
        import asyncio
        if self._native_lock is None:
            self._native_lock = asyncio.Lock()
        async with self._native_lock:
            await self._push_queue_native_unlocked(start_index, autoplay)

    async def _push_queue_native_unlocked(self, start_index: int = 0, autoplay: bool = True):
        self._ensure_audio()
        if not self._audio:
            logger.error("ADB_AUDIO: Audio control not available.")
            return
        items = self._build_playlist_payload()
        if not items:
            try:
                await self._audio.stop()
            except Exception:
                pass
            return
        try:
            target = start_index
            if self._is_shuffle and getattr(self, "_shuffle_order", None):
                try:
                    target = self._shuffle_order.index(start_index)
                except ValueError:
                    target = 0
            target = max(0, min(target, len(items) - 1))
            await self._audio.set_playlist(items, start_index=target)
            if autoplay:
                await self._audio.play()
        except RuntimeError as exc:
            logger.error("ADB_AUDIO: _push_queue_native failed; %s", exc)

    async def _load_src(self, path, title="", artist="", album="", artwork="",
                        autoplay: bool = True):
        """Compatibility shim; single-track load via the playlist machinery.
        Used by restore_queue() to seek into a restored playlist. The
        autoplay flag lets restore_queue prepare the source without
        starting playback so the user can press play themselves."""
        await self._push_queue_native(start_index=self.current_index, autoplay=autoplay)

    def _on_state_change(self, e):
        queue_index = None
        try:
            payload = json.loads(e.data)
            status = payload.get("status", "paused")
            processing_state = payload.get("processing_state", "idle")
            queue_index = payload.get("queue_index")
            dur_ms = payload.get("duration_ms")
            if dur_ms is not None:
                self._set("duration", dur_ms / 1000.0)
        except Exception:
            status = str(e.data) if e.data else "paused"
            processing_state = "idle"

        # Surface the player's processing state so Python-side waiters can read
        # it directly (dirty-checked _set → no dispatch churn when unchanged).
        self._set("processing_state", processing_state)

        # Mirror the native queue index (changed by skip_to_next, notification
        # next/previous, or auto-advance at end-of-track) so Python-side UI
        # stays in sync. Suppressed briefly after a user-initiated queue
        # change so stale events from the prior playback don't flip
        # current_index into a wrong row of the freshly-installed queue.
        if (
            isinstance(queue_index, int)
            and self.queue
            and time.time() >= self._queue_change_until
        ):
            qi = max(0, min(queue_index, len(self.queue) - 1))
            target_idx = qi
            if self._is_shuffle and getattr(self, "_shuffle_order", None):
                if 0 <= qi < len(self._shuffle_order):
                    target_idx = self._shuffle_order[qi]
            if target_idx != self.current_index:
                self.current_index = target_idx
                self._sync_metadata_for_current()
                self.dispatch("on_queue_mutated")

        logger.debug(
            "ADB_AUDIO: _on_state_change status=%s, processing=%s, qi=%s",
            status, processing_state, queue_index
        )

        if status == "playing":
            self._set("is_playing", True)
        elif status == "paused":
            self._set("is_playing", False)

        if processing_state == "completed":
            # Auto-loop back to the first song if repeat_mode is "all"
            if self.repeat_mode == "all" and self.queue:
                target_idx = 0
                if self._is_shuffle and getattr(self, "_shuffle_order", None) and len(self._shuffle_order) > 0:
                    target_idx = self._shuffle_order[0]
                self.current_index = target_idx
                self._sync_metadata_for_current()
                if self._page:
                    self._arm_queue_gate()
                    self._page.run_task(self._push_queue_native, target_idx, True)
            else:
                self._set("is_playing", False)
                if getattr(self, "jarvis_controlled", False):
                    self.dispatch("on_jarvis_continue")
                elif getattr(self, "play_similar_seed_path", ""):
                    self.dispatch("on_similar_continue")
        elif processing_state == "ready":
            self._is_loaded = True
            # Apply any seek that was queued before the source finished
            # loading. `restore_queue` sets this so the user resumes a
            # killed-by-OS session at the right offset instead of 0:00.
            target = self._restore_position
            if target and target > 0.0 and self._audio is not None:
                self._restore_position = 0.0
                async def _apply_restore_seek(audio=self._audio, t=target):
                    try:
                        await audio.seek(int(t * 1000))
                    except Exception as exc:
                        logger.warning("ADB_AUDIO: restore-seek failed: %s", exc)
                if self._page:
                    self._page.run_task(_apply_restore_seek)

    def _sync_metadata_for_current(self):
        """Push current_index's track metadata into engine state."""
        if not self.queue or not (0 <= self.current_index < len(self.queue)):
            return
        track = self.queue[self.current_index]
        path = track.get("path") or ""
        title = (track.get("track_title") or os.path.basename(path)) if path else "Unknown"
        self._set("position", 0.0)
        self._set("duration", 0.0)
        art = self._get_artwork_path(track)
        self._set("current_art", art)
        self._set("current_artist", track.get("artist_name", "Unknown Artist"))
        self._set("current_album",  track.get("album_title",  "Unknown Album"))
        self._set("current_track",  title)
        self._set("current_path",   path)

        # Apply DSP settings (Dynamism and Equalizer)
        try:
            self.reapply_dsp()
        except Exception as ex:
            logger.error(f"DSP: Failed to apply track transition settings: {ex}")



    def _on_position_change(self, e):
        # Dart already throttles position emits (see flet_audio_service.dart);
        # no second throttle needed here. Quantise to integer seconds so _set's
        # dirty-check skips dispatch when the slider would not visibly move.
        try:
            pos_ms = int(e.data)
            pos = float(pos_ms // 1000)
            if self.duration and self.duration > 0.0:
                pos = min(pos, self.duration)
            self._set("position", pos)
        except:
            pass

    def _on_error(self, e):
        logger.error("ADB_AUDIO error: %s", e.data)
        self.dispatch("on_playback_error", str(e.data))

    def _on_loaded(self, e):
        # flet_audio compatibility - not used by service but kept to avoid errors if triggered
        pass

    # ── Observer pattern ──────────────────────────────────────────────────────

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

    def clear_observers(self):
        with self._obs_lock:
            self._observers.clear()
            logger.warning("ADB_AUDIO: Cleared all old observers")

    def dispatch(self, name: str, value=None):
        with self._obs_lock:
            fns = list(self._observers.get(name, []))
        for fn in fns:
            try:
                fn(self, value)
            except Exception as exc:
                if "destroyed session" in str(exc):
                    logger.warning("AudioEngine dispatch suppressed stale session callback (%s): %s", name, exc)
                else:
                    logger.error("AudioEngine dispatch error (%s): %s", name, exc)

    def _set(self, attr: str, value):
        if attr == "position":
            if self.duration and self.duration > 0.0:
                value = min(float(value), self.duration)
            else:
                value = max(0.0, float(value))
        if getattr(self, attr, None) == value:
            return
        setattr(self, attr, value)
        self.dispatch(attr, value)

    # ── Queue management ──────────────────────────────────────────────────────

    def _arm_queue_gate(self, seconds: float = 1.5):
        """Block Dart→Python queue_index mirroring for the next `seconds`
        seconds. Called whenever Python initiates a queue mutation or skip
        so that in-flight state events from the previously-playing track
        can't flip current_index to a wrong row before Dart catches up."""
        self._queue_change_until = time.time() + seconds

    def set_queue(self, tracks: list[dict], start_index: int = 0):
        """Set the playback queue and start from the given index. The full
        queue is pushed to Dart's ConcatenatingAudioSource so notification
        skip and background auto-advance work without Python in the loop."""
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
        if self._page and self.queue:
            self._arm_queue_gate()
            self._page.run_task(self._push_queue_native, self.current_index, True)

    async def play_current(self):
        """Re-push the queue to Dart starting at current_index. Kept for
        compatibility with any caller that expected the old single-track
        load semantics."""
        if not self.queue:
            self.stop()
            return
        now = time.time()
        last_play = getattr(self, "_last_play_time", 0)
        if now - last_play < 0.5:
            logger.warning("ADB_AUDIO: play_current called too rapidly, ignoring.")
            return
        self._last_play_time = now
        self._sync_metadata_for_current()
        await self._push_queue_native(start_index=self.current_index, autoplay=True)

    # ── Transport controls ────────────────────────────────────────────────────

    def play(self):
        if self._audio and self._page:
            self._page.run_task(self._audio.play)

    def pause(self):
        if self._audio and self._page:
            self._page.run_task(self._audio.pause)

    def toggle(self):
        if not self._audio:
            if self.queue and self._page:
                self._page.run_task(self.play_current)
            return

        if self.is_playing:
            self.pause()
        else:
            self.play()

    def seek(self, target: float):
        target_audio = self._audio
        if not target_audio:
            return

        # Clamp to known duration so a user scrub can't land past the end of
        # the track (which just_audio resolves as completed → ConcatenatingAudioSource
        # auto-advance, i.e. the "scrubbing skips the song" symptom).
        bounded = max(0.0, float(target))
        if self.duration and self.duration > 0.0:
            # Leave a small margin so the player doesn't immediately fire
            # `completed` when we land within a few ms of the end.
            bounded = min(bounded, max(0.0, self.duration - 0.5))

        # Reflect the new position in Python state immediately so the
        # mini-player / now-playing slider snaps to where the user dragged.
        # When paused, Dart's positionStream listener filters out updates
        # (`if (!playing) return`), so without this the UI would only show
        # the new offset once playback resumes.
        self._set("position", bounded)

        async def _safe_seek():
            try:
                if target_audio is None or target_audio != self._audio:
                    return

                if not self._is_loaded:
                    # Source isn't ready yet; store as the queued seek so
                    # _on_state_change(processing_state=ready) will apply it.
                    # Now that setPlaylist preloads on the Dart side this
                    # path is rare, but keep it for cold-start robustness.
                    self._restore_position = bounded
                    return

                await target_audio.seek(int(bounded * 1000))
            except Exception:
                pass

        self._page.run_task(_safe_seek)

    def next(self):
        if not self._audio or not self._page:
            return
        if self.repeat_mode == "one":
            self._page.run_task(self._audio.seek, 0)
            self._page.run_task(self._audio.play)
            return
        self._arm_queue_gate(0.8)
        if self._is_shuffle and len(self.queue) > 1 and getattr(self, "_shuffle_order", None):
            try:
                curr_shuf_idx = self._shuffle_order.index(self.current_index)
            except ValueError:
                curr_shuf_idx = -1
            
            if curr_shuf_idx != -1 and curr_shuf_idx < len(self._shuffle_order) - 1:
                target_shuf_idx = curr_shuf_idx + 1
                target = self._shuffle_order[target_shuf_idx]
                self.current_index = target
                self._sync_metadata_for_current()
                self._page.run_task(self._audio.skip_to_index, target_shuf_idx)
                return
            elif self.repeat_mode == "all":
                target = self._shuffle_order[0]
                self.current_index = target
                self._sync_metadata_for_current()
                self._page.run_task(self._audio.skip_to_index, 0)
                return
            else:
                if getattr(self, "jarvis_controlled", False):
                    self.dispatch("on_jarvis_continue")
                elif getattr(self, "play_similar_seed_path", ""):
                    self.dispatch("on_similar_continue")
                else:
                    self.stop()
                return

        if self.current_index < len(self.queue) - 1:
            self.current_index += 1
            self._sync_metadata_for_current()
            self._page.run_task(self._audio.skip_to_next)
        elif self.repeat_mode == "all":
            self.current_index = 0
            self._sync_metadata_for_current()
            self._page.run_task(self._audio.skip_to_index, 0)
        else:
            if getattr(self, "jarvis_controlled", False):
                self.dispatch("on_jarvis_continue")
            elif getattr(self, "play_similar_seed_path", ""):
                self.dispatch("on_similar_continue")
            else:
                self.stop()

    def previous(self):
        if self.position > 3.0:
            self.seek(0)
            return
        if not self._audio or not self._page:
            return
        if self._is_shuffle and len(self.queue) > 1 and getattr(self, "_shuffle_order", None):
            try:
                curr_shuf_idx = self._shuffle_order.index(self.current_index)
            except ValueError:
                curr_shuf_idx = -1
            
            if curr_shuf_idx > 0:
                target_shuf_idx = curr_shuf_idx - 1
                target = self._shuffle_order[target_shuf_idx]
                self._arm_queue_gate(0.8)
                self.current_index = target
                self._sync_metadata_for_current()
                self._page.run_task(self._audio.skip_to_index, target_shuf_idx)
                return

        if self.current_index > 0:
            self._arm_queue_gate(0.8)
            self.current_index -= 1
            self._sync_metadata_for_current()
            self._page.run_task(self._audio.skip_to_previous)

    # ── Queue mutation ────────────────────────────────────────────────────────

    def queue_next(self, track: dict):
        if not self.queue:
            self.set_queue([track])
            return
        insert_at = min(self.current_index + 1, len(self.queue))
        self.queue.insert(insert_at, track)
        
        native_idx = insert_at
        if self._is_shuffle:
            self._on_track_added_to_shuffle(insert_at, play_next=True)
            try:
                native_idx = self._shuffle_order.index(insert_at)
            except ValueError:
                native_idx = insert_at

        self.dispatch("on_queue_mutated")
        if self._page:
            self._arm_queue_gate()
            self._page.run_task(self._native_add_queue_item, track, native_idx)

    def queue_last(self, track: dict):
        if not self.queue:
            self.set_queue([track])
            return
        self.queue.append(track)
        
        native_idx = len(self.queue) - 1
        if self._is_shuffle:
            self._on_track_added_to_shuffle(native_idx, play_next=False)
            try:
                native_idx = self._shuffle_order.index(native_idx)
            except ValueError:
                native_idx = len(self.queue) - 1

        self.dispatch("on_queue_mutated")
        if self._page:
            self._arm_queue_gate()
            # Append: use the Python-side length AFTER our local insert as the
            # native insert index. Dart clamps to the actual playlist length,
            # so any drift between the two sides resolves to a tail append.
            self._page.run_task(
                self._native_add_queue_item, track, native_idx
            )

    async def _native_add_queue_item(self, track: dict, index: int):
        """Non-destructive insert into the live ConcatenatingAudioSource via
        Dart's addQueueItemAt. Does NOT call set_playlist, so the currently
        playing source isn't torn down and position is preserved.

        Falls back to a full set_playlist push if the native call fails
        (network race, Dart side not yet ready), at which point the user
        will see the legacy 'restart on resume' behaviour for that one
        mutation — better than a silently dropped queue insert."""
        import asyncio
        if self._native_lock is None:
            self._native_lock = asyncio.Lock()
        async with self._native_lock:
            self._ensure_audio()
            if not self._audio:
                return
            item = self._track_to_playlist_item(track)
            if item is None:
                return
            try:
                await self._audio.add_queue_item(
                    src=item["src"],
                    title=item["title"],
                    artist=item["artist"],
                    album_art=item.get("album_art"),
                    index=index,
                )
            except Exception as exc:
                logger.warning("ADB_AUDIO: add_queue_item failed; falling back to "
                               "set_playlist (will reset position): %s", exc)
                await self._push_queue_native_unlocked(
                    start_index=self.current_index, autoplay=False
                )

    def queue_extend(self, tracks: list[dict]):
        """Append several tracks in one batch. Mirrors calling queue_last for
        each track, but dispatches on_queue_mutated only ONCE (one queue-sheet
        rebuild, one coalesced queue-save) and pushes every insert to Dart
        under a single native task. Used by Play Similar block-replenishment,
        which appends up to 8 tracks at a time — per-track queue_last would
        otherwise rebuild the queue sheet eight times per replenish."""
        if not tracks:
            return
        if not self.queue:
            # Extending an empty queue is just setting it (and starts playback,
            # matching queue_last's empty-queue behaviour).
            self.set_queue(tracks)
            return
        native_items: list[tuple[dict, int]] = []
        for track in tracks:
            insert_at = len(self.queue)
            self.queue.append(track)
            native_idx = insert_at
            if self._is_shuffle:
                self._on_track_added_to_shuffle(insert_at, play_next=False)
                try:
                    native_idx = self._shuffle_order.index(insert_at)
                except ValueError:
                    native_idx = insert_at
            native_items.append((track, native_idx))
        self.dispatch("on_queue_mutated")
        if self._page:
            self._arm_queue_gate()
            self._page.run_task(self._native_add_queue_items, native_items)

    async def _native_add_queue_items(self, items: list[tuple[dict, int]]):
        """Batch sibling of _native_add_queue_item: insert the whole block into
        the live ConcatenatingAudioSource in ONE Dart call (add_queue_items)
        under a single lock acquisition. The per-item indices are computed in
        append order by queue_extend, so Dart replaying them in order is
        equivalent to N sequential add_queue_item calls. Falls back to one full
        set_playlist rebuild if the native call fails (including against an
        older Dart bundle that predates add_queue_items)."""
        import asyncio
        if self._native_lock is None:
            self._native_lock = asyncio.Lock()
        async with self._native_lock:
            self._ensure_audio()
            if not self._audio:
                return
            payload = []
            for track, index in items:
                item = self._track_to_playlist_item(track)
                if item is None:
                    continue
                payload.append({
                    "src": item["src"],
                    "title": item["title"],
                    "artist": item["artist"],
                    "album_art": item.get("album_art"),
                    "index": index,
                })
            if not payload:
                return
            try:
                await self._audio.add_queue_items(payload)
            except Exception as exc:
                logger.warning("ADB_AUDIO: batch add_queue_items failed; falling "
                               "back to set_playlist (will reset position): %s", exc)
                await self._push_queue_native_unlocked(
                    start_index=self.current_index, autoplay=False
                )

    def play_track_at(self, index: int):
        if not (0 <= index < len(self.queue)) or not self._audio or not self._page:
            return
        self._arm_queue_gate(0.8)
        self.current_index = index
        self._sync_metadata_for_current()
        
        target = index
        if self._is_shuffle and getattr(self, "_shuffle_order", None):
            try:
                target = self._shuffle_order.index(index)
            except ValueError:
                target = index
                
        self._page.run_task(self._audio.skip_to_index, target)
        self._page.run_task(self._audio.play)

    def remove_from_queue(self, index: int):
        if not 0 <= index < len(self.queue):
            return
        removed_active = (index == self.current_index)
        native_index = index
        curr_shuf_idx = -1
        if self._is_shuffle and getattr(self, "_shuffle_order", None):
            try:
                native_index = self._shuffle_order.index(index)
                curr_shuf_idx = native_index
            except ValueError:
                native_index = index
            self._on_track_removed_from_shuffle(index)

        self.queue.pop(index)
        if not self.queue:
            self.stop()
            return
        if index < self.current_index:
            self.current_index -= 1
        elif removed_active:
            if self._is_shuffle and getattr(self, "_shuffle_order", None) and len(self._shuffle_order) > 0:
                shuf_pos = min(curr_shuf_idx, len(self._shuffle_order) - 1) if curr_shuf_idx != -1 else 0
                self.current_index = self._shuffle_order[shuf_pos]
            else:
                self.current_index = min(self.current_index, len(self.queue) - 1)
            self._sync_metadata_for_current()
        self.dispatch("on_queue_mutated")
        if self._page:
            self._arm_queue_gate()
            self._page.run_task(self._native_remove_queue_item, native_index)

    async def _native_remove_queue_item(self, index: int):
        """Non-destructive removal via Dart's removeQueueItemAt. Falls back
        to a full rebuild if the native call fails."""
        import asyncio
        if self._native_lock is None:
            self._native_lock = asyncio.Lock()
        async with self._native_lock:
            self._ensure_audio()
            if not self._audio:
                return
            try:
                await self._audio.remove_queue_item(index)
            except Exception as exc:
                logger.warning("ADB_AUDIO: remove_queue_item failed; falling back "
                               "to set_playlist: %s", exc)
                await self._push_queue_native_unlocked(
                    start_index=self.current_index, autoplay=False
                )

    def move_queue_item(self, old_index: int, new_index: int):
        if not (0 <= old_index < len(self.queue) and 0 <= new_index < len(self.queue)):
            return
        current_obj = self.queue[self.current_index] if self.queue else None
        
        native_old_index = old_index
        native_new_index = new_index
        if self._is_shuffle and getattr(self, "_shuffle_order", None):
            try:
                native_old_index = self._shuffle_order.index(old_index)
            except ValueError:
                pass
            self._on_track_moved_in_shuffle(old_index, new_index)
            try:
                native_new_index = self._shuffle_order.index(new_index)
            except ValueError:
                pass

        item = self.queue.pop(old_index)
        self.queue.insert(new_index, item)
        if current_obj is not None and current_obj in self.queue:
            self.current_index = self.queue.index(current_obj)
        self.dispatch("on_queue_mutated")
        if self._page:
            self._arm_queue_gate()
            self._page.run_task(
                self._native_move_queue_item, native_old_index, native_new_index
            )

    async def _native_move_queue_item(self, old_index: int, new_index: int):
        """Non-destructive reorder via Dart's ConcatenatingAudioSource.move.
        Position is preserved even when the active source is moved."""
        import asyncio
        if self._native_lock is None:
            self._native_lock = asyncio.Lock()
        async with self._native_lock:
            self._ensure_audio()
            if not self._audio:
                return
            try:
                await self._audio.move_queue_item(old_index, new_index)
            except Exception as exc:
                logger.warning("ADB_AUDIO: move_queue_item failed; falling back "
                               "to set_playlist: %s", exc)
                await self._push_queue_native_unlocked(
                    start_index=self.current_index, autoplay=False
                )

    def clear_queue(self):
        self.stop()
        self.queue.clear()
        self.current_index = 0
        self.dispatch("on_queue_mutated")

    # ── Restore / persist ─────────────────────────────────────────────────────

    def restore_queue(self, tracks: list[dict], index: int, position: float = 0.0,
                      duration: float = 0.0):
        if not tracks or not self._page:
            return
        # Restore all items (caching avoids UI blocking)
        self.queue = tracks
        self.current_index = min(max(0, index), len(self.queue) - 1)
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
        track = tracks[self.current_index]
        path  = track.get("path", "")
        title = (track.get("track_title") or os.path.basename(path)) if path else "Unknown"

        # Surface the saved position+duration in the UI immediately so the
        # mini-player and now-playing screen render where the user left
        # off, instead of snapping from 0:00 to position once the source
        # finishes loading. Duration in particular is needed to give the
        # slider a sane max value; without it any pre-load scrub computes
        # a target relative to 0 and the seek-on-ready handler can fling
        # the player past end-of-track, triggering an auto-advance.
        self._set("position",       max(0.0, float(position)))
        self._set("duration",       max(0.0, float(duration)))
        self._set("is_playing",     False)
        self._set("current_artist", track.get("artist_name", "Unknown Artist"))
        self._set("current_album",  track.get("album_title",  "Unknown Album"))
        self._set("current_track",  title)
        self._set("current_path",   path)

        if path and os.path.exists(path):
            self._restore_position = position
            # Generous gate on restore; cold-start codec init + source load
            # on the first track of a new session routinely runs longer than
            # the standard 1.5s gate. If the gate expires before Dart's
            # first state event, an interim null/zero queue_index could flip
            # current_index to the wrong row (and fire the wrong album/artist
            # into the now-playing UI before the real value arrives).
            self._arm_queue_gate(5.0)

            async def _restore_async():
                try:
                    # autoplay=False; we want the previous UI revived, not
                    # an unsolicited resume. The source still gets prepared
                    # so the saved-position seek can apply on `ready`, and
                    # the user's first tap on play resumes from that offset.
                    await self._load_src(path, autoplay=False)
                except Exception as exc:
                    logger.error("ADB_AUDIO: restore_queue error: %s", exc)

            self._page.run_task(_restore_async)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def stop(self):
        self.jarvis_controlled = False
        if self._audio:
            target = self._audio
            self._is_loaded = False

            async def _safe_stop():
                try:
                    await target.stop()
                except Exception:
                    pass

            self._page.run_task(_safe_stop)

        # Reset to beginning so the next play attempt starts from track 0.
        self.current_index = 0

        self._set("is_playing",     False)
        self._set("current_track",  "")
        self._set("current_artist", "")
        self._set("current_album",  "")
        self._set("current_path",   "")
        self._set("duration",       0.0)
        self._set("position",       0.0)

    def shutdown(self):
        if self._audio:
            target = self._audio
            self._audio = None
            self._is_loaded = False

            async def _safe_shutdown():
                try:
                    await target.stop()
                except Exception:
                    pass
                try:
                    svcs = list(self._page.services)
                    if target in svcs:
                        svcs.remove(target)
                        self._page.services = svcs
                        self._page.update()
                except Exception:
                    pass

            self._page.run_task(_safe_shutdown)

    def _update_player_volume(self):
        if self._audio and self._page:
            applied_db = max(0.0, self.loudness_boost_db)
            self._page.run_task(self._audio.set_loudness_boost, applied_db)

    def set_loudness_boost(self, gain_db: float):
        """Set target gain boost in decibels for loudness enhancement."""
        self._loudness_boost_db = float(gain_db)
        self._set("loudness_boost_db", float(gain_db))
        self._update_player_volume()

    def set_eq_band_gain(self, band_index: int, gain_db: float):
        """Set the gain level in decibels for a specific equalizer band."""
        if not (0 <= band_index < 5):
            return
        self._base_eq[band_index] = float(gain_db)
        combined = self._base_eq[band_index] + self._dyn_offsets[band_index]
        self._eq_gains[band_index] = combined
        if self._audio and self._page:
            self._page.run_task(self._audio.set_eq_band_gain, band_index, combined)
        self._update_player_volume()

    def apply_combined_dsp(self, base_eq: list[float], dyn_offsets: list[float]):
        self._base_eq = list(base_eq)
        self._dyn_offsets = list(dyn_offsets)
        for idx in range(5):
            combined = self._base_eq[idx] + self._dyn_offsets[idx]
            self._eq_gains[idx] = combined
            if self._audio and self._page:
                self._page.run_task(self._audio.set_eq_band_gain, idx, combined)
        self._update_player_volume()

    @staticmethod
    def _compute_dynamism_scaler(energy, beat_strength, spectral_contrast) -> float:
        """Calculate a track-by-track dynamism score in [0.0, 1.0] based on energy,
        beat strength, and spectral contrast, with no library-wide dependencies.
        """
        try:
            e = float(energy) if energy is not None else 0.5
            b = float(beat_strength) if beat_strength is not None else 0.5
            c = float(spectral_contrast) if spectral_contrast is not None else 0.3
            
            # Map typical spectral contrast [0.2, 0.4] to [0.0, 1.0]
            norm_contrast = max(0.0, min(1.0, (c - 0.2) / 0.2))
            
            # Weighted average score in [0.0, 1.0]
            score = 0.4 * e + 0.3 * b + 0.3 * norm_contrast
            return round(max(0.0, min(1.0, score)), 2)
        except (ValueError, TypeError):
            return 0.5

    @staticmethod
    def _compute_dynamism_gain_db(energy, beat_strength) -> float:
        """Legacy compatibility wrapper."""
        return 0.0

    def reapply_dsp(self):
        """Re-read configuration and re-apply combined DSP settings (EQ preset + Dynamism) for the current track."""
        if not self.queue or not (0 <= self.current_index < len(self.queue)):
            self.set_loudness_boost(0.0)
            self._base_eq = [0.0] * 5
            self._dyn_offsets = [0.0] * 5
            self.apply_combined_dsp(self._base_eq, self._dyn_offsets)
            return

        track = self.queue[self.current_index]
        path = track.get("path")
        
        from utils.streamrip_api import load_config
        cfg = load_config()
        dsp = cfg.get("dsp", {})

        eq_enabled = bool(dsp.get("equalizer_enabled", False))
        dyn_enabled = bool(dsp.get("dynamism_enabled", False))

        # 1. Base EQ gains
        base_eq = [0.0] * 5
        if eq_enabled:
            active_preset = dsp.get("active_preset", "Flat")
            PRESETS = {
                "Flat":         [0.0, 0.0, 0.0, 0.0, 0.0],
                "Rock":         [4.0, 2.0, -2.0, 2.0, 4.0],
                "Pop":          [2.0, 3.0, 1.0, -1.0, 2.0],
                "Jazz":         [3.0, 2.0, 1.0, 2.0, 3.0],
                "Classical":    [3.0, 2.0, -1.0, 2.0, 3.0],
                "Electronic":   [4.0, 2.0, 0.0, 2.0, 3.0],
                "Bass Booster": [5.0, 3.0, 0.0, 0.0, 0.0],
                "Vocal Booster": [-2.0, -1.0, 3.0, 2.0, -1.0],
            }
            gains = None
            if active_preset in PRESETS:
                gains = PRESETS[active_preset]
            else:
                custom_presets = dsp.get("custom_presets", {})
                if active_preset in custom_presets:
                    gains = custom_presets[active_preset]
            if gains:
                base_eq = [float(g) for g in gains]

        if not dyn_enabled:
            self.set_loudness_boost(0.0)
            self.apply_combined_dsp(base_eq, [0.0] * 5)
            return

        # Dynamism is enabled
        energy = track.get("energy")
        beat_strength = track.get("beat_strength")
        spectral_contrast = track.get("spectral_contrast")

        if energy is not None and beat_strength is not None:
            score = self._compute_dynamism_scaler(energy, beat_strength, spectral_contrast)
            gain_db = 1.0 + 3.0 * score
            self.set_loudness_boost(gain_db)
            if eq_enabled:
                # Manual EQ has 100% exclusive control over EQ bands
                self.apply_combined_dsp(base_eq, [0.0] * 5)
            else:
                # Apply dynamism contour shape to EQ bands
                dyn_offsets = [score * b for b in [3.0, 1.5, 0.0, 1.0, 2.5]]
                self.apply_combined_dsp([0.0] * 5, dyn_offsets)
        elif self._db_manager and path:
            async def _fetch_and_apply_dsp():
                try:
                    conn = await self._db_manager.get_connection()
                    async with conn.execute(
                        "SELECT energy, beat_strength, spectral_contrast FROM play_counts WHERE track_path = ?", (path,)
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            e_val, b_val, c_val = row[0], row[1], row[2]
                            track["energy"] = e_val
                            track["beat_strength"] = b_val
                            track["spectral_contrast"] = c_val
                            score = self._compute_dynamism_scaler(e_val, b_val, c_val)
                            gain_db = 1.0 + 3.0 * score
                            self.set_loudness_boost(gain_db)
                            if eq_enabled:
                                self.apply_combined_dsp(base_eq, [0.0] * 5)
                            else:
                                dyn_offsets = [score * b for b in [3.0, 1.5, 0.0, 1.0, 2.5]]
                                self.apply_combined_dsp([0.0] * 5, dyn_offsets)
                        else:
                            self.set_loudness_boost(0.0)
                            self.apply_combined_dsp(base_eq, [0.0] * 5)
                except Exception as exc:
                    logger.error("DSP fetch failed: %s", exc)
                    self.set_loudness_boost(0.0)
                    self.apply_combined_dsp(base_eq, [0.0] * 5)
            if self._page:
                self._page.run_task(_fetch_and_apply_dsp)
            else:
                self.set_loudness_boost(0.0)
                self.apply_combined_dsp(base_eq, [0.0] * 5)
        else:
            self.set_loudness_boost(0.0)
            self.apply_combined_dsp(base_eq, [0.0] * 5)

    async def get_equalizer_bands(self) -> dict:
        """Fetch the equalizer bands from the player."""
        if self._audio:
            try:
                return await self._audio.get_equalizer_bands()
            except Exception as e:
                logger.error(f"get_equalizer_bands failed: {e}")
        return {"ok": False, "bands": []}


audio_engine = AudioEngine()
