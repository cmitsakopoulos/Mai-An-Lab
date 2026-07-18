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

    async def _seed(self, artist, album_genre, genres, status="ok"):
        conn = await self.db.get_connection()
        aid = await self.db._get_or_create_artist(conn, artist)
        await conn.execute(
            "INSERT INTO albums (artist_id, title, genre) VALUES (?, ?, ?)",
            (aid, f"{artist} LP", album_genre),
        )
        await conn.commit()
        await self.db.upsert_artist_enrichment(artist, genres=genres, status=status)

    async def _seed_discography(self, artist, album_genres, genres=None, status="ok"):
        """Seed one artist with several albums, each carrying its own source tag."""
        conn = await self.db.get_connection()
        aid = await self.db._get_or_create_artist(conn, artist)
        for i, g in enumerate(album_genres):
            await conn.execute(
                "INSERT INTO albums (artist_id, title, genre) VALUES (?, ?, ?)",
                (aid, f"{artist} LP{i}", g),
            )
        await conn.commit()
        if genres is not None:
            await self.db.upsert_artist_enrichment(artist, genres=genres, status=status)

    async def _albums(self, artist):
        conn = await self.db.get_connection()
        async with conn.execute(
            "SELECT al.title, al.genre, al.genre_bucket FROM albums al "
            "JOIN artists ar ON ar.id = al.artist_id WHERE ar.name = ? "
            "ORDER BY al.title",
            (artist,),
        ) as cur:
            return await cur.fetchall()

    def _buckets(self, rows):
        return [set((r[2] or "").split(",")) - {""} for r in rows]

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

    def test_omission_is_noise_addition_is_signal(self):
        """Slipknot's `Iowa` is tagged bare 'Rock' while the rest of the
        discography is 'Nu Metal' — the source dropped a tag, the record didn't
        change. Gojira's `Fortitude` ('Pop, Rock') sits beside four albums
        tagged '{Metal, Pop, Rock}'. Both must recover the missing family from
        the artist consensus, while an album that genuinely ADDS a family keeps
        it."""

        async def scenario():
            await self._seed_discography("Slipknot", [
                "Nu Metal", "Nu Metal", "Nu Metal", "Nu Metal", "Rock",
            ])
            await self._seed_discography("Gojira", [
                "Rock, Metal, Pop", "Pop, Metal, Rock", "Metal, Rock, Pop",
                "Metal, Pop, Rock", "Pop, Rock",
            ])
            # An album that ADDS a family the artist doesn't otherwise carry.
            await self._seed_discography("Experimentalist", [
                "Metal", "Metal", "Metal", "Jazz, Electronic",
            ])
            await self.db.fix_and_normalize_track_genres()

            rows = await self._albums("Slipknot")
            for r in rows:
                self.assertIn("Metal", (r[2] or ""),
                              f"{r[0]} lost Metal: bucket={r[2]}")
            # No album is fenced off into a disjoint family.
            bks = self._buckets(rows)
            for i in range(len(bks)):
                for j in range(i + 1, len(bks)):
                    self.assertTrue(bks[i] & bks[j])

            rows = await self._albums("Gojira")
            for r in rows:
                self.assertIn("Metal", (r[2] or ""),
                              f"{r[0]} lost Metal: bucket={r[2]}")

            rows = await self._albums("Experimentalist")
            odd = [r for r in rows if "Jazz" in (r[1] or "")][0]
            self.assertIn("Jazz", odd[2], "an added family must be preserved")
            self.assertIn("Electronic", odd[2])
            self.assertIn("Metal", odd[2], "and the core family still applies")

        _run(scenario())

    def test_placeholder_and_untagged_albums_inherit_the_artist(self):
        """'Divers' is Qobuz's French placeholder; it was mistaken for a real
        genre and split Greek rappers into a phantom 'Other' family. An artist
        with a single tagged album must still fill its untagged siblings."""

        async def scenario():
            await self._seed_discography("Daima", ["Rap", "Divers", "Divers"])
            await self._seed_discography("Sparse", ["Rap", None, ""])
            await self.db.fix_and_normalize_track_genres()

            for artist in ("Daima", "Sparse"):
                rows = await self._albums(artist)
                for r in rows:
                    self.assertEqual(
                        set((r[2] or "").split(",")) - {""}, {"Hip-Hop"},
                        f"{artist}/{r[0]} bucket={r[2]}")

        _run(scenario())

    def test_lowconfidence_enrichment_never_rewrites_the_display(self):
        """A Greek rapper resolved to a Japanese j-core act. The old code
        re-derived the display tag from that row, overwriting a correct 'Hip-Hop'
        source tag with 'J-Core'."""

        async def scenario():
            await self._seed("GreekRapper", "Hip-Hop",
                             _tags(("j-core", 1)), status="lowconfidence")
            await self.db.fix_and_normalize_track_genres()
            g, b = await self._album("GreekRapper")
            self.assertEqual(g, "Hip-Hop", "untrusted match must not rewrite display")
            self.assertIn("Hip-Hop", b or "")

        _run(scenario())


if __name__ == "__main__":
    unittest.main()
