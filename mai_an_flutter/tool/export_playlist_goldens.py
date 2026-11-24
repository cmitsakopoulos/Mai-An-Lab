#!/usr/bin/env python3
"""Golden values for the port's playlist writes (PORT_MAP rule 3, §10 4d):
db_manager's playlist methods and delete_tracks_by_paths, run one after the
other on one fresh database. No personal data, so the output is committed:

  test/goldens/playlists_synthetic.json
      setup   SQL that adds a small library to the fresh database
      searches get_all_playlists(search_query=…) on the three playlists
      steps   each call, its arguments, what it returned (or the error
              text, as str(exc)), and then the playlists and
              playlist_tracks rows, plus every get_all_playlists sort and
              get_tracks_in_playlist for each playlist

    flutter test test/playlists_test.dart

`created` defaults to the current second, so the script pins it with an
UPDATE after each create (the same statement runs in the Dart test).

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_playlist_goldens.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(REPO, "StreamripApp"))

from utils.db_manager import DatabaseManager  # noqa: E402

OUT = os.path.join(PORT, "test", "goldens", "playlists_synthetic.json")

P = [f"/music/A/{i}.flac" for i in range(1, 8)]

SETUP = [
    "INSERT INTO artists (name) VALUES ('Björk'), ('Χάρις Αλεξίου')",
    "INSERT INTO albums (artist_id, title, genre) VALUES "
    "(1, 'Post', 'Electronic'), (2, 'Η Αγάπη', 'Laiko')",
] + [
    "INSERT INTO tracks (album_id, title, track_num, duration, path, format, "
    f"added_date, bitrate) VALUES ({1 if i < 4 else 2}, 'Song {i + 1}', "
    f"{i + 1}, {180.5 + i}, '{p}', 'FLAC', {1700000000 + i}, 900)"
    for i, p in enumerate(P)
]

# (method, args, created): `created` pins playlists.created for the new id.
STEPS = [
    ("create_playlist", ["Road Trip"], 1700000100),
    ("create_playlist", ["  Chill  "], 1700000300),
    ("create_playlist", ["road trip"], None),          # NOCASE clash
    ("create_playlist", ["Ελληνικά"], 1700000200),
    ("add_track_to_playlist", [1, P[0]], None),
    ("add_track_to_playlist", [1, P[1]], None),
    ("add_track_to_playlist", [1, P[2]], None),
    ("add_track_to_playlist", [1, P[3]], None),
    ("add_track_to_playlist", [1, P[1]], None),         # already there
    ("add_track_to_playlist", [2, P[2]], None),
    ("add_track_to_playlist", [2, P[4]], None),
    ("add_track_to_playlist", [1, "/not/in/library.mp3"], None),
    ("move_playlist_track", [1, 0, 2], None),
    ("move_playlist_track", [1, 3, 0], None),
    ("move_playlist_track", [1, 10, -5], None),         # clamped
    ("move_playlist_track", [3, 0, 1], None),           # empty playlist
    ("remove_track_from_playlist", [1, P[2]], None),
    ("remove_track_from_playlist", [1, "/never/added.mp3"], None),
    ("delete_tracks_by_paths", [[P[1]]], None),
    ("sql", ["DELETE FROM tracks WHERE path = ?", [P[3]]], None),  # orphan row
    ("move_playlist_track", [1, 0, 1], None),           # drops the orphan first
    ("update_playlist", [2, "Chill Out", "#FF5555"], None),
    ("update_playlist", [2, "", None], None),           # nothing to set
    ("update_playlist", [2, "ROAD TRIP", None], None),  # clash
    ("update_playlist", [3, None, "#55FFFF"], None),
    ("add_track_to_playlist", [3, P[6]], None),
    ("add_track_to_playlist", [3, P[5]], None),
    ("delete_playlist", [1], None),
    ("delete_playlist", [99], None),
]

# Run on the three playlists just before the first delete_playlist. ASCII
# only: SQLite's LIKE folds ASCII case alone, where the port's search is
# accent- and case-blind for every script (PORT_MAP §7).
SEARCHES = ["", "chill", "TRIP", "rood trap", "zzzz"]


def _rows(con, sql, args=()):
    return [list(r) for r in con.execute(sql, args).fetchall()]


async def run(path: str) -> dict:
    db = DatabaseManager(path)
    await db.initialize()
    con = sqlite3.connect(path)
    for stmt in SETUP:
        con.execute(stmt)
    con.commit()

    steps = []
    searches = {}
    for method, args, created in STEPS:
        if method == "delete_playlist" and not searches:
            for q in SEARCHES:
                res = await db.get_all_playlists(search_query=q, sort_mode="name")
                searches[q] = {
                    "closest": bool(getattr(res, "is_closest", False)),
                    "names": [r["name"] for r in res],
                }
        result = error = None
        try:
            if method == "sql":
                con.execute(args[0], args[1])
                con.commit()
            else:
                result = await getattr(db, method)(*args)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        if created is not None and isinstance(result, int):
            con.execute("UPDATE playlists SET created = ? WHERE id = ?",
                        (created, result))
            con.commit()
        listing = {}
        for sort in ("name", "date"):
            listing[sort] = [dict(r) for r in await db.get_all_playlists(sort_mode=sort)]
        tracks = {}
        for pl in listing["name"]:
            tracks[str(pl["id"])] = [
                t["path"] for t in await db.get_tracks_in_playlist(pl["id"])
            ]
        steps.append({
            "method": method,
            "args": args,
            "created": created,
            "result": result,
            "error": error,
            "playlists": _rows(con, "SELECT id, name, created, color FROM playlists ORDER BY id"),
            "playlist_tracks": _rows(
                con, "SELECT id, playlist_id, track_path, order_index "
                     "FROM playlist_tracks ORDER BY id"),
            "listing": listing,
            "tracks": tracks,
        })

    con.close()
    await db.close()
    return {"setup": SETUP, "steps": steps, "searches": searches}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = asyncio.run(run(os.path.join(tmp, "library.db")))
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(f"wrote {os.path.relpath(OUT, PORT)}")


if __name__ == "__main__":
    main()
