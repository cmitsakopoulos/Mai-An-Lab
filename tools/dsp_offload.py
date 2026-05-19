#!/usr/bin/env python3
"""
DSP analysis offloader for Mai An Lab.

Runs the same `extract_features_from_pcm` pipeline the on-device analyser
uses, but on the user's laptop — which finishes a track in tens of
milliseconds where the phone takes ten seconds. Round-trip:

    1. In the app: Settings → Advanced → Export State (pick a folder).
    2. `adb pull /sdcard/Download/mai_an_lab_state_<ts>.zip .`
    3. `python3 tools/dsp_offload.py mai_an_lab_state_<ts>.zip`
       (the script: opens the bundle, pulls each unanalysed track's audio
       file via ADB, decodes it with ffmpeg, runs the feature pipeline,
       upserts results into the bundle's library.db, repackages.)
    4. `adb push mai_an_lab_state_<ts>.analysed.zip /sdcard/Download/`
    5. In the app: Settings → Advanced → Import State → pick the bundle.

The script is self-contained beyond two host dependencies:
  • `ffmpeg` on PATH (audio decode)
  • `adb` on PATH and a device connected (audio file transfer)

It reuses the existing Python feature pipeline from `StreamripApp/utils/dsp.py`
so any future change there is picked up without code duplication.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import logging
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

# Wire up sys.path so we can import the on-device feature extractor.
_HERE = pathlib.Path(__file__).resolve().parent
_APP_DIR = _HERE.parent / "StreamripApp"
if not _APP_DIR.is_dir():
    sys.exit(f"can't find StreamripApp dir relative to {_HERE}")
sys.path.insert(0, str(_APP_DIR))

# These imports come from the live app code — no duplicated DSP math.
from utils.dsp import (  # noqa: E402
    FEATURES_VERSION,
    TARGET_SAMPLE_RATE,
    extract_features_from_pcm,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dsp_offload")


# ── Host tooling ─────────────────────────────────────────────────────────────


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"{name!r} not found on PATH — install it before running this script")
    return path


def _adb_args(adb: str, serial: str | None, *args: str) -> list[str]:
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    return cmd


def _adb_pull_chunk(
    adb: str,
    serial: str | None,
    remote_paths: list[str],
    audio_cache: str,
    chunk_idx: int,
) -> dict[str, str]:
    """Stream a batch of files from the device via `adb exec-out tar` and
    extract them under `audio_cache`. Returns a {remote_path: local_path}
    map for the files that arrived (silently drops missing remotes — tar
    warns to stderr but keeps streaming).

    Why tar-stream instead of N parallel `adb pull` calls: each pull pays
    ~50-200ms of ADB protocol handshake. Streaming through one `exec-out`
    invocation pays that cost once per chunk. For 100 small files the
    saving is ~10-20s before any bytes have moved.
    """
    if not remote_paths:
        return {}

    # Materialise the path list on the device; tar reads it via `-T`. We do
    # this rather than passing paths on the command line because device-side
    # ARG_MAX is small and tracks have long paths.
    list_remote = f"/sdcard/.dsp_offload_pull_list_{chunk_idx}.txt"
    list_local = os.path.join(audio_cache, f".pull_list_{chunk_idx}.txt")
    try:
        with open(list_local, "w", encoding="utf-8") as fh:
            for p in remote_paths:
                fh.write(p + "\n")
        push = subprocess.run(
            _adb_args(adb, serial, "push", list_local, list_remote),
            capture_output=True, text=True,
        )
        if push.returncode != 0:
            log.warning("adb push of pull-list failed: %s", push.stderr.strip())
            return {}

        # `adb exec-out` avoids the shell's stdout line-ending munging that
        # `adb shell` does; safe to pipe binary data through it.
        tar_cmd = _adb_args(
            adb, serial, "exec-out",
            "sh", "-c", f"tar -cf - -T {list_remote} 2>/dev/null",
        )
        tar_extract = subprocess.run(
            ["tar", "-xf", "-", "-C", audio_cache],
            input=subprocess.run(tar_cmd, capture_output=True, check=False).stdout,
            capture_output=True,
        )
        if tar_extract.returncode != 0:
            log.warning("local tar extract failed: %s", tar_extract.stderr.decode(errors="replace").strip())
    finally:
        # Clean up local pull list file immediately
        try:
            os.remove(list_local)
        except OSError:
            pass

    # Best-effort cleanup of the device-side list file.
    subprocess.run(
        _adb_args(adb, serial, "shell", "rm", "-f", list_remote),
        capture_output=True,
    )

    # Map remote paths to where tar landed them. tar preserves the full
    # path structure, so /storage/emulated/0/Music/foo.mp3 lands under
    # audio_cache/storage/emulated/0/Music/foo.mp3.
    out: dict[str, str] = {}
    for remote in remote_paths:
        local = os.path.join(audio_cache, remote.lstrip("/"))
        if os.path.exists(local) and os.path.getsize(local) > 0:
            out[remote] = local
    missing = len(remote_paths) - len(out)
    if missing:
        log.warning("%d/%d files in chunk did not arrive (likely deleted on device)",
                    missing, len(remote_paths))
    return out


def _ffmpeg_decode(ffmpeg: str, src_path: str, out_pcm: str) -> bool:
    """Decode MAX_SECONDS from the middle to mono 16-bit LE PCM at
    TARGET_SAMPLE_RATE. Mirrors `_decode_pcm_ffmpeg` in
    StreamripApp/utils/dsp.py, but sync. MAX_SECONDS is imported from the
    app's dsp module so this script tracks the canonical window length
    without manual coordination."""
    from utils.dsp import MAX_SECONDS  # imported lazily to keep top tidy
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-ss", "15", "-t", str(MAX_SECONDS),
        "-i", src_path,
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ac", "1",
        "-ar", str(TARGET_SAMPLE_RATE),
        "-y", out_pcm,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0 and os.path.exists(out_pcm) and os.path.getsize(out_pcm) > 0:
        return True
    # Retry without the seek for very short tracks.
    cmd_retry = [c for c in cmd if c not in ("-ss", "15")]
    proc = subprocess.run(cmd_retry, capture_output=True, text=True)
    if proc.returncode == 0 and os.path.exists(out_pcm) and os.path.getsize(out_pcm) > 0:
        return True
    log.warning("ffmpeg decode failed for %s: %s", src_path, proc.stderr.strip())
    return False


# ── Bundle / DB helpers ──────────────────────────────────────────────────────


def _bundle_extract(bundle_path: str, work_dir: str) -> str:
    """Unzip bundle into work_dir. Returns the extracted library.db path."""
    with zipfile.ZipFile(bundle_path, "r") as zf:
        zf.extractall(work_dir)
    db = os.path.join(work_dir, "library.db")
    if not os.path.exists(db):
        sys.exit(f"bundle {bundle_path} has no library.db")
    return db


def _bundle_rebuild(original_bundle: str, work_dir: str, out_bundle: str) -> None:
    """Repackage the work_dir into a new ZIP, preserving any files from the
    original bundle that the script didn't touch (config.toml, etc.)."""
    with zipfile.ZipFile(original_bundle, "r") as src, \
         zipfile.ZipFile(out_bundle, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        names = set(src.namelist())
        # First: anything we have a freshly-edited version of in work_dir.
        wrote = set()
        for arc_name in names:
            local = os.path.join(work_dir, arc_name)
            if os.path.isfile(local):
                dst.write(local, arc_name)
                wrote.add(arc_name)
        # Then: anything from the original bundle we didn't touch (e.g.
        # nested manifests added by future bundle versions).
        for arc_name in names:
            if arc_name not in wrote:
                dst.writestr(arc_name, src.read(arc_name))


def _missing_paths(db_path: str, features_version: int) -> list[str]:
    """Tracks present in the library but with absent / stale features."""
    conn = sqlite3.connect(db_path)
    try:
        sql = """
            SELECT t.path
            FROM tracks t
            LEFT JOIN play_counts pc ON pc.track_path = t.path
            WHERE pc.timbre IS NULL
               OR COALESCE(pc.features_version, 0) < ?
        """
        return [r[0] for r in conn.execute(sql, (features_version,)).fetchall()]
    finally:
        conn.close()


# ── Per-chunk processing ─────────────────────────────────────────────────────


def _decode_and_extract(
    ffmpeg: str,
    remote_path: str,
    local_audio: str,
):
    """Decode one already-pulled file and extract features. Returns
    (remote_path, Features|None, message). Runs entirely on host CPU; safe
    to parallelise across a ThreadPoolExecutor.
    """
    local_pcm = local_audio + ".pcm"
    try:
        if not (os.path.exists(local_pcm) and os.path.getsize(local_pcm) > 0):
            if not _ffmpeg_decode(ffmpeg, local_audio, local_pcm):
                return remote_path, None, "ffmpeg decode failed"
        feats = extract_features_from_pcm(local_pcm, TARGET_SAMPLE_RATE)
        return remote_path, feats, (
            f"bpm={feats.bpm:.1f} energy={feats.energy:.2f} "
            f"brightness={feats.brightness:.2f}"
        )
    except Exception as ex:
        return remote_path, None, f"extract failed: {ex}"
    finally:
        # Always clean up the massive intermediate raw PCM file to save disk space
        if os.path.exists(local_pcm):
            try:
                os.remove(local_pcm)
            except Exception:
                pass
        # Always clean up the pulled compressed audio file immediately after feature extraction
        if os.path.exists(local_audio):
            try:
                os.remove(local_audio)
            except Exception:
                pass

class CachedFeatures:
    """Helper class to reconstruct features fetched from the local laptop cache."""
    def __init__(self, bpm: float, energy: float, brightness: float, rolloff: float,
                 beat_strength: float, spectral_flatness: float, spectral_contrast: float,
                 key_index: int, timbre: bytes):
        self.bpm = bpm
        self.energy = energy
        self.brightness = brightness
        self.rolloff = rolloff
        self.beat_strength = beat_strength
        self.spectral_flatness = spectral_flatness
        self.spectral_contrast = spectral_contrast
        self.key_index = key_index
        self.timbre = timbre

    def timbre_blob(self) -> bytes:
        return self.timbre


def _init_feature_cache(db_path: str) -> None:
    """Creates a local SQLite database to permanently cache calculated features on the laptop."""
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


def _upsert_chunk(db_path: str, results: list[tuple[str, object]], cache_db_path: str | None = None) -> None:
    """Single-transaction batch UPSERT for a chunk's v3 features. Mirrors
    `db_manager.update_track_features` but commits all rows under one
    BEGIN/COMMIT — orders of magnitude faster than N tiny transactions,
    and keeps the bundle DB at consistent boundaries between chunks so a
    killed run leaves the previously-finished chunks intact.

    Defensively ALTER TABLE for the v3 columns if they're absent. The app's
    on-device init handles this too, but bundles exported from a pre-v3
    build won't have them — and we want this script to work standalone
    against any bundle without requiring the user to bump the app first.
    """
    if not results:
        return
    
    # 1. Update the bundle database
    conn = sqlite3.connect(db_path)
    try:
        for col, ddl in (
            ("spectral_flatness", "REAL DEFAULT 0"),
            ("spectral_contrast", "REAL DEFAULT 0"),
            ("key_index",         "INTEGER DEFAULT 0"),
        ):
            try:
                conn.execute(f"ALTER TABLE play_counts ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.execute("BEGIN")
        for path, feats in results:
            t_blob = feats.timbre if isinstance(feats, CachedFeatures) else feats.timbre_blob()
            conn.execute(
                """
                INSERT INTO play_counts
                    (track_path, count, last_played, bpm, energy, brightness,
                     rolloff, beat_strength, spectral_flatness,
                     spectral_contrast, key_index, timbre, features_version)
                VALUES (?, 0, strftime('%s','now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(track_path) DO UPDATE SET
                    bpm = excluded.bpm,
                    energy = excluded.energy,
                    brightness = excluded.brightness,
                    rolloff = excluded.rolloff,
                    beat_strength = excluded.beat_strength,
                    spectral_flatness = excluded.spectral_flatness,
                    spectral_contrast = excluded.spectral_contrast,
                    key_index = excluded.key_index,
                    timbre = excluded.timbre,
                    features_version = excluded.features_version
                """,
                (path, feats.bpm, feats.energy, feats.brightness,
                 feats.rolloff, feats.beat_strength,
                 feats.spectral_flatness, feats.spectral_contrast,
                 feats.key_index,
                 t_blob, FEATURES_VERSION),
            )
        conn.commit()
    finally:
        conn.close()

    # 2. Optionally cache these computations permanently on the laptop's feature database
    if cache_db_path:
        cache_conn = sqlite3.connect(cache_db_path)
        try:
            cache_conn.execute("BEGIN")
            for path, feats in results:
                t_blob = feats.timbre if isinstance(feats, CachedFeatures) else feats.timbre_blob()
                cache_conn.execute(
                    """
                    INSERT OR REPLACE INTO feature_cache
                        (track_path, bpm, energy, brightness, rolloff, beat_strength,
                         spectral_flatness, spectral_contrast, key_index, timbre, features_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (path, feats.bpm, feats.energy, feats.brightness,
                     feats.rolloff, feats.beat_strength,
                     feats.spectral_flatness, feats.spectral_contrast,
                     feats.key_index, t_blob, FEATURES_VERSION)
                )
            cache_conn.commit()
        except Exception as ex:
            log.warning("Failed to save results to laptop computation cache: %s", ex)
        finally:
            cache_conn.close()


def _upsert_single_to_laptop_cache(cache_db_path: str, path: str, feats: object) -> None:
    """Commit a single computed feature set to the laptop's permanent cache database
    immediately to ensure progress isn't lost if the script finishes abruptly."""
    conn = sqlite3.connect(cache_db_path)
    try:
        t_blob = feats.timbre if isinstance(feats, CachedFeatures) else feats.timbre_blob()
        conn.execute(
            """
            INSERT OR REPLACE INTO feature_cache
                (track_path, bpm, energy, brightness, rolloff, beat_strength,
                 spectral_flatness, spectral_contrast, key_index, timbre, features_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (path, feats.bpm, feats.energy, feats.brightness,
             feats.rolloff, feats.beat_strength,
             feats.spectral_flatness, feats.spectral_contrast,
             feats.key_index, t_blob, FEATURES_VERSION)
        )
        conn.commit()
    except Exception as ex:
        log.warning("Failed to save single result to laptop computation cache: %s", ex)
    finally:
        conn.close()


# Mutex lock to serialize SQLite writes and stats updates across parallel workers
_state_lock = threading.Lock()


# ── Driver ───────────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bundle", help="path to mai_an_lab_state_*.zip exported from the app")
    p.add_argument("--out", help="output bundle path (default: <bundle>.analysed.zip)")
    p.add_argument("--in-place", action="store_true",
                   help="overwrite the input bundle instead of writing a new one")
    p.add_argument("--serial", help="adb device serial (multi-device setups)")
    p.add_argument("--concurrency", type=int, default=4,
                   help="parallel decode+extract workers per chunk (default 4; CPU-bound)")
    p.add_argument("--chunk-size", type=int, default=100,
                   help="tracks per `adb tar` pull batch (default 100). Higher "
                        "amortises adb handshake overhead better but uses more "
                        "disk in the work cache between batches")
    p.add_argument("--workdir",
                   help="reusable work directory for cached audio + PCMs "
                        "(default: a tempdir, deleted on exit)")
    p.add_argument("--keep-workdir", action="store_true",
                   help="keep the work directory after running (implies a non-temp workdir)")
    args = p.parse_args()

    bundle = os.path.abspath(args.bundle)
    if not os.path.isfile(bundle):
        sys.exit(f"bundle not found: {bundle}")
    if args.in_place and args.out:
        sys.exit("--in-place and --out are mutually exclusive")
    out_bundle = (
        bundle if args.in_place
        else (args.out or bundle.replace(".zip", ".analysed.zip"))
    )
    if out_bundle == bundle and not args.in_place:
        out_bundle += ".analysed.zip"

    adb = _require_tool("adb")
    ffmpeg = _require_tool("ffmpeg")

    # Work dir: temp by default; persistent if --workdir or --keep-workdir.
    tmp_ctx = tempfile.TemporaryDirectory() if not args.workdir else None
    work_root = args.workdir or tmp_ctx.name
    os.makedirs(work_root, exist_ok=True)
    extracted_dir = os.path.join(work_root, "bundle")
    audio_cache = os.path.join(work_root, "audio_cache")
    os.makedirs(extracted_dir, exist_ok=True)
    os.makedirs(audio_cache, exist_ok=True)

    log.info("extracting bundle → %s", extracted_dir)
    db_path = _bundle_extract(bundle, extracted_dir)

    missing = _missing_paths(db_path, FEATURES_VERSION)
    if not missing:
        log.info("bundle has no tracks missing features (features_version=%d). nothing to do.",
                 FEATURES_VERSION)
        return 0
    log.info("found %d tracks needing DSP analysis", len(missing))

    # Initialize Laptop's feature cache (always active, defaults to tools/offload_cache)
    if args.workdir:
        cache_db_path = os.path.join(args.workdir, "feature_cache.db")
    else:
        cache_dir = os.path.join(str(_HERE), "offload_cache")
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "mai_an_lab")
            os.makedirs(cache_dir, exist_ok=True)
        cache_db_path = os.path.join(cache_dir, "feature_cache.db")
    
    _init_feature_cache(cache_db_path)
    
    # Check which missing tracks already exist in the laptop's feature cache
    conn = sqlite3.connect(cache_db_path)
    cached_results = []
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT track_path, bpm, energy, brightness, rolloff, beat_strength, spectral_flatness, spectral_contrast, key_index, timbre FROM feature_cache WHERE features_version = ?", (FEATURES_VERSION,))
        cached_map = {row[0]: row for row in cursor.fetchall()}
        
        for path in missing:
            if path in cached_map:
                row = cached_map[path]
                feats = CachedFeatures(
                    bpm=row[1], energy=row[2], brightness=row[3], rolloff=row[4],
                    beat_strength=row[5], spectral_flatness=row[6], spectral_contrast=row[7],
                    key_index=row[8], timbre=row[9]
                )
                cached_results.append((path, feats))
    except Exception as ex:
        log.warning("Failed to read laptop computation cache: %s", ex)
    finally:
        conn.close()

    if cached_results:
        log.info("found %d tracks already processed and cached in laptop's computation cache! Ingesting immediately...", len(cached_results))
        _upsert_chunk(db_path, cached_results)
        # Remove cached tracks from the missing list
        cached_paths = {p for p, _ in cached_results}
        missing = [p for p in missing if p not in cached_paths]
        
        if not missing:
            log.info("all outstanding tracks successfully populated from laptop's computation cache!")
            log.info("repackaging → %s", out_bundle)
            _bundle_rebuild(bundle, extracted_dir, out_bundle)
            log.info("done. enjoy the vibe!")
            return 0

    started = time.perf_counter()
    stats = {"succeeded": 0, "failed": 0, "processed": 0}
    chunk_size = max(1, args.chunk_size)
    chunks = [missing[i:i + chunk_size] for i in range(0, len(missing), chunk_size)]
    log.info("processing %d chunks of up to %d tracks each sequentially (using process pool with %d workers)", len(chunks), chunk_size, args.concurrency)

    def process_chunk(chunk_idx, chunk, process_pool):
        chunk_started = time.perf_counter()

        # Skip files that are already cached from an earlier run; only pull
        # the missing ones for this chunk. tar-stream the survivors.
        to_pull: list[str] = []
        already_local: dict[str, str] = {}
        for remote in chunk:
            local_expected = os.path.join(audio_cache, remote.lstrip("/"))
            if os.path.exists(local_expected) and os.path.getsize(local_expected) > 0:
                already_local[remote] = local_expected
            else:
                to_pull.append(remote)

        pulled: dict[str, str] = dict(already_local)
        if to_pull:
            log.info("[chunk %d/%d] pulling %d files via adb tar (re-using %d cached)",
                     chunk_idx, len(chunks), len(to_pull), len(already_local))
            pulled.update(_adb_pull_chunk(adb, args.serial, to_pull, audio_cache, chunk_idx))
        else:
            log.info("[chunk %d/%d] all %d files already cached locally",
                     chunk_idx, len(chunks), len(chunk))

        # Decode + feature-extract this chunk's files in parallel.
        ok_results: list[tuple[str, object]] = []
        local_succeeded = local_failed = local_processed = 0

        # Decode and process using a fast ProcessPoolExecutor
        futures = {
            process_pool.submit(_decode_and_extract, ffmpeg, remote, local): remote
            for remote, local in pulled.items()
        }
        for fut in cf.as_completed(futures):
            remote, feats, msg = fut.result()
            local_processed += 1
            if feats is None:
                local_failed += 1
                log.warning("  FAIL %s (%s)", os.path.basename(remote), msg)
            else:
                local_succeeded += 1
                ok_results.append((remote, feats))
                log.info("  OK   %s (%s)", os.path.basename(remote), msg)
                if cache_db_path:
                    _upsert_single_to_laptop_cache(cache_db_path, remote, feats)

        # Handle adb pull failures
        for remote in chunk:
            if remote not in pulled:
                local_failed += 1
                local_processed += 1
                log.warning("  FAIL %s (adb pull missed)", os.path.basename(remote))

        if ok_results:
            try:
                _upsert_chunk(db_path, ok_results, cache_db_path)
            except Exception as ex:
                log.error("[chunk %d/%d] db upsert failed for %d tracks: %s",
                          chunk_idx, len(chunks), len(ok_results), ex)
        
        stats["succeeded"] += local_succeeded
        stats["failed"] += local_failed
        stats["processed"] += local_processed

        log.info("[chunk %d/%d] done in %.1fs — %d processed in this chunk",
                 chunk_idx, len(chunks), time.perf_counter() - chunk_started,
                 local_processed)

    try:
        # Execute chunks sequentially, using a single shared ProcessPoolExecutor for parallel feature-extraction
        with cf.ProcessPoolExecutor(max_workers=args.concurrency) as process_pool:
            for idx, chunk in enumerate(chunks, 1):
                process_chunk(idx, chunk, process_pool)
    finally:
        # Clean up any device-side pull list files
        try:
            subprocess.run(
                _adb_args(adb, args.serial, "shell", "rm", "-f", "/sdcard/.dsp_offload_pull_list_*.txt"),
                capture_output=True,
            )
        except Exception:
            pass

    succeeded = stats["succeeded"]
    failed = stats["failed"]
    processed = stats["processed"]

    elapsed = time.perf_counter() - started
    log.info("analysis done: %d OK, %d failed in %.1fs (avg %.2fs/track)",
             succeeded, failed, elapsed,
             elapsed / max(1, succeeded + failed))

    log.info("repackaging → %s", out_bundle)
    _bundle_rebuild(bundle, extracted_dir, out_bundle)
    log.info("done. Next steps:")
    log.info("  adb push %s /sdcard/Download/", out_bundle)
    log.info("  → in app: Settings → Advanced → Import State → pick the bundle")

    if args.keep_workdir or args.workdir:
        log.info("work dir preserved at %s (audio cache is reusable on re-run)", work_root)
    if tmp_ctx is not None:
        tmp_ctx.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
