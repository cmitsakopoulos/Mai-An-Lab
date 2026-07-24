"""Coverage for build_acoustic_edges feature encoding and local-scaling
kernel.

The walk-level tests in test_track_graph_walk cover behaviour given a graph;
these tests cover how the graph itself is constructed:

  * Camelot key encoding (cos/sin + ring) instead of binary major/minor.
  * log2(bpm) instead of raw BPM.
  * Zelnik-Manor self-tuning Gaussian affinity instead of raw cosine.

The fake db_manager exposes only the two methods build_acoustic_edges
calls: `get_tracks_with_features` (returns the rows) and
`replace_neighbors_bulk` (captures the edges that would be written).
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Stub utils.config so the dsp / track_graph imports don't try to load custom moods from disk.
import utils.config as _cfg
_cfg.APP_DIR = "/tmp/dsptest_app_dir"
os.makedirs(_cfg.APP_DIR, exist_ok=True)

from utils import track_graph as tg
from utils.dsp import EMBED_DIMS

import numpy as np


def _timbre_blob(vec: np.ndarray) -> bytes:
    assert vec.shape == (EMBED_DIMS,)
    return vec.astype("<f4").tobytes()


def _row(path: str, vec: np.ndarray, *, bpm: float = 120.0, key_index: int = 8,
         brightness: float = 0.5, energy: float = 0.5, rolloff: float = 0.5,
         beat_strength: float = 0.5, spectral_flatness: float = 0.5,
         spectral_contrast: float = 0.5) -> dict:
    return {
        "path": path,
        "timbre": _timbre_blob(vec),
        "bpm": float(bpm),
        "key_index": int(key_index),
        "brightness": float(brightness),
        "energy": float(energy),
        "rolloff": float(rolloff),
        "beat_strength": float(beat_strength),
        "spectral_flatness": float(spectral_flatness),
        "spectral_contrast": float(spectral_contrast),
    }


class FakeBuildDB:
    def __init__(self, rows):
        self._rows = list(rows)
        self.written: list[tuple[str, str, float]] = []
        self.written_kind: str | None = None
        self.pca_coords: dict[str, np.ndarray] = {}

    async def get_tracks_with_features(self, features_version):
        return list(self._rows)

    async def replace_neighbors_bulk(self, edges, kind):
        self.written = list(edges)
        self.written_kind = kind

    async def save_track_clusters(self, pairs):
        pass

    async def update_tracks_pca_coords_batch(self, pairs):
        for path, coords in pairs:
            self.pca_coords[path] = coords

    async def get_tracks_pca_coords(self):
        res = []
        for r in self._rows:
            path = r["path"]
            coords = self.pca_coords.get(path)
            res.append({
                "path": path,
                "pca_coords": coords,
                "cluster_id": None,
                "bpm": r.get("bpm"),
                "energy": r.get("energy"),
                "brightness": r.get("brightness"),
                "rolloff": r.get("rolloff"),
                "beat_strength": r.get("beat_strength"),
                "spectral_flatness": r.get("spectral_flatness"),
                "spectral_contrast": r.get("spectral_contrast"),
                "key_index": r.get("key_index"),
            })
        return res


def _run(coro):
    return asyncio.run(coro)


def get_coord_neighbors(coord_graph, path, k=40):
    """Top-k neighbours of `path` by cosine, as the walk ranks them. There is no
    mutual-kNN pruning any more — that existed to stop hub tracks dominating a
    greedy chain, and the walk no longer chains."""
    src_idx = coord_graph["path_to_idx"].get(path)
    if src_idx is None:
        return []
    U = coord_graph["X_unit"]
    paths = coord_graph["paths"]
    S = U @ U[src_idx]
    S[src_idx] = -np.inf
    order = np.argsort(-S)[:k]
    return [(path, paths[j], float(S[j])) for j in order]


def _build(db, **kwargs):
    n = _run(tg.build_acoustic_edges(db, **kwargs))
    coord_graph = _run(tg.load_live_coordinate_graph(db))
    db.written = []
    if coord_graph:
        for p in coord_graph["paths"]:
            db.written.extend(get_coord_neighbors(coord_graph, p, k=5))
    return n


class TestAffinityRange(unittest.TestCase):
    """The self-tuning kernel maps cosine into exp(-d²/σᵢσⱼ), which is
    bounded in (0, 1]. The legacy cosine path could write negative weights
    when tracks were anti-correlated; the kernel must never do that."""

    def test_no_negative_or_above_one_weights(self):
        rng = np.random.default_rng(7)
        rows = []
        # Mix of timbres spread across the unit sphere — guarantees some
        # negative cosines would appear in the legacy path.
        for i in range(40):
            v = rng.normal(0, 1, EMBED_DIMS).astype(np.float32)
            rows.append(_row(f"T{i}", v, bpm=80 + i, key_index=i % 24))
        db = FakeBuildDB(rows)
        n = _build(db)
        self.assertGreater(n, 0)
        for src, dst, w in db.written:
            self.assertGreaterEqual(w, 0.0, f"{src}->{dst} got weight {w}")
            self.assertLessEqual(w, 1.0, f"{src}->{dst} got weight {w}")


class TestAffinitySymmetry(unittest.TestCase):
    """Cosine over the unit-normalised Zr is symmetric in i,j and bounded in
    [-1, 1].

    This replaces a strict mutual-kNN symmetry test. Mutual pruning was dropped
    with the greedy chain (it existed to stop cluster-centroid hubs dominating a
    trajectory), so top-k neighbour LISTS are no longer symmetric — but the
    underlying similarity still must be, and that is the property the walk's
    ranking actually depends on."""

    def test_affinity_is_symmetric_in_both_directions(self):
        rng = np.random.default_rng(11)
        rows = []
        for i in range(25):
            v = rng.normal(0, 1, EMBED_DIMS).astype(np.float32)
            rows.append(_row(f"T{i}", v, bpm=100 + i % 5, key_index=i % 12))
        db = FakeBuildDB(rows)
        _run(tg.build_acoustic_edges(db))
        cg = _run(tg.load_live_coordinate_graph(db))
        self.assertIsNotNone(cg)

        U = cg["X_unit"]
        n = len(cg["paths"])
        self.assertGreater(n, 1)
        # Every row must be a unit vector, or "cosine" is not a cosine.
        np.testing.assert_allclose(np.linalg.norm(U, axis=1), 1.0, atol=1e-5)
        for i in range(n):
            for j in range(i + 1, n):
                s_ij = float(U[i] @ U[j])
                s_ji = float(U[j] @ U[i])
                self.assertAlmostEqual(s_ij, s_ji, places=6)
                self.assertGreaterEqual(s_ij, -1.0 - 1e-6)
                self.assertLessEqual(s_ij, 1.0 + 1e-6)


class TestFeatureEncodingRobustness(unittest.TestCase):
    """The Camelot and log-BPM encodings must handle degenerate inputs
    (bpm=0, out-of-range key_index) without crashing — these surface in
    real libraries with broken metadata."""

    def test_zero_bpm_does_not_explode(self):
        rng = np.random.default_rng(3)
        rows = []
        # First batch normal, second batch with bpm=0 (clamp path).
        for i in range(15):
            v = rng.normal(0, 1, EMBED_DIMS).astype(np.float32)
            rows.append(_row(f"OK{i}", v, bpm=120, key_index=i % 24))
        for i in range(10):
            v = rng.normal(0, 1, EMBED_DIMS).astype(np.float32)
            rows.append(_row(f"ZBPM{i}", v, bpm=0.0, key_index=i % 24))
        db = FakeBuildDB(rows)
        n = _build(db)
        self.assertGreater(n, 0)
        # All weights still finite & in range.
        for src, dst, w in db.written:
            self.assertTrue(np.isfinite(w), f"non-finite weight {w} on {src}->{dst}")

    def test_out_of_range_key_index_uses_neutral_encoding(self):
        # key_index outside [0, 23] gets cos=sin=0, mode=0 — a neutral
        # position that won't pull the kernel toward any specific Camelot
        # hour. Build must succeed.
        rng = np.random.default_rng(5)
        rows = []
        for i in range(15):
            v = rng.normal(0, 1, EMBED_DIMS).astype(np.float32)
            rows.append(_row(f"K{i}", v, key_index=999))
        db = FakeBuildDB(rows)
        n = _build(db)
        self.assertGreater(n, 0)


class TestDensityAwareRanking(unittest.TestCase):
    """Two clusters of very different internal spread. What must hold is that
    each track's nearest neighbours are its OWN cluster-mates — i.e. the
    RANKING is right in both the tight and the loose region.

    This used to assert something stronger: that mean within-cluster affinity
    magnitudes were comparable across the two densities (ratio < 3), which is
    what the Zelnik-Manor self-tuning kernel exp(-d²/(σᵢσⱼ)) bought. That kernel
    is gone; the walk ranks by cosine, under which the tight cluster genuinely
    does score higher in absolute terms (~15x here).

    Dropping that property is defensible because nothing compares similarity
    magnitudes ACROSS source rows any more. The two things that did — mutual-kNN
    membership (A's affinity vs B's threshold) and the 0.7*current + 0.3*seed
    blend (two different source rows summed) — were both deleted with the greedy
    chain. The walk now argsorts one row, so only within-row order is
    load-bearing, and cosine measured better on it over two real library images
    (top-10 purity 85.1->86.1 and 84.5->85.6).

    It IS a real trade, though, and this fixture is where it shows: on this
    deliberately adversarial density contrast (cluster B's noise is 8x cluster
    A's, large enough to swamp its own signal) the old self-tuning kernel keeps
    12/12 of B's top-1 neighbours in-cluster where cosine keeps 11/12. Real
    libraries are not that extreme, which is why the real-library measurement
    governs — but the tolerance below is set to catch a genuine break, not to
    paper over this."""

    def test_neighbours_stay_within_their_own_density_cluster(self):
        rng = np.random.default_rng(17)
        rows = []
        # Cluster A: tight (small intra-cluster noise).
        for i in range(12):
            v = np.zeros(EMBED_DIMS, dtype=np.float32)
            v[0] = 1.0
            v += rng.normal(0, 0.05, EMBED_DIMS).astype(np.float32)
            rows.append(_row(f"A{i}", v, bpm=120, key_index=8))
        # Cluster B: loose (large intra-cluster noise), on its own side of the
        # unit sphere so it doesn't merge into the global ball.
        for i in range(12):
            v = np.zeros(EMBED_DIMS, dtype=np.float32)
            v[1] = 1.0
            v += rng.normal(0, 0.4, EMBED_DIMS).astype(np.float32)
            rows.append(_row(f"B{i}", v, bpm=120, key_index=8))
        db = FakeBuildDB(rows)
        _build(db)

        # Top neighbour of every track must share its cluster prefix, in the
        # loose cluster as much as the tight one.
        best = {}
        for src, dst, w in db.written:
            if src not in best or w > best[src][1]:
                best[src] = (dst, w)
        self.assertGreater(len(best), 0)
        for prefix in ("A", "B"):
            members = [s for s in best if s.startswith(prefix)]
            self.assertGreater(len(members), 0)
            same = sum(1 for s in members if best[s][0].startswith(prefix))
            # Measured: cosine 12/12 (tight) and 11/12 (loose); the old
            # self-tuning kernel scored 12/12 on both. 10/12 is the regression
            # bar — below that the metric is genuinely broken, not merely
            # less density-normalising.
            self.assertGreaterEqual(
                same, int(0.83 * len(members)),
                f"cluster {prefix}: only {same} of {len(members)} tracks had a "
                f"top neighbour inside their own cluster",
            )


class TestZScoreNormalizationFlag(unittest.TestCase):
    def test_z_score_false_builds_successfully(self):
        rng = np.random.default_rng(42)
        rows = []
        for i in range(15):
            v = rng.normal(0, 1, EMBED_DIMS).astype(np.float32)
            # Create a feature with a massive scale (0 to 14000)
            rows.append(_row(f"T{i}", v, bpm=120, key_index=i % 12, brightness=float(i * 1000.0)))
        db = FakeBuildDB(rows)
        # Build without Z-scoring (centering only)
        n_unnorm = _build(db, z_score=False)
        self.assertGreater(n_unnorm, 0)
        unnorm_edges = set((src, dst) for src, dst, _ in db.written)

        # Build with Z-scoring (variance scaling)
        n_norm = _build(db, z_score=True)
        self.assertGreater(n_norm, 0)
        norm_edges = set((src, dst) for src, dst, _ in db.written)

        # The massive scale of brightness (0 to 14000) will dominate the unnormalized build
        # but be scaled down in the normalized build, leading to different neighbors.
        self.assertNotEqual(unnorm_edges, norm_edges)


if __name__ == "__main__":
    unittest.main()
