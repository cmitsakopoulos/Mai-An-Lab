#!/usr/bin/env python3
"""Trial: enrich the library's artists with MusicBrainz provenance + genres.

Runs against the same library.db the app uses and writes ONLY the additive
`artist_enrichment` cache table — it never touches tracks/albums/features, so it
cannot force a re-analysis. This is the Mac-side trial before any of the
enrichment is wired into the graph colouring / evaluation.

Examples
--------
    # dry-ish first pass: 10 most-played artists, country only
    python tools/enrich_artists.py --limit 10

    # full pass with genres (slower: 2 requests/artist, ~2s each)
    python tools/enrich_artists.py --with-genres

    # point at an explicit DB and re-try previously failed lookups
    python tools/enrich_artists.py --db ~/library.db --include-failed
"""

import argparse
import asyncio
import os
import sys
from collections import Counter

# Make the StreamripApp package importable when run as `python tools/...`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.db_manager import DatabaseManager  # noqa: E402
from utils.metadata_enrich import enrich_library  # noqa: E402


def _default_db() -> str:
    # Mirror the app: get_app_dir() resolves to $HOME on desktop → ~/library.db.
    try:
        from utils.filepath_utils import get_app_dir
        return os.path.join(get_app_dir(), "library.db")
    except Exception:
        return os.path.expanduser("~/library.db")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=_default_db(), help="path to library.db")
    ap.add_argument("--limit", type=int, default=None, help="max artists this run")
    ap.add_argument("--with-genres", action="store_true",
                    help="also fetch genres (2 requests/artist)")
    ap.add_argument("--include-failed", action="store_true",
                    help="also retry artists whose last lookup errored / wasn't found")
    ap.add_argument("--contact", default="mitsacopoulos@gmail.com",
                    help="contact string for the MusicBrainz User-Agent")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"✗ no database at {args.db} (pass --db)")
        return 1

    db = DatabaseManager(args.db)
    # Opening the connection runs the idempotent migrations, creating the
    # artist_enrichment table if this DB predates it. Safe on every DB.
    await db.get_connection()

    countries = Counter()

    def progress(i, total, name, res):
        if res.get("country"):
            countries[res["country"]] += 1
        loc = res.get("country") or res.get("area") or "—"
        genres = ", ".join(g["name"] for g in (res.get("genres") or [])[:3]) or "—"
        print(f"  [{i}/{total}] {name!r:36} {res['status']:13} "
              f"loc={loc:4} score={res.get('score', 0):3}  {genres}")

    # Same incremental orchestration the app uses (utils.metadata_enrich), so
    # the CLI and the in-app trigger never drift.
    summary = await enrich_library(
        db, with_genres=args.with_genres, limit=args.limit,
        contact=args.contact, include_failed=args.include_failed,
        progress=progress,
    )

    print(f"\nSummary  {summary}")
    if countries:
        top = ", ".join(f"{c}:{n}" for c, n in countries.most_common(10))
        print(f"  countries     {top}")

    await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
