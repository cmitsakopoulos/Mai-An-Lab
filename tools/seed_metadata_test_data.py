#!/usr/bin/env python3
"""Seed / wipe fake artist-metadata rows for exercising the Metadata Workbench UI.

The workbench renders real `artists` + `artist_enrichment` rows, so to iterate on
its layout, filters, and editor without waiting on genuine MusicBrainz gaps we
inject recognisable fake artists across every gap state, then wipe them by their
name prefix.

Only rows whose artist name starts with TEST_PREFIX are ever touched, so your
real library is never modified. The fake artists carry NO tracks, so the
track-level coverage bar is unaffected; the artist gap list and the
"N artists need attention" counts do reflect them (that is the point).

Runs against the LIVE desktop DB by default (~/library.db via get_app_dir).
Commit is visible to the running app immediately — just reopen the Metadata
page (a fresh pane loads each time). No app restart needed.

Usage (conda base env):
    python tools/seed_metadata_test_data.py --seed     # inject the fixtures
    python tools/seed_metadata_test_data.py --wipe     # remove every fake row
    python tools/seed_metadata_test_data.py --list      # show current fake rows
    python tools/seed_metadata_test_data.py --db PATH --seed   # override DB
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "StreamripApp"))

TEST_PREFIX = "[TEST] "

# One fixture per (artist name suffix, track_count, enrichment-or-None). The
# enrichment dict mirrors the artist_enrichment columns; None means NO row at
# all (a critical gap — the LEFT JOIN comes back empty). `genres` is a list of
# plain strings here; seed() encodes it to the stored JSON shape.
#
# Coverage spans every branch the workbench must render:
#   • critical (sev 0)   — no genres AND no country
#   • no-tags (sev 1)     — country known, no genres  → scene grouping by country
#   • partial (sev 2)     — genres known, no country
#   • lowconfidence       — the "Uncertain" filter (accept / reject / rebind)
#   • manual              — an incomplete row that must be EXCLUDED from gaps
_FIXTURES: list[tuple[str, int, dict | None]] = [
    # ── critical: no enrichment row at all ────────────────────────────────────
    ("Ghost Signal", 42, None),
    ("Nul Provenance", 7, None),
    # ── no-tags (country only) — three countries to exercise scene headers ─────
    ("Blank Athenian", 30, dict(country="GR", genres=[], status="ok", score=100)),
    ("Untagged Yankee", 12, dict(country="US", genres=[], status="ok", score=98)),
    ("Tagless Brit", 9, dict(country="GB", genres=[], status="ok", score=95)),
    # ── partial (genres, no country) ───────────────────────────────────────────
    ("Stateless Rocker", 19, dict(country=None, genres=["rock", "alt rock"], status="ok", score=100)),
    ("Floating Producer", 4, dict(country=None, genres=["house", "techno"], status="ok", score=100)),
    # ── low-confidence (Uncertain filter) ──────────────────────────────────────
    # Has genres + country → Accept works fully offline; only in Uncertain.
    ("Maybe This One", 8, dict(country="GB", genres=["trip hop", "electronic"],
                               status="lowconfidence", score=62,
                               mbid="00000000-0000-0000-0000-000000000001")),
    # No genres, no mbid → Accept confirms blank → amber "add by hand". Also a
    # critical gap (shows in both Uncertain and the Critical list — a real case).
    ("Shaky Fallback", 5, dict(country=None, genres=[], status="lowconfidence", score=40)),
    # No genres but a REAL mbid (Radiohead) → Accept does the detail-fetch and
    # resolves green when online, amber when offline. Verifies the fetch path.
    ("Real Mbid Lowconf", 6, dict(country=None, genres=[],
                                  status="lowconfidence", score=55,
                                  mbid="a74b1b7f-71a5-4011-9441-d0b5e4122711")),
    # ── manual override, deliberately incomplete → must NOT appear in gaps ─────
    ("Hand Curated", 3, dict(country=None, genres=["laiko"], source="manual", status="ok", score=100)),
]


def _default_db_path() -> str:
    try:
        from utils.filepath_utils import get_app_dir
        return os.path.join(get_app_dir(), "library.db")
    except Exception:
        return os.path.join(os.path.expanduser("~"), "library.db")


def _genres_json(genres) -> str | None:
    """Match the stored shape: JSON [{"name","count"}]. An empty list stays '[]'
    (a present-but-empty tag list = a gap), None stays NULL."""
    if genres is None:
        return None
    return json.dumps([{"name": g, "count": 1} for g in genres])


def seed(conn: sqlite3.Connection) -> int:
    now = time.time()
    n = 0
    for suffix, tracks, enr in _FIXTURES:
        name = TEST_PREFIX + suffix
        conn.execute(
            "INSERT OR IGNORE INTO artists (name, album_count, track_count) VALUES (?, 0, ?)",
            (name, tracks),
        )
        conn.execute("UPDATE artists SET track_count = ? WHERE name = ?", (tracks, name))
        if enr is None:
            conn.execute("DELETE FROM artist_enrichment WHERE artist_name = ?", (name,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO artist_enrichment "
                "(artist_name, mbid, country, area, genres, source, score, status, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name, enr.get("mbid"), enr.get("country"), enr.get("area"),
                    _genres_json(enr.get("genres")), enr.get("source", "musicbrainz"),
                    enr.get("score", 100), enr.get("status", "ok"), now,
                ),
            )
        n += 1
    conn.commit()
    return n


def wipe(conn: sqlite3.Connection) -> tuple[int, int]:
    like = TEST_PREFIX + "%"
    e = conn.execute("DELETE FROM artist_enrichment WHERE artist_name LIKE ?", (like,)).rowcount
    a = conn.execute("DELETE FROM artists WHERE name LIKE ?", (like,)).rowcount
    conn.commit()
    return a, e


def show(conn: sqlite3.Connection) -> None:
    like = TEST_PREFIX + "%"
    rows = conn.execute(
        "SELECT a.name, a.track_count, e.country, e.genres, e.source, e.status "
        "FROM artists a LEFT JOIN artist_enrichment e ON e.artist_name = a.name "
        "WHERE a.name LIKE ? ORDER BY a.name", (like,),
    ).fetchall()
    if not rows:
        print("No fake test rows present.")
        return
    print(f"{len(rows)} fake test artist(s):")
    for name, tc, country, genres, source, status in rows:
        try:
            gs = ", ".join(g["name"] for g in json.loads(genres or "[]")) or "—"
        except Exception:
            gs = "—"
        print(f"  • {name:<28} tracks={tc:<4} country={country or '—':<4} "
              f"status={status or 'NONE':<13} src={source or '—':<11} genres={gs}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=_default_db_path(), help="SQLite library path (default: the app's ~/library.db)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--seed", action="store_true", help="inject the fake fixtures")
    g.add_argument("--wipe", action="store_true", help="remove every fake test row")
    g.add_argument("--list", action="store_true", help="list current fake test rows")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db, timeout=30.0)
    try:
        if args.seed:
            n = seed(conn)
            print(f"Seeded {n} fake artists into {args.db}")
            show(conn)
            print("\nReopen the Metadata page in the app to see them.")
        elif args.wipe:
            a, e = wipe(conn)
            print(f"Wiped {a} artist row(s) + {e} enrichment row(s) from {args.db}")
        else:
            show(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
