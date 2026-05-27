"""Unit tests for play queue modes, mutual exclusivity, aggressive context switching, and queue-dependent cache clearance in StreamripApp.
"""

import os
import sys
import tempfile
import shutil
import json
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

# ── Mock External Modules to run dependency-free ───────────────────────────
stub_modules = [
    "flet",
    "flet_audio",
    "flet_audio_service",
    "aiohttp",
    "aiosqlite",
    "aiofiles",
    "mutagen",
    "mutagen.mp3",
    "mutagen.flac",
    "mutagen.id3",
    "mutagen.easyid3",
    "mutagen.mp4",
    "tinytag",
    "tomlkit",
    "tomlkit.api",
    "tomlkit.toml_document",
    "numpy",
    "matplotlib",
    "matplotlib.pyplot",
    "seaborn",
    "certifi"
]

for mod in stub_modules:
    mock_mod = MagicMock()
    if mod == "numpy":
        mock_mod.ndarray = MagicMock
        mock_mod.float32 = MagicMock
    sys.modules[mod] = mock_mod

# Add parent dir to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Isolate DATA_DIR & XDG_CACHE_HOME
temp_dir = tempfile.mkdtemp(prefix="streamrip_test_")
os.environ["HOME"] = temp_dir
os.environ["XDG_CONFIG_HOME"] = temp_dir
os.environ["XDG_CACHE_HOME"] = os.path.join(temp_dir, ".cache")
os.makedirs(os.environ["XDG_CACHE_HOME"], exist_ok=True)

import main
from utils.audio_engine import audio_engine

# Helper to run async tests
def run_async(coro):
    return asyncio.run(coro)

class TestQueueModes(unittest.TestCase):
    def setUp(self):
        # Create temp dir for each test run to ensure total isolation
        self.test_dir = tempfile.mkdtemp(prefix="streamrip_test_")
        main.DATA_DIR = self.test_dir
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.test_dir, ".cache")
        os.makedirs(os.environ["XDG_CACHE_HOME"], exist_ok=True)

        # Mock Flet Page
        self.mock_page = MagicMock()
        def _mock_run_task(func, *args, **kwargs):
            if asyncio.iscoroutinefunction(func):
                try:
                    loop = asyncio.get_running_loop()
                    return loop.create_task(func(*args, **kwargs))
                except RuntimeError:
                    return asyncio.run(func(*args, **kwargs))
            else:
                return func(*args, **kwargs)
        self.mock_page.run_task = _mock_run_task
        self.mock_page.update = MagicMock()

        # Mock AudioEngine's native push
        audio_engine._page = self.mock_page
        audio_engine._push_queue_native = AsyncMock()
        audio_engine.clear_queue()
        audio_engine.is_shuffle = False

        # Mock StreamripFletApp
        self.app = MagicMock(spec=main.StreamripFletApp)
        self.app.page = self.mock_page
        self.app.play_similar_mode = False
        self.app._play_similar_gen = 0
        self.app.db_manager = AsyncMock()
        self.app._initiate_play_similar_queue_async = AsyncMock()
        
        # UI Mocks
        self.app.now_playing = MagicMock()
        self.app.mini_player = MagicMock()
        self.app.queue_sheet = MagicMock()
        
        # LibraryView Cache Mocks
        self.app.library_view = MagicMock()
        self.app.library_view.view_mode = "tracks"
        self.app.library_view.search_query = ""
        self.app.library_view.sort_mode = "date"
        self.app.library_view._tracks_cache_key = ("tracks", "", "date")
        
        # Preferences & save mock
        self.app._save_pref = MagicMock()
        self.app.show_snackbar = MagicMock()
        self.app.safe_update = lambda fn: fn()
        
        # Attach the real implementations of our new/modified methods to the mock app
        self.app._save_queue_to_file = main.StreamripFletApp._save_queue_to_file.__get__(self.app, main.StreamripFletApp)
        self.app._load_queue_from_file = main.StreamripFletApp._load_queue_from_file.__get__(self.app, main.StreamripFletApp)
        self.app._save_queue_state = main.StreamripFletApp._save_queue_state.__get__(self.app, main.StreamripFletApp)
        self.app._play_track_core = main.StreamripFletApp._play_track_core.__get__(self.app, main.StreamripFletApp)
        self.app.toggle_shuffle = main.StreamripFletApp.toggle_shuffle.__get__(self.app, main.StreamripFletApp)
        self.app._toggle_shuffle_async = main.StreamripFletApp._toggle_shuffle_async.__get__(self.app, main.StreamripFletApp)
        self.app.set_play_similar_mode = main.StreamripFletApp.set_play_similar_mode.__get__(self.app, main.StreamripFletApp)
        self.app.wipe_database = main.StreamripFletApp.wipe_database.__get__(self.app, main.StreamripFletApp)
        self.app.clear_library_index = main.StreamripFletApp.clear_library_index.__get__(self.app, main.StreamripFletApp)

        # Set up a sample library in the mock db_manager
        self.sample_tracks = [
            {"path": "/music/song1.mp3", "title": "Song 1", "artist": "Artist A", "album": "Album 1"},
            {"path": "/music/song2.mp3", "title": "Song 2", "artist": "Artist B", "album": "Album 2"},
            {"path": "/music/song3.mp3", "title": "Song 3", "artist": "Artist C", "album": "Album 3"},
        ]
        self.app.db_manager.get_all_tracks.return_value = self.sample_tracks
        self.app.library_view._tracks_cache = self.sample_tracks

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_regular_sequential_play(self):
        # Play track sequentially in normal mode
        run_async(self.app._play_track_core("/music/song2.mp3"))
        
        # Verify queue is populated sequentially
        self.assertEqual(len(audio_engine.queue), 3)
        self.assertEqual(audio_engine.current_index, 1)
        self.assertEqual(audio_engine.queue[audio_engine.current_index]["path"], "/music/song2.mp3")
        self.assertFalse(audio_engine.is_shuffle)
        
        # Verify that regular queue is cached in background
        self.app._save_queue_state()
        state = self.app._load_queue_from_file("queue_regular.json")
        self.assertIsNotNone(state)
        self.assertEqual(len(state["queue"]), 3)
        self.assertEqual(state["current_index"], 1)

    def test_shuffle_mutual_exclusivity_with_similar(self):
        # Start sequential play
        run_async(self.app._play_track_core("/music/song2.mp3"))
        
        # Set similar mode ON
        self.app.set_play_similar_mode(True)
        self.assertTrue(self.app.play_similar_mode)
        
        # Verify that toggling shuffle ON turns Play Similar mode OFF
        run_async(self.app._toggle_shuffle_async())
        self.assertTrue(audio_engine.is_shuffle)
        self.assertFalse(self.app.play_similar_mode)
        
        # Verify that toggling Play Similar ON turns Shuffle mode OFF
        self.app.set_play_similar_mode(True)
        self.assertTrue(self.app.play_similar_mode)
        self.assertFalse(audio_engine.is_shuffle)

    def test_shuffle_from_entire_library(self):
        # Set shuffle mode ON
        audio_engine.is_shuffle = True
        
        # Simulate tapping song 3 in search/album context containing only one item
        self.app.db_manager.get_tracks_by_album.return_value = [self.sample_tracks[2]]
        run_async(self.app._play_track_core("/music/song3.mp3", source=("album", "Artist C", "Album 3")))
        
        # Even though album context had only one song, shuffle mode shuffles from the ENTIRE library
        self.assertEqual(len(audio_engine.queue), 3)
        self.assertEqual(audio_engine.queue[audio_engine.current_index]["path"], "/music/song3.mp3")
        
        # Verify that shuffle queue state is cached to queue_shuffle.json
        self.app._save_queue_state()
        state = self.app._load_queue_from_file("queue_shuffle.json")
        self.assertIsNotNone(state)
        self.assertEqual(len(state["queue"]), 3)

    def test_aggressive_mode_switching_and_surviving(self):
        # 1. Start sequential play in regular mode
        run_async(self.app._play_track_core("/music/song1.mp3"))
        self.app._save_queue_state()
        
        # 2. Toggle Shuffle ON
        run_async(self.app._toggle_shuffle_async())
        self.app._save_queue_state()
        self.assertTrue(audio_engine.is_shuffle)
        
        # Verify regular state was written to file
        reg_state = self.app._load_queue_from_file("queue_regular.json")
        self.assertIsNotNone(reg_state)
        self.assertEqual(reg_state["current_index"], 0)
        
        # 3. Toggle Shuffle OFF
        run_async(self.app._toggle_shuffle_async())
        self.assertFalse(audio_engine.is_shuffle)
        
        # Verify regular queue was seamlessly restored and current index is correct
        self.assertEqual(audio_engine.queue[audio_engine.current_index]["path"], "/music/song1.mp3")

    def test_queue_dependent_clearance(self):
        # Setup active regular queue and populate files
        run_async(self.app._play_track_core("/music/song1.mp3"))
        self.app._save_queue_state()
        
        # Enable shuffle and populate files
        run_async(self.app._toggle_shuffle_async())
        self.app._save_queue_state()
        
        # Verify that regular and shuffle queue files exist
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, ".cache", "queue_regular.json")))
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, ".cache", "queue_shuffle.json")))
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "queue_state.json")))
        
        # Trigger database wipe (maintenance task)
        run_async(self.app.wipe_database())
        
        # Verify queue is empty and ALL cache files are deleted (queue dependent clearance)
        self.assertEqual(len(audio_engine.queue), 0)
        self.assertFalse(os.path.exists(os.path.join(self.test_dir, ".cache", "queue_regular.json")))
        self.assertFalse(os.path.exists(os.path.join(self.test_dir, ".cache", "queue_shuffle.json")))
        self.assertFalse(os.path.exists(os.path.join(self.test_dir, "queue_state.json")))

if __name__ == "__main__":
    unittest.main()
