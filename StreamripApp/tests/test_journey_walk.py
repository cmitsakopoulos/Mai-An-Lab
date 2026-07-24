"""Integration test: walk() is now the genre-adjacency JOURNEY, with the
seed-affinity radius as the fallback.

Uses a real DatabaseManager on a temp file (like test_live_graph) with
interspersed Hip-Hop ↔ Pop (genuinely PAGA-adjacent) plus a distant Metal
cluster, each with enrichment. Proves:

  • build_journey_graph persists regional-aware nodes + adjacency;
  • after a build, a Hip-Hop seed's queue TRAVELS into the adjacent Pop (a genre
    the radius pool-gate would have fenced out entirely);
  • with no graph built, walk() falls back to the radius and stays in-genre;
  • build degrades to 0 on a backend without the accessors.

TestNodeFence additionally proves the fences that stop an acoustically-close
foreign genre leaking into the queue when a partition exists.
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import utils.config as _cfg
_cfg.APP_DIR = os.path.join(os.path.dirname(__file__), "journey_app_dir")
os.makedirs(_cfg.APP_DIR, exist_ok=True)

from utils.db_manager import DatabaseManager
from utils import track_graph as tg


def _cone(center_deg, n, spread, seed):
    rng = np.random.default_rng(seed)
    th = np.deg2rad(rng.normal(center_deg, spread, n))
    # 4-D: two informative dims (the cone) + tiny noise to break ties
    pad = rng.normal(0, 0.02, (n, 2))
    return np.c_[np.cos(th), np.sin(th), pad].astype(np.float32)


class TestJourneyWalk(unittest.IsolatedAsyncioTestCase):
    def _cleanup(self):
        for s in ["", "-shm", "-wal"]:
            p = self.db_path + s
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    async def asyncSetUp(self):
        self.db_path = os.path.join(os.path.dirname(__file__), "test_journey_walk.db")
        self._cleanup()
        self.db = DatabaseManager(self.db_path)
        await self.db.initialize()
        tg.invalidate_coord_graph_cache()
        conn = await self.db.get_connection()
        await self.db._migrate_clusters(conn)

        # Hip-Hop and Pop are INTERSPERSED in one acoustic region (tag-distinct
        # but geometrically overlapping — like real rap ↔ Greek-rap), which is
        # what makes them genuinely PAGA-adjacent. A distant Metal cluster is the
        # baseline sink: with only TWO nodes each is the other's sole candidate,
        # the expected cross-rate saturates, and no pair can ever exceed chance
        # (lift ≥ 1) — so a third, separated node is required for real adjacency.
        # One artist/album per track so repeat caps never truncate the queue.
        self.n = 15
        hip = _cone(0, self.n, 12, 1)
        pop = _cone(2, self.n, 12, 7)
        met = _cone(150, self.n, 12, 3)
        self.coords = np.vstack([hip, pop, met])
        self.paths = ([f"/m/hip_{i}.flac" for i in range(self.n)] +
                      [f"/m/pop_{i}.flac" for i in range(self.n)] +
                      [f"/m/met_{i}.flac" for i in range(self.n)])
        genres = (['[{"name": "hip hop", "count": 5}, {"name": "trap", "count": 3}]'] * self.n +
                  ['[{"name": "pop", "count": 5}, {"name": "dance-pop", "count": 2}]'] * self.n +
                  ['[{"name": "metal", "count": 5}, {"name": "heavy metal", "count": 2}]'] * self.n)

        for i, path in enumerate(self.paths):
            aid = i + 1
            name = f"Artist {i}"
            await conn.execute("INSERT OR IGNORE INTO artists (id, name) VALUES (?, ?)", (aid, name))
            await conn.execute(
                "INSERT OR IGNORE INTO albums (id, artist_id, title, genre) VALUES (?, ?, ?, 'x')",
                (aid, aid, f"Album {i}"),
            )
            await conn.execute(
                "INSERT INTO tracks (path, title, duration, album_id) VALUES (?, ?, 180.0, ?)",
                (path, f"Track {i}", aid),
            )
            await conn.execute(
                "INSERT INTO play_counts (track_path, count, pca_coords) VALUES (?, 0, ?)",
                (path, self.coords[i].tobytes()),
            )
            await conn.execute(
                "INSERT INTO artist_enrichment (artist_name, country, genres, source, status) "
                "VALUES (?, 'US', ?, 'musicbrainz', 'ok')",
                (name, genres[i]),
            )
        await conn.commit()
        await tg.build_genre_affinity(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._cleanup()
        tg.invalidate_coord_graph_cache()

    def _node_of(self, payload):
        return payload.get("nodes", {})

    async def test_build_persists_nodes_and_adjacency(self):
        n = await tg.build_journey_graph(self.db)
        self.assertEqual(n, 3 * self.n)
        payload = await self.db.get_journey_graph()
        nodes = self._node_of(payload)
        self.assertEqual(len(nodes), 3 * self.n)
        self.assertEqual(nodes["/m/hip_0.flac"], "Hip-Hop")
        self.assertEqual(nodes["/m/pop_0.flac"], "Pop")            # count-weighted, not folded
        self.assertEqual(nodes["/m/met_0.flac"], "Metal")
        # Interspersed Hip-Hop ↔ Pop are genuinely adjacent; the distant Metal is
        # the baseline sink that lets their cross-rate exceed chance.
        adj = payload.get("adj") or {}
        self.assertIn("Pop", [b for b, _ in adj.get("Hip-Hop", [])])

    async def test_walk_travels_across_genres_after_build(self):
        await tg.build_journey_graph(self.db)
        tg.invalidate_coord_graph_cache()
        payload = await self.db.get_journey_graph()
        nodes = self._node_of(payload)

        queue = await tg.walk(self.db, self.paths[0], length=8)   # a Hip-Hop seed
        self.assertEqual(len(queue), 8)
        self.assertNotIn(self.paths[0], queue)
        genres = [nodes.get(p) for p in queue]
        self.assertIn("Pop", genres, "journey did not cross into the adjacent genre")
        self.assertIn("Hip-Hop", genres, "journey abandoned the seed genre entirely")

    async def test_radius_fallback_stays_in_genre(self):
        # No journey graph built: walk() must still return a queue, and the
        # pool-gate keeps it inside the seed's genre (never crosses to Pop).
        empty = await self.db.get_journey_graph()
        self.assertFalse(empty.get("nodes"))                       # nothing built yet
        queue = await tg.walk(self.db, self.paths[0], length=8)
        self.assertTrue(queue)
        self.assertNotIn(self.paths[0], queue)
        self.assertTrue(all(p.startswith("/m/hip_") for p in queue),
                        "radius fallback leaked across the genre gate")

    async def test_build_degrades_without_accessors(self):
        class Bare:
            pass
        self.assertEqual(await tg.build_journey_graph(Bare()), 0)


class TestNodeFence(unittest.IsolatedAsyncioTestCase):
    """The radius fallback and the journey's no-adjacency degradation must NOT
    leak an acoustically-close foreign genre into the queue when a partition
    exists — even for an UNTAGGED foreign candidate the tag gate cannot fence.

    Reproduces the Hip-Hop→Electronic jump: the acoustic geometry places foreign
    genres right next to a seed, and the tag gate needs genres on both sides, so
    an untagged foreign neighbour used to ride straight into the fallback queue.
    The propagated node partition places every track, so it fences them.
    """
    def _cleanup(self):
        for s in ["", "-shm", "-wal"]:
            p = self.db_path + s
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    async def asyncSetUp(self):
        self.db_path = os.path.join(os.path.dirname(__file__), "test_node_fence.db")
        self._cleanup()
        self.db = DatabaseManager(self.db_path)
        await self.db.initialize()
        tg.invalidate_coord_graph_cache()
        conn = await self.db.get_connection()
        await self.db._migrate_clusters(conn)

        # 10 tagged Hip-Hop tracks in a tight cone, plus ONE UNTAGGED foreign
        # track placed as the seed's nearest acoustic neighbour — pure cosine
        # would rank it first, and being untagged the pool-gate cannot fence it.
        self.n = 10
        hip = _cone(0, self.n, 8, 3)
        self.foreign_path = "/m/foreign_0.flac"
        self.hip_paths = [f"/m/hip_{i}.flac" for i in range(self.n)]
        self.paths = self.hip_paths + [self.foreign_path]
        foreign = (hip[0] + np.array([0.001, 0.001, 0.0, 0.0], dtype=np.float32))[None, :]
        self.coords = np.vstack([hip, foreign])

        for i, path in enumerate(self.paths):
            aid = i + 1
            name = f"Artist {i}"
            await conn.execute("INSERT OR IGNORE INTO artists (id, name) VALUES (?, ?)", (aid, name))
            await conn.execute(
                "INSERT OR IGNORE INTO albums (id, artist_id, title, genre) VALUES (?, ?, ?, 'x')",
                (aid, aid, f"Album {i}"),
            )
            await conn.execute(
                "INSERT INTO tracks (path, title, duration, album_id) VALUES (?, ?, 180.0, ?)",
                (path, f"Track {i}", aid),
            )
            await conn.execute(
                "INSERT INTO play_counts (track_path, count, pca_coords) VALUES (?, 0, ?)",
                (path, self.coords[i].tobytes()),
            )
            # The foreign track is deliberately left UNENRICHED (no tags), so the
            # tag gate has no evidence to fence it — only the node fence can.
            if path != self.foreign_path:
                await conn.execute(
                    "INSERT INTO artist_enrichment (artist_name, country, genres, source, status) "
                    "VALUES (?, 'US', ?, 'musicbrainz', 'ok')",
                    (name, '[{"name": "hip hop", "count": 5}, {"name": "trap", "count": 3}]'),
                )
        await conn.commit()

        # Hand-build the partition: the foreign track is its own node so the
        # fence can recognise it (a real build would propagate it INTO Hip-Hop,
        # since all its neighbours are Hip-Hop — that is the point of the fence).
        self._nodes = {p: "Hip-Hop" for p in self.hip_paths}
        self._nodes[self.foreign_path] = "Electronic"

    async def asyncTearDown(self):
        await self.db.close()
        self._cleanup()
        tg.invalidate_coord_graph_cache()

    async def test_radius_fallback_fences_untagged_foreign(self):
        # adj empty ⇒ journey returns None ⇒ walk uses the radius fallback, with
        # the partition available to the node fence.
        await self.db.save_journey_graph({"version": 1, "nodes": self._nodes, "adj": {}})
        tg.invalidate_coord_graph_cache()
        queue = await tg.walk(self.db, self.hip_paths[0], length=8)
        self.assertEqual(len(queue), 8)
        self.assertNotIn(self.foreign_path, queue,
                         "radius fence let an untagged foreign neighbour leak in")
        self.assertTrue(all(p.startswith("/m/hip_") for p in queue))

    async def test_journey_no_adjacency_stays_in_node(self):
        # adj present but the seed node has NO exits ⇒ journey runs and degrades;
        # the degradation must stay in the seed's node, not fill from any node.
        await self.db.save_journey_graph(
            {"version": 1, "nodes": self._nodes, "adj": {"Hip-Hop": []}}
        )
        tg.invalidate_coord_graph_cache()
        queue = await tg.walk(self.db, self.hip_paths[0], length=8)
        self.assertTrue(queue)
        self.assertNotIn(self.foreign_path, queue,
                         "journey degradation leaked into a foreign node")


if __name__ == "__main__":
    unittest.main()
