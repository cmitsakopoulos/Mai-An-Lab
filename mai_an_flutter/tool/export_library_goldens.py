#!/usr/bin/env python3
"""Golden values for the library tabs (PORT_MAP rule 3): what the Flet app's
Genres, Artists and Albums tabs compute, so the Dart port can be checked.

Two files:

  test/goldens/library_synthetic.json   (committed)
      library.py's genre grouping and colour (`_genre_group`, `_genre_color`)
      over hand-picked tags, and the Search tab's landing queries
      (`get_most_played`, `_artists`, `_genres`, `get_library_counts`) on a
      small library built by `LANDING_SETUP` in a fresh database. No
      personal data.

  tools/state_backups/goldens/<bundle>/library_views.json   (gitignored)
      db_manager's list queries on a real library: every Albums, Artists and
      Genres sort, and the expansions (artists of a genre, albums of an artist
      with and without the genre, tracks of an album, tracks of a genre) for
      a fixed sample, and the landing queries. Reads the library.db that export_station_goldens.py
      copied beside it (run that first), or a bundle passed here.
          flutter test test/library_views_golden_test.dart

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_library_goldens.py            # newest goldens dir
    ~/miniconda3/bin/python tool/export_library_goldens.py path/to/bundle.zip
    ~/miniconda3/bin/python tool/export_library_goldens.py --synthetic-only
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "StreamripApp"))

from export_station_goldens import TAGS  # noqa: E402
from ui.views.library import _genre_color, _genre_group  # noqa: E402
from utils.genre_taxonomy import _GENRE_PALETTE  # noqa: E402

EXTRA_TAGS = [
    "Hip-Hop", "Trip-Hop", "Laiko", "Divers", "DIVERS", "Pop, Rock",
    "Rock, Pop", "Rock, Metal, Pop", "Pop, Rock, Metal", "Shoegaze",
    "post-punk", "Neo-Soul / R&B", "K-Pop", "musique diverse",
    "Ambient; Drone", "World Music", "Synthpop+Coldwave", "Bombas",
    "Bombas, Laikopop", "  Rebetiko  ", "Chanson Française", "Vaporwave",
    "Lo-Fi", "Lo-fi / Chillhop", "Afrobeats & Amapiano", "Nu Jazz",
    "drum'n'bass", "Indie", "Indie Folk", "Μπαλάντα", "Ελληνικό Ροκ",
    "Alt,Electronic,Hip-Hop,Pop", "Alt,Metal,Rock", "Electronic,Hip-Hop",
    "Classical,Folk/Cntry,Jazz", "Other", "Unknown", "Various Artists",
    "Special Purpose Artist", "Autre", "+", "/ ,", "Σ", "ΟΔΟΣ",
]


# A library for the landing queries: multi-genre albums (bucket over genre,
# Greek tokens, a junk token, "rock" next to "Rock"), albums with no usable
# genre, a play_counts row for a path not in the library, count-0 rows, and
# ties on count (broken by last_played) and on plays (broken by name).
LANDING_SETUP = [
    "INSERT INTO artists (name) VALUES ('Björk'), ('Χάρις Αλεξίου'), "
    "('Aphex Twin'), ('Nobody'), ('Tie A'), ('Tie B'), ('Zero')",
    "INSERT INTO albums (artist_id, title, genre, genre_bucket) VALUES "
    "(1, 'Post', 'Electronic, Pop', NULL), "
    "(2, 'Η Αγάπη', 'Laiko', 'Laiko, Λαϊκό'), "
    "(3, 'SAW', 'Unknown', ''), "
    "(3, 'Drukqs', NULL, NULL), "
    "(4, 'Silence', 'Ambient', NULL), "
    "(5, 'TA', NULL, 'rock, Other'), "
    "(6, 'TB', 'Pop', 'Rock'), "
    "(7, 'Nil', 'Jazz', NULL)",
] + [
    "INSERT INTO tracks (album_id, title, track_num, duration, path, format, "
    f"added_date) VALUES ({al}, {title}, {n}, 200, '{path}', 'FLAC', {1700000000 + i})"
    for i, (al, title, n, path) in enumerate([
        (1, "'Hyperballad'", 1, "/m/bjork/1.flac"),
        (1, "'Isobel'", 2, "/m/bjork/2.flac"),
        (1, "NULL", 3, "/m/bjork/3.flac"),
        (2, "'Σ’ αγαπώ'", 1, "/m/haris/1.flac"),
        (2, "'Δρόμοι'", 2, "/m/haris/2.flac"),
        (3, "'Xtal'", 1, "/m/aphex/1.flac"),
        (4, "'Avril 14th'", 1, "/m/aphex/2.flac"),
        (5, "'Quiet'", 1, "/m/nobody/1.flac"),
        (6, "'A'", 1, "/m/tie/a.flac"),
        (7, "'B'", 1, "/m/tie/b.flac"),
        (8, "'Zero'", 1, "/m/zero/1.flac"),
    ])
] + [
    "INSERT INTO play_counts (track_path, count, last_played) VALUES "
    "('/m/bjork/1.flac', 9, 1700000500), "
    "('/m/bjork/2.flac', 4, 1700000400), "
    "('/m/bjork/3.flac', 4, 1700000600), "
    "('/m/haris/1.flac', 7, 1700000100), "
    "('/m/haris/2.flac', 6, 1700000200), "
    "('/m/aphex/1.flac', 3, 1700000300), "
    "('/m/aphex/2.flac', 2, 1700000700), "
    "('/m/nobody/1.flac', 0, 1700000800), "
    "('/m/tie/a.flac', 5, 1700000900), "
    "('/m/tie/b.flac', 5, 1700000950), "
    "('/m/gone/1.flac', 50, 1700001000), "
    "('/m/zero/1.flac', 0, NULL)",
    "INSERT INTO playlists (name, created) VALUES ('One', 1700000000), ('Two', 1700000001)",
]


def _landing_rows(most, artists, genres, counts) -> dict:
    return {
        "most_played": [[r["path"], r["title"], r["artist"], r["count"]] for r in most],
        "artists": [[r["name"], r["plays"], r["tracks_played"]] for r in artists],
        "genres": [[r["name"], r["plays"], r["albums_played"]] for r in genres],
        "counts": [counts["tracks"], counts["albums"], counts["artists"], counts["playlists"]],
    }


async def _landing(db, limits=(5, 50)) -> dict:
    return {
        str(n): _landing_rows(
            await db.get_most_played(limit=n),
            await db.get_most_played_artists(limit=n),
            await db.get_most_played_genres(limit=n),
            await db.get_library_counts(),
        )
        for n in limits
    }


async def _landing_synthetic() -> dict:
    from utils.db_manager import DatabaseManager

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "library.db")
        db = DatabaseManager(path)
        await db.initialize()
        con = sqlite3.connect(path)
        for stmt in LANDING_SETUP:
            con.execute(stmt)
        con.commit()
        con.close()
        try:
            return {"setup": LANDING_SETUP, "limits": await _landing(db)}
        finally:
            await db.close()


def synthetic() -> dict:
    tags = list(dict.fromkeys(TAGS + EXTRA_TAGS + sorted(_GENRE_PALETTE)))
    return {
        "genres": [
            {"tag": t, "color": _genre_color(t), "group": list(_genre_group(t))}
            for t in tags
        ],
        "landing": asyncio.run(_landing_synthetic()),
    }


SAMPLE = 6


async def real(db_path: str) -> str:
    from utils.db_manager import DatabaseManager

    db = DatabaseManager(db_path)
    await db.initialize()
    try:
        def album_keys(rows):
            return [[r["id"], r["album"], r["artist"], r["track_count"]] for r in rows]

        albums = {s: album_keys(await db.get_all_albums(sort_mode=s))
                  for s in ("date", "artist", "album", "track")}
        artists = {s: [[r["id"], r["name"], r["album_count"], r["track_count"]]
                       for r in await db.get_all_artists(sort_mode=s)]
                   for s in ("name", "tracks", "albums")}
        genres = {s: [[r["name"], r["artist_count"], r["album_count"], r["track_count"]]
                      for r in await db.get_all_genres(sort_mode=s)]
                  for s in ("name", "tracks", "albums", "artists")}

        rng = random.Random(0)
        g_names = rng.sample([g[0] for g in genres["name"]], min(SAMPLE, len(genres["name"])))
        by_genre = {}
        for g in g_names:
            arts = await db.get_artists_by_genre(g)
            sample = arts[: 3]
            by_genre[g] = {
                "artists": [[r["id"], r["name"], r["album_count"], r["track_count"]] for r in arts],
                "albums": {a["name"]: album_keys(await db.get_albums_by_artist(a["name"], genre=g))
                           for a in sample},
                "tracks": [t["path"] for t in await db.get_tracks_by_genre(g)],
            }
        a_names = rng.sample([a[1] for a in artists["name"]], min(SAMPLE, len(artists["name"])))
        by_artist = {a: album_keys(await db.get_albums_by_artist(a)) for a in a_names}
        al_sample = rng.sample(albums["album"], min(SAMPLE, len(albums["album"])))
        by_album = [
            {"album": al[1], "artist": al[2],
             "tracks": [t["path"] for t in await db.get_tracks_by_album(al[1], al[2])]}
            for al in al_sample
        ]
        doc = {
            "db": os.path.basename(db_path),
            "albums": albums, "artists": artists, "genres": genres,
            "by_genre": by_genre, "by_artist": by_artist, "by_album": by_album,
            "landing": await _landing(db),
        }
    finally:
        await db.close()
    out = os.path.join(os.path.dirname(db_path), "library_views.json")
    with open(out, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return out


def _db_for(arg: str | None) -> str:
    goldens = os.path.join(REPO, "tools", "state_backups", "goldens")
    if arg is None:
        found = sorted(glob.glob(os.path.join(goldens, "*", "library.db")))
        if not found:
            raise SystemExit("no goldens dir: run export_station_goldens.py first")
        return found[-1]
    if arg.endswith(".db"):
        return arg
    name = os.path.splitext(os.path.basename(arg))[0]
    out_dir = os.path.join(goldens, name)
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, "library.db")
    with zipfile.ZipFile(arg) as z, z.open("library.db") as src, open(db_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return db_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", nargs="?")
    ap.add_argument("--synthetic-only", action="store_true")
    args = ap.parse_args()

    syn = os.path.join(PORT, "test", "goldens", "library_synthetic.json")
    with open(syn, "w") as f:
        json.dump(synthetic(), f, ensure_ascii=False, indent=1)
    print("wrote", os.path.relpath(syn, PORT))
    if args.synthetic_only:
        return
    print("wrote", os.path.relpath(asyncio.run(real(_db_for(args.bundle))), PORT))


if __name__ == "__main__":
    main()
