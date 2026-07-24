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
        tg.invalidate_coord_graph_cache()  # a fresh db may reuse a freed id()

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
        # One artist + album PER TRACK. A shared album would (correctly) trip
        # the walk's per-album repeat cap and truncate every queue to one track.
        for i in range(self.num_tracks):
            await conn.execute(
                "INSERT OR IGNORE INTO artists (id, name) VALUES (?, ?)",
                (i + 1, f"Fake Artist {i}"),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO albums (id, artist_id, title, genre) "
                "VALUES (?, ?, ?, 'Electronic')",
                (i + 1, i + 1, f"Fake Album {i}"),
            )
        for i in range(self.num_tracks):
            path = self.paths[i]
            # Write to tracks
            await conn.execute(
                "INSERT INTO tracks (path, title, duration, album_id) VALUES (?, ?, ?, ?)",
                (path, f"Track {i}", 180.0, i + 1)
            )
            # Write to play_counts (which holds pca_coords, cluster_id, etc.)
            await conn.execute(
                "INSERT INTO play_counts (track_path, count, pca_coords, cluster_id) VALUES (?, 0, ?, ?)",
                (path, self.coords[i].tobytes(), i % 3)
            )
        await conn.commit()

    async def asyncTearDown(self):
        await self.db.close()
        self._cleanup_db_files()
        tg.invalidate_coord_graph_cache()

    async def test_coordinate_graph_walk_runs_and_is_deterministic(self):
        # The walk is served by the in-RAM coordinate graph and has no
        # stochastic mode at all now, so repeat calls must be byte-identical.
        seed = self.paths[0]
        walk_a = await tg.walk(self.db, seed, length=6)
        walk_b = await tg.walk(self.db, seed, length=6)
        self.assertEqual(walk_a, walk_b, "Coordinate-graph walk is not reproducible!")
        self.assertEqual(len(walk_a), 6)
        self.assertNotIn(seed, walk_a)

if __name__ == "__main__":
    unittest.main()
