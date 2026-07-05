"""Unit tests for the seed-anchored smooth-flow walk (the single queue builder).

These use an in-memory fake db_manager and cover the walk's contract:

  • seed exclusion, length bound, avoid-set, deterministic greedy selection;
  • dual-similarity ordering (0.7·current + 0.3·seed);
  • dead-end fallback to seed neighbours;
  • the metadata (genre/country) factor — on vs off;
  • the cross-community (cluster) penalty — on vs off;
  • graceful degradation when the backend lacks enrichment/cluster accessors.

The metadata/cluster factors are exercised here on tiny hand-built fixtures;
`test_walk_real_library.py` stress-tests the same walk on a real, diverse
library image where the interactions actually surface.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock utils.config so tests don't touch the user's real app dir.
import utils.config as _cfg
_cfg.APP_DIR = "/tmp/dsptest_app_dir"
os.makedirs(_cfg.APP_DIR, exist_ok=True)

from utils import track_graph as tg


class FakeDB:
    """Minimal stand-in for DatabaseManager. Stores acoustic/metadata edges,
    Louvain cluster ids, and artist enrichment. Only implements what `walk`
    touches. Enrichment/cluster maps are empty by default, so a bare FakeDB
    drives the pure acoustic dual-similarity flow (graceful degradation)."""

    def __init__(self):
        self.edges: dict = {}
        self.clusters: dict[str, int] = {}
        self.meta: dict[str, dict] = {}
        self.genre_model: dict = {}

    def add_edge(self, src, dst, weight, kind="acoustic"):
        self.edges.setdefault((src, kind), []).append({
            "path": dst, "weight": weight, "edge_kind": kind,
            "title": dst, "artist": "fake", "album": "fake",
        })

    def add_meta(self, path, artist, country, genres):
        self.meta[path] = {
            "artist": artist, "country": country, "genres": frozenset(genres),
        }

    async def get_neighbors_multi(self, path, kinds, k=30):
        pooled = []
        for kind in kinds:
            for edge in self.edges.get((path, kind), []):
                e = dict(edge)
                if e["path"] in self.clusters:
                    e["cluster_id"] = self.clusters[e["path"]]
                pooled.append(e)
        pooled.sort(key=lambda r: r["weight"], reverse=True)
        return pooled[:k]

    async def get_neighbors(self, path, k=20, edge_kind=None):
        kinds = ("acoustic", "artist", "album") if edge_kind is None else (edge_kind,)
        return await self.get_neighbors_multi(path, kinds, k)

    async def get_track_cluster(self, path):
        return self.clusters.get(path)

    async def get_artist_meta_for_paths(self, paths):
        return {p: self.meta[p] for p in paths if p in self.meta}

    async def get_genre_affinity(self):
        return self.genre_model


class NoMetaDB:
    """A backend that lacks the enrichment/cluster accessors entirely — used to
    prove the hasattr guards degrade to a pure acoustic walk without error."""

    def __init__(self):
        self.edges: dict = {}

    def add_edge(self, src, dst, weight, kind="acoustic"):
        self.edges.setdefault((src, kind), []).append(
            {"path": dst, "weight": weight, "edge_kind": kind, "title": dst}
        )

    async def get_neighbors_multi(self, path, kinds, k=30):
        pooled = []
        for kind in kinds:
            pooled.extend(dict(e) for e in self.edges.get((path, kind), []))
        pooled.sort(key=lambda r: r["weight"], reverse=True)
        return pooled[:k]


def _run(coro):
    return asyncio.run(coro)


def _chain_db():
    """SEED → A → B → C → D acoustic chain; greedy smooth walk = [A, B, C, D]."""
    db = FakeDB()
    db.add_edge("SEED", "A", 0.95)
    db.add_edge("SEED", "B", 0.85)
    db.add_edge("A", "B", 0.90)
    db.add_edge("A", "C", 0.80)
    db.add_edge("B", "C", 0.92)
    db.add_edge("B", "D", 0.70)
    db.add_edge("C", "D", 0.88)
    return db


class TestWalkContract(unittest.TestCase):
    def test_seed_excluded_and_length_bounded(self):
        out = _run(tg.walk(_chain_db(), "SEED", length=4))
        self.assertNotIn("SEED", out)
        self.assertTrue(0 < len(out) <= 4)

    def test_no_duplicates(self):
        out = _run(tg.walk(_chain_db(), "SEED", length=10))
        self.assertEqual(len(out), len(set(out)))

    def test_deterministic(self):
        db = _chain_db()
        self.assertEqual(
            _run(tg.walk(db, "SEED", length=4)),
            _run(tg.walk(db, "SEED", length=4)),
        )

    def test_greedy_dual_similarity_order(self):
        # SEED→A(.95)/B(.85); A→B(.90)/C(.80); B→C(.92)/D(.70); C→D(.88).
        # Greedy on 0.7·current+0.3·seed → A, then B, then C, then D.
        out = _run(tg.walk(_chain_db(), "SEED", length=4))
        self.assertEqual(out, ["A", "B", "C", "D"])

    def test_avoid_set_respected(self):
        db = FakeDB()
        db.add_edge("SEED", "A", 0.95)
        db.add_edge("SEED", "B", 0.90)
        db.add_edge("SEED", "E", 0.60)
        db.add_edge("E", "F", 0.90)
        out = _run(tg.walk(db, "SEED", length=5, avoid={"A", "B"}))
        self.assertTrue(set(out).isdisjoint({"A", "B"}))
        self.assertIn("E", out)  # forced onto the only un-avoided branch

    def test_dead_end_falls_back_to_seed_neighbours(self):
        # A is a dead end (no out-edges); the walk should recover via the seed's
        # neighbour pool rather than terminating after one step.
        db = FakeDB()
        db.add_edge("SEED", "A", 0.90)
        db.add_edge("SEED", "B", 0.80)
        out = _run(tg.walk(db, "SEED", length=2))
        self.assertEqual(out, ["A", "B"])


class TestWalkGracefulDegradation(unittest.TestCase):
    def test_walk_runs_without_meta_or_cluster_accessors(self):
        db = NoMetaDB()
        db.add_edge("SEED", "A", 0.95)
        db.add_edge("A", "C", 0.90)
        # meta_lambda/cluster_lambda > 0 but the backend can't serve them:
        # the factors must collapse to 1.0 and the walk still flows.
        out = _run(tg.walk(db, "SEED", length=2, meta_lambda=0.5, cluster_lambda=0.5))
        self.assertEqual(out, ["A", "C"])

    def test_empty_enrichment_is_a_noop(self):
        # FakeDB has the accessors but no enrichment rows → metadata factor 1.0.
        out = _run(tg.walk(_chain_db(), "SEED", length=4, meta_lambda=2.0))
        self.assertEqual(out, ["A", "B", "C", "D"])


class TestWalkClusterPenalty(unittest.TestCase):
    def _db(self):
        # SEED→A(1.0); A→C(0.90, same cluster) / D(0.95, different cluster).
        db = FakeDB()
        db.add_edge("SEED", "A", 1.0)
        db.add_edge("A", "C", 0.90)
        db.add_edge("A", "D", 0.95)
        db.clusters.update({"SEED": 0, "A": 0, "C": 0, "D": 1})
        return db

    def test_penalty_keeps_walk_in_community(self):
        # D has higher raw affinity but leaves A's community; with a strong
        # penalty C (same community) must win.
        out = _run(tg.walk(self._db(), "SEED", length=2, cluster_lambda=0.9,
                           meta_lambda=0.0))
        self.assertEqual(out, ["A", "C"])

    def test_no_penalty_lets_higher_affinity_win(self):
        # cluster_lambda=0 → D's higher raw affinity wins.
        out = _run(tg.walk(self._db(), "SEED", length=2, cluster_lambda=0.0,
                           meta_lambda=0.0))
        self.assertEqual(out, ["A", "D"])


class TestWalkMetadataFactor(unittest.TestCase):
    def _db(self):
        # From SEED: G (genre-match, lower affinity) vs F (genre-foreign, higher
        # affinity). Metadata should let G overtake F.
        db = FakeDB()
        db.add_edge("SEED", "G", 0.80)
        db.add_edge("SEED", "F", 0.90)
        db.add_meta("SEED", "S", "US", ["hiphop"])
        db.add_meta("G", "Gee", "US", ["hiphop"])   # shares genre with seed
        db.add_meta("F", "Eff", "US", ["rock"])     # disjoint genre
        return db

    def test_metadata_overtakes_higher_affinity_foreign_track(self):
        out = _run(tg.walk(self._db(), "SEED", length=1, meta_lambda=2.0,
                           cluster_lambda=0.0))
        self.assertEqual(out[0], "G")

    def test_metadata_off_lets_raw_affinity_win(self):
        out = _run(tg.walk(self._db(), "SEED", length=1, meta_lambda=0.0,
                           cluster_lambda=0.0))
        self.assertEqual(out[0], "F")

    def test_same_artist_is_not_boosted(self):
        # G is genre-match but SAME artist as seed → meta_score returns 0, so it
        # gets no boost and the higher-affinity F wins even with metadata on.
        db = self._db()
        db.add_meta("G", "S", "US", ["hiphop"])  # same artist as SEED
        out = _run(tg.walk(db, "SEED", length=1, meta_lambda=2.0, cluster_lambda=0.0))
        self.assertEqual(out[0], "F")


if __name__ == "__main__":
    unittest.main()
