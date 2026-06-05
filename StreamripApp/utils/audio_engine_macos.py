import sys
import os
import time
import logging
import threading
import random
import math
import flet as ft
from tinytag import TinyTag

if sys.platform == "darwin":
    try:
        from Foundation import NSURL
        from AVFoundation import (
            AVAudioPlayer,
            AVAudioEngine,
            AVAudioPlayerNode,
            AVAudioUnitEQ,
        )
        # AVAudioUnitEQFilterType constants
        AVAudioUnitEQFilterTypeParametric = 0
        _AVF_AVAILABLE = True
    except ImportError:
        NSURL = None
        AVAudioPlayer = None
        AVAudioEngine = None
        AVAudioPlayerNode = None
        AVAudioUnitEQ = None
        AVAudioUnitEQFilterTypeParametric = 0
        _AVF_AVAILABLE = False
else:
    NSURL = None
    AVAudioPlayer = None
    AVAudioEngine = None
    AVAudioPlayerNode = None
    AVAudioUnitEQ = None
    AVAudioUnitEQFilterTypeParametric = 0
    _AVF_AVAILABLE = False

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


class AudioEngine:
    """macOS audio engine backed by AVAudioPlayer (AVFoundation via pyobjc)."""

    # ── Centre frequencies for the 5 parametric EQ bands (Hz) ─────────────────
    _EQ_CENTRE_FREQS = [60.0, 230.0, 910.0, 4000.0, 14000.0]
    _EQ_BANDWIDTH    = 1.0   # octaves per band

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
        self.play_similar_seed_path = ""
        self._db_manager = None

        self._page:  ft.Page | None = None
        self._observers: dict[str, list] = {}
        self._obs_lock = threading.Lock()

        # AVAudioPlayer fallback (used only when AVAudioEngine is unavailable)
        self._player = None
        self._poll_thread: threading.Thread | None = None
        self._poll_stop: threading.Event | None = None
        self._lock = threading.RLock()

        # AVAudioEngine DSP pipeline
        self._dsp_engine: "AVAudioEngine | None" = None
        self._dsp_player: "AVAudioPlayerNode | None" = None
        self._dsp_eq:     "AVAudioUnitEQ | None"    = None
        # Shadow state for live param updates
        self._eq_gains: list[float] = [0.0] * 5
        self._loudness_boost_db: float = 0.0
        self.loudness_boost_db: float = 0.0
        self._dsp_start_time: float = 0.0
        self._base_eq: list[float] = [0.0] * 5
        self._dyn_offsets: list[float] = [0.0] * 5

    def setup(self, page: ft.Page, db_manager=None):
        if self._page is not page:
            logger.warning("ADB_AUDIO: page changed; clearing stale observers")
            self.clear_observers()
        self._page = page
        self._db_manager = db_manager

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

    def _sync_metadata_for_current(self):
        if not self.queue or not (0 <= self.current_index < len(self.queue)):
            return
        track = self.queue[self.current_index]
        path = track.get("path") or ""
        title = (track.get("track_title") or os.path.basename(path)) if path else "Unknown"
        self._set("position", 0.0)
        self._set("duration", 0.0)
        art = track.get("artwork_path") or ""
        if not art and path:
            folder = os.path.dirname(path)
            for name in ("cover.jpg", "folder.jpg", "cover.png", "front.jpg"):
                p = os.path.join(folder, name)
                if os.path.exists(p):
                    art = p
                    break
        self._set("current_art", art)
        self._set("current_artist", track.get("artist_name", "Unknown Artist"))
        self._set("current_album",  track.get("album_title",  "Unknown Album"))
        self._set("current_track",  title)
        self._set("current_path",   path)

        if path and os.path.exists(path):
            try:
                tag = TinyTag.get(path)
                if tag.duration:
                    self._set("duration", tag.duration)
            except Exception as e:
                logger.error(f"Failed to extract duration: {e}")

        # Apply DSP settings (Dynamism and Equalizer)
        try:
            self.reapply_dsp()
        except Exception as ex:
            logger.error("macOS DSP: Failed to apply track transition settings: %s", ex)


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

    # ── DSP engine construction ───────────────────────────────────────────────

    def _build_dsp_engine(self, file_format):
        """Construct (or rebuild) an AVAudioEngine pipeline:
        AVAudioPlayerNode → AVAudioUnitEQ → mainMixerNode → output.
        Returns (engine, player_node, eq_node) or raises on failure.
        """
        engine     = AVAudioEngine.alloc().init()
        player     = AVAudioPlayerNode.alloc().init()
        eq         = AVAudioUnitEQ.alloc().initWithNumberOfBands_(5)

        # Configure each parametric EQ band
        bands = eq.bands()
        for i, band in enumerate(bands):
            band.setFilterType_(AVAudioUnitEQFilterTypeParametric)
            band.setFrequency_(self._EQ_CENTRE_FREQS[i])
            band.setBandwidth_(self._EQ_BANDWIDTH)
            band.setGain_(self._eq_gains[i])
            band.setBypass_(False)

        engine.attachNode_(player)
        engine.attachNode_(eq)

        # Connect player -> eq -> mixer using file processing format,
        # then connect mixer -> output using hardware format.
        mixer = engine.mainMixerNode()
        hardware_format = engine.outputNode().inputFormatForBus_(0)
        engine.connect_to_format_(player, eq,   file_format)
        engine.connect_to_format_(eq,    mixer, file_format)
        engine.connect_to_format_(mixer, engine.outputNode(), hardware_format)

        error_ref = None
        ok, err = engine.startAndReturnError_(error_ref)
        if not ok:
            raise RuntimeError(f"AVAudioEngine start failed: {err}")

        # Apply any pending loudness boost to the player node directly, with headroom.
        max_eq = max(0.0, max(self._eq_gains)) if self._eq_gains else 0.0
        applied_db = self._loudness_boost_db - max_eq
        lin = self._db_to_linear(applied_db)
        lin = max(0.01, lin)   # never mute
        player.setVolume_(lin)

        return engine, player, eq

    @staticmethod
    def _db_to_linear(db: float) -> float:
        """Convert dB to linear amplitude (0 dB → 1.0)."""
        try:
            return math.pow(10.0, float(db) / 20.0)
        except Exception:
            return 1.0

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

                # ── Primary path: AVAudioEngine + DSP chain ───────────────────
                try:
                    # Open the audio file first to get its processing format
                    from AVFoundation import AVAudioFile
                    audio_file, av_err = AVAudioFile.alloc().initForReading_error_(url, None)
                    if audio_file is None:
                        raise RuntimeError(f"AVAudioFile open failed: {av_err}")
                    file_format = audio_file.processingFormat()

                    engine, player_node, eq_node = self._build_dsp_engine(file_format)

                    # Schedule the audio file on the player node.
                    # Completion handler is intentionally None — end-of-track
                    # detection is handled by the polling thread watching
                    # player_node.isPlaying(), which avoids the ObjC void-return
                    # constraint on Python lambdas.
                    if start_time > 0:
                        # Seek to a frame offset by scheduling only the tail segment.
                        sample_rate  = file_format.sampleRate()
                        frame_offset = int(start_time * sample_rate)
                        total_frames = int(audio_file.length())
                        play_frames  = max(1, total_frames - frame_offset)
                        player_node.scheduleSegment_startingFrame_frameCount_atTime_completionHandler_(
                            audio_file, frame_offset, play_frames, None, None
                        )
                    else:
                        player_node.scheduleFile_atTime_completionHandler_(
                            audio_file, None, None
                        )

                    player_node.play()

                    # Read duration from the file
                    sample_rate = file_format.sampleRate()
                    length      = audio_file.length()
                    dur = float(length) / float(sample_rate) if sample_rate > 0 else 0.0

                    self._dsp_engine  = engine
                    self._dsp_player  = player_node
                    self._dsp_eq      = eq_node
                    self._player      = None   # not using bare AVAudioPlayer
                    self._dsp_start_time = float(start_time)
                    self._update_player_volume()

                    if dur > 0:
                        self._set("duration", dur)
                    self._set("position", float(start_time))
                    self._set("is_playing", True)
                    self._spawn_poller(player_node=player_node)
                    return

                except Exception as dsp_err:
                    logger.error("AVAudioEngine DSP chain failed (%s), falling back to AVAudioPlayer", dsp_err, exc_info=True)
                    # Tear down partial engine
                    try:
                        if self._dsp_engine:
                            self._dsp_engine.stop()
                    except Exception:
                        pass
                    self._dsp_engine = self._dsp_player = self._dsp_eq = None

                # ── Fallback path: bare AVAudioPlayer (no DSP) ───────────────
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
                self._dsp_start_time = float(start_time)
                self._set("position", float(start_time))
                self._set("is_playing", True)
                self._spawn_poller()
            except Exception as e:
                logger.error(f"_start_playback error: {e}")
                self._player = None
                self._set("is_playing", False)
                self.dispatch("on_playback_error", str(e))

    def _spawn_poller(self, player_node=None):
        self._poll_stop = threading.Event()
        stop_event = self._poll_stop
        self._poll_thread = threading.Thread(
            target=self._poll_position,
            args=(self._player, player_node, stop_event),
            daemon=True,
        )
        self._poll_thread.start()

    # Poll cadence — desktop, battery cost is negligible.
    _POLL_INTERVAL = 0.10   # 10 Hz: end-of-track detection latency floor
    _DISPATCH_THROTTLE = 0.20  # 5 Hz: position UI update rate

    def _poll_position(self, av_player, player_node, stop_event):
        """Unified position poller — works for both AVAudioPlayer (legacy)
        and AVAudioPlayerNode (engine chain). The node-based path uses the
        node's lastRenderTime + playerTime to compute elapsed seconds.
        End-of-track is detected when the node's `isPlaying()` flips False
        or when the playback position reaches the track duration.
        """
        last_dispatch = 0.0
        try:
            time.sleep(0.05)
            while not stop_event.is_set():
                try:
                    if player_node is not None:
                        # AVAudioPlayerNode path
                        render_time = player_node.lastRenderTime()
                        if render_time is not None:
                            player_time = player_node.playerTimeForNodeTime_(render_time)
                            if player_time is not None:
                                pos = self._dsp_start_time + float(player_time.sampleTime()) / max(1.0, float(player_time.sampleRate()))
                            else:
                                pos = self.position
                        else:
                            pos = self.position
                        playing = bool(player_node.isPlaying())
                    else:
                        # Bare AVAudioPlayer fallback
                        pos     = float(av_player.currentTime())
                        playing = bool(av_player.isPlaying())
                except Exception:
                    break

                # If position reaches or exceeds duration, detect track end
                if self.duration and self.duration > 0.0:
                    if pos >= self.duration:
                        playing = False
                    pos = min(pos, self.duration)
                else:
                    pos = max(0.0, pos)

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
            if self._dsp_player:
                try:
                    self._dsp_player.stop()
                except Exception:
                    pass
                self._dsp_player = None
            if self._dsp_engine:
                try:
                    self._dsp_engine.stop()
                except Exception:
                    pass
                self._dsp_engine = None
            self._dsp_eq = None
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
            # Engine path: resume the player node
            if self._dsp_player is not None and self._dsp_engine is not None:
                try:
                    self._dsp_player.play()
                    self._set("is_playing", True)
                    self._spawn_poller(player_node=self._dsp_player)
                    return
                except Exception as e:
                    logger.error(f"Engine resume failed, restarting: {e}")
            # Legacy fallback path
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
            if not self.is_playing:
                return
            try:
                if self._dsp_player is not None:
                    self._dsp_player.pause()
                elif self._player is not None:
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

            if self._dsp_player is not None and self._dsp_engine is not None:
                # For AVAudioPlayerNode, seeking requires re-scheduling from the new frame
                path = self.queue[self.current_index].get("path") if self.queue else None
                if path:
                    was_playing = self.is_playing
                    self._stop_playback()
                    self._start_playback(path, bounded)
                    if not was_playing:
                        self.pause()
            elif self._player is not None:
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
                    elif getattr(self, "play_similar_seed_path", ""):
                        self.dispatch("on_similar_continue")
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
                elif getattr(self, "play_similar_seed_path", ""):
                    self.dispatch("on_similar_continue")
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
            curr_shuf_idx = -1
            if self._is_shuffle and getattr(self, "_shuffle_order", None):
                try:
                    curr_shuf_idx = self._shuffle_order.index(index)
                except ValueError:
                    pass
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
            self.queue = tracks
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

    # ── DSP: Dynamism / Loudness & Equalizer ──────────────────────────────────

    def _update_player_volume(self):
        with self._lock:
            if self._dsp_player is not None:
                applied_db = self._loudness_boost_db
                lin = self._db_to_linear(applied_db)
                lin = max(0.01, lin)  # never fully mute
                try:
                    self._dsp_player.setVolume_(lin)
                    logger.debug("DSP volume update: boost=%.1f dB, applied=%.1f dB (%.3fx)",
                                 self._loudness_boost_db, applied_db, lin)
                except Exception as exc:
                    logger.debug("_update_player_volume failed: %s", exc)

    def set_loudness_boost(self, gain_db: float):
        """Apply a per-track loudness gain (dB) via the AVAudioPlayerNode volume.
        AVAudioPlayerNode.setVolume_() supports values > 1.0 for amplification,
        unlike mainMixerNode.setOutputVolume_() which is capped at 1.0.
        """
        self._loudness_boost_db = float(gain_db)
        self._set("loudness_boost_db", float(gain_db))
        self._update_player_volume()

    def set_eq_band_gain(self, band_index: int, gain_db: float):
        """Update the gain on a single parametric EQ band in real-time."""
        if not (0 <= band_index < 5):
            return
        self._base_eq[band_index] = float(gain_db)
        combined = self._base_eq[band_index] + self._dyn_offsets[band_index]
        self._eq_gains[band_index] = combined
        with self._lock:
            if self._dsp_eq is not None:
                try:
                    self._dsp_eq.bands()[band_index].setGain_(float(combined))
                except Exception as exc:
                    logger.debug("set_eq_band_gain[%d] failed: %s", band_index, exc)
        self._update_player_volume()

    def apply_combined_dsp(self, base_eq: list[float], dyn_offsets: list[float]):
        self._base_eq = list(base_eq)
        self._dyn_offsets = list(dyn_offsets)
        with self._lock:
            for idx in range(5):
                combined = self._base_eq[idx] + self._dyn_offsets[idx]
                self._eq_gains[idx] = combined
                if self._dsp_eq is not None:
                    try:
                        self._dsp_eq.bands()[idx].setGain_(float(combined))
                    except Exception as exc:
                        logger.debug("apply_combined_dsp band[%d] failed: %s", idx, exc)
        self._update_player_volume()

    async def get_equalizer_bands(self) -> dict:
        """Return live equalizer band state from the DSP chain."""
        bands = []
        for i, freq in enumerate(self._EQ_CENTRE_FREQS):
            gain = 0.0
            if self._dsp_eq is not None:
                try:
                    gain = float(self._dsp_eq.bands()[i].gain())
                except Exception:
                    gain = self._eq_gains[i]
            else:
                gain = self._eq_gains[i]
            bands.append({"index": i, "center_frequency": freq, "gain": gain})
        return {
            "ok": True,
            "min_db": -15.0,
            "max_db": 15.0,
            "bands": bands,
        }

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


audio_engine = AudioEngine()
