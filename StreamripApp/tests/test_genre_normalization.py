"""Integration tests for db_manager.fix_and_normalize_track_genres.

The rewrite is NON-DESTRUCTIVE: `albums.genre` becomes the human display tag
(specific tags are kept; empty/placeholder/collapse-artifact values are
re-derived, but ONLY to a tag inside the consensus family so display never
contradicts the bucket or drifts into an off-genre top tag), and the coarse
family is persisted separately in `albums.genre_bucket`.

Uses a real on-disk SQLite DB so the migration adds the genre_bucket column."""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.db_manager import DatabaseManager


def _run(coro):
    return asyncio.run(coro)


def _tags(*pairs):
    return [{"name": n, "count": c} for n, c in pairs]


class TestGenreNormalization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DatabaseManager(self.tmp.name)
        _run(self.db.initialize())

    def tearDown(self):
        _run(self.db.close())
        os.unlink(self.tmp.name)

    async def _seed(self, artist, album_genre, genres):
        conn = await self.db.get_connection()
        aid = await self.db._get_or_create_artist(conn, artist)
        await conn.execute(
            "INSERT INTO albums (artist_id, title, genre) VALUES (?, ?, ?)",
            (aid, f"{artist} LP", album_genre),
        )
        await conn.commit()
        await self.db.upsert_artist_enrichment(artist, genres=genres, status="ok")

    async def _album(self, artist):
        conn = await self.db.get_connection()
        async with conn.execute(
            "SELECT al.genre, al.genre_bucket FROM albums al "
            "JOIN artists ar ON ar.id = al.artist_id WHERE ar.name = ?",
            (artist,),
        ) as cur:
            row = await cur.fetchone()
        return row[0], row[1]

    def test_non_destructive_and_consensus_rederivation(self):
        from utils.pca_engine import genre_bucket

        async def scenario():
            # 1) A genuinely specific tag is KEPT; bucket is set alongside it.
            await self._seed("SpecificA", "Trap", _tags(("trap", 5), ("hip hop", 3)))
            # 2) A collapse-artifact bucket label is re-derived to the top tag of
            #    the consensus family (grime outranks 'hip hop').
            await self._seed("CollapsedA", "Hip-Hop", _tags(("grime", 10), ("hip hop", 4)))
            # 3) A noisy off-genre top tag must NOT win — it isn't in the consensus.
            await self._seed("NoisyA", "Hip-Hop", _tags(("psychobilly", 10), ("hip hop", 4)))
            # 4) An empty placeholder is back-filled from the consensus family.
            await self._seed("EmptyA", "", _tags(("deep house", 7)))
            # 5) A bucket label with NO family consensus (all Other) is left as-is.
            await self._seed("NoConsensusA", "Hip-Hop", _tags(("psychobilly", 5)))

            summary = await self.db.fix_and_normalize_track_genres()
            self.assertEqual(summary["scanned"], 5)

            g, b = await self._album("SpecificA")
            self.assertEqual(g, "Trap")                 # specific tag preserved
            self.assertEqual(b, "Hip-Hop")              # bucket persisted alongside

            g, b = await self._album("CollapsedA")
            self.assertEqual(g, "Grime")                # nuance restored
            self.assertEqual(b, "Hip-Hop")
            self.assertEqual(genre_bucket(g), b)        # display never contradicts bucket

            g, b = await self._album("NoisyA")
            self.assertNotEqual(g, "Psychobilly")       # noise rejected
            self.assertEqual(b, "Hip-Hop")
            self.assertEqual(genre_bucket(g), "Hip-Hop")

            g, b = await self._album("EmptyA")
            self.assertEqual(g, "Deep House")           # placeholder back-filled
            self.assertEqual(b, "Electronic")

            g, b = await self._album("NoConsensusA")
            self.assertEqual(g, "Hip-Hop")              # kept: no family to trust
            self.assertEqual(b, "Hip-Hop")

        _run(scenario())


if __name__ == "__main__":
    unittest.main()
