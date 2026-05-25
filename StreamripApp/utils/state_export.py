"""
Debug-only: bundle the app's persistent state (SQLite library DB, Streamrip
config, search history) into a single ZIP that can be exported off-device and
re-imported into another build. Lets us stress-test DSP/graph work on a fresh
APK without having to re-scan paths or recompute features.

NOT a backup format. There is no schema migration story here: an import only
makes sense when the bundle and the target build share the same DB schema and
config layout. Bundles older than the current build may fail or behave oddly.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sqlite3
import time
import zipfile
from typing import Optional

logger = logging.getLogger("state_export")

# Bump this when the bundle layout or any of the included file formats change
# in an incompatible way. Import refuses bundles with a different major.
BUNDLE_VERSION = 2  # bumped from 1: bundle now carries custom_moods.json.

# Where bundles get written / read from. /sdcard/Download is the obvious spot
# on Android because the app already has MANAGE_EXTERNAL_STORAGE and the user
# can `adb pull` it without root. Desktop falls back to ~/Downloads.
def _default_bundle_dir() -> str:
    for p in ("/sdcard/Download", "/storage/emulated/0/Download"):
        if os.path.isdir(p):
            return p
    return os.path.join(os.path.expanduser("~"), "Downloads")


def list_bundles() -> list[str]:
    """Returns absolute paths to candidate bundle files in the default dir,
    newest first. Used to populate the import dropdown."""
    d = _default_bundle_dir()
    if not os.path.isdir(d):
        return []
    items = []
    for name in os.listdir(d):
        if name.startswith("mai_an_lab_state_") and name.endswith(".zip"):
            full = os.path.join(d, name)
            try:
                items.append((os.path.getmtime(full), full))
            except OSError:
                continue
    items.sort(reverse=True)
    return [p for _, p in items]


def _snapshot_sqlite(src_path: str, dst_path: str) -> None:
    """Use SQLite's online backup API so we get a consistent snapshot even if
    the live DB is in WAL mode or has open transactions. Falls back to a raw
    copy if the source isn't an openable SQLite DB (e.g. zero-byte file)."""
    if not os.path.exists(src_path) or os.path.getsize(src_path) == 0:
        return
    try:
        # Open read-only via URI so we don't accidentally create a WAL file
        # on the source side. The destination is a fresh file.
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(dst_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as ex:
        logger.warning("sqlite backup failed (%s); falling back to copy", ex)
        shutil.copy2(src_path, dst_path)


def export_state(
    db_path: str,
    config_path: str,
    search_history_path: Optional[str] = None,
    out_dir: Optional[str] = None,
    custom_moods_path: Optional[str] = None,
) -> str:
    """Write a bundle ZIP and return its absolute path. The DB file is
    snapshotted via SQLite's online backup API so callers don't have to close
    their `aiosqlite` connection.

    `custom_moods_path`, when supplied and present on disk, is included in
    the bundle so user-created islets and their thresholds survive
    export/import."""
    out_dir = out_dir or _default_bundle_dir()
    os.makedirs(out_dir, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(out_dir, f"mai_an_lab_state_{ts}.zip")

    # Snapshot DB to a tempfile next to the destination so we can stream it
    # into the zip without holding a sqlite write lock during compression.
    tmp_db = out_path + ".db.tmp"
    _snapshot_sqlite(db_path, tmp_db)

    contents: dict[str, dict] = {}
    try:
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(tmp_db):
                zf.write(tmp_db, "library.db")
                contents["library.db"] = {"bytes": os.path.getsize(tmp_db)}
            if config_path and os.path.exists(config_path):
                zf.write(config_path, "config.toml")
                contents["config.toml"] = {"bytes": os.path.getsize(config_path)}
            if search_history_path and os.path.exists(search_history_path):
                zf.write(search_history_path, "recent_searches.json")
                contents["recent_searches.json"] = {
                    "bytes": os.path.getsize(search_history_path)
                }
            if custom_moods_path and os.path.exists(custom_moods_path):
                zf.write(custom_moods_path, "custom_moods.json")
                contents["custom_moods.json"] = {
                    "bytes": os.path.getsize(custom_moods_path)
                }

            manifest = {
                "bundle_version": BUNDLE_VERSION,
                "exported_at": ts,
                "contents": contents,
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    finally:
        try:
            os.remove(tmp_db)
        except OSError:
            pass

    # Auto-prune older state bundles to prevent disk bloat (keep newest 5)
    try:
        state_files = []
        for name in os.listdir(out_dir):
            if name.startswith("mai_an_lab_state_") and name.endswith(".zip"):
                full = os.path.join(out_dir, name)
                if os.path.isfile(full):
                    state_files.append((os.path.getmtime(full), full))
        state_files.sort(reverse=True)
        if len(state_files) > 5:
            for _, path_to_delete in state_files[5:]:
                try:
                    os.remove(path_to_delete)
                    logger.info("Auto-pruned older state bundle: %s", path_to_delete)
                except OSError as ex:
                    logger.warning("Failed to auto-prune %s: %s", path_to_delete, ex)
    except Exception as ex:
        logger.warning("Error running state bundle auto-pruning: %s", ex)

    return out_path


def inspect_bundle(zip_path: str) -> dict:
    """Read manifest.json out of a bundle without extracting anything else.
    Used by the import dialog to preview what'll be replaced."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        try:
            raw = zf.read("manifest.json")
        except KeyError:
            return {"error": "missing manifest.json"}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as ex:
            return {"error": f"manifest parse failed: {ex}"}


def import_state(
    zip_path: str,
    db_path: str,
    config_path: str,
    search_history_path: Optional[str] = None,
    custom_moods_path: Optional[str] = None,
) -> dict:
    """Replace the live state files with what's inside the bundle. Caller is
    responsible for closing any open DB connection BEFORE calling this and
    restarting the app afterwards — there is no in-place reload of the DB
    handle or in-memory config caches.

    `custom_moods_path` is optional for back-compat with v1 bundles that
    didn't carry the file. If supplied and the bundle contains the member,
    the live JSON gets replaced.

    Returns a dict describing what was replaced.
    """
    manifest = inspect_bundle(zip_path)
    if "error" in manifest:
        raise ValueError(manifest["error"])
    if manifest.get("bundle_version") != BUNDLE_VERSION:
        raise ValueError(
            f"bundle_version {manifest.get('bundle_version')} "
            f"!= expected {BUNDLE_VERSION}"
        )

    replaced: dict[str, str] = {}
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())

        def _replace(member: str, target: Optional[str]) -> None:
            if not target or member not in names:
                return
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".import.tmp"
            with zf.open(member) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(tmp, target)
            replaced[member] = target

        _replace("library.db", db_path)
        _replace("config.toml", config_path)
        _replace("recent_searches.json", search_history_path)
        _replace("custom_moods.json", custom_moods_path)

    return {"replaced": replaced, "manifest": manifest}
