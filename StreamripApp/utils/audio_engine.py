"""
StreamripApp Audio Engine — flet_audio backend (audioplayers / cross-platform).

A single Audio service is created on first play and kept alive for the session.
Subsequent tracks update Audio.src in-place; AudioService.update() on the Dart
side detects the change and calls setSourceDeviceFile() on the still-active
AudioPlayer, then fires the 'loaded' event.  This avoids the timing window
("Calling resume method of inexistent control") that occurs when a new
AudioService is created per track — the invokeMethod listener registered once
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
        self.is_shuffle     = False
        self.repeat_mode    = "none"

        self.queue: list[dict] = []
        self.current_index: int = 0

        self._audio: AudioServiceControl | None = None
        self._is_loaded: bool = False
        self._page:  ft.Page | None = None
        self._restore_position: float = 0.0
        self._load_gen: int = 0

        # Wall-clock cutoff: after a user-initiated queue change, ignore
        # Dart-side queue_index mirroring until this time. Prevents stale
        # events from the previously-playing track flipping current_index
        # into a wrong row of the new queue while Dart is still catching up.
        self._queue_change_until: float = 0.0

        self._observers: dict[str, list] = {}
        self._obs_lock = threading.Lock()

    # ── Initialisation ────────────────────────────────────────────────────────

    def setup(self, page: ft.Page):
        """Call from Flet main() after page is ready. Safe to call again on
        session re-entry — the existing AudioServiceControl is bound to the
        previous page's session, which becomes invalid when the OS suspends
        the app long enough. We detect a new page object and rebind."""
        logger.warning("ADB_AUDIO: setup() called")
        if self._page is page and self._audio is not None:
            return  # already set up on this page
        if self._audio is not None and self._page is not page:
            logger.warning("ADB_AUDIO: page changed — discarding stale audio control")
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

    def _track_to_playlist_item(self, track: dict) -> dict | None:
        """Convert a queue track dict into the payload set_playlist expects.
        Returns None if the track has no playable source."""
        path = track.get("path") or ""
        if not path:
            return None
        title = track.get("track_title") or os.path.basename(path) or "Unknown"
        artist = track.get("artist_name", "Unknown Artist")
        art = track.get("artwork_path") or ""
        if not art and path:
            folder = os.path.dirname(path)
            for name in ("cover.jpg", "folder.jpg", "cover.png", "front.jpg"):
                p = os.path.join(folder, name)
                if os.path.exists(p):
                    art = p
                    break
        return {
            "src": self._to_uri(path),
            "title": title,
            "artist": artist,
            "album_art": self._to_uri(art),
        }

    def _build_playlist_payload(self) -> list[dict]:
        items = []
        for track in self.queue:
            item = self._track_to_playlist_item(track)
            if item is not None:
                items.append(item)
        return items

    async def _push_queue_native(self, start_index: int = 0, autoplay: bool = True):
        """Push the current Python queue to Dart's ConcatenatingAudioSource so
        the native player owns advancement, notification skip, and background
        continuation. Atomic: set_playlist takes start_index, so there's no
        race between source-load and a follow-up skip."""
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
            target = max(0, min(start_index, len(items) - 1))
            await self._audio.set_playlist(items, start_index=target)
            if autoplay:
                await self._audio.play()
        except RuntimeError as exc:
            logger.error("ADB_AUDIO: _push_queue_native failed — %s", exc)

    async def _load_src(self, path, title="", artist="", album="", artwork="",
                        autoplay: bool = True):
        """Compatibility shim — single-track load via the playlist machinery.
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
            if qi != self.current_index:
                self.current_index = qi
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
            # Dart handles auto-advance via ConcatenatingAudioSource; we just
            # reflect end-of-queue state here.
            self._set("is_playing", False)
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

    def _on_position_change(self, e):
        # Python-side guard: ignore clumped events faster than 150ms
        now = time.time()
        if not hasattr(self, "_last_pos_received"):
            self._last_pos_received = 0
        if now - self._last_pos_received < 0.95:
            return
        self._last_pos_received = now

        try:
            # Position is received as int ms
            pos_ms = int(e.data)
            self._set("position", pos_ms / 1000.0)
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
                    # Source isn't ready yet — store as the queued seek so
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
        if self.is_shuffle and len(self.queue) > 1:
            candidates = [i for i in range(len(self.queue)) if i != self.current_index]
            target = random.choice(candidates)
            self.current_index = target
            self._sync_metadata_for_current()
            self._page.run_task(self._audio.skip_to_index, target)
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
            self.stop()

    def previous(self):
        if self.position > 3.0:
            self.seek(0)
            return
        if not self._audio or not self._page:
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
        if len(self.queue) >= 25:
            return
        insert_at = min(self.current_index + 1, len(self.queue))
        self.queue.insert(insert_at, track)
        self.dispatch("on_queue_mutated")
        # Re-push the playlist without restarting playback. autoplay=False keeps
        # the current track playing while the new item becomes part of the
        # native queue.
        if self._page:
            self._arm_queue_gate()
            self._page.run_task(self._push_queue_native, self.current_index, False)

    def play_track_at(self, index: int):
        if not (0 <= index < len(self.queue)) or not self._audio or not self._page:
            return
        self._arm_queue_gate(0.8)
        self.current_index = index
        self._sync_metadata_for_current()
        self._page.run_task(self._audio.skip_to_index, index)
        self._page.run_task(self._audio.play)

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
        self.dispatch("on_queue_mutated")
        if self._page:
            self._arm_queue_gate()
            self._page.run_task(
                self._push_queue_native, self.current_index, removed_active
            )

    def move_queue_item(self, old_index: int, new_index: int):
        if not (0 <= old_index < len(self.queue) and 0 <= new_index < len(self.queue)):
            return
        current_obj = self.queue[self.current_index] if self.queue else None
        item = self.queue.pop(old_index)
        self.queue.insert(new_index, item)
        if current_obj is not None and current_obj in self.queue:
            self.current_index = self.queue.index(current_obj)
        self.dispatch("on_queue_mutated")
        if self._page:
            self._arm_queue_gate()
            self._page.run_task(self._push_queue_native, self.current_index, False)

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
        # Condense to 25 items
        self.queue = tracks[:25]
        self.current_index = min(max(0, index), len(self.queue) - 1)
        track = tracks[self.current_index]
        path  = track.get("path", "")
        title = (track.get("track_title") or os.path.basename(path)) if path else "Unknown"

        # Surface the saved position+duration in the UI immediately so the
        # mini-player and now-playing screen render where the user left
        # off, instead of snapping from 0:00 to position once the source
        # finishes loading. Duration in particular is needed to give the
        # slider a sane max value — without it any pre-load scrub computes
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
            # Generous gate on restore — cold-start codec init + source load
            # on the first track of a new session routinely runs longer than
            # the standard 1.5s gate. If the gate expires before Dart's
            # first state event, an interim null/zero queue_index could flip
            # current_index to the wrong row (and fire the wrong album/artist
            # into the now-playing UI before the real value arrives).
            self._arm_queue_gate(5.0)

            async def _restore_async():
                try:
                    # autoplay=False — we want the previous UI revived, not
                    # an unsolicited resume. The source still gets prepared
                    # so the saved-position seek can apply on `ready`, and
                    # the user's first tap on play resumes from that offset.
                    await self._load_src(path, autoplay=False)
                except Exception as exc:
                    logger.error("ADB_AUDIO: restore_queue error: %s", exc)

            self._page.run_task(_restore_async)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def stop(self):
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


audio_engine = AudioEngine()
