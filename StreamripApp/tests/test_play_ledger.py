"""Play ledger → DB ingestion: parsing the native handler's JSONL, the
scrobble rule in record_listens, and crash-replay idempotency."""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import play_ledger
from utils.db_manager import DatabaseManager


def _run(coro):
    return asyncio.run(coro)


def _line(path, ms, dur_ms=200_000, end=1_700_000_000):
    return json.dumps({"id": pathlib.Path(path).as_uri(), "ms": ms,
                       "dur": dur_ms, "start": end - ms // 1000, "end": end})


class TestParse(unittest.TestCase):
    def test_file_uri_round_trips_to_library_path(self):
        path = "/storage/emulated/0/Music/Sigur Rós/Ágætis byrjun/01 #.flac"
        [e] = play_ledger.parse_lines([_line(path, 61_500)])
        self.assertEqual(e["path"], path)
        self.assertAlmostEqual(e["listened_s"], 61.5)
        self.assertAlmostEqual(e["duration_s"], 200.0)

    def test_streams_and_torn_lines_are_dropped(self):
        lines = [
            json.dumps({"id": "https://cdn.example/x.flac", "ms": 90_000, "end": 1}),
            '{"id": "file:///m/a.flac", "ms": 9',  # killed mid-append
            "",
        ]
        self.assertEqual(play_ledger.parse_lines(lines), [])


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = DatabaseManager(os.path.join(self.dir, "t.db"))
        _run(self.db.initialize())

    def tearDown(self):
        _run(self.db.close())

    def _counts(self):
        async def q():
            conn = await self.db.get_connection()
            async with conn.execute("SELECT track_path, count FROM play_counts") as c:
                return {r[0]: r[1] for r in await c.fetchall()}
        return _run(q())

    def _events(self):
        async def q():
            conn = await self.db.get_connection()
            async with conn.execute(
                "SELECT track_path, event, listened_s FROM playback_history ORDER BY rowid"
            ) as c:
                return [tuple(r) for r in await c.fetchall()]
        return _run(q())

    def _write_ledger(self, *lines, name=play_ledger.LEDGER_NAME):
        with open(os.path.join(self.dir, name), "a") as fh:
            for ln in lines:
                fh.write(ln + "\n")

    def test_scrobble_rule(self):
        self._write_ledger(
            _line("/m/long.flac", 45_000, end=1),            # ≥30 s → counts
            _line("/m/skip.flac", 8_000, end=2),             # skipped early
            _line("/m/short.flac", 25_000, dur_ms=40_000, end=3),  # ≥ half → counts
        )
        counted = _run(play_ledger.drain_ledger(self.dir, self.db))
        self.assertEqual(counted, 2)
        self.assertEqual(self._counts(), {"/m/long.flac": 1, "/m/short.flac": 1})
        self.assertEqual([e[1] for e in self._events()],
                         ["completed", "skipped_early", "completed"])
        self.assertFalse(os.path.exists(play_ledger.ledger_path(self.dir)))

    def test_interrupted_drain_replays_without_double_count(self):
        ln = _line("/m/a.flac", 120_000, end=10)
        self._write_ledger(ln)
        _run(play_ledger.drain_ledger(self.dir, self.db))
        # Simulate a crash after commit but before the .draining file was
        # removed: the same line comes back, plus a genuinely new listen.
        self._write_ledger(ln, name=play_ledger.LEDGER_NAME + ".draining")
        self._write_ledger(_line("/m/a.flac", 150_000, end=500))
        _run(play_ledger.drain_ledger(self.dir, self.db))
        self.assertEqual(self._counts(), {"/m/a.flac": 2})
        self.assertEqual(len(self._events()), 2)

    def test_entries_reach_the_callback_in_play_order(self):
        self._write_ledger(_line("/m/a.flac", 4_000, end=1), _line("/m/b.flac", 90_000, end=2))
        seen = []
        _run(play_ledger.drain_ledger(self.dir, self.db, on_entries=seen.extend))
        self.assertEqual([e["path"] for e in seen], ["/m/a.flac", "/m/b.flac"])
        self.assertFalse(play_ledger.is_listen(seen[0]["listened_s"], seen[0]["duration_s"]))
        self.assertTrue(play_ledger.is_listen(seen[1]["listened_s"], seen[1]["duration_s"]))

    def test_no_ledger_is_noop(self):
        self.assertEqual(_run(play_ledger.drain_ledger(self.dir, self.db)), 0)


class TestTopArtistsGenres(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = DatabaseManager(os.path.join(self.dir, "t.db"))
        _run(self.db.initialize())

        async def seed():
            conn = await self.db.get_connection()
            a1 = await self.db._get_or_create_artist(conn, "Radiohead")
            a2 = await self.db._get_or_create_artist(conn, "Portishead")
            for alb_id, artist, genre, bucket in [
                (1, a1, "Alternative Rock", "Alt,Rock"),
                (2, a2, "Trip Hop", "Electronic,Trip Hop"),
                (3, a2, "unknown", "unknown"),
            ]:
                await conn.execute(
                    "INSERT INTO albums (id, artist_id, title, year, genre, genre_bucket, track_count) "
                    "VALUES (?, ?, ?, 2000, ?, ?, 1)",
                    (alb_id, artist, f"alb{alb_id}", genre, bucket),
                )
            for tid, alb_id in [(1, 1), (2, 1), (3, 2), (4, 3)]:
                await conn.execute(
                    "INSERT INTO tracks (id, album_id, title, track_num, path, duration, added_date) "
                    "VALUES (?, ?, ?, 1, ?, 200, 0)",
                    (tid, alb_id, f"t{tid}", f"/m/{tid}.flac"),
                )
            for tid, count in [(1, 3), (2, 2), (3, 6), (4, 1)]:
                await conn.execute(
                    "INSERT INTO play_counts (track_path, count, last_played) VALUES (?, ?, 0)",
                    (f"/m/{tid}.flac", count),
                )
            await conn.commit()
        _run(seed())

    def tearDown(self):
        _run(self.db.close())

    def test_artists_ranked_by_summed_plays(self):
        rows = _run(self.db.get_most_played_artists(limit=5))
        # Portishead: 6 (Dummy) + 1 (unknown-genre album) beats Radiohead's 3 + 2.
        self.assertEqual([(r["name"], r["plays"], r["tracks_played"]) for r in rows],
                         [("Portishead", 7, 2), ("Radiohead", 5, 2)])

    def test_genres_split_buckets_and_drop_junk(self):
        rows = {r["name"]: r["plays"] for r in _run(self.db.get_most_played_genres(limit=10))}
        self.assertEqual(rows, {"Alt": 5, "Rock": 5, "Electronic": 6, "Trip Hop": 6})


if __name__ == "__main__":
    unittest.main()
