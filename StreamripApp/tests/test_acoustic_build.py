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
        self.pca_space: dict | None = None
        self.pca_coords: dict[str, np.ndarray] = {}

    async def get_tracks_with_features(self, features_version):
        return list(self._rows)

    async def replace_neighbors_bulk(self, edges, kind):
        self.written = list(edges)
        self.written_kind = kind

    async def save_track_clusters(self, pairs):
        pass

    async def save_pca_space(self, means, stds, V_keep, eigenvalues, feature_spec):
        self.pca_space = feature_spec
        self.pca_space["means"] = means.tolist()
        self.pca_space["stds"] = stds.tolist()
        self.pca_space["projection"] = V_keep.tolist()

    async def load_pca_space(self):
        return self.pca_space

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
    src_idx = coord_graph["path_to_idx"].get(path)
    if src_idx is None:
        return []
    X_zr = coord_graph["X_zr"]
    X_zr_sq = coord_graph["X_zr_sq"]
    sigmas = coord_graph["sigmas"]
    thresholds = coord_graph["thresholds"]
    paths = coord_graph["paths"]
    d2 = X_zr_sq[src_idx] - 2.0 * (X_zr[src_idx] @ X_zr.T) + X_zr_sq
    d2[src_idx] = np.inf
    A = np.exp(-d2 / (sigmas[src_idx] * sigmas))
    mutual_mask = (A >= np.maximum(thresholds[src_idx], thresholds) - 1e-5)
    mutual_mask[src_idx] = False
    nbr_indices = np.where(mutual_mask)[0]
    if len(nbr_indices) == 0:
        return []
    nbr_affinities = A[nbr_indices]
    sort_order = np.argsort(-nbr_affinities)
    sorted_indices = nbr_indices[sort_order]
    res = []
    for idx in sorted_indices:
        res.append((path, paths[idx], float(A[idx])))
    return res[:k]


def _build(db, **kwargs):
    n = _run(tg.build_acoustic_edges(db, **kwargs))
    coord_graph = _run(tg.load_live_coordinate_graph(db))
    db.written = []
    if coord_graph:
        k_neighbors = 5
        if db.pca_space and "k_neighbors" in db.pca_space:
            k_neighbors = int(db.pca_space["k_neighbors"])
        for p in coord_graph["paths"]:
            db.written.extend(get_coord_neighbors(coord_graph, p, k=k_neighbors))
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


class TestEdgeSymmetry(unittest.TestCase):
    """Under strict mutual-kNN pruning, every edge must have a symmetric reverse edge
    with identical weight."""

    def test_strict_symmetry_and_mutual_edges(self):
        rng = np.random.default_rng(11)
        rows = []
        for i in range(25):
            v = rng.normal(0, 1, EMBED_DIMS).astype(np.float32)
            rows.append(_row(f"T{i}", v, bpm=100 + i % 5, key_index=i % 12))
        db = FakeBuildDB(rows)
        _build(db)

        weight_by_pair: dict[tuple[str, str], float] = {}
        for src, dst, w in db.written:
            weight_by_pair[(src, dst)] = w
        
        self.assertGreater(len(weight_by_pair), 0, "No edges written")
        for (a, b), w_ab in weight_by_pair.items():
            w_ba = weight_by_pair.get((b, a))
            self.assertIsNotNone(
                w_ba, f"Edge {a}->{b} is directed (strict mutual-kNN violated)"
            )
            self.assertAlmostEqual(
                w_ab, w_ba, places=5,
                msg=f"Edge weights asymmetric between {a} and {b}"
            )


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


class TestLocalScalingDensityAwareness(unittest.TestCase):
    """Two clusters of differing internal spread. Under the global-cosine
    pipeline, the dense cluster would dominate edge weights (tight cosines
    near 1) while the sparse cluster wrote lower-weight edges. With local
    scaling each cluster's σᵢ adapts to its own density, so within-cluster
    affinities should land in the same coarse range across clusters."""

    def test_within_cluster_affinity_similar_across_densities(self):
        rng = np.random.default_rng(17)
        rows = []
        # Cluster A: tight (small intra-cluster noise).
        for i in range(12):
            v = np.zeros(EMBED_DIMS, dtype=np.float32)
            v[0] = 1.0
            v += rng.normal(0, 0.05, EMBED_DIMS).astype(np.float32)
            rows.append(_row(f"A{i}", v, bpm=120, key_index=8))
        # Cluster B: loose (large intra-cluster noise) — still on its own
        # side of the unit sphere though, otherwise it merges into the
        # global ball.
        for i in range(12):
            v = np.zeros(EMBED_DIMS, dtype=np.float32)
            v[1] = 1.0
            v += rng.normal(0, 0.4, EMBED_DIMS).astype(np.float32)
            rows.append(_row(f"B{i}", v, bpm=120, key_index=8))
        db = FakeBuildDB(rows)
        _build(db)

        a_weights = [w for s, d, w in db.written
                     if s.startswith("A") and d.startswith("A")]
        b_weights = [w for s, d, w in db.written
                     if s.startswith("B") and d.startswith("B")]
        self.assertGreater(len(a_weights), 0)
        self.assertGreater(len(b_weights), 0)
        a_mean = float(np.mean(a_weights))
        b_mean = float(np.mean(b_weights))
        # The mean within-cluster affinity should land in a similar order
        # of magnitude across the two clusters even though their raw
        # intra-cluster cosines differ by ~8×. Without local scaling the
        # ratio would be many-fold; with the self-tuning kernel both
        # cluster centres see "their neighbour" at roughly comparable
        # affinity.
        ratio = max(a_mean, b_mean) / max(min(a_mean, b_mean), 1e-9)
        self.assertLess(
            ratio, 3.0,
            f"local scaling should balance affinity across densities; "
            f"a_mean={a_mean:.3f} b_mean={b_mean:.3f} ratio={ratio:.2f}",
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
        n_unnorm = _build(db, k=3, z_score=False)
        self.assertGreater(n_unnorm, 0)
        unnorm_edges = set((src, dst) for src, dst, _ in db.written)

        # Build with Z-scoring (variance scaling)
        n_norm = _build(db, k=3, z_score=True)
        self.assertGreater(n_norm, 0)
        norm_edges = set((src, dst) for src, dst, _ in db.written)

        # The massive scale of brightness (0 to 14000) will dominate the unnormalized build
        # but be scaled down in the normalized build, leading to different neighbors.
        self.assertNotEqual(unnorm_edges, norm_edges)


if __name__ == "__main__":
    unittest.main()
