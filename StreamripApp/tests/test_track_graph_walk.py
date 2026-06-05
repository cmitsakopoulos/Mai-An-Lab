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

# Provide a mocked utils.config so tests don't touch the user's actual custom-moods file
import utils.config as _cfg
_cfg.APP_DIR = "/tmp/dsptest_app_dir"
os.makedirs(_cfg.APP_DIR, exist_ok=True)

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
        self.clusters: dict[str, int] = {}

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
            for edge in self.edges.get((path, kind), []):
                e = dict(edge)
                if e["path"] in self.clusters:
                    e["cluster_id"] = self.clusters[e["path"]]
                pooled.append(e)
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

    async def get_track_cluster(self, path):
        return self.clusters.get(path)

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


class TestWalkDiversityStepDecay(unittest.TestCase):
    """The MMR diversity penalty decays by step index (1/(1+step)) so the
    walker isn't pushed structurally away from its cluster late in a chain.
    Step 0 still gets the full nudge; by step ~5 the penalty is ~λ/6."""

    def test_decay_reduces_late_step_penalty(self):
        # Construct a 2-cluster timbre fixture where SEED is in cluster A and
        # cluster A's three members are all SEED's direct neighbours with
        # equal raw weight. A high diversity_lambda would, without decay,
        # force every late-walk pick into cluster B regardless of how many
        # A candidates remain. With decay, the walker should still find
        # remaining A candidates after the first 1-2 picks.
        db = FakeDB()
        rng = np.random.default_rng(0)
        # Six A-cluster timbres (excess of A so a diversity-only walker
        # would still have unvisited A targets even after 3 picks).
        a_names = ["A1", "A2", "A3", "A4", "A5", "A6"]
        for name in a_names:
            v = np.zeros(EMBED_DIMS, dtype=np.float32)
            v[0] = 1.0
            v += rng.normal(0, 0.01, EMBED_DIMS).astype(np.float32)
            db.add_timbre(name, v)
        # Two B-cluster timbres to provide a clear escape route — but A is
        # what we expect the *late* walk to still touch thanks to decay.
        for name in ("B1", "B2"):
            v = np.zeros(EMBED_DIMS, dtype=np.float32)
            v[0] = -1.0
            v += rng.normal(0, 0.01, EMBED_DIMS).astype(np.float32)
            db.add_timbre(name, v)
        v = np.zeros(EMBED_DIMS, dtype=np.float32)
        v[0] = 1.0
        db.add_timbre("SEED", v)
        for name in a_names + ["B1", "B2"]:
            db.add_edge("SEED", name, 0.9, "acoustic")

        # Count how often the *fourth* pick lands in A. Without decay (constant
        # λ), repeated picks would compound into λ * len(visited) ≈ 8 — every
        # A candidate gets a brutal penalty and B wins. With decay, step 3's
        # effective λ is λ/4, so unvisited A members can still be selected.
        a_late_hits = 0
        trials = 30
        for t in range(trials):
            out = _run(tg.walk(db, "SEED", length=4,
                               edge_kinds=("acoustic",),
                               seed_rng=random.Random(t),
                               diversity_lambda=2.0,
                               restart_prob=0.0,
                               temperature=0.05))
            if len(out) >= 4 and out[3].startswith("A"):
                a_late_hits += 1
        # Without step decay the late-walk A-rate would be near-zero. With
        # decay it should be meaningfully non-zero. Loose threshold so the
        # test is robust to fixture re-seeding.
        self.assertGreater(
            a_late_hits, trials * 0.15,
            f"step decay should keep late-walk A picks possible; got {a_late_hits}/{trials}",
        )


class TestWalkLogitStandardisation(unittest.TestCase):
    """At fixed temperature, the per-node z-scoring of logits should give
    HUB-style nodes (neighbours bunched near 1.0) and OUTLIER-style nodes
    (neighbours spread thinly) the same selection-entropy profile."""

    def test_temperature_consistent_across_density(self):
        # Two source nodes with very different raw-weight spreads. Before
        # standardisation, the same `temperature` would read as near-greedy
        # at HUB (tight spread → tiny scaled differences) and near-uniform
        # at OUTLIER (wide spread → large scaled differences). After
        # standardisation both spreads collapse to ~[1.22, 0, -1.22] so
        # the temperature has consistent semantics.
        db = FakeDB()
        db.add_edge("HUB", "N1", 0.99, "acoustic")
        db.add_edge("HUB", "N2", 0.98, "acoustic")
        db.add_edge("HUB", "N3", 0.97, "acoustic")
        db.add_edge("OUTLIER", "M1", 0.40, "acoustic")
        db.add_edge("OUTLIER", "M2", 0.30, "acoustic")
        db.add_edge("OUTLIER", "M3", 0.20, "acoustic")

        trials = 60

        def top_pick_rate(seed_path: str, top: str) -> float:
            hits = 0
            for t in range(trials):
                out = _run(tg.walk(
                    db, seed_path, length=1,
                    edge_kinds=("acoustic",),
                    seed_rng=random.Random(t),
                    diversity_lambda=0.0,
                    restart_prob=0.0,
                    temperature=0.05,
                ))
                if out and out[0] == top:
                    hits += 1
            return hits / trials

        hub_top = top_pick_rate("HUB", "N1")
        outlier_top = top_pick_rate("OUTLIER", "M1")
        # The top-1 pick rates should be comparable — both walks behave
        # "near-greedy at the same temperature" because the z-score made
        # the relative spreads identical.
        self.assertGreater(hub_top, 0.85)
        self.assertGreater(outlier_top, 0.85)


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


class TestWalkFallbackConstraints(unittest.TestCase):
    def test_fallback_cluster_penalty(self):
        # SEED -> A -> B -> C (same cluster) and D (diff cluster)
        # B's candidates C and D are fetched via fallback query.
        db = FakeDB()
        db.add_edge("SEED", "A", 1.0, "acoustic")
        db.add_edge("A", "B", 1.0, "acoustic")
        # D has slightly higher raw weight, but is in a different cluster.
        db.add_edge("B", "C", 0.90, "acoustic")
        db.add_edge("B", "D", 0.95, "acoustic")

        db.clusters["SEED"] = 0
        db.clusters["A"] = 0
        db.clusters["B"] = 0
        db.clusters["C"] = 0
        db.clusters["D"] = 1 # Different cluster!

        # Run multiple trials with cluster_lambda=0.9.
        # D's effective logit should be multiplied by 0.1, making it 0.095.
        # C's effective logit remains 0.90. C must win.
        c_hits = 0
        trials = 20
        for t in range(trials):
            out = _run(tg.walk(db, "SEED", length=3,
                               edge_kinds=("acoustic",),
                               seed_rng=random.Random(t),
                               diversity_lambda=0.0,
                               restart_prob=0.0,
                               temperature=0.01,
                               cluster_lambda=0.9))
            self.assertEqual(len(out), 3)
            self.assertEqual(out[0], "A")
            self.assertEqual(out[1], "B")
            if out[2] == "C":
                c_hits += 1
        self.assertEqual(c_hits, trials, "Fallback cluster penalty failed to penalize D")

    def test_fallback_diversity_penalty(self):
        # SEED -> A -> B -> C (similar to A) and D (dissimilar to A)
        # B's candidates C and D are fetched via fallback query.
        db = FakeDB()
        db.add_edge("SEED", "A", 1.0, "acoustic")
        db.add_edge("A", "B", 1.0, "acoustic")
        # C has slightly higher raw weight, but is identical to visited A.
        db.add_edge("B", "C", 0.95, "acoustic")
        db.add_edge("B", "D", 0.90, "acoustic")

        # Setup timbres
        v_ac = np.zeros(EMBED_DIMS, dtype=np.float32)
        v_ac[0] = 1.0
        v_seed_b = np.zeros(EMBED_DIMS, dtype=np.float32)
        v_seed_b[1] = 1.0
        v_d = np.zeros(EMBED_DIMS, dtype=np.float32)
        v_d[2] = 1.0

        db.add_timbre("SEED", v_seed_b)
        db.add_timbre("A", v_ac)
        db.add_timbre("B", v_seed_b)
        db.add_timbre("C", v_ac) # Identical to A
        db.add_timbre("D", v_d)  # Dissimilar

        # Run trials with diversity_lambda=2.0
        # C has visited similarity 1.0 with A. Eff weight becomes 0.95 - 2.0 * 1.0 = -1.05.
        # D has visited similarity 0.0. Eff weight remains 0.90. D must win.
        d_hits = 0
        trials = 20
        for t in range(trials):
            out = _run(tg.walk(db, "SEED", length=3,
                               edge_kinds=("acoustic",),
                               seed_rng=random.Random(t),
                               diversity_lambda=2.0,
                               restart_prob=0.0,
                               temperature=0.01,
                               cluster_lambda=0.0))
            self.assertEqual(len(out), 3)
            self.assertEqual(out[0], "A")
            self.assertEqual(out[1], "B")
            if out[2] == "D":
                d_hits += 1
        self.assertEqual(d_hits, trials, "Fallback diversity penalty failed to penalize C")


if __name__ == "__main__":
    unittest.main()
