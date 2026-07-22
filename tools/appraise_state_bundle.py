#!/usr/bin/env python3
"""Appraise a Mai An Lab state bundle (or a raw library.db).

Reports how much *computed* state a snapshot carries — total tracks, how many
have current DSP features, and how many have persisted graph coordinates — so
the Android build scripts can:

  1. decide, BEFORE an `adb uninstall`, whether a snapshot is actually worth
     preserving (refuse to wipe a phone whose only backup is empty/missing), and
  2. CONFIRM, after the reinstall + auto-import, that the features/graph really
     landed on the freshly-built app.

Reads ONLY the SQLite inside the bundle. No app imports, no device access, no
network — just `zipfile` + `sqlite3` from the stdlib, so it runs anywhere the
build scripts do.

Schema (see StreamripApp/utils/db_manager.py):
  • tracks                                     — one row per library track
  • play_counts.timbre / .features_version     — DSP feature BLOB + its version
  • play_counts.pca_coords                     — persisted Zr graph coordinate

Usage:
    appraise_state_bundle.py <bundle.zip | library.db> [--features-version N] [--json]

Exit codes (so a shell `if` can branch on them):
    0  readable AND non-empty (has tracks)      → safe to preserve/proceed
    1  readable but EMPTY (0 tracks)            → nothing worth preserving
    2  unreadable / missing / not a bundle      → hard error
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import zipfile

# Current DSP feature schema version. Kept in sync with utils/dsp.FEATURES_VERSION;
# imported from the app when reachable so this file never silently drifts, with a
# literal fallback for when the tool is run outside the source tree.
_DEFAULT_FEATURES_VERSION = 5
try:
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(_here, os.pardir, "StreamripApp"))
    from utils.dsp import FEATURES_VERSION as _DEFAULT_FEATURES_VERSION  # type: ignore
except Exception:
    pass
finally:
    # Don't leave the app dir on the path — this tool imports nothing else.
    if sys.path and sys.path[0].endswith("StreamripApp"):
        sys.path.pop(0)


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """Run a COUNT(*) query, returning 0 if the table/column doesn't exist
    (older or partial bundles) rather than crashing the appraisal."""
    try:
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        return 0


def appraise_db(db_path: str, features_version: int) -> dict:
    # Open read-only so appraising a bundle can never mutate it.
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        total = _scalar(conn, "SELECT COUNT(*) FROM tracks")
        analysed = _scalar(
            conn,
            "SELECT COUNT(*) FROM play_counts "
            "WHERE timbre IS NOT NULL AND COALESCE(features_version, 0) >= ?",
            (features_version,),
        )
        coords = _scalar(
            conn,
            "SELECT COUNT(*) FROM play_counts WHERE pca_coords IS NOT NULL",
        )
    finally:
        conn.close()
    return {
        "total_tracks": total,
        "analysed_tracks": analysed,
        "coord_tracks": coords,
        "features_version": features_version,
    }


def appraise(path: str, features_version: int) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path, "r") as zf:
            if "library.db" not in zf.namelist():
                raise ValueError("bundle has no library.db")
            with tempfile.TemporaryDirectory() as td:
                zf.extract("library.db", td)
                return appraise_db(os.path.join(td, "library.db"), features_version)

    # Fall back to treating it as a raw SQLite file.
    return appraise_db(path, features_version)


def _fmt(stats: dict, source: str) -> str:
    total = stats["total_tracks"]
    analysed = stats["analysed_tracks"]
    coords = stats["coord_tracks"]
    pct = (100.0 * analysed / total) if total else 0.0
    graph = "built" if coords > 0 else "NOT built"
    return (
        f"Snapshot appraisal — {os.path.basename(source)}\n"
        f"  tracks              : {total}\n"
        f"  DSP features (v{stats['features_version']}) : {analysed}  ({pct:.0f}% of library)\n"
        f"  graph coordinates   : {coords}  → similarity graph {graph}"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Appraise a Mai An Lab state bundle.")
    ap.add_argument("path", help="Path to a bundle .zip or a raw library.db")
    ap.add_argument("--features-version", type=int, default=_DEFAULT_FEATURES_VERSION,
                    help=f"DSP schema version to count as current (default {_DEFAULT_FEATURES_VERSION})")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = ap.parse_args(argv)

    try:
        stats = appraise(args.path, args.features_version)
    except Exception as exc:
        msg = {"error": str(exc), "path": args.path}
        print(json.dumps(msg) if args.json else f"Appraisal FAILED: {exc}",
              file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(stats))
    else:
        print(_fmt(stats, args.path))

    # Empty is a distinct, non-error outcome the caller must be able to gate on.
    return 0 if stats["total_tracks"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
