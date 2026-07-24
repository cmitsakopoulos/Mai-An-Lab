"""Unit tests for the PAGA-style genre-adjacency graph + journey traversal
(`utils.genre_graph`). Synthetic directional clusters stand in for genres: four
cones on the unit circle where A and B overlap at the tails (adjacent) while C
(opposite) and D (orthogonal) are far. That geometry lets us assert the pieces
the device-image probe validated, deterministically:

  • family / node assignment incl. the country regional split
  • block-chunked kNN == single-pass kNN, self excluded, sim-ordered
  • label-propagation fills an untagged interior point, leaves the all-untagged
    cluster homeless, and marks what it inferred
  • PAGA lift ranks the adjacent node above the far ones (touch >1, avoid <1)
  • journey starts at the seed, fills the seed node then crosses the interface
    into its adjacent node, respects length / exclude, and is reproducible
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import genre_graph as gg


def _cluster(center_deg, n, spread, seed):
    rng = np.random.default_rng(seed)
    th = np.deg2rad(rng.normal(center_deg, spread, n))
    return np.c_[np.cos(th), np.sin(th)]


def _fixture(n=30):
    # A@0° and B@16° INTERPENETRATE at the tails (wide spread) -> a shared
    # boundary the kNN graph crosses = adjacency. C@180° (opposite) and D@95°
    # (orthogonal) sit far from A with no boundary overlap.
    A = _cluster(0, n, 12, 1)
    B = _cluster(16, n, 12, 2)
    C = _cluster(180, n, 8, 3)
    D = _cluster(95, n, 8, 4)
    X = np.vstack([A, B, C, D]).astype(np.float32)
    X_unit = X / np.linalg.norm(X, axis=1, keepdims=True)
    nodes = ["A"] * n + ["B"] * n + ["C"] * n + ["D"] * n
    return X_unit, nodes, n


class TestAssignment(unittest.TestCase):
    def test_primary_family_priority(self):
        self.assertEqual(gg.primary_family({"hiphop"}), "Hip-Hop")
        # trap + pop -> Hip-Hop wins on taxonomy priority (specific beats generic)
        self.assertEqual(gg.primary_family({"trap", "pop"}), "Hip-Hop")

    def test_primary_family_none(self):
        self.assertIsNone(gg.primary_family(set()))
        self.assertIsNone(gg.primary_family({"zzznotarealgenre"}))

    def test_primary_family_count_weighted(self):
        # token multiplicity across pop-variants keeps a Pop track in Pop, not
        # Electronic (which a lone 'electropop' would otherwise win on priority)
        self.assertEqual(
            gg.primary_family({"pop", "hyperpop", "artpop", "electropop"}), "Pop"
        )
        # explicit {token: weight} mapping overrides that
        self.assertEqual(gg.primary_family({"pop": 1, "electropop": 10}), "Electronic")

    def test_node_label_regional_split(self):
        self.assertEqual(gg.node_label("Hip-Hop", "US"), "Hip-Hop")
        self.assertEqual(gg.node_label("Hip-Hop", "GR"), "Hip-Hop" + gg.NODE_SEP + "GR")
        self.assertEqual(gg.node_label("Folk/Cntry", "gr"), "Folk/Cntry" + gg.NODE_SEP + "GR")
        self.assertIsNone(gg.node_label(None, "GR"))


class TestKnn(unittest.TestCase):
    def test_shape_and_self_exclusion(self):
        X_unit, _, _ = _fixture()
        knn = gg.knn_graph(X_unit, k=5)
        self.assertEqual(knn.shape, (X_unit.shape[0], 5))
        for i in range(X_unit.shape[0]):
            self.assertNotIn(i, knn[i].tolist())

    def test_block_chunk_matches_single_pass(self):
        X_unit, _, _ = _fixture()
        a = gg.knn_graph(X_unit, k=5, block=7)
        b = gg.knn_graph(X_unit, k=5, block=10_000)
        np.testing.assert_array_equal(a, b)

    def test_neighbours_sim_ordered(self):
        X_unit, _, _ = _fixture()
        knn = gg.knn_graph(X_unit, k=5)
        for i in range(0, X_unit.shape[0], 13):
            sims = X_unit[knn[i]] @ X_unit[i]
            self.assertTrue(np.all(np.diff(sims) <= 1e-6), f"row {i} not ordered")


class TestPropagation(unittest.TestCase):
    def test_fills_interior_leaves_homeless(self):
        X_unit, nodes, n = _fixture()
        knn = gg.knn_graph(X_unit, k=5)
        fam = list(nodes)
        fam[0] = None                      # untag one interior A point
        for i in range(2 * n, 3 * n):      # untag the WHOLE C cluster
            fam[i] = None
        filled, inferred, _ = gg.propagate_families(fam, knn, X_unit)
        self.assertEqual(filled[0], "A")   # interior point recovered from A neighbours
        self.assertTrue(inferred[0])
        self.assertFalse(inferred[1])      # a track that was already tagged
        # C had no tagged neighbours anywhere -> stays homeless (None)
        self.assertTrue(all(filled[i] is None for i in range(2 * n, 3 * n)))

    def test_min_conf_gate(self):
        X_unit, nodes, n = _fixture()
        knn = gg.knn_graph(X_unit, k=5)
        fam = list(nodes)
        fam[0] = None
        # an impossible confidence gate refuses to auto-fill
        filled, inferred, _ = gg.propagate_families(fam, knn, X_unit, min_conf=1.01)
        self.assertIsNone(filled[0])
        self.assertFalse(inferred[0])


class TestConnectivity(unittest.TestCase):
    def test_lift_ranks_adjacent_above_far(self):
        X_unit, nodes, _ = _fixture()
        knn = gg.knn_graph(X_unit, k=5)
        order, idx, lift, sizes = gg.paga_connectivity(nodes, knn)
        ab = lift[idx["A"], idx["B"]]
        ac = lift[idx["A"], idx["C"]]
        ad = lift[idx["A"], idx["D"]]
        self.assertGreater(ab, 1.0)        # A and B touch
        self.assertLess(ac, 1.0)           # A and C avoid
        self.assertGreater(ab, ac)
        self.assertGreater(ab, ad)

    def test_adjacency_top_is_adjacent_node(self):
        X_unit, nodes, _ = _fixture()
        knn = gg.knn_graph(X_unit, k=5)
        order, idx, lift, sizes = gg.paga_connectivity(nodes, knn)
        adj = gg.adjacency(order, idx, lift, sizes, min_size=5, min_lift=1.0)
        self.assertEqual(adj["A"][0][0], "B")


class TestJourney(unittest.TestCase):
    def _adj(self):
        self.X_unit, self.nodes, self.n = _fixture()
        knn = gg.knn_graph(self.X_unit, k=5)
        order, idx, lift, sizes = gg.paga_connectivity(self.nodes, knn)
        return gg.adjacency(order, idx, lift, sizes, min_size=5, min_lift=1.0)

    def test_crosses_interface_and_respects_length(self):
        adj = self._adj()
        seed = 5  # an A track
        out = gg.journey(seed, self.X_unit, self.nodes, adj, length=8, hops=1)
        self.assertEqual(out[0], seed)
        self.assertEqual(len(out), 8)
        fams = [self.nodes[i] for i in out]
        self.assertIn("B", fams)                      # actually jumped
        # first leg all A, second leg all B — a boundary, not a shuffle
        self.assertTrue(all(f == "A" for f in fams[:4]))
        self.assertTrue(all(f == "B" for f in fams[4:]))
        self.assertEqual(len(set(out)), len(out))     # no repeats

    def test_deterministic_without_rng(self):
        adj = self._adj()
        a = gg.journey(10, self.X_unit, self.nodes, adj, length=8)
        b = gg.journey(10, self.X_unit, self.nodes, adj, length=8)
        self.assertEqual(a, b)

    def test_exclude_respected(self):
        adj = self._adj()
        banned = {self.n + 3, self.n + 4}  # two B tracks
        out = gg.journey(6, self.X_unit, self.nodes, adj, length=8, exclude=set(banned))
        self.assertFalse(banned & set(out))

    def test_stochastic_still_valid(self):
        import random
        adj = self._adj()
        out = gg.journey(7, self.X_unit, self.nodes, adj, length=8,
                         rng=random.Random(0), jump_temp=0.5)
        self.assertEqual(out[0], 7)
        self.assertEqual(len(out), 8)
        self.assertEqual(len(set(out)), len(out))

    def test_artist_cap_enforced_during_selection(self):
        from collections import Counter
        adj = self._adj()
        n = self.n
        # every block of 5 consecutive tracks shares one artist key
        akeys = [frozenset({f"art{i // 5}"}) for i in range(4 * n)]
        out = gg.journey(0, self.X_unit, self.nodes, adj, length=10, hops=1,
                         artist_keys=akeys, max_per_artist=2)
        # seed (out[0]) is deliberately not counted; the emitted queue must obey
        c = Counter(k for i in out[1:] for k in akeys[i])
        self.assertTrue(all(v <= 2 for v in c.values()), c)

    def test_degrades_to_radius_without_adjacency(self):
        adj = self._adj()
        # D has no adjacency >= 1.0 in the fixture; a D seed must still fill.
        d_seed = 3 * self.n + 2
        out = gg.journey(d_seed, self.X_unit, self.nodes, adj, length=8)
        self.assertEqual(out[0], d_seed)
        self.assertEqual(len(out), 8)


class TestBuild(unittest.TestCase):
    def test_end_to_end(self):
        X_unit, nodes, n = _fixture()
        meta = []
        tok = {"A": {"hiphop"}, "B": {"pop"}, "C": {"metal"}, "D": {"house"}}
        for i, nd in enumerate(nodes):
            genres = set() if i == 0 else tok[nd]     # leave one untagged
            country = "GR" if nd == "A" and i % 5 == 0 else "US"
            meta.append({"genres": genres, "country": country})
        g = gg.build_genre_graph(X_unit, meta, k=5, min_size=5)
        self.assertEqual(len(g["nodes"]), len(nodes))
        self.assertTrue(all(x is not None for x in g["nodes"]))   # no None leaks
        self.assertTrue(g["inferred"][0])                          # untagged got a home
        self.assertIn("Hip-Hop" + gg.NODE_SEP + "GR", set(g["nodes"]))  # regional split fired
        self.assertTrue(len(g["adj"]) > 0)


if __name__ == "__main__":
    unittest.main()
