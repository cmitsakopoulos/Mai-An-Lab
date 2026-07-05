"""Integration test: run the walk over a REAL, built library image.

The FakeDB unit tests prove the walk's mechanics on hand-built fixtures. They
cannot catch what a diverse real library does — a Greek-hip-hop / classic-rock /
metal / laiko mix is far more prone to the acoustic-bridge drift the metadata
term is meant to stop than any synthetic graph. This test runs the shipping
walk over such an image and asserts:

  • structural invariants hold across many random seeds (seed excluded, no dupes,
    avoid respected, length bounded, every hop is a real acoustic edge);
  • the metadata/cluster factors do their job in aggregate — smooth+meta makes
    NO MORE cross-community jumps than the acoustic-only walk, and strictly
    fewer on at least some seeds.

It needs a *built* image (acoustic edges + clusters + enrichment). Point it with

    MAIANLAB_WALK_TEST_DB=/path/to/library.db

or drop a built DB at tools/offload_cache/walk_diag_db/library_built.db (build
one with:  python tools/walk_probe.py --db <image> --build ). The test SKIPS
cleanly when no built image is present, so it never breaks CI on a fresh clone.
"""

import asyncio
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import utils.config as _cfg
_cfg.APP_DIR = "/tmp/dsptest_app_dir"
os.makedirs(_cfg.APP_DIR, exist_ok=True)

from utils import track_graph as tg           # noqa: E402
from utils.db_manager import DatabaseManager   # noqa: E402

_DEFAULT_DB = os.path.join(
    os.path.dirname(__file__), "..", "..", "tools", "offload_cache",
    "walk_diag_db", "library_built.db",
)
_DB_PATH = os.environ.get("MAIANLAB_WALK_TEST_DB", _DEFAULT_DB)

N_SEEDS = int(os.environ.get("MAIANLAB_WALK_TEST_SEEDS", "25"))
WALK_LEN = 10


def _run(coro):
    return asyncio.run(coro)


async def _acoustic_edge(db, a, b) -> bool:
    conn = await db.get_connection()
    async with conn.execute(
        "SELECT 1 FROM track_neighbors "
        "WHERE track_path=? AND neighbor_path=? AND edge_kind='acoustic'",
        (a, b),
    ) as cur:
        return (await cur.fetchone()) is not None


async def _cluster_switches(db, seq) -> int:
    cl = [await db.get_track_cluster(p) for p in seq]
    return sum(
        1 for a, b in zip(cl, cl[1:])
        if a is not None and b is not None and a != b
    )


async def _has_acoustic_edges(db) -> bool:
    conn = await db.get_connection()
    async with conn.execute(
        "SELECT COUNT(*) FROM track_neighbors WHERE edge_kind='acoustic'"
    ) as cur:
        return (await cur.fetchone())[0] > 0


async def _pick_seeds(db, n, rng):
    conn = await db.get_connection()
    async with conn.execute(
        "SELECT DISTINCT track_path FROM track_neighbors WHERE edge_kind='acoustic'"
    ) as cur:
        paths = [r[0] for r in await cur.fetchall()]
    rng.shuffle(paths)
    return paths[:n]


@unittest.skipUnless(
    os.path.exists(_DB_PATH),
    f"no built library image at {_DB_PATH} "
    f"(set MAIANLAB_WALK_TEST_DB or run tools/walk_probe.py --build)",
)
class TestWalkOnRealLibrary(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        async def _load():
            db = DatabaseManager(_DB_PATH)
            await db.initialize()
            if not await _has_acoustic_edges(db):
                await db.close()
                return None
            rng = random.Random(0)
            seeds = await _pick_seeds(db, N_SEEDS, rng)
            return db, seeds
        loaded = _run(_load())
        if loaded is None:
            raise unittest.SkipTest(f"{_DB_PATH} has no acoustic edges (not built)")
        cls.db, cls.seeds = loaded

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "db", None) is not None:
            _run(cls.db.close())

    def test_structural_invariants_hold_across_seeds(self):
        async def _check():
            for seed in self.seeds:
                out = await tg.walk(self.db, seed, length=WALK_LEN)
                self.assertNotIn(seed, out, f"seed leaked into its own walk: {seed}")
                self.assertEqual(len(out), len(set(out)), f"duplicate in walk from {seed}")
                self.assertLessEqual(len(out), WALK_LEN)
                # every consecutive pair must be a genuine acoustic edge (or a
                # seed-neighbour fallback hop) — never a fabricated jump.
                prev = seed
                for p in out:
                    ok = await _acoustic_edge(self.db, prev, p) or \
                         await _acoustic_edge(self.db, seed, p)
                    self.assertTrue(ok, f"non-edge hop {prev}->{p} (seed {seed})")
                    prev = p
        _run(_check())

    def test_avoid_set_is_never_emitted(self):
        async def _check():
            seed = self.seeds[0]
            first = await tg.walk(self.db, seed, length=WALK_LEN)
            avoid = set(first[: len(first) // 2])
            second = await tg.walk(self.db, seed, length=WALK_LEN, avoid=avoid)
            self.assertTrue(set(second).isdisjoint(avoid))
            self.assertNotIn(seed, second)
        _run(_check())

    def test_metadata_reduces_cross_community_drift(self):
        async def _check():
            meta_switches = 0
            acoustic_switches = 0
            strictly_better = 0
            nonempty = 0
            for seed in self.seeds:
                meta = await tg.walk(self.db, seed, length=WALK_LEN)  # defaults on
                aco = await tg.walk(self.db, seed, length=WALK_LEN,
                                    meta_lambda=0.0, cluster_lambda=0.0)
                if not meta and not aco:
                    continue
                nonempty += 1
                ms = await _cluster_switches(self.db, [seed] + meta)
                as_ = await _cluster_switches(self.db, [seed] + aco)
                meta_switches += ms
                acoustic_switches += as_
                if ms < as_:
                    strictly_better += 1

            self.assertGreater(nonempty, 0, "no non-empty walks — image not built?")
            # Aggregate: metadata must not INCREASE community drift, and should
            # cut it on a meaningful share of seeds.
            self.assertLessEqual(
                meta_switches, acoustic_switches,
                f"metadata increased drift: {meta_switches} > {acoustic_switches}",
            )
            self.assertGreater(
                strictly_better, 0,
                "metadata never reduced drift on any seed — factor not firing? "
                f"(meta={meta_switches}, acoustic={acoustic_switches})",
            )
            # Surface the numbers when run with -s.
            print(f"\n[real-library] seeds={nonempty}  cross-community switches: "
                  f"smooth+meta={meta_switches}  acoustic-only={acoustic_switches}  "
                  f"strictly-better-seeds={strictly_better}/{nonempty}")
        _run(_check())


if __name__ == "__main__":
    unittest.main()
