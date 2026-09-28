"""Unit tests for the genre-adjacency graph + journey traversal
(`utils.genre_graph`). Synthetic directional clusters stand in for genres: four
cones on the unit circle where A and B overlap at the tails while C (opposite)
and D (orthogonal) are far. That geometry lets us assert, deterministically:

  • family / node assignment incl. the culture split (country, scene tag, script)
  • block-chunked kNN == single-pass kNN, self excluded, sim-ordered
  • label-propagation fills an untagged interior point, leaves the all-untagged
    cluster homeless, and marks what it inferred
  • bridge-artist adjacency: ≥ 2 shared artists, one-way out of a small node,
    never across cultures
  • journey starts at the seed, fills the seed node then hops (through a bridge
    artist when there is one), respects length / exclude / learned hop weights,
    and is reproducible
"""
import os
import sys
import random
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

    def test_node_label_culture_split(self):
        self.assertEqual(gg.node_label("Hip-Hop", None), "Hip-Hop")
        self.assertEqual(gg.node_label("Hip-Hop", "GR"), "Hip-Hop" + gg.NODE_SEP + "GR")
        self.assertEqual(gg.node_label("Folk/Cntry", "gr"), "Folk/Cntry" + gg.NODE_SEP + "GR")
        self.assertIsNone(gg.node_label(None, "GR"))
        self.assertEqual(gg.node_culture("Folk/Cntry" + gg.NODE_SEP + "GR"), "GR")
        self.assertIsNone(gg.node_culture("Rock"))
        self.assertEqual(gg.node_display("Folk/Cntry" + gg.NODE_SEP + "GR"), "Greek Folk/Cntry")
        self.assertEqual(gg.node_display("Rock"), "Rock")

    def test_culture_from_any_signal(self):
        self.assertEqual(gg.track_culture("GR"), "GR")
        self.assertEqual(gg.track_culture(None, {"laiko", "folkpop"}), "GR")
        # A wrong country (a short name matched to the wrong entity) doesn't
        # veto the script.
        self.assertEqual(gg.track_culture("US", {"pop"}, ("Pantelis", "Ψάξε με")), "GR")
        self.assertIsNone(gg.track_culture("US", {"rock"}, ("Pink Floyd", "Animals")))
        self.assertIsNone(gg.track_culture("JP"))                # not a regional culture

    def test_culture_words_do_not_vote_for_a_family(self):
        # 'greek' is a Pop key for display, but a laiko act tagged 'greek' is not pop.
        self.assertEqual(gg.primary_family({"greek", "laiko"}), "Folk/Cntry")
        self.assertEqual(gg.artist_families({"greek", "laiko", "poprock"}), {"Folk/Cntry", "Rock"})


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


GR = gg.NODE_SEP + "GR"


def _bridges(memberships, **kw):
    """bridge_adjacency over one track per artist, in its first (home) node."""
    artists = list(memberships)
    nodes = [memberships[a][0] for a in artists]
    return gg.bridge_adjacency(nodes, artists, {a: set(ns) for a, ns in memberships.items()},
                               min_size=1, **kw)


class TestBridgeAdjacency(unittest.TestCase):
    def setUp(self):
        self.m = {
            "r1": ["Rock", "Metal"], "r2": ["Rock", "Metal"], "r3": ["Rock"], "r4": ["Rock"],
            "m1": ["Metal"], "m2": ["Metal"], "m3": ["Metal"],
            "p1": ["Pop"], "p2": ["Pop"], "p3": ["Pop", "Rock"],
            "c1": ["Classical", "Electronic"],
            "e1": ["Electronic"], "e2": ["Electronic"], "e3": ["Electronic"],
            "g1": ["Folk" + GR, "Pop" + GR], "g2": ["Folk" + GR, "Pop" + GR],
            "g3": ["Folk" + GR], "g4": ["Pop" + GR], "g5": ["Pop" + GR],
        }

    def test_two_bridge_artists_make_a_two_way_edge(self):
        adj, via = _bridges(self.m)
        self.assertIn("Metal", [b for b, _ in adj["Rock"]])
        self.assertIn("Rock", [b for b, _ in adj["Metal"]])
        self.assertEqual(via[("Rock", "Metal")], ["r1", "r2"])

    def test_one_bridge_between_established_genres_is_not_enough(self):
        adj, _ = _bridges(self.m)
        self.assertNotIn("Pop", [b for b, _ in adj["Rock"]])

    def test_small_genre_gets_a_one_way_exit(self):
        adj, via = _bridges(self.m)
        self.assertEqual([b for b, _ in adj["Classical"]], ["Electronic"])
        self.assertNotIn("Classical", [b for b, _ in adj["Electronic"]])
        self.assertEqual(via[("Classical", "Electronic")], ["c1"])

    def test_same_culture_edges_only(self):
        adj, _ = _bridges(self.m)
        self.assertEqual([b for b, _ in adj["Folk" + GR]], ["Pop" + GR])
        # Even an artist spanning both cultures can't join them.
        self.m["x1"] = ["Rock", "Folk" + GR]
        self.m["x2"] = ["Rock", "Folk" + GR]
        adj, _ = _bridges(self.m)
        self.assertNotIn("Folk" + GR, [b for b, _ in adj["Rock"]])

    def test_ties_below_the_lift_floor_are_dropped(self):
        adj, _ = _bridges(self.m, min_lift=100.0)
        self.assertTrue(all(not v for v in adj.values()))


class TestJourney(unittest.TestCase):
    def setUp(self):
        self.X_unit, self.nodes, self.n = _fixture()
        # A and B overlap in the geometry; C and D are islands.
        self.adj = {"A": [("B", 1.5)], "B": [("A", 1.5)], "C": [], "D": []}

    def _journey(self, seed, **kw):
        return gg.Journey(seed, self.X_unit, self.nodes, self.adj, **kw)

    def test_crosses_interface_after_a_leg(self):
        out = self._journey(5, leg_len=4).take(8)          # 5 is an A track
        fams = [self.nodes[i] for i in out]
        self.assertEqual(len(out), 8)
        # one full leg of A, then B entered through its interface — a boundary,
        # not a shuffle
        self.assertTrue(all(f == "A" for f in fams[:4]), fams)
        self.assertTrue(all(f == "B" for f in fams[4:]), fams)
        self.assertEqual(len(set(out)), len(out))
        self.assertNotIn(5, out)                             # the seed is never emitted

    def test_resumable_takes_continue_without_repeats(self):
        whole = self._journey(10, leg_len=4).take(12)
        j = self._journey(10, leg_len=4)
        parts = j.take(3) + j.take(5) + j.take(4)
        self.assertEqual(parts, whole)                       # same station, paged
        self.assertEqual(len(set(parts)), len(parts))

    def test_deterministic_without_rng(self):
        self.assertEqual(self._journey(10).take(12), self._journey(10).take(12))

    def test_seeded_rng_reproduces_and_different_seeds_vary(self):
        a = self._journey(10, rng=random.Random(7)).take(12)
        b = self._journey(10, rng=random.Random(7)).take(12)
        self.assertEqual(a, b)
        others = [self._journey(10, rng=random.Random(s)).take(12) for s in range(8)]
        self.assertTrue(any(o != a for o in others))

    def test_random_picks_stay_in_the_leg_node(self):
        out = self._journey(10, leg_len=6, rng=random.Random(3)).take(6)
        self.assertTrue(all(self.nodes[i] == "A" for i in out))

    def test_exclude_respected(self):
        banned = {self.n + 3, self.n + 4}  # two B tracks
        j = self._journey(6, exclude=set(banned), leg_len=4)
        self.assertFalse(banned & set(j.take(10)))

    def test_end_leg_hops_and_never_returns_rejected(self):
        j = self._journey(5, leg_len=8)
        first = j.take(2)
        j.end_leg(reject=first)
        nxt = j.take(4)
        self.assertEqual(self.nodes[nxt[0]], "B")            # hopped on at once
        self.assertFalse(set(first) & set(nxt))

    def test_end_leg_without_exit_steers_away_from_rejected(self):
        # D has no adjacency: the new leg stays in D but must rank AWAY from the
        # rejected tracks rather than serving their nearest neighbours again.
        d_seed = 3 * self.n + 2
        plain = self._journey(d_seed, leg_len=4)
        rejected = plain.take(2)
        again = self._journey(d_seed, leg_len=4, exclude=set(rejected)).take(3)
        steered = self._journey(d_seed, leg_len=4)
        steered.take(2)
        steered.end_leg(reject=rejected)
        away = steered.take(3)
        sim = lambda idxs: float(np.mean([self.X_unit[i] @ self.X_unit[r] for i in idxs for r in rejected]))
        self.assertTrue(all(self.nodes[i] == "D" for i in away))
        self.assertLess(sim(away), sim(again))

    def test_rolling_artist_cap(self):
        from collections import Counter
        n = self.n
        akeys = [frozenset({f"art{i // 5}"}) for i in range(4 * n)]
        caps = gg.RollingCaps(max_per_artist=2)
        out = self._journey(0, artist_keys=akeys, caps=caps, leg_len=5).take(10)
        c = Counter(k for i in out for k in akeys[i])
        self.assertTrue(all(v <= 2 for v in c.values()), c)

    def test_rolling_window_frees_an_act_again(self):
        caps = gg.RollingCaps(max_per_artist=1, window=2)
        caps.add({"x"}, None)
        self.assertFalse(caps.ok({"x"}, None))
        caps.add({"y"}, None)
        caps.add({"z"}, None)                                 # "x" slides out
        self.assertTrue(caps.ok({"x"}, None))

    def test_album_cap_and_ban(self):
        caps = gg.RollingCaps(max_per_album=1)
        caps.add({"a"}, "LP")
        self.assertFalse(caps.ok({"b"}, "LP"))
        caps.banned.add("b")
        self.assertFalse(caps.ok({"b"}, "Other"))

    def test_hop_enters_through_a_bridge_artist(self):
        # The bridge act's track sits far away (in D), yet it carries the hop
        # into B, and the first HOP_WINDOW tracks after it are blamed on the hop.
        bridge = 3 * self.n + 7
        akeys = [frozenset({f"t{i}"}) for i in range(4 * self.n)]
        akeys[bridge] = frozenset({"bx"})
        j = self._journey(5, leg_len=4, artist_keys=akeys, via={("A", "B"): frozenset({"bx"})})
        out = j.take(8)
        self.assertEqual(out[4], bridge)
        self.assertEqual(j.node, "B")
        hopped = out[4:4 + gg.HOP_WINDOW]
        self.assertEqual({j.hop_of[i] for i in hopped}, {("A", "B")})
        self.assertFalse(set(out[:4]) & set(j.hop_of))
        self.assertFalse(set(out[4 + gg.HOP_WINDOW:]) & set(j.hop_of))

    def test_blocked_hop_is_not_taken(self):
        j = self._journey(5, leg_len=4, hop_weights={("A", "B"): 0.0})
        self.assertTrue(all(self.nodes[i] == "A" for i in j.take(10)))
        j = self._journey(5, leg_len=4)
        j.weight_hop("A", "B", 0.0)                          # learned mid-station
        self.assertTrue(all(self.nodes[i] == "A" for i in j.take(10)))

    def test_hop_goes_where_the_leg_ended_not_just_by_lift(self):
        # C has the stronger tie but sits opposite A; B is where the leg ends.
        adj = dict(self.adj, A=[("C", 1.2), ("B", 1.0)])
        out = gg.Journey(5, self.X_unit, self.nodes, adj, leg_len=4).take(6)
        self.assertEqual(self.nodes[out[4]], "B")

    def test_exhausted_node_never_pads_across_culture(self):
        nodes = [("B" + GR if nd == "B" else nd) for nd in self.nodes]
        out = gg.Journey(5, self.X_unit, nodes, {}, leg_len=4).take(3 * self.n)
        self.assertTrue(out)
        self.assertFalse(any(nodes[i] == "B" + GR for i in out))

    def test_degrades_without_adjacency(self):
        d_seed = 3 * self.n + 2                               # D has no adjacency
        out = self._journey(d_seed).take(8)
        self.assertEqual(len(out), 8)

    def test_rank_pick_is_geometric(self):
        from collections import Counter
        self.assertEqual(gg.rank_pick(10, None), 0)
        rng = random.Random(0)
        c = Counter(gg.rank_pick(10, rng, 0.6) for _ in range(20000))
        self.assertAlmostEqual(c[0] / 20000, 0.4, delta=0.02)
        self.assertAlmostEqual(c[1] / c[0], 0.6, delta=0.05)
        self.assertLess(max(c), 10)


class TestBuild(unittest.TestCase):
    def test_end_to_end(self):
        X_unit, nodes, n = _fixture()
        meta = []
        tok = {"A": {"hiphop"}, "B": {"pop"}, "C": {"metal"}, "D": {"house"}}
        for i, nd in enumerate(nodes):
            artist = f"{nd}{i % 6}"                    # six acts per genre
            genres = set(tok[nd])
            if artist in ("A1", "A2"):
                genres.add("pop")                      # two Hip-Hop/Pop bridge acts
            meta.append({
                "genres": set() if i == 6 else genres,  # one track untagged
                "country": "GR" if artist == "A0" else "US",
                "artist": artist,
                "names": (artist, "Ωδή" if artist == "C0" else "LP", f"t{i}"),
            })
        meta[6]["genres"] = set()
        meta[6]["artist"] = "U"                        # an artist with no tags at all
        g = gg.build_genre_graph(X_unit, meta, k=5, min_size=5)
        self.assertEqual(len(g["nodes"]), len(nodes))
        self.assertTrue(all(x is not None for x in g["nodes"]))   # no None leaks
        self.assertTrue(g["inferred"][6])                          # untagged got a home
        self.assertIn("Hip-Hop" + GR, set(g["nodes"]))             # culture by country
        self.assertIn("Metal" + GR, set(g["nodes"]))               # culture by script
        self.assertEqual(g["via"][("Hip-Hop", "Pop")], ["A1", "A2"])
        self.assertIn("Pop", [b for b, _ in g["adj"]["Hip-Hop"]])


if __name__ == "__main__":
    unittest.main()
