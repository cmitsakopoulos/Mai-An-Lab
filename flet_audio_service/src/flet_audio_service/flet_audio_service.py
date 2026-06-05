import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from flet.controls.base_control import control
from flet.controls.control_event import EventHandler
from flet.controls.services.service import Service

__all__ = ["AudioServiceControl"]

_READY_TIMEOUT = 25.0  # seconds to wait for Dart AudioService.init() to complete
# Decoding a 60s PCM clip on a phone takes 1–3s typical, but cold-start of the
# hardware decoder can spike to 8–10s on the first track. 30s is generous and
# still bails out cleanly if the codec wedges on a malformed file.
_DECODE_TIMEOUT = 30.0

_logger = logging.getLogger("flet_audio_service")


def _log(msg: str) -> None:
    """Use logging.warning so messages appear in adb logcat under the
    `serious_python` tag (matching the project's existing logger output).
    `print()` is unreliable here because Flet apps commonly redirect stdout."""
    _logger.warning(f"FAS: {msg}")


@control("flet_audio_service")
class AudioServiceControl(Service):
    """
    AudioServiceControl enables background audio playback with Android media notifications.
    It bridges the Flet Python side with the Dart 'audio_service' and 'just_audio' implementation.

    Every public invoke method automatically waits for the Dart side to signal readiness
    (via mark_ready()) before sending the command, preventing TimeoutException races on startup.
    """

    src: Optional[str] = None
    title: Optional[str] = None
    artist: Optional[str] = None
    album_art: Optional[str] = None

    on_state_change: Optional[EventHandler] = None
    on_position_change: Optional[EventHandler] = None
    on_error: Optional[EventHandler] = None
    on_ready: Optional[EventHandler] = None
    # Internal-use event: completion signal for decode_pcm calls. Fired by the
    # Dart side with a JSON payload {request_id, ok, output_path, sample_rate,
    # num_samples, error?}. Python correlates request_id back to a pending
    # asyncio.Future so callers can `await audio_service.decode_pcm(...)`.
    on_decode_complete: Optional[EventHandler] = None
    # Internal-use events for the permissions bridge. Same correlation pattern
    # as decode_pcm: Dart emits with {request_id, ok, ...payload}.
    on_permissions_result: Optional[EventHandler] = None
    on_permission_request_result: Optional[EventHandler] = None
    # Internal-use event for TTS speak completion (utterance finished).
    on_tts_complete: Optional[EventHandler] = None
    # Internal-use event for STT transcription results.
    on_stt_result: Optional[EventHandler] = None
    on_equalizer_bands_result: Optional[EventHandler] = None

    # ── Internal readiness gate ───────────────────────────────────────────────

    def __init__(self, **kwargs):
        _log("__init__ entered")
        on_ready_cb = kwargs.pop("on_ready", None)
        on_state_change_cb = kwargs.pop("on_state_change", None)
        on_position_change_cb = kwargs.pop("on_position_change", None)
        on_error_cb = kwargs.pop("on_error", None)

        src_val = kwargs.pop("src", None)
        title_val = kwargs.pop("title", None)
        artist_val = kwargs.pop("artist", None)
        album_art_val = kwargs.pop("album_art", None)

        super().__init__(**kwargs)
        _log("__init__ super() returned")

        self.on_state_change = on_state_change_cb
        self.on_position_change = on_position_change_cb
        self.on_error = on_error_cb
        self.src = src_val
        self.title = title_val
        self.artist = artist_val
        self.album_art = album_art_val

        object.__setattr__(self, "_native_ready_event", asyncio.Event())
        # Pending decode requests, keyed by request_id. Each value is the
        # asyncio.Future awaiting the Dart-side decode_complete event.
        object.__setattr__(self, "_pending_decodes", {})
        # Same pattern for permission queries / requests.
        object.__setattr__(self, "_pending_perm_queries", {})
        object.__setattr__(self, "_pending_perm_requests", {})
        # TTS utterance completions, keyed by request_id.
        object.__setattr__(self, "_pending_tts", {})
        # STT results, keyed by request_id.
        object.__setattr__(self, "_pending_stt", {})
        object.__setattr__(self, "_pending_eq_queries", {})

        def internal_on_decode_complete(e):
            try:
                payload = json.loads(getattr(e, "data", "") or "{}")
            except Exception as ex:
                _log(f"decode_complete: bad payload: {ex}")
                return
            req_id = payload.get("request_id")
            if not req_id:
                return
            pending = object.__getattribute__(self, "_pending_decodes")
            fut = pending.pop(req_id, None)
            if fut is None or fut.done():
                return
            if payload.get("ok"):
                fut.set_result(payload)
            else:
                fut.set_exception(
                    RuntimeError(payload.get("error") or "decode failed")
                )

        self.on_decode_complete = internal_on_decode_complete

        def _resolve_pending(pending_attr: str, e) -> None:
            try:
                payload = json.loads(getattr(e, "data", "") or "{}")
            except Exception as ex:
                _log(f"{pending_attr}: bad payload: {ex}")
                return
            req_id = payload.get("request_id")
            if not req_id:
                return
            pending = object.__getattribute__(self, pending_attr)
            fut = pending.pop(req_id, None)
            if fut is None or fut.done():
                return
            if payload.get("ok"):
                fut.set_result(payload)
            else:
                fut.set_exception(
                    RuntimeError(payload.get("error") or "permission op failed")
                )

        self.on_permissions_result = lambda e: _resolve_pending(
            "_pending_perm_queries", e
        )
        self.on_permission_request_result = lambda e: _resolve_pending(
            "_pending_perm_requests", e
        )
        self.on_tts_complete = lambda e: _resolve_pending("_pending_tts", e)
        self.on_stt_result = lambda e: _resolve_pending("_pending_stt", e)
        self.on_equalizer_bands_result = lambda e: _resolve_pending(
            "_pending_eq_queries", e
        )

        def internal_on_ready(e):
            _log("internal_on_ready FIRED; Dart said ready")
            self.mark_ready()
            if on_ready_cb:
                if asyncio.iscoroutinefunction(on_ready_cb):
                    self.page.run_task(on_ready_cb, e)
                else:
                    on_ready_cb(e)

        self.on_ready = internal_on_ready
        _log(f"__init__ done; on_ready bound, type(_native_ready_event)={type(self._get_ready_event()).__name__}")

    def _get_ready_event(self) -> asyncio.Event:
        return object.__getattribute__(self, "_native_ready_event")

    def mark_ready(self) -> None:
        """
        Signal that the native Dart audio handler has finished initialising.
        """
        ev = self._get_ready_event()
        ev.set()
        _log(f"mark_ready called; event.is_set()={ev.is_set()}")

    async def _wait_ready(self) -> None:
        """
        Awaits the internal ready event, with a generous timeout.
        Raises RuntimeError if the Dart side never becomes ready.
        """
        ev = self._get_ready_event()
        if ev.is_set():
            return  # fast path; already ready
        _log("_wait_ready: event NOT set, will wait up to 25s")
        try:
            await asyncio.wait_for(asyncio.shield(ev.wait()), timeout=_READY_TIMEOUT)
            _log("_wait_ready: event was set, proceeding")
        except asyncio.TimeoutError:
            _log("_wait_ready: TIMEOUT; Dart never fired ready event")
            raise RuntimeError(
                f"flet_audio_service: native audio handler not ready after {_READY_TIMEOUT}s"
            )

    # ── Public API; all gate on _wait_ready() ────────────────────────────────

    async def play(self):
        """Starts or resumes audio playback."""
        await self._wait_ready()
        print("FLET_AUDIO_SERVICE: Calling play()")
        await self._invoke_method("play")

    async def pause(self):
        """Pauses audio playback."""
        await self._wait_ready()
        print("FLET_AUDIO_SERVICE: Calling pause()")
        await self._invoke_method("pause")

    async def stop(self):
        """Stops audio playback and releases resources."""
        await self._wait_ready()
        print("FLET_AUDIO_SERVICE: Calling stop()")
        await self._invoke_method("stop")

    async def seek(self, position_ms: int):
        """Seeks to a specific position in the audio."""
        await self._wait_ready()
        await self._invoke_method("seek", {"position": position_ms})

    async def set_media_item(
        self,
        title: str,
        artist: str,
        album_art: Optional[str] = None,
        src: Optional[str] = None,
    ):
        """Updates the current media item metadata and optionally the audio source."""
        await self._wait_ready()
        print(f"FLET_AUDIO_SERVICE: set_media_item title={title}, src={src}")
        await self._invoke_method(
            "set_media_item",
            {
                "title": title,
                "artist": artist,
                "album_art": album_art,
                "src": src,
            },
        )

    async def show_progress_notification(
        self,
        title: str,
        content: str,
        progress: int,
        total: int,
        done: bool = False,
    ):
        """Displays a native Android notification showing progress of a task (e.g. DSP scan)."""
        await self._wait_ready()
        await self._invoke_method(
            "show_progress_notification",
            {
                "title": title,
                "content": content,
                "progress": progress,
                "total": total,
                "done": done,
            },
        )

    async def set_playlist(self, items: list, start_index: int = 0):
        """Replaces the entire playlist with a new list of tracks and starts
        playback at start_index. Atomic on the Dart side; avoids the race
        between set_playlist + skip_to_index where the seek could be clobbered
        by the source-load resetting the player to index 0."""
        await self._wait_ready()
        await self._invoke_method(
            "set_playlist", {"items": items, "start_index": start_index}
        )

    async def add_queue_item(
        self,
        src: str,
        title: str,
        artist: str,
        album_art: Optional[str] = None,
        index: Optional[int] = None,
    ):
        """Inserts a track into the queue at the given index (appends if omitted)."""
        await self._wait_ready()
        payload = {"src": src, "title": title, "artist": artist, "album_art": album_art}
        if index is not None:
            payload["index"] = index
        await self._invoke_method("add_queue_item", payload)

    async def remove_queue_item(self, index: int):
        """Removes the queue item at the given index."""
        await self._wait_ready()
        await self._invoke_method("remove_queue_item", {"index": index})

    async def move_queue_item(self, from_index: int, to_index: int):
        """Reorders the queue item at from_index to to_index. In-place on
        the live ConcatenatingAudioSource; the active source is not
        reloaded and playback continues uninterrupted."""
        await self._wait_ready()
        await self._invoke_method(
            "move_queue_item", {"from_index": from_index, "to_index": to_index}
        )

    async def skip_to_next(self):
        """Skips to the next track in the queue."""
        await self._wait_ready()
        await self._invoke_method("skip_to_next")

    async def skip_to_previous(self):
        """Skips to the previous track in the queue."""
        await self._wait_ready()
        await self._invoke_method("skip_to_previous")

    async def skip_to_index(self, index: int):
        """Jumps directly to a specific track in the queue."""
        await self._wait_ready()
        await self._invoke_method("skip_to_index", {"index": index})

    async def set_repeat_mode(self, mode: str):
        """Sets the repeat mode: 'none', 'one', or 'all'."""
        await self._wait_ready()
        await self._invoke_method("set_repeat_mode", {"mode": mode})

    async def decode_pcm(self, path: str) -> dict[str, Any]:
        """Decode an audio file to mono 16-bit little-endian PCM via the
        platform's hardware codec. Returns a dict with keys:

            output_path: absolute path to the raw int16 LE PCM file
            sample_rate: output sample rate (Hz, currently 22050)
            num_samples: number of mono samples in the output file
            channels:    always 1

        Raises RuntimeError on decode failure or asyncio.TimeoutError if the
        native side does not reply within the budget. The output file lives
        in the app cache and is overwritten on subsequent decodes of the
        same source path (filenames are derived from the input hash).
        """
        await self._wait_ready()
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending = object.__getattribute__(self, "_pending_decodes")
        pending[request_id] = fut
        try:
            await self._invoke_method(
                "decode_pcm",
                {"request_id": request_id, "path": path},
            )
            return await asyncio.wait_for(fut, timeout=_DECODE_TIMEOUT)
        except asyncio.TimeoutError:
            pending.pop(request_id, None)
            raise
        except Exception:
            pending.pop(request_id, None)
            raise

    async def query_permissions(self) -> dict[str, Any]:
        """Returns current runtime permission status for the four permissions
        the app cares about. Keys: notification, audio, storage,
        manage_external_storage. Values are permission_handler status names
        ('granted', 'denied', 'permanentlyDenied', 'restricted', 'limited',
        'provisional'). On non-Android platforms this raises RuntimeError via
        the Dart side."""
        return await self._await_perm_op("query_permissions", "_pending_perm_queries", {})

    async def request_permission(self, name: str) -> dict[str, Any]:
        """Request a single permission and return its resulting status. For
        'manage_external_storage' this opens Android Settings; the future
        resolves when the user returns to the app. `name` must be one of:
        notification, audio, storage, manage_external_storage."""
        return await self._await_perm_op(
            "request_permission", "_pending_perm_requests", {"name": name}
        )

    async def open_app_settings(self) -> None:
        """Launch the Android app's system Settings page. Fire-and-forget."""
        await self._wait_ready()
        await self._invoke_method("open_app_settings")

    async def tts_speak(self, text: str, timeout: float = 30.0) -> dict[str, Any]:
        """Speak `text` using the platform TTS engine and resolve once the
        utterance finishes. Caller is responsible for pausing music playback
        if it doesn't want overlap — flutter_tts mixes by default."""
        await self._wait_ready()
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending = object.__getattribute__(self, "_pending_tts")
        pending[request_id] = fut
        try:
            await self._invoke_method(
                "tts_speak", {"request_id": request_id, "text": text}
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            pending.pop(request_id, None)
            raise
        except Exception:
            pending.pop(request_id, None)
            raise

    async def tts_stop(self) -> None:
        """Interrupt the current utterance, if any. No-op when nothing is
        speaking. Use this when the user dismisses the assistant mid-reply."""
        await self._wait_ready()
        await self._invoke_method("tts_stop")

    async def tts_set_voice(
        self, rate: Optional[float] = None, pitch: Optional[float] = None
    ) -> None:
        """Adjust the voice characteristics for subsequent utterances. Rate is
        clamped to [0.1, 1.5] and pitch to [0.5, 2.0] on the Dart side."""
        await self._wait_ready()
        payload: dict[str, Any] = {}
        if rate is not None:
            payload["rate"] = float(rate)
        if pitch is not None:
            payload["pitch"] = float(pitch)
        await self._invoke_method("tts_set_voice", payload)

    async def stt_listen(self, timeout: float = 15.0) -> dict[str, Any]:
        """Start listening for speech input and resolve with the transcribed
        text once the utterance finishes. Returns a dict:
            ok: bool
            text: string (if ok)
            error: string (if not ok)
        This automatically handles RECORD_AUDIO permission checks on the Dart
        side. Music playback is NOT automatically paused."""
        await self._wait_ready()
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending = object.__getattribute__(self, "_pending_stt")
        pending[request_id] = fut
        try:
            await self._invoke_method(
                "stt_listen", {"request_id": request_id, "timeout": timeout}
            )
            return await asyncio.wait_for(fut, timeout=timeout + 5.0)
        finally:
            pending.pop(request_id, None)

    async def stt_stop(self) -> None:
        """Manually stop the current STT session. No-op if not listening."""
        await self._wait_ready()
        await self._invoke_method("stt_stop")

    async def set_loudness_boost(self, gain: float) -> None:
        """Set target gain boost in decibels for loudness enhancement."""
        await self._wait_ready()
        await self._invoke_method("set_loudness_boost", {"gain": gain})

    async def set_eq_band_gain(self, index: int, gain: float) -> None:
        """Set the gain level in decibels for a specific equalizer band."""
        await self._wait_ready()
        await self._invoke_method("set_eq_band_gain", {"index": index, "gain": gain})

    async def get_equalizer_bands(self) -> dict[str, Any]:
        """Fetch the available equalizer bands from the player."""
        return await self._await_perm_op(
            "get_equalizer_bands", "_pending_eq_queries", {}
        )

    async def _await_perm_op(
        self, method: str, pending_attr: str, extra_args: dict
    ) -> dict[str, Any]:
        await self._wait_ready()
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending = object.__getattribute__(self, pending_attr)
        pending[request_id] = fut
        args = {"request_id": request_id, **extra_args}
        try:
            await self._invoke_method(method, args)
            # Permission requests can sit on the Settings activity indefinitely
            # for manage_external_storage; allow a generous wait.
            return await asyncio.wait_for(fut, timeout=120.0)
        except asyncio.TimeoutError:
            pending.pop(request_id, None)
            raise
        except Exception:
            pending.pop(request_id, None)
            raise
