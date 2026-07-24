"""Unit tests for the seed-anchored similarity queue (the single queue builder).

These use an in-memory fake db_manager and cover the walk's contract:

  • seed exclusion, length bound, avoid-set, determinism;
  • ordering by affinity to the SEED — the walk ranks, it does not chain;
  • per-artist / per-album repeat caps;
  • the metadata POOL GATE (genre boundary, regional-seed country boundary) —
    on vs off. Metadata does not appear in the ordering score at all, so there
    is nothing else about it to test here;
  • graceful degradation when the backend lacks the enrichment accessors.

The pool gate is exercised here on tiny hand-built fixtures;
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

    def add_edge(self, src, dst, weight, kind="acoustic", artist=None, album=None):
        # artist/album default to the destination's own name so the walk's
        # per-artist / per-album repeat caps don't fire on unrelated fixtures.
        # Pass them explicitly to exercise the caps.
        self.edges.setdefault((src, kind), []).append({
            "path": dst, "weight": weight, "edge_kind": kind,
            "title": dst, "artist": artist or dst, "album": album or dst,
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


def _fan_db():
    """SEED's acoustic neighbourhood, descending affinity. The walk ranks by
    proximity to the SEED, so the queue is just this list: [A, B, C, D].

    (This used to be a SEED→A→B→C→D *chain*, because the walk hopped from each
    chosen track to that track's own neighbours. It doesn't chain any more —
    chaining measured strictly worse than ranking, see `track_graph.walk`.)"""
    db = FakeDB()
    db.add_edge("SEED", "A", 0.95)
    db.add_edge("SEED", "B", 0.85)
    db.add_edge("SEED", "C", 0.80)
    db.add_edge("SEED", "D", 0.70)
    return db


class TestWalkContract(unittest.TestCase):
    def test_seed_excluded_and_length_bounded(self):
        out = _run(tg.walk(_fan_db(), "SEED", length=4))
        self.assertNotIn("SEED", out)
        self.assertTrue(0 < len(out) <= 4)

    def test_no_duplicates(self):
        out = _run(tg.walk(_fan_db(), "SEED", length=10))
        self.assertEqual(len(out), len(set(out)))

    def test_deterministic(self):
        db = _fan_db()
        self.assertEqual(
            _run(tg.walk(db, "SEED", length=4)),
            _run(tg.walk(db, "SEED", length=4)),
        )

    def test_seed_affinity_ordering(self):
        # The queue is the seed's neighbourhood in descending affinity.
        out = _run(tg.walk(_fan_db(), "SEED", length=4))
        self.assertEqual(out, ["A", "B", "C", "D"])

    def test_avoid_set_respected(self):
        db = _fan_db()
        out = _run(tg.walk(db, "SEED", length=5, avoid={"A", "B"}))
        self.assertTrue(set(out).isdisjoint({"A", "B"}))
        self.assertEqual(out, ["C", "D"])

    def test_queue_truncates_when_the_pool_runs_out(self):
        # Only two candidates exist; asking for ten returns two rather than
        # padding or repeating.
        db = FakeDB()
        db.add_edge("SEED", "A", 0.90)
        db.add_edge("SEED", "B", 0.80)
        out = _run(tg.walk(db, "SEED", length=10))
        self.assertEqual(out, ["A", "B"])


class TestWalkRepeatCaps(unittest.TestCase):
    """`max_per_artist` / `max_per_album` bound how much of a queue one act or
    release may take. This replaces the MMR diversity term, which measured a
    2.5% effect on picks because cosine over the non-centred timbre block spans
    only 0.56-0.97."""

    def test_album_cap_stops_one_release_filling_the_queue(self):
        db = FakeDB()
        for i, w in enumerate([0.95, 0.94, 0.93]):
            db.add_edge("SEED", f"R{i}", w, artist="Same", album="Deluxe")
        db.add_edge("SEED", "OTHER", 0.10, artist="Other", album="Other")
        out = _run(tg.walk(db, "SEED", length=4))
        self.assertEqual(out, ["R0", "OTHER"])

    def test_artist_cap_allows_two_then_moves_on(self):
        db = FakeDB()
        for i, w in enumerate([0.95, 0.94, 0.93]):
            db.add_edge("SEED", f"T{i}", w, artist="Prolific", album=f"Album{i}")
        db.add_edge("SEED", "OTHER", 0.10, artist="Other", album="Other")
        out = _run(tg.walk(db, "SEED", length=4))
        self.assertEqual(out, ["T0", "T1", "OTHER"])

    def test_collab_credit_counts_against_the_same_act(self):
        # A solo credit and a collab credit naming the same artist must share
        # the cap rather than being counted as two different acts.
        db = FakeDB()
        db.add_edge("SEED", "S1", 0.95, artist="21 Savage", album="A1")
        db.add_edge("SEED", "S2", 0.94, artist="21 Savage", album="A2")
        db.add_edge("SEED", "S3", 0.93, artist="21 Savage & Metro Boomin", album="A3")
        db.add_edge("SEED", "OTHER", 0.10, artist="Other", album="Other")
        out = _run(tg.walk(db, "SEED", length=4))
        self.assertEqual(out, ["S1", "S2", "OTHER"])

    def test_caps_disabled_with_zero(self):
        db = FakeDB()
        for i, w in enumerate([0.95, 0.94, 0.93]):
            db.add_edge("SEED", f"R{i}", w, artist="Same", album="Deluxe")
        out = _run(tg.walk(db, "SEED", length=3,
                           max_per_artist=0, max_per_album=0))
        self.assertEqual(out, ["R0", "R1", "R2"])


class TestWalkGracefulDegradation(unittest.TestCase):
    def test_walk_runs_without_meta_or_cluster_accessors(self):
        db = NoMetaDB()
        db.add_edge("SEED", "A", 0.95)
        db.add_edge("SEED", "C", 0.90)
        # The backend can't serve enrichment: the pool gate must collapse to a
        # no-op and the walk still flows.
        out = _run(tg.walk(db, "SEED", length=2))
        self.assertEqual(out, ["A", "C"])

    def test_empty_enrichment_is_a_noop(self):
        # FakeDB has the accessors but no enrichment rows → nothing is foreign.
        out = _run(tg.walk(_fan_db(), "SEED", length=4))
        self.assertEqual(out, ["A", "B", "C", "D"])


class TestWalkScoreIsPurelyAcoustic(unittest.TestCase):
    """Metadata decides pool MEMBERSHIP and never rank. Two in-pool candidates
    must therefore be ordered by acoustics alone, no matter how much provenance
    they share with the seed.

    This is the regression guard for the deleted `_meta_score` /`_genre_flow`
    terms: measured on the real library they changed 43% and 16% of picks
    respectively while adding +0.4 points of on-family purity over the gate that
    was already running, and they penalised untagged candidates (0.0 is last
    place under an arg-max, not neutral)."""

    def test_shared_country_and_genre_cannot_outrank_a_closer_track(self):
        # M shares the seed's country AND genre exactly; N is a different country
        # with a merely genre-compatible tag, but is acoustically nearer. Both are
        # in-pool, so the nearer track must win.
        db = FakeDB()
        db.add_edge("SEED", "M", 0.70)   # farther, perfect provenance match
        db.add_edge("SEED", "N", 0.90)   # nearer, weaker provenance
        db.add_meta("SEED", "S",   "US", ["hiphop"])
        db.add_meta("M",    "Emm", "US", ["hiphop"])
        db.add_meta("N",    "Enn", "GB", ["hiphop"])
        out = _run(tg.walk(db, "SEED", length=1))
        self.assertEqual(out[0], "N")

    def test_untagged_candidate_is_not_penalised(self):
        # U has no genre tags at all. It must still win on acoustics alone —
        # missing enrichment is "we cannot tell", not "worse".
        db = FakeDB()
        db.add_edge("SEED", "T", 0.80)   # tagged, genre-matched, farther
        db.add_edge("SEED", "U", 0.90)   # untagged, nearer
        db.add_meta("SEED", "S",   "US", ["hiphop"])
        db.add_meta("T",    "Tee", "US", ["hiphop"])
        db.add_meta("U",    "You", "US", [])
        out = _run(tg.walk(db, "SEED", length=1))
        self.assertEqual(out[0], "U")


class TestWalkGenreVeto(unittest.TestCase):
    def _db(self):
        # SEED (hiphop). Two neighbours: N (nearer, genre-FOREIGN) and
        # G (farther, same genre). The old soft nudge let the nearer foreign
        # track win; the hard veto must forbid N outright.
        db = FakeDB()
        db.add_edge("SEED", "N", 0.95)   # nearest, but foreign genre
        db.add_edge("SEED", "G", 0.70)   # farther, same genre
        db.add_meta("SEED", "S",  "US", ["hiphop"])
        db.add_meta("N",    "Enn", "GR", ["laiko"])   # genre-foreign to seed
        db.add_meta("G",    "Gee", "US", ["hiphop"])  # same genre as seed
        return db

    def test_foreign_neighbour_is_vetoed(self):
        # Even though N is the nearest acoustic neighbour, it is genre-foreign to
        # the seed, so it must be vetoed and the same-genre G chosen instead.
        out = _run(tg.walk(self._db(), "SEED", length=1))
        self.assertEqual(out[0], "G")

    def test_veto_disabled_lets_foreign_win(self):
        # veto_genre_floor=0 restores the pre-veto behaviour: N's raw affinity
        # wins despite the genre gap.
        out = _run(tg.walk(self._db(), "SEED", length=1,
                           veto_genre_floor=0.0))
        self.assertEqual(out[0], "N")

    def test_veto_truncates_rather_than_stepping_foreign(self):
        # If the only reachable neighbour is genre-foreign, the walk must emit
        # NOTHING rather than admit it — the guarantee is "never play a foreign
        # track", not "always fill the queue". (Lowering veto_genre_floor is the
        # escape valve; see test_veto_disabled_lets_foreign_win.)
        db = FakeDB()
        db.add_edge("SEED", "N", 0.95)
        db.add_meta("SEED", "S",   "US", ["hiphop"])
        db.add_meta("N",    "Enn", "GR", ["laiko"])
        out = _run(tg.walk(db, "SEED", length=1))
        self.assertEqual(out, [])


class TestWalkRegionalCountryPool(unittest.TestCase):
    """For a regional-scene seed (laiko/Latin/Reggae/Asian-Pop), a foreign
    country is itself foreign — the lever that keeps a Greek-laiko seed from
    drifting into acoustically-near foreign pop whose coherent same-country
    continuation happens to be untagged."""

    def test_regional_seed_vetoes_foreign_country_even_when_genre_matches(self):
        # SEED is laiko/GR (regional). N is the nearest neighbour and even shares
        # the 'laiko' tag, but it is GB → the country boundary vetoes it. G is a
        # farther GR track with NO genre tags — kept, because same-country and no
        # genre evidence to judge — so the queue stays Greek.
        db = FakeDB()
        db.add_edge("SEED", "N", 0.95)   # nearest, laiko but GB
        db.add_edge("SEED", "G", 0.70)   # farther, GR, untagged
        db.add_meta("SEED", "S",   "GR", ["laiko"])
        db.add_meta("N",    "Enn", "GB", ["laiko"])
        db.add_meta("G",    "Gee", "GR", [])
        out = _run(tg.walk(db, "SEED", length=1))
        self.assertEqual(out[0], "G")

    def test_nonregional_seed_does_not_veto_foreign_country(self):
        # SEED is hiphop/US (NOT regional). N is hiphop/GB — same genre, foreign
        # country — must NOT be country-vetoed, so its higher affinity wins.
        db = FakeDB()
        db.add_edge("SEED", "N", 0.95)
        db.add_edge("SEED", "G", 0.70)
        db.add_meta("SEED", "S",   "US", ["hiphop"])
        db.add_meta("N",    "Enn", "GB", ["hiphop"])
        db.add_meta("G",    "Gee", "US", ["hiphop"])
        out = _run(tg.walk(db, "SEED", length=1))
        self.assertEqual(out[0], "N")


class TestWalkSameActIsNeverForeign(unittest.TestCase):
    """`_same_act` survives the metadata-score deletion because `_pool_foreign`
    still needs it: streaming sources credit one artist under several strings
    ('21 Savage', '21 Savage & Metro Boomin'), each getting its own enrichment
    row, so an artist's own collab track can resolve to the wrong entity and be
    vetoed out of that artist's own queue."""

    def test_same_artist_survives_a_genre_boundary(self):
        # A is credited to a collab string that decomposes to the seed's artist,
        # and its enrichment resolved to a wildly different genre. The same-act
        # exemption must admit it anyway.
        db = FakeDB()
        db.add_edge("SEED", "A", 0.90)
        db.add_meta("SEED", "21 Savage", "US", ["hiphop"])
        db.add_meta("A", "21 Savage & Metro Boomin", "GR", ["laiko"])
        out = _run(tg.walk(db, "SEED", length=1))
        self.assertEqual(out, ["A"])


if __name__ == "__main__":
    unittest.main()
