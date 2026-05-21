"""Random-walk tests using an in-memory fake db_manager.

Covers the new behaviours: avoid-set respect, restart probability anchoring,
multi-tier edge pooling, and softmax-temperature determinism with a fixed
RNG seed. The diversity term is exercised implicitly by the multi-cluster
fixture.
"""

import asyncio
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Provide a no-op utils.config so dsp / track_graph import cleanly without
# touching disk for the custom-moods file.
import types as _types
_cfg = _types.ModuleType("utils.config")
_cfg.APP_DIR = "/tmp/dsptest_app_dir"
os.makedirs(_cfg.APP_DIR, exist_ok=True)
sys.modules["utils.config"] = _cfg

from utils import track_graph as tg
from utils.dsp import EMBED_DIMS

import numpy as np


def _blob(vec: np.ndarray) -> bytes:
    """Pack a length-EMBED_DIMS float32 vector as a timbre BLOB."""
    assert vec.shape == (EMBED_DIMS,)
    return vec.astype("<f4").tobytes()


class FakeDB:
    """Minimal stand-in for DatabaseManager used by tg.walk + tracks_by_mood.

    Stores edges as {(src, kind): [{path, weight, edge_kind, title, artist,
    album}]} and timbres as {path: bytes}. Only the methods the walker /
    mood scorer call are implemented."""

    def __init__(self):
        self.edges: dict = {}
        self.timbres: dict[str, bytes] = {}
        self.signal: dict[str, float] = {}
        self.recent: set[str] = set()

    def add_edge(self, src: str, dst: str, weight: float, kind: str):
        self.edges.setdefault((src, kind), []).append({
            "path": dst,
            "weight": weight,
            "edge_kind": kind,
            "title": dst,
            "artist": "fake",
            "album": "fake",
        })

    def add_timbre(self, path: str, vec: np.ndarray):
        self.timbres[path] = _blob(vec)

    async def get_neighbors_multi(self, path, kinds, k=30):
        pooled: list[dict] = []
        for kind in kinds:
            pooled.extend(self.edges.get((path, kind), []))
        # Sort by weight desc to mimic the real SQL ORDER BY n.weight DESC.
        pooled.sort(key=lambda r: r["weight"], reverse=True)
        return pooled[:k]

    async def get_neighbors(self, path, k=20, edge_kind=None):
        if edge_kind is None:
            kinds = ("acoustic", "artist", "album")
        else:
            kinds = (edge_kind,)
        return await self.get_neighbors_multi(path, kinds, k)

    async def get_embeddings_for_paths(self, paths):
        return {p: self.timbres[p] for p in paths if p in self.timbres}

    async def listen_signal_map(self):
        return dict(self.signal)

    async def recent_played_paths(self, window_seconds=0):
        return set(self.recent)


def _run(coro):
    return asyncio.run(coro)


class TestWalkBasics(unittest.TestCase):
    def _simple_db(self):
        """Linear chain SEED → A → B → C → D, all acoustic, weight ramps down."""
        db = FakeDB()
        db.add_edge("SEED", "A", 0.95, "acoustic")
        db.add_edge("SEED", "B", 0.85, "acoustic")
        db.add_edge("A", "B", 0.90, "acoustic")
        db.add_edge("A", "C", 0.80, "acoustic")
        db.add_edge("B", "C", 0.92, "acoustic")
        db.add_edge("B", "D", 0.70, "acoustic")
        db.add_edge("C", "D", 0.88, "acoustic")
        return db

    def test_seed_not_in_output(self):
        db = self._simple_db()
        out = _run(tg.walk(db, "SEED", length=4, seed_rng=random.Random(0),
                           diversity_lambda=0.0, restart_prob=0.0))
        self.assertNotIn("SEED", out)
        self.assertTrue(0 < len(out) <= 4)

    def test_avoid_set_respected(self):
        db = self._simple_db()
        out = _run(tg.walk(db, "SEED", length=5,
                           avoid={"A", "B"},
                           seed_rng=random.Random(0),
                           diversity_lambda=0.0,
                           restart_prob=0.0))
        for p in out:
            self.assertNotIn(p, {"A", "B"})

    def test_deterministic_with_fixed_rng(self):
        db = self._simple_db()
        out1 = _run(tg.walk(db, "SEED", length=4,
                            seed_rng=random.Random(42),
                            diversity_lambda=0.0,
                            restart_prob=0.0))
        out2 = _run(tg.walk(db, "SEED", length=4,
                            seed_rng=random.Random(42),
                            diversity_lambda=0.0,
                            restart_prob=0.0))
        self.assertEqual(out1, out2)

    def test_restart_one_anchors_to_seed_neighbours(self):
        """restart_prob=1.0 ⇒ every step starts from seed, so every output
        path must be a direct neighbour of the seed."""
        db = self._simple_db()
        seed_nbrs = {"A", "B"}
        for trial in range(5):
            out = _run(tg.walk(db, "SEED", length=2,
                               seed_rng=random.Random(trial),
                               diversity_lambda=0.0,
                               restart_prob=1.0))
            for p in out:
                self.assertIn(p, seed_nbrs,
                              f"trial {trial}: {p} not in {seed_nbrs}")


class TestWalkMultiTier(unittest.TestCase):
    def test_artist_neighbours_used_when_seed_lacks_acoustic(self):
        """Seed has no acoustic edges, only artist. Walker should still
        produce a path via the artist tier (legacy walker dead-ended here
        unless the call site manually fell back)."""
        db = FakeDB()
        db.add_edge("SEED", "X", 1.0, "artist")
        db.add_edge("X", "Y", 1.0, "artist")
        out = _run(tg.walk(db, "SEED", length=2,
                           edge_kinds=("acoustic", "artist"),
                           seed_rng=random.Random(0),
                           diversity_lambda=0.0,
                           restart_prob=0.0))
        self.assertTrue(out, "walk should not dead-end when artist tier has edges")
        self.assertEqual(out[0], "X")

    def test_acoustic_outweighs_artist_at_equal_raw_weight(self):
        """When both tiers offer the same candidate, the effective weight
        should reflect the per-tier multiplier (acoustic 1.0 > artist 0.4),
        so at low temperature the acoustic-only neighbour wins."""
        db = FakeDB()
        # ACO is acoustic-only, ART is artist-only. Pure tier comparison.
        db.add_edge("SEED", "ACO", 0.90, "acoustic")
        db.add_edge("SEED", "ART", 0.90, "artist")
        # Run many trials with low temperature; ACO must dominate.
        wins_aco = 0
        trials = 50
        for t in range(trials):
            out = _run(tg.walk(db, "SEED", length=1,
                               edge_kinds=("acoustic", "artist"),
                               seed_rng=random.Random(t),
                               diversity_lambda=0.0,
                               restart_prob=0.0,
                               temperature=0.05))
            if out and out[0] == "ACO":
                wins_aco += 1
        self.assertGreater(wins_aco, trials * 0.85,
                           f"acoustic should dominate at low temperature; got {wins_aco}/{trials}")


class TestWalkDiversity(unittest.TestCase):
    def test_diversity_prefers_different_cluster(self):
        """Two clusters of timbres. The walker should be willing to leave
        cluster A under a strong diversity penalty rather than staying
        inside it."""
        db = FakeDB()
        # Cluster A timbres clustered around +1 on dim 0.
        rng = np.random.default_rng(0)
        for name in ("A1", "A2", "A3"):
            v = np.zeros(EMBED_DIMS, dtype=np.float32)
            v[0] = 1.0
            v += rng.normal(0, 0.01, EMBED_DIMS).astype(np.float32)
            db.add_timbre(name, v)
        # Cluster B timbres clustered around -1 on dim 0.
        for name in ("B1", "B2"):
            v = np.zeros(EMBED_DIMS, dtype=np.float32)
            v[0] = -1.0
            v += rng.normal(0, 0.01, EMBED_DIMS).astype(np.float32)
            db.add_timbre(name, v)
        # SEED is in cluster A.
        v = np.zeros(EMBED_DIMS, dtype=np.float32)
        v[0] = 1.0
        db.add_timbre("SEED", v)
        # All candidates are direct neighbours of SEED with equal raw weight.
        for name in ("A1", "A2", "A3", "B1", "B2"):
            db.add_edge("SEED", name, 0.9, "acoustic")

        # No diversity: with equal weights and low temp, picks essentially
        # uniformly across the 5 candidates — cluster B chosen ~40% of trials.
        # WITH diversity: after picking one A-cluster candidate, subsequent
        # picks should swing toward B.
        b_seen = 0
        trials = 30
        for t in range(trials):
            out = _run(tg.walk(db, "SEED", length=2,
                               edge_kinds=("acoustic",),
                               seed_rng=random.Random(t),
                               diversity_lambda=2.0,  # very strong
                               restart_prob=0.0,
                               temperature=0.05))
            if any(p.startswith("B") for p in out):
                b_seen += 1
        self.assertGreater(b_seen, trials * 0.5,
                           f"diversity should bias toward cluster B; got {b_seen}/{trials}")


if __name__ == "__main__":
    unittest.main()
