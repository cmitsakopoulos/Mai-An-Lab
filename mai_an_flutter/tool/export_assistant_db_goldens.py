#!/usr/bin/env python3
"""Golden values for phase 5 stage c2 (PORT_MAP rule 3): Flet's
`DatabaseManager` + `track_graph` queries behind `AssistantLibrary` on a
synthetic database (`search_tracks_simple`, `get_all_tracks`,
`get_track_full`, `get_most_played`, `has_current_features`,
`recent_played_paths`, `get_all_artists(search_query)`,
`_apply_anaphora_resolution`'s artist seed SQL, `neighbors(KIND_ARTIST)`,
and `walk`).

    test/goldens/assistant_db.json   (committed; no personal data)

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_assistant_db_goldens.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(REPO, "StreamripApp"))

from utils import db_manager as dbm  # noqa: E402
from utils import track_graph as tg  # noqa: E402

logging.disable(logging.CRITICAL)

OUT = os.path.join(PORT, "test", "goldens", "assistant_db.json")

ARTISTS = [
    (1, "Pink Floyd"),
    (2, "Radiohead"),
    (3, "Young the Giant"),
    (4, "The Beatles"),
    (5, "Sigur Rós"),
    (6, "Αλέξης"),
]

ALBUMS = [
    (1, 1, "Meddle", 1971, "Progressive Rock"),
    (2, 1, "The Dark Side of the Moon", 1973, "Rock"),
    (3, 2, "OK Computer", 1997, "Alternative Rock"),
    (4, 3, "Home of the Strange", 2016, "Indie Rock"),
    (5, 4, "Of The Night", 1968, "Rock"),
    (6, 5, "Ágætis byrjun", 1999, "Post-Rock"),
    (7, 6, "Μόνος", 2020, "Acoustic"),
]

# (id, album_id, title, path, duration, format, added_date, bpm, energy, brightness)
TRACKS = [
    (1, 1, "Echoes", "/m/echoes_pf.flac", 1412.0, "flac", 1000.0, 68.0, 0.35, 0.3),
    (2, 2, "Time", "/m/time_pf.flac", 413.0, "flac", 1010.0, 120.0, 0.55, 0.5),
    (3, 2, "Money", "/m/money_pf.flac", 382.0, "flac", 1020.0, 124.0, 0.65, 0.6),
    (4, 3, "Paranoid Android", "/m/paranoid.flac", 387.0, "flac", 1030.0, 84.0, 0.6, 0.45),
    (5, 3, "Karma Police", "/m/karma.flac", 264.0, "flac", 1040.0, 75.0, 0.4, 0.4),
    (6, 4, "Echoes", "/m/echoes_ytg.flac", 225.0, "mp3", 1050.0, 122.0, 0.72, 0.65),
    (7, 5, "The", "/m/the.mp3", 180.0, "mp3", 1060.0, 100.0, 0.5, 0.5),
    (8, 6, "Svefn-g-englar", "/m/svefn.flac", 604.0, "flac", 1070.0, 70.0, 0.25, 0.2),
    (9, 7, "Pink Moon", "/m/pink_moon.mp3", 125.0, "mp3", 1080.0, 90.0, 0.3, 0.3),
]

# (path, count, last_played, features_version, timbre_bytes_list_or_None, pca_floats_or_None)
PLAY_COUNTS = [
    ("/m/echoes_pf.flac", 12, "2026-11-20 10:00:00", 5, [1, 2, 3, 4], [1.0, 0.0, 0.0, 0.0]),
    ("/m/time_pf.flac", 12, "2026-11-22 10:00:00", 5, [5, 6, 7, 8], [0.9, 0.4, 0.0, 0.0]),
    ("/m/money_pf.flac", 5, "2026-11-21 10:00:00", 4, [9, 10, 11, 12], [0.8, 0.5, 0.1, 0.0]),
    ("/m/paranoid.flac", 8, "2026-11-19 10:00:00", 5, [13, 14, 15, 16], [0.6, 0.8, 0.0, 0.0]),
    ("/m/karma.flac", 0, "2026-11-18 10:00:00", 5, None, None),
    ("/m/svefn.flac", 3, "2026-11-17 10:00:00", 5, [17, 18, 19, 20], [0.2, 0.9, 0.3, 0.0]),
]

# (track_path, neighbor_path, weight, edge_kind)
NEIGHBORS = [
    ("/m/echoes_pf.flac", "/m/paranoid.flac", 0.99, "acoustic"),
    ("/m/echoes_pf.flac", "/m/money_pf.flac", 1.0, "artist"),
    ("/m/echoes_pf.flac", "/m/time_pf.flac", 0.8, "artist"),
    ("/m/echoes_pf.flac", "/m/karma.flac", 0.5, "artist"),
]

# (track_path, age_seconds, event, seed_path)
HISTORY = [
    ("/m/echoes_pf.flac", 120, "played", None),
    ("/m/time_pf.flac", 3600, "played", "/m/echoes_pf.flac"),
    ("/m/money_pf.flac", 10 * 86400, "played", None),
    ("/m/paranoid.flac", 60, "completed", None),
    ("/m/karma.flac", 60, "skipped_early", None),
]

SEARCH_SIMPLE_CASES = [
    ("", 5),
    ("   ", 5),
    ("echoes by pink floyd", 5),
    ("pink floyd by echoes", 5),
    ("time from dark side", 5),
    ("karma from radiohead", 5),
    ("the dark moon", 5),
    ("pink", 5),
    ("pink", 2),
    ("of the", 5),
    ("android radiohead", 5),
    ("ok computer", 5),
    ("nonexistent xyz", 5),
]

ARTIST_SEARCH_CASES = [
    "",
    "pink",
    "radio",
    "paranoid",     # FTS match on track title pulls in Radiohead!
    "radiohed",     # closest-match kmer fallback
    "sigur",
    "zzzzzz",
]

FIRST_TRACK_ARTIST_CASES = [
    "pink floyd",
    "PINK FLOYD",
    "Radiohead",
    "Αλέξης",
    "Nobody",
]


def _clean_row(r):
    if r is None:
        return None
    out = {}
    for k, v in r.items():
        if isinstance(v, (bytes, bytearray)):
            out[k] = {"__bytes__": list(v)}
        else:
            out[k] = v
    return out


async def _build() -> dict:
    tmp = tempfile.mkdtemp(prefix="mai_an_asst_db_")
    db_path = os.path.join(tmp, "library.db")
    db = dbm.DatabaseManager(db_path)
    await db.initialize()
    await db.close()
    # Second open adds play_counts.cluster_id (the fresh-DB migration quirk).
    db = dbm.DatabaseManager(db_path)
    await db.initialize()
    conn = await db.get_connection()

    for aid, name in ARTISTS:
        await conn.execute(
            "INSERT INTO artists (id, name) VALUES (?, ?)", (aid, name)
        )
    for alid, aid, title, year, genre in ALBUMS:
        await conn.execute(
            "INSERT INTO albums (id, artist_id, title, year, genre) VALUES (?, ?, ?, ?, ?)",
            (alid, aid, title, year, genre),
        )
    for tid, alid, title, path, dur, fmt, added, bpm, energy, bright in TRACKS:
        await conn.execute(
            "INSERT INTO tracks (id, album_id, title, path, duration, format, added_date, bpm, energy, brightness) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, alid, title, path, dur, fmt, added, bpm, energy, bright),
        )
    for path, cnt, lp, fv, timbre, coords in PLAY_COUNTS:
        tb = bytes(timbre) if timbre is not None else None
        cb = struct.pack(f"<{len(coords)}f", *coords) if coords is not None else None
        await conn.execute(
            "INSERT INTO play_counts (track_path, count, last_played, features_version, timbre, pca_coords) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (path, cnt, lp, fv, tb, cb),
        )
    for src, dst, w, kind in NEIGHBORS:
        await conn.execute(
            "INSERT INTO track_neighbors (track_path, neighbor_path, weight, edge_kind) "
            "VALUES (?, ?, ?, ?)",
            (src, dst, w, kind),
        )
    now_s = int(time.time())
    for path, age, ev, seed in HISTORY:
        await conn.execute(
            "INSERT INTO playback_history (track_path, played_at, event, seed_path) "
            "VALUES (?, ?, ?, ?)",
            (path, now_s - age, ev, seed),
        )
    await conn.commit()

    search_simple = [
        {"query": q, "limit": lim, "result": [_clean_row(r) for r in await db.search_tracks_simple(q, limit=lim)]}
        for q, lim in SEARCH_SIMPLE_CASES
    ]
    all_tracks = [_clean_row(r) for r in await db.get_all_tracks()]
    track_full = {
        p: _clean_row(await db.get_track_full(p))
        for p in ("/m/echoes_pf.flac", "/m/echoes_ytg.flac", "/m/missing.flac")
    }
    has_features = {
        p: await db.has_current_features(p, tg.FEATURES_VERSION)
        for p in ("/m/echoes_pf.flac", "/m/money_pf.flac", "/m/karma.flac", "/m/echoes_ytg.flac", "/m/missing.flac")
    }
    most_played = {
        str(lim): [_clean_row(r) for r in await db.get_most_played(limit=lim)]
        for lim in (2, 10)
    }
    recent_played = {
        str(win): sorted(await db.recent_played_paths(window_seconds=win))
        for win in (600, 7 * 86400, 30 * 86400)
    }
    artist_search = [
        {"query": q, "result": [a["name"] for a in await db.get_all_artists(search_query=q)]}
        for q in ARTIST_SEARCH_CASES
    ]

    first_track_artist = {}
    for name in FIRST_TRACK_ARTIST_CASES:
        async with conn.execute(
            """
            SELECT t.path, t.title, ar.name AS artist
            FROM tracks t
            JOIN albums al ON al.id = t.album_id
            JOIN artists ar ON ar.id = al.artist_id
            WHERE ar.name = ? COLLATE NOCASE
            LIMIT 1
            """,
            (name,),
        ) as cur:
            row = await cur.fetchone()
            first_track_artist[name] = row["path"] if row else None

    artist_neighbors = {
        str(k): [n["path"] for n in await tg.neighbors(db, "/m/echoes_pf.flac", k=k, edge_kind=tg.KIND_ARTIST)]
        for k in (2, 10)
    }
    walk_plain = await tg.walk(db, "/m/echoes_pf.flac", length=5, avoid={"/m/echoes_pf.flac"})
    walk_avoid = await tg.walk(db, "/m/echoes_pf.flac", length=5, avoid={"/m/echoes_pf.flac", "/m/paranoid.flac"})

    await db.close()
    return {
        "fixture": {
            "artists": ARTISTS,
            "albums": ALBUMS,
            "tracks": TRACKS,
            "play_counts": PLAY_COUNTS,
            "neighbors": NEIGHBORS,
            "history": HISTORY,
        },
        "search_tracks_simple": search_simple,
        "get_all_tracks": all_tracks,
        "get_track_full": track_full,
        "has_current_features": has_features,
        "get_most_played": most_played,
        "recent_played_paths": recent_played,
        "search_artist_names": artist_search,
        "first_track_path_by_artist": first_track_artist,
        "artist_neighbors": artist_neighbors,
        "walk": {"plain": walk_plain, "avoid": walk_avoid},
    }


def main():
    data = asyncio.run(_build())
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"{OUT}: {len(data['search_tracks_simple'])} search_simple, "
          f"{len(data['search_artist_names'])} artist_search cases")


if __name__ == "__main__":
    main()
