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
import uuid
import logging
import threading
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
        # Read-only cache of just_audio's shuffle order (play-position → logical
        # index), refreshed from every Dart state/op event. Dart OWNS the order
        # now; Python only reads this to render "up next" and to compute the
        # next/previous target under shuffle. Empty when not shuffling.
        self._shuffle_indices: list[int] = []
        self._repeat_mode   = "none"

        self.queue: list[dict] = []
        self.current_index: int = 0
        self.play_similar_seed_path = ""

        self._audio: AudioServiceControl | None = None
        self._is_loaded: bool = False
        self._page:  ft.Page | None = None
        self._db_manager = None
        self._restore_position: float = 0.0
        self._load_gen: int = 0

        # Queue-generation counter. Bumped on every Python-initiated queue
        # mutation OR skip and sent to Dart with the command. Dart adopts it
        # once the op lands and stamps it on every event; Python mirrors a
        # queue_index only when the event's epoch matches this value (Dart has
        # caught up). Deterministic replacement for the old wall-clock gate.
        self._queue_epoch: int = 0

        self._observers: dict[str, list] = {}
        self._obs_lock = threading.Lock()
        self._eq_gains: list[float] = [0.0] * 5
        self._loudness_boost_db: float = 0.0
        self.loudness_boost_db: float = 0.0
        self._base_eq: list[float] = [0.0] * 5
        self._dyn_offsets: list[float] = [0.0] * 5
        self._completed_terminal = False

    def _next_epoch(self) -> int:
        """Advance and return the queue-generation counter. Call on every
        Python-initiated mutation/skip immediately before dispatching to Dart."""
        self._queue_epoch += 1
        return self._queue_epoch

    @property
    def shuffle_indices(self) -> list[int]:
        """Dart-reported shuffle order (play-position → logical index). Read-only
        cache for the queue sheet and next/previous target computation."""
        return self._shuffle_indices

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
        # Dart owns the shuffle order. Toggle native shuffle IN PLACE (no source
        # reload, so the current track keeps playing) and let the fresh
        # shuffle_indices arrive on the op ack / next state event. Insert/remove/
        # move against the live ConcatenatingAudioSource keep the native shuffle
        # order coherent automatically — no hand-maintained permutation.
        if not self._is_shuffle:
            self._shuffle_indices = []
        if not (self._page and self._audio):
            return
        # Defensive: this runs at startup (is_shuffle restored from prefs). If a
        # build bundled an OLDER flet_audio_service control (local-dep version
        # skew: the file:// package's version wasn't bumped so pip reused a
        # cached install), set_shuffle won't exist. Skip rather than let the
        # AttributeError abort _heavy_init and hang the app on the splash screen.
        if not hasattr(self._audio, "set_shuffle"):
            logger.error(
                "ADB_AUDIO: audio control lacks set_shuffle — stale "
                "flet_audio_service build; native shuffle disabled until rebuild"
            )
            return
        ep = self._next_epoch()
        self._page.run_task(self._audio.set_shuffle, self._is_shuffle, ep)

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
                on_custom_action=self._on_custom_action,
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
        album = track.get("album_title", "Unknown Album")

        # Duration: convert seconds (float) to milliseconds (int)
        duration_ms = None
        duration = track.get("duration")
        if duration is not None:
            try:
                duration_ms = int(float(duration) * 1000)
            except Exception:
                pass

        return {
            "src": self._to_uri(path),
            "title": title,
            "artist": artist,
            "album": album,
            "album_art": self._to_uri(art),
            "duration_ms": duration_ms,
        }

    def _build_playlist_payload(self) -> list[dict]:
        # Always LOGICAL order now — Dart applies shuffle natively and reports
        # the resulting order back. The native index Dart emits is therefore an
        # index into THIS list, i.e. a logical index, so mirroring is a direct
        # assignment with no permutation lookup.
        items = []
        for track in self.queue:
            item = self._track_to_playlist_item(track)
            if item is not None:
                items.append(item)
        return items

    # ── Epoch / ack plumbing ───────────────────────────────────────────────────

    def _apply_ack(self, ack: dict):
        """Reconcile local state against a Dart op ack. Always refreshes the
        shuffle_indices cache; reconciles current_index only when the ack is for
        the LATEST generation (no newer mutation pending), so a slow ack can't
        yank the index back after a subsequent user action."""
        if not ack:
            return
        si = ack.get("shuffle_indices")
        if isinstance(si, list):
            self._shuffle_indices = [int(x) for x in si]
        elif not self._is_shuffle:
            self._shuffle_indices = []
        ci = ack.get("current_index")
        if (
            isinstance(ci, int)
            and ack.get("epoch") == self._queue_epoch
            and self.queue
        ):
            ci = max(0, min(ci, len(self.queue) - 1))
            if ci != self.current_index:
                self.current_index = ci
                self._sync_metadata_for_current()
                self.dispatch("on_queue_mutated")

    async def _dispatch_op(self, send) -> dict:
        """Register a pending ack, invoke `send(request_id)` to issue the
        command, and await the ack. Returns the ack payload ({} / {'ok': False}
        on failure). Dart serializes ops, so awaiting this before the next
        command guarantees ordering."""
        if not self._audio:
            return {"ok": False}
        rid = uuid.uuid4().hex
        try:
            self._audio.register_op(rid)
            await send(rid)
            return await self._audio.wait_for_op(rid)
        except Exception as exc:
            logger.warning("ADB_AUDIO: native op failed: %s", exc)
            return {"ok": False}

    def _schedule_push(self, start_index: int | None = None, autoplay: bool = True):
        """Bump the epoch synchronously (so any state event arriving before Dart
        applies the push is rejected) and schedule a full logical-order playlist
        push. Replaces the old arm_queue_gate + run_task(_push_queue_native)."""
        if start_index is None:
            start_index = self.current_index
        ep = self._next_epoch()
        if self._page:
            self._page.run_task(self._push_queue_native, start_index, autoplay, ep)

    def _schedule_skip(self, logical_index: int, autoplay: bool = True):
        """Bump the epoch synchronously and schedule a skip to a LOGICAL index.
        Dart serializes it after any pending mutation, so append-then-skip is
        race-free without Python-side coalescing."""
        ep = self._next_epoch()
        if self._page:
            self._page.run_task(self._do_skip, logical_index, autoplay, ep)

    async def _do_skip(self, logical_index: int, autoplay: bool, epoch: int):
        self._ensure_audio()
        if not self._audio:
            return
        async def send(rid):
            await self._audio.skip_to_index(
                logical_index, autoplay=autoplay, epoch=epoch, request_id=rid
            )
        ack = await self._dispatch_op(send)
        self._apply_ack(ack)

    async def _push_queue_native(self, start_index: int = 0, autoplay: bool = True,
                                 epoch: int | None = None):
        """Push the current (logical-order) Python queue to Dart's
        ConcatenatingAudioSource so the native player owns advancement,
        notification skip, and background continuation. The shuffle flag is
        folded into set_playlist so source + shuffle state can't disagree; the
        follow-up play() rides inside the same serialized op."""
        await self._push_queue_native_unlocked(start_index, autoplay, epoch)

    async def _push_queue_native_unlocked(self, start_index: int = 0, autoplay: bool = True,
                                          epoch: int | None = None):
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
        target = max(0, min(start_index, len(items) - 1))
        ep = epoch if epoch is not None else self._next_epoch()
        async def send(rid):
            await self._audio.set_playlist(
                items, start_index=target, autoplay=autoplay,
                shuffle=self._is_shuffle, epoch=ep, request_id=rid,
            )
        ack = await self._dispatch_op(send)
        self._apply_ack(ack)

    async def _load_src(self, path, title="", artist="", album="", artwork="",
                        autoplay: bool = True):
        """Compatibility shim; single-track load via the playlist machinery.
        Used by restore_queue() to seek into a restored playlist. The
        autoplay flag lets restore_queue prepare the source without
        starting playback so the user can press play themselves."""
        await self._push_queue_native(start_index=self.current_index, autoplay=autoplay)

    def _on_state_change(self, e):
        queue_index = None
        ev_epoch = 0
        try:
            payload = json.loads(e.data)
            status = payload.get("status", "paused")
            processing_state = payload.get("processing_state", "idle")
            queue_index = payload.get("queue_index")
            ev_epoch = payload.get("epoch", 0)
            si = payload.get("shuffle_indices")
            if isinstance(si, list):
                self._shuffle_indices = [int(x) for x in si]
            dur_ms = payload.get("duration_ms")
            if dur_ms is not None:
                self._set("duration", dur_ms / 1000.0)
        except Exception:
            status = str(e.data) if e.data else "paused"
            processing_state = "idle"

        # Surface the player's processing state so Python-side waiters can read
        # it directly (dirty-checked _set → no dispatch churn when unchanged).
        self._set("processing_state", processing_state)

        # Mirror the native (logical) queue index — changed by skip, notification
        # next/previous, OR gapless auto-advance — but ONLY when the event's
        # epoch equals the latest epoch we sent. A lower epoch means Dart hasn't
        # yet applied our most recent queue change, so its index still describes
        # the previous generation; accepting it would flip current_index into a
        # wrong row. This deterministic check replaces the old wall-clock gate.
        if (
            isinstance(queue_index, int)
            and self.queue
            and ev_epoch == self._queue_epoch
        ):
            target_idx = max(0, min(queue_index, len(self.queue) - 1))
            if target_idx != self.current_index:
                self.current_index = target_idx
                self._sync_metadata_for_current()
                self.dispatch("on_queue_mutated")

        logger.debug(
            "ADB_AUDIO: _on_state_change status=%s, processing=%s, qi=%s, epoch=%s/%s",
            status, processing_state, queue_index, ev_epoch, self._queue_epoch
        )

        if status == "playing":
            self._set("is_playing", True)
        elif status == "paused":
            self._set("is_playing", False)

        if processing_state == "completed":
            # Auto-loop back to the first PLAY-ORDER track if repeat is "all".
            # Under shuffle the first play-order row is shuffle_indices[0]; Dart
            # will report the authoritative index back once the re-push lands.
            if self.repeat_mode == "all" and self.queue:
                target_idx = 0
                if self._is_shuffle and self._shuffle_indices:
                    target_idx = self._shuffle_indices[0]
                target_idx = max(0, min(target_idx, len(self.queue) - 1))
                self.current_index = target_idx
                self._sync_metadata_for_current()
                self._schedule_push(target_idx, True)
            else:
                self._set("is_playing", False)
                if getattr(self, "play_similar_seed_path", ""):
                    self.dispatch("on_similar_continue")
                else:
                    # Terminal end of a finite queue: remember it so the next
                    # play() restarts from the top instead of no-opping on a
                    # 'completed' Dart player.
                    self._completed_terminal = True
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
        # Seed duration from the track's own metadata so the slider shows the
        # correct length immediately — before Dart's durationStream reports, and
        # even when playback isn't (re)started (e.g. reloading a completed
        # queue, where Dart never re-emits duration → stuck at 0:00). Dart's
        # later duration_ms (exact, from the decoder) overwrites this via _set's
        # dirty-check if it differs.
        try:
            seeded_dur = float(track.get("duration") or 0.0)
        except (TypeError, ValueError):
            seeded_dur = 0.0
        self._set("duration", seeded_dur)
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

    def _on_custom_action(self, e):
        """Called when a custom notification action is triggered by the Dart side."""
        try:
            payload = json.loads(e.data)
            self.dispatch("on_custom_action", payload)
        except Exception as ex:
            logger.error("ADB_AUDIO: _on_custom_action failed: %s", ex)

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

    def set_queue(self, tracks: list[dict], start_index: int = 0):
        """Set the playback queue and start from the given index. The full
        queue is pushed (in logical order) to Dart's ConcatenatingAudioSource so
        notification skip and background auto-advance work without Python in the
        loop. Dart applies shuffle natively per the engine's is_shuffle flag."""
        self._completed_terminal = False
        self.queue = tracks
        self.current_index = min(max(0, start_index), len(self.queue) - 1) if self.queue else 0
        self.dispatch("on_queue_mutated")
        self._sync_metadata_for_current()
        if self._page and self.queue:
            self._schedule_push(self.current_index, True)

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
        if not (self._audio and self._page):
            return
        if getattr(self, "_completed_terminal", False) and self.queue:
            # Replay a finished queue from the top; a 'completed' player won't
            # resume on a bare play().
            self._completed_terminal = False
            self.current_index = 0
            self._sync_metadata_for_current()
            self._schedule_push(0, True)
            return
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

    def _logical_neighbour(self, delta: int) -> int | None:
        """Return the logical index `delta` steps from current_index in PLAY
        order — the Dart-reported shuffle order when shuffling, sequential
        otherwise — or None if there is no such neighbour (queue end, before any
        repeat-all wrap handling)."""
        if not self.queue:
            return None
        order = self._shuffle_indices
        if self._is_shuffle and order and len(order) == len(self.queue):
            try:
                pos = order.index(self.current_index)
            except ValueError:
                return None
            npos = pos + delta
            if 0 <= npos < len(order):
                return order[npos]
            return None
        n = self.current_index + delta
        if 0 <= n < len(self.queue):
            return n
        return None

    def next(self):
        if not self._audio or not self._page:
            return
        self._completed_terminal = False
        if self.repeat_mode == "one":
            self._page.run_task(self._audio.seek, 0)
            self._page.run_task(self._audio.play)
            return

        target = self._logical_neighbour(+1)
        if target is not None:
            self.current_index = target
            self._sync_metadata_for_current()
            self._schedule_skip(target)
            return

        # No successor in play order.
        if self.repeat_mode == "all" and self.queue:
            target = self._shuffle_indices[0] if (self._is_shuffle and self._shuffle_indices) else 0
            target = max(0, min(target, len(self.queue) - 1))
            self.current_index = target
            self._sync_metadata_for_current()
            self._schedule_skip(target)
            return
        if getattr(self, "play_similar_seed_path", ""):
            self.dispatch("on_similar_continue")
        else:
            self.stop()

    def previous(self):
        if self.position > 3.0:
            self.seek(0)
            return
        if not self._audio or not self._page:
            return
        self._completed_terminal = False
        target = self._logical_neighbour(-1)
        if target is not None:
            self.current_index = target
            self._sync_metadata_for_current()
            self._schedule_skip(target)

    # ── Queue mutation ────────────────────────────────────────────────────────

    async def _run_native_mutation(self, epoch: int, send):
        """Await an incremental queue-mutation's ack. On success, refresh the
        shuffle cache / reconcile the index from Dart's authoritative reply; on
        failure, fall back to a full logical re-push (reusing the SAME epoch, so
        the generation counter stays consistent)."""
        ack = await self._dispatch_op(send)
        if not ack.get("ok", False):
            logger.warning("ADB_AUDIO: native mutation failed; full re-push fallback")
            await self._push_queue_native_unlocked(
                start_index=self.current_index, autoplay=False, epoch=epoch
            )
            return
        self._apply_ack(ack)

    def queue_next(self, track: dict):
        if not self.queue:
            self.set_queue([track])
            return
        # Insert directly after the current track (logical). insert_at is always
        # > current_index, so current_index needs no adjustment.
        insert_at = min(self.current_index + 1, len(self.queue))
        self.queue.insert(insert_at, track)
        self.dispatch("on_queue_mutated")
        if self._page:
            ep = self._next_epoch()
            self._page.run_task(self._native_add_queue_item, track, insert_at, ep)

    def queue_last(self, track: dict):
        if not self.queue:
            self.set_queue([track])
            return
        self.queue.append(track)
        insert_at = len(self.queue) - 1
        self.dispatch("on_queue_mutated")
        if self._page:
            ep = self._next_epoch()
            self._page.run_task(self._native_add_queue_item, track, insert_at, ep)

    async def _native_add_queue_item(self, track: dict, index: int, epoch: int):
        """Non-destructive insert at a LOGICAL index into the live
        ConcatenatingAudioSource via Dart's addQueueItemAt. Does NOT reload the
        source, so playback/position is preserved. Under shuffle, just_audio
        updates its own shuffle order and reports it back on the ack. Falls back
        to a full logical re-push if Dart reports the op failed."""
        self._ensure_audio()
        if not self._audio:
            return
        item = self._track_to_playlist_item(track)
        if item is None:
            return
        async def send(rid):
            await self._audio.add_queue_item(
                src=item["src"], title=item["title"], artist=item["artist"],
                album=item.get("album"), album_art=item.get("album_art"),
                duration_ms=item.get("duration_ms"), index=index,
                epoch=epoch, request_id=rid,
            )
        await self._run_native_mutation(epoch, send)

    def queue_extend(self, tracks: list[dict]):
        """Append several tracks in one batch. Dispatches on_queue_mutated only
        ONCE (one queue-sheet rebuild, one coalesced save) and sends every insert
        to Dart in a single serialized op. Used by Play Similar block-
        replenishment, which appends up to 8 tracks at a time."""
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
            native_items.append((track, insert_at))
        self.dispatch("on_queue_mutated")
        if self._page:
            ep = self._next_epoch()
            self._page.run_task(self._native_add_queue_items, native_items, ep)

    def queue_after_current(self, tracks: list[dict], after_index: int | None = None):
        """Insert a block of tracks right AFTER the current track (or after
        `after_index`) in one batched op, NON-destructively: the live source is
        NOT reloaded, so the current track keeps playing (no cut). The tail of the
        queue (e.g. the rest of the library) is preserved below the block, and
        current_index is unchanged (every insert lands after it). Order is kept.
        Used by Auto-play to maintain a rolling 'up next' similar buffer without
        disturbing playback or the queue tail."""
        if not tracks:
            return
        if not self.queue:
            self.set_queue(tracks)
            return
        base = self.current_index if after_index is None else after_index
        start = min(max(int(base) + 1, 0), len(self.queue))
        native_items: list[tuple[dict, int]] = []
        for offset, track in enumerate(tracks):
            insert_at = start + offset
            self.queue.insert(insert_at, track)
            native_items.append((track, insert_at))
        self.dispatch("on_queue_mutated")
        if self._page:
            ep = self._next_epoch()
            self._page.run_task(self._native_add_queue_items, native_items, ep)

    async def _native_add_queue_items(self, items: list[tuple[dict, int]], epoch: int):
        """Batch sibling of _native_add_queue_item: insert the whole block into
        the live ConcatenatingAudioSource in ONE serialized Dart call. Indices
        are LOGICAL, computed in append order by queue_extend, so Dart replaying
        them in order is equivalent to N sequential inserts. Falls back to one
        full re-push if Dart reports failure."""
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
                "album": item.get("album"),
                "album_art": item.get("album_art"),
                "duration_ms": item.get("duration_ms"),
                "index": index,
            })
        if not payload:
            return
        async def send(rid):
            await self._audio.add_queue_items(payload, epoch=epoch, request_id=rid)
        await self._run_native_mutation(epoch, send)

    def play_track_at(self, index: int):
        if not (0 <= index < len(self.queue)) or not self._audio or not self._page:
            return
        self._completed_terminal = False
        self.current_index = index
        self._sync_metadata_for_current()
        # skip_to_index takes a LOGICAL index; Dart resolves it under shuffle.
        self._schedule_skip(index)

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
            # Provisional: Dart auto-advances the active source (following its
            # shuffle order) and the op ack reports the authoritative new index,
            # which _apply_ack reconciles.
            self.current_index = min(self.current_index, len(self.queue) - 1)
            self._sync_metadata_for_current()
        self.dispatch("on_queue_mutated")
        if self._page:
            ep = self._next_epoch()
            self._page.run_task(self._native_remove_queue_item, index, ep)

    async def _native_remove_queue_item(self, index: int, epoch: int):
        """Non-destructive removal (logical index) via Dart's removeQueueItemAt.
        Falls back to a full re-push if Dart reports failure."""
        self._ensure_audio()
        if not self._audio:
            return
        async def send(rid):
            await self._audio.remove_queue_item(index, epoch=epoch, request_id=rid)
        await self._run_native_mutation(epoch, send)

    def remove_indices(self, indices: list[int]):
        """Remove several queue slots as ONE logical mutation: one local edit,
        one on_queue_mutated dispatch (one sheet rebuild, one coalesced save) and
        one serialized task that replays the removals on Dart. The currently
        playing slot is never removed, so the live source is not reloaded and
        playback continues uninterrupted. Used by Auto-play to drop its pending
        'up next' buffer when the mode is switched off."""
        targets = sorted(
            {i for i in indices if 0 <= i < len(self.queue) and i != self.current_index},
            reverse=True,
        )
        if not targets:
            return
        for i in targets:
            self.queue.pop(i)
        # Callers only drop tracks AHEAD of current, but stay correct anyway.
        shift = sum(1 for i in targets if i < self.current_index)
        if shift:
            self.current_index = max(0, self.current_index - shift)
        if not self.queue:
            self.stop()
            return
        self.dispatch("on_queue_mutated")
        if self._page:
            ep = self._next_epoch()
            self._page.run_task(self._native_remove_queue_items, targets, ep)

    async def _native_remove_queue_items(self, indices: list[int], epoch: int):
        """Replay a block removal against the live ConcatenatingAudioSource.
        Dart has no batch remove, so the (already DESCENDING) logical indices are
        sent one at a time and awaited in order — descending order is what keeps
        each index valid, since an earlier removal only shifts the slots above it.
        They share one epoch: the block is a single generation, and one failure
        falls back to a single full re-push."""
        self._ensure_audio()
        if not self._audio:
            return
        for idx in indices:
            async def send(rid, i=idx):
                await self._audio.remove_queue_item(i, epoch=epoch, request_id=rid)
            ack = await self._dispatch_op(send)
            if not ack.get("ok", False):
                logger.warning(
                    "ADB_AUDIO: batch remove failed at %d; full re-push fallback", idx
                )
                await self._push_queue_native_unlocked(
                    start_index=self.current_index, autoplay=False, epoch=epoch
                )
                return
            self._apply_ack(ack)

    def move_queue_item(self, old_index: int, new_index: int):
        if not (0 <= old_index < len(self.queue) and 0 <= new_index < len(self.queue)):
            return
        current_obj = self.queue[self.current_index] if self.queue else None
        item = self.queue.pop(old_index)
        self.queue.insert(new_index, item)
        # Track the active track by identity so current_index follows it.
        if current_obj is not None and current_obj in self.queue:
            self.current_index = self.queue.index(current_obj)
        self.dispatch("on_queue_mutated")
        if self._page:
            ep = self._next_epoch()
            # Logical old/new indices; reordering is disabled in the UI under
            # shuffle, so these are sequential positions.
            self._page.run_task(self._native_move_queue_item, old_index, new_index, ep)

    async def _native_move_queue_item(self, old_index: int, new_index: int, epoch: int):
        """Non-destructive reorder (logical indices) via Dart's
        ConcatenatingAudioSource.move. Falls back to a full re-push on failure."""
        self._ensure_audio()
        if not self._audio:
            return
        async def send(rid):
            await self._audio.move_queue_item(old_index, new_index, epoch=epoch, request_id=rid)
        await self._run_native_mutation(epoch, send)

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
        # Dart owns the shuffle order; the restore push applies is_shuffle and
        # reports fresh shuffle_indices back. Clear any stale cache.
        self._shuffle_indices = []
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
            # Bump the epoch SYNCHRONOUSLY before scheduling the restore push.
            # Cold-start codec init + source load can run for seconds; any
            # interim Dart state event carries a lower epoch and is now rejected
            # deterministically (replacing the old generous 5s wall-clock gate),
            # so a null/zero queue_index can't flip current_index to a wrong row
            # before the real source is loaded.
            ep = self._next_epoch()

            async def _restore_async():
                try:
                    # autoplay=False; we want the previous UI revived, not an
                    # unsolicited resume. The source still gets prepared so the
                    # saved-position seek can apply on `ready`, and the user's
                    # first tap on play resumes from that offset.
                    await self._push_queue_native_unlocked(
                        start_index=self.current_index, autoplay=False, epoch=ep
                    )
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
