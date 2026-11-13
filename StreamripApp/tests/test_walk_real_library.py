"""Integration test: run the walk over a REAL, built library image.

The FakeDB unit tests prove the walk's mechanics on hand-built fixtures. They
cannot catch what a diverse real library does — a Greek-hip-hop / classic-rock /
metal / laiko mix is far more prone to the acoustic-bridge drift the metadata
term is meant to stop than any synthetic graph. This test runs the shipping
walk over such an image and asserts:

  • structural invariants hold across many random seeds (seed excluded, no dupes,
    avoid respected, length bounded);
  • the artist drift stays in the seed's genres at least as well as the plain
    nearest-in-sound list does. A guardrail only — queue quality is judged by
    reading queues (tools/walk_probe.py), not by this number.

It needs a *built* image (Zr coordinates + enrichment). Point it with

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


async def _on_family(db, seed, seq):
    """Share of `seq` whose artist shares a coarse genre family with the seed's,
    over the tracks whose artist is tagged. (n_on, n_scored)."""
    from utils.genre_taxonomy import genre_families
    meta = await db.get_artist_meta_for_paths([seed] + list(seq))
    fs = genre_families((meta.get(seed) or {}).get("genres") or ())
    if not fs:
        return 0, 0
    fams = [genre_families((meta.get(p) or {}).get("genres") or ()) for p in seq]
    scored = [f for f in fams if f]
    return sum(1 for f in scored if f & fs), len(scored)


def _nearest_in_sound(graph, seed, n):
    U = graph["X_unit"]
    i = graph["path_to_idx"][seed]
    order = (U @ U[i]).argsort()[::-1]
    return [graph["paths"][j] for j in order if j != i][:n]


async def _has_coords(db) -> bool:
    conn = await db.get_connection()
    async with conn.execute(
        "SELECT COUNT(*) FROM play_counts WHERE pca_coords IS NOT NULL"
    ) as cur:
        return (await cur.fetchone())[0] > 0


async def _pick_seeds(db, n, rng):
    conn = await db.get_connection()
    async with conn.execute(
        "SELECT track_path FROM play_counts WHERE pca_coords IS NOT NULL"
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
            if not await _has_coords(db):
                await db.close()
                return None
            rng = random.Random(0)
            seeds = await _pick_seeds(db, N_SEEDS, rng)
            return db, seeds
        loaded = _run(_load())
        if loaded is None:
            raise unittest.SkipTest(f"{_DB_PATH} has no Zr coordinates (not built)")
        cls.db, cls.seeds = loaded

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "db", None) is not None:
            _run(cls.db.close())

    def test_structural_invariants_hold_across_seeds(self):
        async def _check():
            graph = await tg.load_live_coordinate_graph(self.db)
            self.assertIsNotNone(graph, "built image has no coordinate graph")
            for seed in self.seeds:
                out = await tg.walk(self.db, seed, length=WALK_LEN)
                self.assertNotIn(seed, out, f"seed leaked into its own walk: {seed}")
                self.assertEqual(len(out), len(set(out)), f"duplicate in walk from {seed}")
                self.assertLessEqual(len(out), WALK_LEN)
                self.assertTrue(set(out) <= set(graph["paths"]))
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

    def test_drift_holds_genre_at_least_as_well_as_sound(self):
        async def _check():
            graph = await tg.load_live_coordinate_graph(self.db)
            drift = [0, 0]
            sound = [0, 0]
            for seed in self.seeds:
                out = await tg.walk(self.db, seed, length=WALK_LEN)
                for acc, seq in ((drift, out), (sound, _nearest_in_sound(graph, seed, WALK_LEN))):
                    on, n = await _on_family(self.db, seed, seq)
                    acc[0] += on
                    acc[1] += n
            self.assertGreater(drift[1], 0, "no tagged neighbours — image not enriched?")
            d, s_ = drift[0] / drift[1], sound[0] / max(sound[1], 1)
            print(f"\n[real-library] on-family: drift={d:.1%}  nearest-in-sound={s_:.1%}")
            self.assertGreaterEqual(d, s_ - 1e-9)
        _run(_check())
        _run(_check())


if __name__ == "__main__":
    unittest.main()
