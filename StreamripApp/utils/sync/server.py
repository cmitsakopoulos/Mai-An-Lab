"""
macOS Companion Server for Mai-An Lab.
Runs an opt-in local HTTP service on macOS using aiohttp.web, with pairing PIN and DSP offload workers.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import platform
import secrets
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote, unquote

from aiohttp import web

from utils.dsp import (
    FEATURES_VERSION,
    MAX_SECONDS,
    TARGET_SAMPLE_RATE,
    extract_features_from_pcm,
)
from .discovery import BeaconBroadcaster, resolve_local_ip
from .transcoder import TranscodeProfile, find_tool, transcode_audio

logger = logging.getLogger("sync.server")

DEFAULT_PORT = 8945
MAX_PORT_ATTEMPTS = 5
# Wrong PINs tolerated before the PIN is rotated. A 4-digit PIN falls to a
# scripted loop in seconds otherwise; rotating it makes guessing pointless.
MAX_PIN_FAILURES = 5
TOKEN_PREF = "companion_server_token"


_SERVER: Optional["CompanionServer"] = None


def get_companion_server(app) -> "CompanionServer":
    """The process's single CompanionServer, created on first use.

    One per process, not per app object: a UI restart builds a new app while
    the old server still holds the port and the queued transfers.

    The bearer token lives in app prefs: a token minted per process meant every
    Mac relaunch silently invalidated every paired phone, which kept looking
    "connected" (ping was unauthenticated) while every real call got a 401.
    """
    global _SERVER
    if _SERVER is None:
        prefs = getattr(app, "_prefs", {}) or {}
        token = prefs.get(TOKEN_PREF) or ""
        if not token:
            token = secrets.token_hex(16)
            app._save_pref(TOKEN_PREF, token)
        prefs_path = getattr(app, "_prefs_path", "") or ""
        state_path = (os.path.join(os.path.dirname(prefs_path), "companion_transfers.json")
                      if prefs_path else None)
        _SERVER = CompanionServer(db_manager=getattr(app, "db_manager", None),
                                  auth_token=token, transfers_path=state_path)
    _SERVER.db_manager = getattr(app, "db_manager", None) or _SERVER.db_manager
    _SERVER.on_token_reset = lambda t: app._save_pref(TOKEN_PREF, t)
    app._companion_server = _SERVER
    return _SERVER


def _run_decode(ffmpeg: str, src: str, out_pcm: str, seek: bool) -> tuple[bool, str]:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if seek:
        cmd += ["-ss", "15"]
    cmd += [
        "-t", str(MAX_SECONDS), "-i", src,
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE), "-y", out_pcm,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    ok = res.returncode == 0 and os.path.exists(out_pcm) and os.path.getsize(out_pcm) > 0
    return ok, res.stderr


def _relocate_moov(src: str) -> Optional[str]:
    """tools/dsp_offload.py's fragmented-MP4 fix, when the repo is around."""
    try:
        import importlib.util
        tool = Path(__file__).resolve().parents[3] / "tools" / "dsp_offload.py"
        if not tool.is_file():
            return None
        spec = importlib.util.spec_from_file_location("_mai_dsp_offload", tool)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._relocate_moov_to_front(src)
    except (Exception, SystemExit) as ex:
        logger.debug("moov relocation unavailable: %s", ex)
        return None


def decode_and_extract(src: str):
    """Decode the canonical window and extract features (blocking).

    Same window as the on-device analyser and tools/dsp_offload.py: skip 15 s,
    read MAX_SECONDS. A shorter window here made Mac-computed features drift
    from every other track's, which skews the similarity graph.
    """
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on the Mac (brew install ffmpeg)")
    fd, out_pcm = tempfile.mkstemp(suffix=".pcm")
    os.close(fd)
    try:
        ok, err = _run_decode(ffmpeg, src, out_pcm, seek=True)
        if not ok:  # very short track
            ok, err = _run_decode(ffmpeg, src, out_pcm, seek=False)
        if not ok:  # fragmented MP4 with moov at the end
            fixed = _relocate_moov(src)
            if fixed:
                try:
                    ok, err = _run_decode(ffmpeg, fixed, out_pcm, seek=True)
                    if not ok:
                        ok, err = _run_decode(ffmpeg, fixed, out_pcm, seek=False)
                finally:
                    try:
                        os.remove(fixed)
                    except OSError:
                        pass
        if not ok:
            raise RuntimeError(f"ffmpeg decode failed: {err.strip()}")
        return extract_features_from_pcm(out_pcm, TARGET_SAMPLE_RATE)
    finally:
        try:
            os.remove(out_pcm)
        except OSError:
            pass


def _transfer_id(path: str, quality: str) -> str:
    # Stable across calls and processes (hash() is salted per process), so
    # re-sending a track replaces its pending entry instead of duplicating it.
    return hashlib.sha1(f"{path}\0{quality}".encode("utf-8")).hexdigest()[:16]


def _init_feature_cache_db() -> str:
    """Locate or initialize tools/offload_cache/feature_cache.db."""
    # Find repository root relative to this file: StreamripApp/utils/sync/server.py -> parent x 3
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    cache_dir = repo_root / "tools" / "offload_cache"
    try:
        os.makedirs(cache_dir, exist_ok=True)
        db_path = str(cache_dir / "feature_cache.db")
    except OSError:
        fallback = Path.home() / ".cache" / "mai_an_lab"
        os.makedirs(fallback, exist_ok=True)
        db_path = str(fallback / "feature_cache.db")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feature_cache (
                track_path TEXT PRIMARY KEY,
                bpm REAL,
                energy REAL,
                brightness REAL,
                rolloff REAL,
                beat_strength REAL,
                spectral_flatness REAL,
                spectral_contrast REAL,
                key_index INTEGER,
                timbre BLOB,
                features_version INTEGER
            )
        """)
        conn.commit()
    finally:
        conn.close()
    return db_path


class CompanionServer:
    """Manually-controlled local server for macOS.
    Exposes endpoints for DSP offload, music streaming, and bundle transfer.
    """

    def __init__(
        self,
        db_manager=None,
        port: int = DEFAULT_PORT,
        device_name: Optional[str] = None,
        pin: Optional[str] = None,
        idle_timeout_s: float = 900.0,  # 15 minutes
        auth_token: Optional[str] = None,
        trust_loopback: bool = True,
        transfers_path: Optional[str] = None,
    ):
        self.db_manager = db_manager
        self.preferred_port = port
        self.bound_port: Optional[int] = None
        self.device_name = device_name or platform.node() or "Mac Companion"
        self.pin = pin or f"{secrets.randbelow(9000) + 1000}"  # 4-digit PIN
        self.auth_token = auth_token or secrets.token_hex(16)
        self.idle_timeout_s = idle_timeout_s
        self.trust_loopback = trust_loopback
        self.on_token_reset: Optional[Callable[[str], None]] = None
        self._pin_failures = 0
        # Who last talked to us, for the host UI: (device name, ip, monotonic).
        self.last_client: Optional[tuple[str, str, float]] = None
        self._state_listeners: list[Callable[[], None]] = []

        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._broadcaster: Optional[BeaconBroadcaster] = None
        self._cache_db_path = _init_feature_cache_db()
        self._caffeinate_proc: Optional[subprocess.Popen] = None
        self._sleep_holders = 0
        # Persisted so a Mac app quit/relaunch doesn't silently drop tracks
        # the phone hasn't picked up yet.
        self._transfers_path = transfers_path
        self._pending_transfers: list[dict] = self._load_transfers()

        self._is_running = False
        self._last_active_time = time.time()
        self._idle_task: Optional[asyncio.Task] = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    def _touch(self):
        self._last_active_time = time.time()

    def _load_transfers(self) -> list[dict]:
        if not self._transfers_path:
            return []
        try:
            with open(self._transfers_path, encoding="utf-8") as fh:
                items = json.load(fh)
        except FileNotFoundError:
            return []
        except Exception as ex:
            logger.warning("Ignoring unreadable transfer queue %s: %s", self._transfers_path, ex)
            return []
        # Files moved or deleted since are dropped, not offered and then 404'd.
        return [t for t in items if isinstance(t, dict) and os.path.isfile(t.get("path") or "")]

    def _save_transfers(self):
        if not self._transfers_path:
            return
        try:
            tmp = self._transfers_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._pending_transfers, fh, ensure_ascii=False)
            os.replace(tmp, self._transfers_path)
        except Exception as ex:
            logger.warning("Could not persist transfer queue: %s", ex)

    def clear_pending_transfers(self):
        self._pending_transfers = []
        self._save_transfers()
        self._notify()

    def subscribe(self, cb: Callable[[], None]):
        if cb not in self._state_listeners:
            self._state_listeners.append(cb)

    def unsubscribe(self, cb: Callable[[], None]):
        if cb in self._state_listeners:
            self._state_listeners.remove(cb)

    def _notify(self):
        for cb in list(self._state_listeners):
            try:
                cb()
            except Exception as ex:
                logger.debug("server state listener failed: %s", ex)

    def _rotate_pin(self):
        self.pin = f"{secrets.randbelow(9000) + 1000}"
        self._pin_failures = 0
        self._notify()

    def reset_pairing(self):
        """Revoke every paired device: new token, new PIN."""
        self.auth_token = secrets.token_hex(16)
        if self.on_token_reset:
            self.on_token_reset(self.auth_token)
        self._rotate_pin()

    def _acquire_sleep_lock(self):
        """Prevent macOS sleep during active compute/transfers via caffeinate.
        Reference-counted: overlapping requests each hold it, and the first
        to finish must not let the Mac sleep under the others."""
        self._sleep_holders += 1
        if platform.system() == "Darwin" and not self._caffeinate_proc:
            try:
                self._caffeinate_proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except Exception as ex:
                logger.debug("Failed to spawn caffeinate: %s", ex)

    def _release_sleep_lock(self):
        self._sleep_holders = max(0, self._sleep_holders - 1)
        if self._sleep_holders == 0:
            self._kill_caffeinate()

    def _kill_caffeinate(self):
        if self._caffeinate_proc:
            try:
                self._caffeinate_proc.terminate()
            except Exception:
                pass
            self._caffeinate_proc = None

    def _check_auth(self, request: web.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        presented = auth[7:].strip() if auth.startswith("Bearer ") else ""
        # Download URLs carry the token as a query parameter: the phone's
        # download pipeline issues plain GETs with no custom headers.
        presented = presented or request.query.get("token", "")
        if presented:
            return secrets.compare_digest(presented, self.auth_token)
        # USB: `adb reverse` lands phone traffic on loopback. Physical access
        # to the cable is the trust anchor there, so no PIN is needed.
        return self.trust_loopback and request.remote in ("127.0.0.1", "::1")

    # ─── Endpoints ─────────────────────────────────────────────────────────────

    async def handle_ping(self, request: web.Request) -> web.Response:
        self._touch()
        authorized = self._check_auth(request)
        if authorized:
            name = unquote(request.headers.get("X-Device-Name", "Phone"))
            self.last_client = (name, request.remote or "", time.monotonic())
        total_tracks = 0
        if self.db_manager:
            try:
                total_tracks = await self.db_manager.get_total_tracks()
            except Exception:
                pass
        return web.json_response({
            "status": "ok",
            "name": self.device_name,
            "device": "macOS",
            "features_version": FEATURES_VERSION,
            "total_tracks": total_tracks,
            "requires_pairing": True,
            # Ping itself is open (discovery needs it), so the client learns
            # here whether its token still works instead of on the next 401.
            "authorized": authorized,
        })

    async def handle_pair(self, request: web.Request) -> web.Response:
        self._touch()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        pin_attempt = str(body.get("pin", "")).strip()
        client_name = body.get("device_name", "Android Client")
        if secrets.compare_digest(pin_attempt, self.pin):
            logger.info("Successfully paired with client: %s", client_name)
            self._pin_failures = 0
            self.last_client = (client_name, request.remote or "", time.monotonic())
            self._notify()
            return web.json_response({
                "status": "paired",
                "token": self.auth_token,
                "server_name": self.device_name,
            })
        logger.warning("Pairing attempt failed from %s: invalid PIN", request.remote)
        self._pin_failures += 1
        if self._pin_failures >= MAX_PIN_FAILURES:
            logger.warning("Too many wrong PINs; rotating pairing PIN")
            self._rotate_pin()
        return web.json_response({"error": "Invalid PIN"}, status=403)

    async def handle_dsp_match(self, request: web.Request) -> web.Response:
        """Check a list of track paths or titles against Mac's feature cache."""
        self._touch()
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            body = await request.json()
            tracks = body.get("tracks", [])
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        cached_results = []
        missing_paths = []

        conn = sqlite3.connect(self._cache_db_path)
        try:
            cursor = conn.cursor()
            for t in tracks:
                path = t.get("path")
                if not path:
                    continue
                cursor.execute(
                    """
                    SELECT bpm, energy, brightness, rolloff, beat_strength,
                           spectral_flatness, spectral_contrast, key_index, timbre
                    FROM feature_cache
                    WHERE track_path = ? AND features_version = ?
                    """,
                    (path, FEATURES_VERSION),
                )
                row = cursor.fetchone()
                if row:
                    cached_results.append({
                        "path": path,
                        "bpm": row[0],
                        "energy": row[1],
                        "brightness": row[2],
                        "rolloff": row[3],
                        "beat_strength": row[4],
                        "spectral_flatness": row[5],
                        "spectral_contrast": row[6],
                        "key_index": row[7],
                        "timbre_b64": base64.b64encode(row[8]).decode("ascii") if row[8] else "",
                        "features_version": FEATURES_VERSION,
                    })
                else:
                    missing_paths.append(path)
        finally:
            conn.close()

        return web.json_response({
            "cached": cached_results,
            "missing": missing_paths,
        })

    async def handle_dsp_extract(self, request: web.Request) -> web.Response:
        """Receive an audio upload, decode to PCM via ffmpeg, run feature extraction,
        and update feature_cache.db.
        """
        self._touch()
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        self._acquire_sleep_lock()
        tmp_audio_path = None
        try:
            reader = await request.multipart()
            # The phone path comes as a form field ahead of the file: as a
            # header, a Greek/accented path is not valid latin-1 HTTP. The
            # percent-encoded header is kept for older clients.
            track_path = unquote(request.headers.get("X-Track-Path", ""))
            part = await reader.next()
            while part is not None and part.name != "file":
                if part.name == "path":
                    track_path = (await part.text()) or track_path
                part = await reader.next()
            if not part:
                return web.json_response({"error": "No file uploaded"}, status=400)
            track_path = track_path or part.filename or "unknown.audio"

            # Save uploaded chunk to temp file
            suffix = Path(part.filename or "audio").suffix or ".tmp"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_audio:
                tmp_audio_path = tmp_audio.name
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    tmp_audio.write(chunk)

            feats = await asyncio.to_thread(decode_and_extract, tmp_audio_path)
            timbre_bytes = feats.timbre_blob()

            # Persist to host feature cache
            conn = sqlite3.connect(self._cache_db_path)
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO feature_cache
                        (track_path, bpm, energy, brightness, rolloff, beat_strength,
                         spectral_flatness, spectral_contrast, key_index, timbre, features_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (track_path, feats.bpm, feats.energy, feats.brightness,
                     feats.rolloff, feats.beat_strength,
                     feats.spectral_flatness, feats.spectral_contrast,
                     feats.key_index, timbre_bytes, FEATURES_VERSION),
                )
                conn.commit()
            finally:
                conn.close()

            return web.json_response({
                "status": "ok",
                "path": track_path,
                "bpm": feats.bpm,
                "energy": feats.energy,
                "brightness": feats.brightness,
                "rolloff": feats.rolloff,
                "beat_strength": feats.beat_strength,
                "spectral_flatness": feats.spectral_flatness,
                "spectral_contrast": feats.spectral_contrast,
                "key_index": feats.key_index,
                "timbre_b64": base64.b64encode(timbre_bytes).decode("ascii"),
                "features_version": FEATURES_VERSION,
            })
        except Exception as ex:
            logger.exception("DSP extract failed: %s", ex)
            return web.json_response({"error": str(ex)}, status=500)
        finally:
            self._release_sleep_lock()
            if tmp_audio_path and os.path.exists(tmp_audio_path):
                try:
                    os.remove(tmp_audio_path)
                except OSError:
                    pass

    async def handle_stream_audio(self, request: web.Request) -> web.StreamResponse:
        """Stream an audio file from the Mac library, optionally transcoding on-the-fly."""
        self._touch()
        if not self._check_auth(request):
            raise web.HTTPUnauthorized()

        file_path = request.query.get("path")
        # Only files that were queued for transfer are served: a valid token
        # must not turn into read access to the whole Mac disk.
        if not file_path or not any(t.get("path") == file_path for t in self._pending_transfers):
            raise web.HTTPNotFound(text="Track is not queued for transfer")
        if not os.path.isfile(file_path):
            raise web.HTTPNotFound(text="Audio file not found")

        quality = request.query.get("quality", "original")
        profile = TranscodeProfile(quality) if quality in TranscodeProfile._value2member_map_ else TranscodeProfile.ORIGINAL

        if profile != TranscodeProfile.ORIGINAL:
            self._acquire_sleep_lock()
            try:
                send_path = await transcode_audio(file_path, profile)
            except Exception as ex:
                logger.warning("Transcode failed for %s (%s); sending original", file_path, ex)
                send_path = file_path
            finally:
                self._release_sleep_lock()
        else:
            send_path = file_path

        response = web.FileResponse(send_path)
        # RFC 5987 form: a raw non-ASCII filename is not a valid header value.
        response.headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{quote(Path(send_path).name)}")
        return response

    async def handle_bundle_upload(self, request: web.Request) -> web.Response:
        """Receive an exported state zip bundle from Android."""
        self._touch()
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        backup_dir = repo_root / "tools" / "state_backups"
        os.makedirs(backup_dir, exist_ok=True)

        reader = await request.multipart()
        part = await reader.next()
        if not part:
            return web.json_response({"error": "No bundle file provided"}, status=400)

        ts = time.strftime("%Y%m%d_%H%M%S")
        dest = backup_dir / f"mai_an_lab_state_{ts}.zip"
        with open(dest, "wb") as f:
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                f.write(chunk)

        logger.info("Saved state bundle uploaded from client to %s", dest)
        return web.json_response({"status": "ok", "saved_path": str(dest)})

    # ─── Track Transfer Queue & Precomputation ──────────────────────────────────

    async def get_or_extract_features(self, file_path: str) -> Optional[dict]:
        """Fetch DSP features from DB or cache, or compute them on Mac."""
        if not os.path.isfile(file_path):
            return None

        # 1. Check local cache DB
        try:
            conn = sqlite3.connect(self._cache_db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT bpm, energy, brightness, rolloff, beat_strength, "
                "spectral_flatness, spectral_contrast, key_index, timbre, features_version "
                "FROM feature_cache WHERE track_path = ?",
                (file_path,)
            )
            row = cur.fetchone()
            conn.close()
            if row:
                timbre_blob = row[8]
                timbre_b64 = base64.b64encode(timbre_blob).decode("ascii") if timbre_blob else ""
                return {
                    "bpm": row[0], "energy": row[1], "brightness": row[2],
                    "rolloff": row[3], "beat_strength": row[4],
                    "spectral_flatness": row[5], "spectral_contrast": row[6],
                    "key_index": row[7], "timbre_b64": timbre_b64,
                    "features_version": row[9] or FEATURES_VERSION,
                }
        except Exception as ex:
            logger.debug("Cache lookup failed: %s", ex)

        # 2. Check db_manager if available
        if self.db_manager:
            try:
                conn = await self.db_manager.get_connection()
                async with conn.execute(
                    "SELECT bpm, energy, brightness, rolloff, beat_strength, "
                    "spectral_flatness, spectral_contrast, key_index, timbre, features_version "
                    "FROM play_counts WHERE track_path = ?",
                    (file_path,)
                ) as cur:
                    row = await cur.fetchone()
                    if row and row[0] is not None:
                        timbre_blob = row[8]
                        timbre_b64 = base64.b64encode(timbre_blob).decode("ascii") if timbre_blob else ""
                        return {
                            "bpm": row[0], "energy": row[1], "brightness": row[2],
                            "rolloff": row[3], "beat_strength": row[4],
                            "spectral_flatness": row[5], "spectral_contrast": row[6],
                            "key_index": row[7], "timbre_b64": timbre_b64,
                            "features_version": row[9] or FEATURES_VERSION,
                        }
            except Exception as ex:
                logger.debug("db_manager lookup failed: %s", ex)

        # 3. Compute via ffmpeg
        try:
            self._acquire_sleep_lock()
            feats = await asyncio.to_thread(decode_and_extract, file_path)
            timbre_bytes = feats.timbre_blob()
            timbre_b64 = base64.b64encode(timbre_bytes).decode("ascii")

            # Save in feature_cache
            try:
                conn = sqlite3.connect(self._cache_db_path)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO feature_cache
                        (track_path, bpm, energy, brightness, rolloff, beat_strength,
                         spectral_flatness, spectral_contrast, key_index, timbre, features_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (file_path, feats.bpm, feats.energy, feats.brightness,
                     feats.rolloff, feats.beat_strength,
                     feats.spectral_flatness, feats.spectral_contrast,
                     feats.key_index, timbre_bytes, FEATURES_VERSION),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

            return {
                "bpm": feats.bpm, "energy": feats.energy, "brightness": feats.brightness,
                "rolloff": feats.rolloff, "beat_strength": feats.beat_strength,
                "spectral_flatness": feats.spectral_flatness, "spectral_contrast": feats.spectral_contrast,
                "key_index": feats.key_index, "timbre_b64": timbre_b64,
                "features_version": FEATURES_VERSION,
            }
        except Exception as ex:
            logger.warning("Feature extraction failed for %s: %s", file_path, ex)
            return None
        finally:
            self._release_sleep_lock()

    async def queue_tracks_for_transfer(
        self,
        tracks: list[dict],
        quality: str = "aac_vbr_max",
        precompute_dsp: bool = True
    ) -> list[dict]:
        """Prepare and enqueue tracks on Mac for Phone to receive."""
        local_ip = resolve_local_ip()
        port = self.bound_port or self.preferred_port
        queued = []

        for t in tracks:
            path = t.get("path")
            if not path or not os.path.isfile(path):
                continue

            title = t.get("title") or t.get("track_title") or Path(path).stem
            artist = t.get("artist") or t.get("artist_name") or "Unknown"
            album = t.get("album") or t.get("album_title") or "Unknown Album"
            track_id = _transfer_id(path, quality)

            dsp_features = None
            if precompute_dsp:
                dsp_features = await self.get_or_extract_features(path)

            ext = "m4a" if ("aac" in quality.lower() or path.endswith(".m4a")) else Path(path).suffix.lstrip(".") or "flac"
            # The phone rebuilds the host part from the address it actually
            # reached us on (USB tunnel vs LAN); stream_path is what it needs.
            stream_path = f"/api/v1/tracks/stream?path={quote(path, safe='')}&quality={quote(quality)}"
            stream_url = f"http://{local_ip}:{port}{stream_path}"

            item = {
                "id": track_id,
                "title": title,
                "artist": artist,
                "album": album,
                "path": path,
                "quality": quality,
                "extension": ext,
                "stream_url": stream_url,
                "stream_path": stream_path,
                "dsp_features": dsp_features,
                "timestamp": time.time(),
            }
            self._pending_transfers = [x for x in self._pending_transfers if x.get("id") != track_id]
            self._pending_transfers.append(item)
            queued.append(item)

        self._touch()
        self._save_transfers()
        self._notify()
        logger.info("Queued %d tracks for transfer (total pending: %d)", len(queued), len(self._pending_transfers))
        return queued

    async def handle_get_pending_transfers(self, request: web.Request) -> web.Response:
        self._touch()
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return web.json_response({
            "status": "ok",
            "count": len(self._pending_transfers),
            "transfers": self._pending_transfers,
        })

    async def handle_queue_transfers(self, request: web.Request) -> web.Response:
        self._touch()
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        tracks = body.get("tracks", [])
        quality = body.get("quality", "aac_vbr_max")
        precompute = body.get("precompute_dsp", True)
        queued = await self.queue_tracks_for_transfer(tracks, quality, precompute)
        return web.json_response({"status": "ok", "queued": len(queued)})

    async def handle_ack_transfers(self, request: web.Request) -> web.Response:
        self._touch()
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            body = {}

        if body.get("clear_all"):
            self._pending_transfers.clear()
        else:
            ids = set(str(i) for i in body.get("ids", []))
            self._pending_transfers = [t for t in self._pending_transfers if str(t.get("id")) not in ids]
        self._save_transfers()
        self._notify()

        return web.json_response({
            "status": "ok",
            "remaining": len(self._pending_transfers),
        })

    # ─── Lifecycle Management ──────────────────────────────────────────────────

    async def start(self) -> int:
        """Bind and start the HTTP server and UDP beacon broadcaster.
        Tries preferred_port up to MAX_PORT_ATTEMPTS.
        """
        if self._is_running:
            return self.bound_port or self.preferred_port

        self._app = web.Application()
        self._app.router.add_get("/api/v1/ping", self.handle_ping)
        self._app.router.add_post("/api/v1/pair", self.handle_pair)
        self._app.router.add_post("/api/v1/dsp/match", self.handle_dsp_match)
        self._app.router.add_post("/api/v1/dsp/extract", self.handle_dsp_extract)
        self._app.router.add_get("/api/v1/tracks/stream", self.handle_stream_audio)
        self._app.router.add_post("/api/v1/bundle/upload", self.handle_bundle_upload)
        self._app.router.add_get("/api/v1/transfers/pending", self.handle_get_pending_transfers)
        self._app.router.add_post("/api/v1/transfers/queue", self.handle_queue_transfers)
        self._app.router.add_post("/api/v1/transfers/ack", self.handle_ack_transfers)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        # Port probing
        bound = False
        target_port = self.preferred_port
        for attempt in range(MAX_PORT_ATTEMPTS):
            try:
                self._site = web.TCPSite(self._runner, "0.0.0.0", target_port)
                await self._site.start()
                self.bound_port = target_port
                bound = True
                break
            except OSError as ex:
                logger.warning("Port %d busy (%s), trying %d", target_port, ex, target_port + 1)
                target_port += 1

        if not bound:
            await self._runner.cleanup()
            raise RuntimeError(f"Could not bind CompanionServer to any port in {self.preferred_port}..{target_port}")

        self._is_running = True
        self._touch()

        # Start UDP beacon broadcaster
        self._broadcaster = BeaconBroadcaster(port=self.bound_port, device_name=self.device_name)
        self._broadcaster.start()

        # Start idle watchdog
        self._idle_task = asyncio.create_task(self._idle_watchdog())

        logger.info(
            "CompanionServer running on %s:%d (PIN: %s)",
            resolve_local_ip(), self.bound_port, self.pin
        )
        self._notify()
        return self.bound_port

    async def stop(self):
        """Cleanly shut down the server, broadcaster, and background workers."""
        if not self._is_running:
            return
        self._is_running = False

        if self._broadcaster:
            self._broadcaster.stop()
            self._broadcaster = None

        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None

        self._release_sleep_lock()
        # Force-release: a stop mid-request leaves the holder count unbalanced.
        self._sleep_holders = 0
        self._kill_caffeinate()

        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        logger.info("CompanionServer stopped cleanly")
        self._notify()

    async def _idle_watchdog(self):
        """Auto-shutdown after idle_timeout_s without requests."""
        while self._is_running:
            await asyncio.sleep(30)
            # Never idle out while a transfer is waiting: the phone may only
            # check in when the user next opens the app.
            if self._pending_transfers:
                continue
            if time.time() - self._last_active_time > self.idle_timeout_s:
                logger.info("CompanionServer idle for > %ds; auto-stopping", self.idle_timeout_s)
                asyncio.create_task(self.stop())
                break

