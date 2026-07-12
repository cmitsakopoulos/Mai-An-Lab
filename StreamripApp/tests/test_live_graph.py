import asyncio
import os
import sys
import unittest
import numpy as np
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock config so tests don't touch the user's real app config
import utils.config as _cfg
_cfg.APP_DIR = os.path.join(os.path.dirname(__file__), "dsptest_app_dir")
os.makedirs(_cfg.APP_DIR, exist_ok=True)

from utils.db_manager import DatabaseManager
from utils import track_graph as tg
from utils.streamrip_api import get_config_path, update_config_params

class TestLiveGraphCoordinatesWalk(unittest.IsolatedAsyncioTestCase):

    def _cleanup_db_files(self):
        for suffix in ["", "-shm", "-wal"]:
            path = self.db_path + suffix
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

    async def asyncSetUp(self):
        # Create a fresh database manager for the test
        self.db_path = os.path.join(os.path.dirname(__file__), "test_live_graph.db")
        self._cleanup_db_files()
            
        self.db = DatabaseManager(self.db_path)
        await self.db.initialize()
        
        # Explicitly run the cluster_id column migration since play_counts was created during initialize
        conn = await self.db.get_connection()
        await self.db._migrate_clusters(conn)
        
        # Insert a set of synthetic tracks with PCA coordinates, cluster IDs, etc.
        # Let's create 12 tracks
        self.num_tracks = 12
        self.paths = [f"/path/track_{i}.mp3" for i in range(self.num_tracks)]
        
        # 10-dimensional PCA coordinate space
        np.random.seed(42)
        self.coords = np.random.randn(self.num_tracks, 10).astype(np.float32)
        
        # Save tracks to db
        conn = await self.db.get_connection()
        await conn.execute("INSERT OR IGNORE INTO artists (id, name) VALUES (1, 'Fake Artist')")
        await conn.execute("INSERT OR IGNORE INTO albums (id, artist_id, title, genre) VALUES (1, 1, 'Fake Album', 'Electronic')")
        for i in range(self.num_tracks):
            path = self.paths[i]
            # Write to tracks
            await conn.execute(
                "INSERT INTO tracks (path, title, duration, album_id) VALUES (?, ?, ?, 1)",
                (path, f"Track {i}", 180.0)
            )
            # Write to play_counts (which holds pca_coords, cluster_id, etc.)
            await conn.execute(
                "INSERT INTO play_counts (track_path, count, pca_coords, cluster_id) VALUES (?, 0, ?, ?)",
                (path, self.coords[i].tobytes(), i % 3)
            )
        await conn.commit()

        # Build acoustic edges (this saves V_keep, means, stds, feature_spec to pca_space)
        # We manually update pca_space feature spec so that load_pca_space works
        spec = {
            "surviving": [],
            "scalar_weight": 1.0,
            "embed_dims": 10,
            "z_score": True,
            "harmonic_names": [],
            "harmonic_weight": 1.5,
            "harmonic_means": [],
            "harmonic_stds": [],
            "k_neighbors": 5,
            "csls_beta": 0.0
        }
        means = np.zeros(10, dtype=np.float32)
        stds = np.ones(10, dtype=np.float32)
        V_keep = np.eye(10, dtype=np.float32)
        eigenvalues = np.ones(10, dtype=np.float32)
        await self.db.save_pca_space(means, stds, V_keep, eigenvalues, spec)

        # Build track_neighbors using the standard _knn_edges algorithm
        from utils.track_graph import _knn_edges
        edges, mutual_pairs, N, k_eff, mutual_total = _knn_edges(self.coords, self.paths, k=5, csls_beta=0.0)
        await self.db.replace_neighbors_bulk(edges, "acoustic")

    async def asyncTearDown(self):
        await self.db.close()
        self._cleanup_db_files()
        update_config_params({"general": {"walk_coordinates_only": False}})

    async def test_coordinates_only_walk_identical_to_db_walk(self):
        # 1. First run walk in default mode (walk_coordinates_only = False)
        update_config_params({"general": {"walk_coordinates_only": False}})
        seed = self.paths[0]
        
        walk_db = await tg.walk(self.db, seed, length=6, temperature=0.0)
        
        # 2. Run walk in coordinates-only mode (walk_coordinates_only = True)
        update_config_params({"general": {"walk_coordinates_only": True}})
        
        walk_live = await tg.walk(self.db, seed, length=6, temperature=0.0)
        
        # Assert they are 100% identical
        self.assertEqual(walk_db, walk_live, "Walk paths do not match between DB-backed and coordinates-only modes!")
        
        # Assert structural properties
        self.assertEqual(len(walk_live), 6)
        self.assertNotIn(seed, walk_live)

    async def test_coordinates_only_walk_temperature(self):
        # Verify that temperature works identically or produces variety in coordinates-only mode too
        update_config_params({"general": {"walk_coordinates_only": True}})
        seed = self.paths[0]
        
        # Walk with temp > 0 using same seed should return same result if seed is locked
        walk_1 = await tg.walk(self.db, seed, length=6, temperature=0.4, rng_seed=42)
        walk_2 = await tg.walk(self.db, seed, length=6, temperature=0.4, rng_seed=42)
        self.assertEqual(walk_1, walk_2, "Stochastic walks with identical rng_seed and coordinates-only mode differ!")

if __name__ == "__main__":
    unittest.main()
