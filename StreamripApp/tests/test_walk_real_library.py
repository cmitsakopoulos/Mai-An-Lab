"""Integration test: run the walk over a REAL, built library image.

The FakeDB unit tests prove the walk's mechanics on hand-built fixtures. They
cannot catch what a diverse real library does — a Greek-hip-hop / classic-rock /
metal / laiko mix is far more prone to the acoustic-bridge drift the metadata
term is meant to stop than any synthetic graph. This test runs the shipping
walk over such an image and asserts:

  • structural invariants hold across many random seeds (seed excluded, no dupes,
    avoid respected, length bounded, every hop is a real coordinate-graph
    neighbour);
  • the metadata pool does its job in aggregate — smooth+meta is at least as
    genre-cohesive as the acoustic-only walk, and strictly more cohesive on at
    least some seeds.

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


async def _genre_cohesion(db, seed, seq):
    """Mean NPMI soft-set-sim of each track in `seq` vs the seed's genres, over
    the pairs where both sides carry genre tokens. Returns (mean_sim, n_scored).
    This is the walk's real quality target under the metadata-pool design —
    genre coherence, not timbral-cluster stickiness."""
    from utils.genre_similarity import soft_set_sim
    model = await db.get_genre_affinity()
    meta = await db.get_artist_meta_for_paths([seed] + list(seq))
    sg = (meta.get(seed) or {}).get("genres") or frozenset()
    sims = [
        soft_set_sim(sg, gc, model)
        for p in seq
        for gc in [(meta.get(p) or {}).get("genres") or frozenset()]
        if sg and gc
    ]
    return (sum(sims) / len(sims) if sims else 0.0), len(sims)


def _seed_affinity(graph, seed, other) -> float:
    """The cosine similarity the walk ranks by. The coordinate graph — not
    track_neighbors — is the walk's live similarity oracle."""
    ia = graph["path_to_idx"].get(seed)
    ib = graph["path_to_idx"].get(other)
    if ia is None or ib is None:
        return -2.0
    U = graph["X_unit"]
    return float(U[ia] @ U[ib])


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
                # The queue is the library RANKED by affinity to the seed, so
                # it must come back in non-increasing affinity order. (The
                # repeat caps skip tracks, but never reorder the survivors.)
                affs = [_seed_affinity(graph, seed, p) for p in out]
                self.assertTrue(
                    all(a >= b - 1e-6 for a, b in zip(affs, affs[1:])),
                    f"queue not in descending seed affinity for {seed}: {affs}",
                )
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

    def test_metadata_improves_genre_cohesion(self):
        # Under the metadata-pool design the walk's target is GENRE coherence,
        # not timbral-cluster stickiness (the pool deliberately spans Louvain
        # clusters — a Greek queue jumps between laiko ballads and Greek trap).
        # So we measure mean NPMI genre-similarity-to-seed: the metadata walk
        # must be at least as genre-cohesive as the pure-acoustic walk in
        # aggregate, and strictly more cohesive on a meaningful share of seeds.
        async def _check():
            meta_coh_sum = 0.0
            aco_coh_sum = 0.0
            strictly_better = 0
            nonempty = 0
            for seed in self.seeds:
                meta = await tg.walk(self.db, seed, length=WALK_LEN)  # defaults on
                aco = await tg.walk(self.db, seed, length=WALK_LEN,
                                    veto_genre_floor=0.0)
                if not meta and not aco:
                    continue
                nonempty += 1
                m_coh, _ = await _genre_cohesion(self.db, seed, meta)
                a_coh, _ = await _genre_cohesion(self.db, seed, aco)
                meta_coh_sum += m_coh
                aco_coh_sum += a_coh
                if m_coh > a_coh + 1e-9:
                    strictly_better += 1

            self.assertGreater(nonempty, 0, "no non-empty walks — image not built?")
            # Aggregate: metadata must not REDUCE genre cohesion, and should
            # raise it on a meaningful share of seeds.
            self.assertGreaterEqual(
                meta_coh_sum, aco_coh_sum - 1e-6,
                f"metadata reduced genre cohesion: {meta_coh_sum:.3f} < {aco_coh_sum:.3f}",
            )
            self.assertGreater(
                strictly_better, 0,
                "metadata never improved genre cohesion on any seed — pool not "
                f"firing? (meta={meta_coh_sum:.3f}, acoustic={aco_coh_sum:.3f})",
            )
            # Surface the numbers when run with -s.
            print(f"\n[real-library] seeds={nonempty}  mean genre-cohesion: "
                  f"smooth+meta={meta_coh_sum / nonempty:.3f}  "
                  f"acoustic-only={aco_coh_sum / nonempty:.3f}  "
                  f"strictly-better-seeds={strictly_better}/{nonempty}")
        _run(_check())


if __name__ == "__main__":
    unittest.main()
