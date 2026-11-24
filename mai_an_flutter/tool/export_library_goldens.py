#!/usr/bin/env python3
"""Golden values for the library tabs (PORT_MAP rule 3): what the Flet app's
Genres, Artists and Albums tabs compute, so the Dart port can be checked.

Two files:

  test/goldens/library_synthetic.json   (committed)
      library.py's genre grouping and colour (`_genre_group`, `_genre_color`)
      over hand-picked tags. No personal data.

  tools/state_backups/goldens/<bundle>/library_views.json   (gitignored)
      db_manager's list queries on a real library: every Albums, Artists and
      Genres sort, and the expansions (artists of a genre, albums of an artist
      with and without the genre, tracks of an album, tracks of a genre) for
      a fixed sample. Reads the library.db that export_station_goldens.py
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
import sys
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


def synthetic() -> dict:
    tags = list(dict.fromkeys(TAGS + EXTRA_TAGS + sorted(_GENRE_PALETTE)))
    return {
        "genres": [
            {"tag": t, "color": _genre_color(t), "group": list(_genre_group(t))}
            for t in tags
        ],
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
