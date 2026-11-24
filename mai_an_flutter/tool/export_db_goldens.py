#!/usr/bin/env python3
"""Golden values for the port's database writes (PORT_MAP rule 3): the schema
db_manager leaves behind when it opens a database, and what the play-ledger
drain writes. No personal data, so the output is committed:

  test/goldens/db_synthetic.json
      schema   for each case: the SQL statements that build the starting
               database (none for a fresh one), how many times the app opens
               it, and the result: PRAGMA user_version, every sqlite_master
               row (whitespace-collapsed SQL) and every table's rows.
      ledger   ledger files, drained one after the other into one database:
               what each drain returned and the playback_history and
               play_counts rows after it.

    flutter test test/db_schema_test.dart test/play_ledger_test.dart

And, when a real library is at hand (gitignored, personal data):

  tools/state_backups/goldens/<bundle>/library_migrated.db
      the library.db that export_station_goldens.py copied there, after
      DatabaseManager opened it. test/db_schema_real_test.dart migrates its
      own copy and compares every table.

The app opens a database the way main.py does: DatabaseManager(path) then
initialize(), which first runs get_connection()'s migrations.

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_db_goldens.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(REPO, "StreamripApp"))

from utils import play_ledger  # noqa: E402
from utils.db_manager import DatabaseManager  # noqa: E402

OUT = os.path.join(PORT, "test", "goldens", "db_synthetic.json")


def _f32(values) -> str:
    """A float32 blob literal, as X'..'."""
    return "X'" + struct.pack(f"<{len(values)}f", *values).hex().upper() + "'"


V4 = _f32([i * 0.5 for i in range(88)])          # 352 bytes: migrated to v5
V4_ODD = _f32([1.0, 2.0, 3.0])                    # v4 with the wrong length: left alone
V5 = _f32([i * 0.25 for i in range(68)])
COORDS = _f32([0.1 * i for i in range(17)])

# The shapes older Flet builds left behind. Every migration in
# get_connection() and initialize() has something to do here.
LEGACY_OLD = [
    # Before the DSP columns, coordinates, playlists' colour, artist tags.
    "CREATE TABLE artists (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE COLLATE NOCASE NOT NULL, album_count INTEGER DEFAULT 0, track_count INTEGER DEFAULT 0)",
    "CREATE TABLE albums (id INTEGER PRIMARY KEY AUTOINCREMENT, artist_id INTEGER NOT NULL, title TEXT NOT NULL COLLATE NOCASE, year TEXT, genre TEXT, track_count INTEGER DEFAULT 0, FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE, UNIQUE (artist_id, title))",
    "CREATE TABLE tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, album_id INTEGER NOT NULL, title TEXT, track_num INTEGER, duration REAL, path TEXT UNIQUE NOT NULL, format TEXT, added_date REAL, bitrate INTEGER, FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE)",
    "CREATE TABLE play_counts (track_path TEXT PRIMARY KEY, count INTEGER DEFAULT 0, last_played INTEGER)",
    "CREATE TABLE playlists (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, created INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER)))",
    "CREATE TABLE playback_history (track_path TEXT NOT NULL, played_at REAL NOT NULL, event TEXT NOT NULL, seed_path TEXT)",
    "CREATE TABLE track_partitions (track_path TEXT PRIMARY KEY REFERENCES tracks(path) ON DELETE CASCADE, mood TEXT, islet_id INTEGER)",
    "CREATE TABLE mood_feedback (x)",
    "CREATE TABLE mood_profiles (x)",
    "CREATE TABLE pca_space (x)",
    "CREATE TABLE genre_graph (x)",
    "CREATE TABLE autoplay_hop_rejects (x)",
    "CREATE TABLE autoplay_hidden (x)",
    "INSERT INTO artists (name) VALUES ('Björk'), ('Χάρις Αλεξίου')",
    "INSERT INTO albums (artist_id, title, genre) VALUES (1, 'Post', 'Electronic'), (2, 'Η Αγάπη', 'Laiko')",
    "INSERT INTO tracks (album_id, title, track_num, duration, path, format, added_date) VALUES "
    "(1, 'Army of Me', 1, 234.5, '/m/bjork/01.flac', 'flac', 1700000000.5), "
    "(2, 'Τα Κλειδιά', 1, 201.0, '/m/haris/01.mp3', 'mp3', 1700000001)",
    "INSERT INTO play_counts (track_path, count, last_played) VALUES ('/m/bjork/01.flac', 3, 1700000100)",
    "INSERT INTO playlists (name, created) VALUES ('Morning', 1700000000)",
    "INSERT INTO playback_history VALUES ('/m/bjork/01.flac', 1700000050, 'played', NULL), ('/m/bjork/01.flac', 1700000050, 'played', NULL)",
    "INSERT INTO track_partitions VALUES ('/m/bjork/01.flac', 'calm', 2), ('/m/haris/01.mp3', NULL, 3)",
    "PRAGMA user_version = 0",
]

LEGACY_MID = [
    # Has the DSP columns and coordinates from geometry v1, timbre blobs at
    # features v4, a feature_cache, enrichment before provenance/attempts.
    "CREATE TABLE artists (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE COLLATE NOCASE NOT NULL, album_count INTEGER DEFAULT 0, track_count INTEGER DEFAULT 0)",
    "CREATE TABLE albums (id INTEGER PRIMARY KEY AUTOINCREMENT, artist_id INTEGER NOT NULL, title TEXT NOT NULL COLLATE NOCASE, year TEXT, genre TEXT, track_count INTEGER DEFAULT 0, FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE, UNIQUE (artist_id, title))",
    "CREATE TABLE tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, album_id INTEGER NOT NULL, title TEXT, track_num INTEGER, duration REAL, path TEXT UNIQUE NOT NULL, format TEXT, added_date REAL, bitrate INTEGER, bpm REAL DEFAULT 0, energy REAL DEFAULT 0, brightness REAL DEFAULT 0, FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE)",
    "CREATE TABLE play_counts (track_path TEXT PRIMARY KEY, count INTEGER DEFAULT 0, last_played INTEGER, bpm REAL DEFAULT 0, energy REAL DEFAULT 0, brightness REAL DEFAULT 0, pca_coords BLOB, timbre BLOB, features_version INTEGER DEFAULT 0)",
    "CREATE TABLE feature_cache (track_path TEXT PRIMARY KEY, timbre BLOB, features_version INTEGER)",
    "CREATE TABLE artist_enrichment (artist_name TEXT PRIMARY KEY COLLATE NOCASE, mbid TEXT, country TEXT, area TEXT, genres TEXT, source TEXT, score INTEGER, status TEXT, fetched_at REAL)",
    "CREATE TABLE track_neighbors (track_path TEXT NOT NULL, neighbor_path TEXT NOT NULL, weight REAL NOT NULL, edge_kind TEXT NOT NULL, PRIMARY KEY (track_path, neighbor_path, edge_kind))",
    "CREATE VIRTUAL TABLE fts_search USING fts5(title, album, artist, tokenize='unicode61')",
    "CREATE TRIGGER trg_tracks_insert_counts AFTER INSERT ON tracks BEGIN UPDATE albums SET track_count = track_count + 1 WHERE id = NEW.album_id; END",
    "INSERT INTO artists (name) VALUES ('Massive Attack')",
    "INSERT INTO albums (artist_id, title, genre) VALUES (1, 'Mezzanine', 'Trip-Hop')",
    "INSERT INTO tracks (album_id, title, track_num, duration, path, format, added_date, bpm, energy) VALUES "
    "(1, 'Angel', 1, 379.0, '/m/ma/01.flac', 'flac', 1700000000, 80.5, 0.4), "
    "(1, 'Risingson', 2, 298.0, '/m/ma/02.flac', 'flac', 1700000000, 90.0, 0.5), "
    "(1, 'Teardrop', 3, 330.0, '/m/ma/03.flac', 'flac', 1700000000, 77.0, 0.3)",
    f"INSERT INTO play_counts (track_path, count, last_played, bpm, energy, pca_coords, timbre, features_version) VALUES "
    f"('/m/ma/01.flac', 2, 1700000200, 80.5, 0.4, {COORDS}, {V4}, 4), "
    f"('/m/ma/02.flac', 0, NULL, 90.0, 0.5, {COORDS}, {V4_ODD}, 4), "
    f"('/m/ma/03.flac', 1, 1700000300, 77.0, 0.3, NULL, {V5}, 5)",
    f"INSERT INTO feature_cache VALUES ('/m/ma/01.flac', {V4}, 4), ('/m/ma/03.flac', {V5}, 5)",
    "INSERT INTO artist_enrichment (artist_name, country, genres, source, status) VALUES ('Massive Attack', 'GB', '[{\"name\": \"trip hop\", \"count\": 9}]', 'musicbrainz', 'ok')",
    "PRAGMA user_version = 1",
]

SCHEMA_CASES = [
    {"name": "fresh", "sql": [], "opens": 1},
    {"name": "fresh_reopened", "sql": [], "opens": 2},
    {"name": "legacy_old", "sql": LEGACY_OLD, "opens": 1},
    {"name": "legacy_old_reopened", "sql": LEGACY_OLD, "opens": 2},
    {"name": "legacy_mid", "sql": LEGACY_MID, "opens": 1},
]


def _value(v):
    if isinstance(v, bytes):
        return {"blob": v.hex()}
    if isinstance(v, float):
        return {"real": repr(v)}
    return v


def dump(path: str) -> dict:
    con = sqlite3.connect(path)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        master = [
            [t, n, tb, re.sub(r"\s+", " ", s).strip() if s else None]
            for t, n, tb, s in con.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name")
        ]
        tables = {}
        for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
            if name.startswith("fts_search_"):
                continue  # FTS5's own shadow tables
            cols = [c[1] for c in con.execute(f'PRAGMA table_info("{name}")')]
            rowid = "" if name == "fts_search" else "rowid, "
            if name == "fts_search":
                cols = ["rowid"] + cols
            rows = con.execute(
                f'SELECT {rowid}* FROM "{name}" ORDER BY 1').fetchall()
            tables[name] = {
                "columns": cols if name == "fts_search" else ["rowid"] + cols,
                "rows": [[_value(v) for v in r] for r in rows],
            }
        return {"user_version": version, "master": master, "tables": tables}
    finally:
        con.close()


async def _open(path: str) -> None:
    db = DatabaseManager(path)
    await db.initialize()
    await db.close()


def schema_case(case: dict, tmp: str) -> dict:
    path = os.path.join(tmp, f"{case['name']}.db")
    if case["sql"]:
        con = sqlite3.connect(path)
        for stmt in case["sql"]:
            con.execute(stmt)
        con.commit()
        con.close()
    for _ in range(case["opens"]):
        asyncio.run(_open(path))
    return {**case, "result": dump(path)}


def _line(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def _uri(p: str) -> str:
    from pathlib import PurePosixPath
    return PurePosixPath(p).as_uri()


GREEK = "/storage/emulated/0/Music/Χάρις Αλεξίου/Η Αγάπη #1 (live) 100%.mp3"

# Ledger files in drain order. Each is a list of raw lines (some
# deliberately broken). A "draining" key is a leftover from an interrupted
# drain, ingested before the live file.
LEDGERS = [
    {
        "lines": [
            _line(id=_uri("/m/bjork/01.flac"), ms=45_000, dur=234_500, start=1_700_001_000, end=1_700_001_045),
            _line(id=_uri("/m/bjork/01.flac"), ms=8_000, dur=234_500, start=1_700_001_050, end=1_700_001_058),
            _line(id=_uri("/m/haris/01.mp3"), ms=25_000, dur=40_000, start=1_700_001_060, end=1_700_001_085),
            _line(id=_uri("/m/haris/01.mp3"), ms=19_999, dur=40_000, start=1_700_001_090, end=1_700_001_110),
            _line(id=_uri(GREEK), ms=61_250, dur=0, start=1_700_001_200, end=1_700_001_261),
            _line(id=_uri("/m/not/in/library.flac"), ms=30_000, dur=0, start=1, end=1_700_001_300),
            _line(id="https://cdn.example/x.flac", ms=90_000, dur=0, end=1_700_001_400),
            _line(id="/m/bare/path.ogg", ms=30_750, dur=100_000, end=1_700_001_500),
            _line(id=_uri("/m/bjork/01.flac"), ms="31000", dur=None, end=1_700_001_600.9),
            _line(ms=50_000, end=1_700_001_700),
            _line(id=_uri("/m/bjork/01.flac"), ms="abc", end=1_700_001_800),
            _line(id=_uri("/m/bjork/01.flac"), ms=40_000, end="1700001900"),
            _line(id=_uri("/m/bjork/01.flac"), ms=40_000, end="17.5"),
            _line(id=_uri("/m/bjork/01.flac"), ms=True, dur=1, end=1_700_002_000),
            "",
            "   ",
            "not json",
            '{"id": "file:///m/haris/01.mp3", "ms": 9',
        ],
    },
    {
        # A replay of lines already ingested (a drain killed after its
        # commit) next to one new listen: only the new one counts.
        "draining": [
            _line(id=_uri("/m/bjork/01.flac"), ms=45_000, dur=234_500, start=1_700_001_000, end=1_700_001_045),
            _line(id=_uri("/m/haris/01.mp3"), ms=25_000, dur=40_000, start=1_700_001_060, end=1_700_001_085),
        ],
        "lines": [
            _line(id=_uri("/m/bjork/01.flac"), ms=200_000, dur=234_500, start=1_700_003_000, end=1_700_003_200),
            _line(id=_uri("/m/bjork/01.flac"), ms=12_340, dur=234_500, start=1_700_003_300, end=1_700_003_312),
        ],
    },
    {"lines": []},
]

# Rounding of listened_s (Python's round(x, 1), half-even on the exact value).
ROUNDING_MS = [0, 1, 49, 50, 51, 149, 150, 250, 350, 750, 1250, 1350, 2250,
               61_250, 61_750, 99_950, 100_050, 123_456, 999_999, 1_000_050]


def ledger_cases(tmp: str) -> dict:
    data = os.path.join(tmp, "ledger")
    os.makedirs(data)
    path = os.path.join(data, "library.db")
    con = sqlite3.connect(path)
    for stmt in LEGACY_OLD:
        con.execute(stmt)
    con.commit()
    con.close()

    async def run():
        db = DatabaseManager(path)
        await db.initialize()
        conn = await db.get_connection()
        await conn.execute("UPDATE tracks SET bpm = 120.5, energy = 0.7, brightness = 0.2 WHERE path = '/m/bjork/01.flac'")
        await conn.commit()
        steps = []
        for ledger in LEDGERS:
            live = play_ledger.ledger_path(data)
            if "draining" in ledger:
                with open(live + ".draining", "w", encoding="utf-8") as fh:
                    fh.write("".join(x + "\n" for x in ledger["draining"]))
            if ledger["lines"]:
                with open(live, "w", encoding="utf-8") as fh:
                    fh.write("".join(x + "\n" for x in ledger["lines"]))
            counted = await play_ledger.drain_ledger(data, db)
            hist = await (await conn.execute(
                "SELECT track_path, played_at, event, listened_s, seed_path "
                "FROM playback_history ORDER BY rowid")).fetchall()
            counts = await (await conn.execute(
                "SELECT track_path, count, last_played, bpm, energy, brightness "
                "FROM play_counts ORDER BY track_path")).fetchall()
            steps.append({
                **ledger,
                "counted": counted,
                "left_over": sorted(os.listdir(data)),
                "playback_history": [[_value(v) for v in r] for r in hist],
                "play_counts": [[_value(v) for v in r] for r in counts],
            })
        await db.close()
        return steps

    rounding = [[ms, _value(round(float(ms) / 1000.0, 1))] for ms in ROUNDING_MS]
    parsed = play_ledger.parse_lines(LEDGERS[0]["lines"])
    return {
        "setup_sql": LEGACY_OLD,
        "setup_update": "UPDATE tracks SET bpm = 120.5, energy = 0.7, brightness = 0.2 WHERE path = '/m/bjork/01.flac'",
        "steps": asyncio.run(run()),
        "parsed": [{k: _value(v) for k, v in e.items()} for e in parsed],
        "rounding": rounding,
    }


def real() -> None:
    goldens = os.path.join(REPO, "tools", "state_backups", "goldens")
    if not os.path.isdir(goldens):
        return
    for name in sorted(os.listdir(goldens), reverse=True):
        src = os.path.join(goldens, name, "library.db")
        if not os.path.exists(src):
            continue
        dst = os.path.join(goldens, name, "library_migrated.db")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(dst + suffix):
                os.remove(dst + suffix)
        # .backup folds a pending WAL in; the copy starts in DELETE mode.
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        out = sqlite3.connect(dst)
        con.backup(out)
        con.close()
        out.close()
        asyncio.run(_open(dst))
        con = sqlite3.connect(dst)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode = DELETE")
        con.close()
        print(f"wrote {os.path.relpath(dst, REPO)}")
        return


def main() -> None:
    real()
    with tempfile.TemporaryDirectory() as tmp:
        out = {
            "geometry_version": DatabaseManager.GEOMETRY_VERSION,
            "schema": [schema_case(c, tmp) for c in SCHEMA_CASES],
            "ledger": ledger_cases(tmp),
        }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(f"wrote {os.path.relpath(OUT, PORT)}")


if __name__ == "__main__":
    main()
