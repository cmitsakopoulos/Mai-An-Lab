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
from main import audio_engine

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
        audio_engine._audio = MagicMock()
        audio_engine._push_queue_native = AsyncMock()
        audio_engine.clear_queue()
        audio_engine.is_shuffle = False
        audio_engine.repeat_mode = "none"
        audio_engine.play_similar_seed_path = ""
        audio_engine.clear_observers()

        # Mock StreamripFletApp
        self.app = MagicMock(spec=main.StreamripFletApp)
        self.app.page = self.mock_page
        self.app.play_similar_mode = False
        self.app.auto_dj_mode = False
        self.app._play_similar_gen = 0
        self.app._session_bad_paths = []
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
        self.app._prefs = {}
        self.app._prefs_path = os.path.join(self.test_dir, "flet_prefs.json")
        def _mock_save_pref(key, value):
            print(f"DIAGNOSTIC_WRITE: key={key}, value={value}")
            self.app._prefs[key] = value
            with open(self.app._prefs_path, "w") as fh:
                json.dump(self.app._prefs, fh)
        self.app._save_pref = _mock_save_pref
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
        self.app.set_auto_dj_mode = main.StreamripFletApp.set_auto_dj_mode.__get__(self.app, main.StreamripFletApp)
        self.app._initiate_auto_dj_queue_async = main.StreamripFletApp._initiate_auto_dj_queue_async.__get__(self.app, main.StreamripFletApp)
        self.app._auto_dj_auto_continue_queue = main.StreamripFletApp._auto_dj_auto_continue_queue.__get__(self.app, main.StreamripFletApp)
        self.app._on_feedback_click = main.StreamripFletApp._on_feedback_click.__get__(self.app, main.StreamripFletApp)
        self.app._explicit_feedback_cache = {}
        self.app._refresh_feedback_buttons = MagicMock()
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

    def test_play_similar_toggle_and_session_recovery(self):
        # 1. Start sequential play in regular mode
        run_async(self.app._play_track_core("/music/song1.mp3"))
        self.assertEqual(len(audio_engine.queue), 3)
        self.assertEqual(audio_engine.current_index, 0)

        # 2. Toggle Play Similar ON
        self.app.set_play_similar_mode(True)
        self.assertTrue(self.app.play_similar_mode)
        
        # Verify regular queue was backed up in memory and written to disk
        self.assertEqual(len(self.app.play_similar_saved_queue), 3)
        self.assertFalse(self.app.play_similar_saved_shuffle)
        
        reg_state = self.app._load_queue_from_file("queue_regular.json")
        self.assertIsNotNone(reg_state)
        self.assertEqual(len(reg_state["queue"]), 3)

        # 3. Simulate walks appending tracks by manually mutating the queue (representing walk completion)
        audio_engine.queue = list(audio_engine.queue) + [{"path": "/music/walk1.mp3", "title": "Walk 1", "artist": "Artist W", "album": "Walk Album"}]
        audio_engine.current_index = 3 # Move to walk track
        
        # 4. Save queue state (session termination simulation)
        self.app._save_queue_state()
        
        # Verify state.json contains similar queue AND backup regular queue metadata
        with open(os.path.join(main.DATA_DIR, "queue_state.json")) as f:
            saved_state = json.load(f)
        self.assertEqual(len(saved_state["queue"]), 4)
        self.assertEqual(saved_state["current_index"], 3)
        self.assertIsNotNone(saved_state["play_similar_saved_queue"])
        self.assertFalse(saved_state["play_similar_saved_shuffle"])

        # 5. Clear memory state & simulate full session restoration
        self.app.play_similar_saved_queue = None
        self.app.play_similar_saved_index = None
        self.app.play_similar_saved_shuffle = False
        
        # Mock the async restore method's environment and run it
        self.app.is_restoring_session = False
        self.app._read_queue_state = main.StreamripFletApp._read_queue_state.__get__(self.app, main.StreamripFletApp)
        self.app._restore_queue_state_async = main.StreamripFletApp._restore_queue_state_async.__get__(self.app, main.StreamripFletApp)
        run_async(self.app._restore_queue_state_async())

        # Verify restoration successfully loaded the similar queue AND restored backup memory states
        self.assertEqual(len(audio_engine.queue), 4)
        self.assertEqual(audio_engine.current_index, 3)
        self.assertTrue(self.app.play_similar_mode)
        self.assertEqual(len(self.app.play_similar_saved_queue), 3)
        self.assertFalse(self.app.play_similar_saved_shuffle)

        # 6. Toggle Play Similar OFF
        self.app.set_play_similar_mode(False)
        self.assertFalse(self.app.play_similar_mode)
        
        # Verify the regular queue was restored behind the active track, and UI was synced
        self.assertEqual(audio_engine.queue[0]["path"], "/music/walk1.mp3")
        self.assertEqual(audio_engine.current_index, 0)
        self.assertEqual(len(audio_engine.queue), 4) # walk1.mp3 + 3 original tracks
        self.app.now_playing.update_play_similar.assert_called_with(False)
        self.app.mini_player.update_play_similar.assert_called_with(False)

    def test_play_new_track_in_similar_mode(self):
        # 1. Start sequential play in regular mode (3 tracks)
        run_async(self.app._play_track_core("/music/song1.mp3"))
        self.assertEqual(len(audio_engine.queue), 3)

        # 2. Toggle Play Similar ON
        self.app.set_play_similar_mode(True)
        self.assertTrue(self.app.play_similar_mode)
        self.assertEqual(len(self.app.play_similar_saved_queue), 3)

        # 3. Simulate playing a completely new track (e.g. from an album click)
        # Mock database manager to return a new album of 2 tracks when resolved
        new_album_tracks = [
            {"path": "/music/album1.mp3", "title": "Album Track 1", "artist": "Artist X", "album": "Album X"},
            {"path": "/music/album2.mp3", "title": "Album Track 2", "artist": "Artist X", "album": "Album X"},
        ]
        self.app.library_view._tracks_cache = new_album_tracks
        self.app.library_view._tracks_cache_key = ("tracks", "", "date") # force matches general cache key
        
        # Trigger play for the first track of the new album
        run_async(self.app._play_track_core("/music/album1.mp3"))

        # 4. Verify Play Similar mode remains active, but context is updated
        self.assertTrue(self.app.play_similar_mode)
        
        # The active queue should only contain the clicked track at this stage (awaiting walk injection)
        self.assertEqual(len(audio_engine.queue), 1)
        self.assertEqual(audio_engine.queue[0]["path"], "/music/album1.mp3")

        # The new album context must be backed up to memory and partition cache
        self.assertEqual(len(self.app.play_similar_saved_queue), 2)
        self.assertEqual(self.app.play_similar_saved_index, 0)
        
        reg_state = self.app._load_queue_from_file("queue_regular.json")
        self.assertIsNotNone(reg_state)
        self.assertEqual(len(reg_state["queue"]), 2)
        self.assertEqual(reg_state["queue"][0]["path"], "/music/album1.mp3")

        # 5. Toggle Play Similar OFF
        self.app.set_play_similar_mode(False)
        self.assertFalse(self.app.play_similar_mode)

        # The new album context should be restored sequentially behind the currently playing track
        self.assertEqual(len(audio_engine.queue), 2)
        self.assertEqual(audio_engine.queue[0]["path"], "/music/album1.mp3")
        self.assertEqual(audio_engine.queue[1]["path"], "/music/album2.mp3")
        self.assertEqual(audio_engine.current_index, 0)

    def test_play_similar_autoreplenish_non_jarvis(self):
        # Setup real callbacks and bindings for on_similar_continue
        self.app._session_bad_paths = []
        self.app._on_similar_continue = main.StreamripFletApp._on_similar_continue.__get__(self.app, main.StreamripFletApp)
        self.app._similar_auto_continue_queue = main.StreamripFletApp._similar_auto_continue_queue.__get__(self.app, main.StreamripFletApp)
        audio_engine.bind(on_similar_continue=self.app._on_similar_continue)

        # 1. Start sequential play in Play Similar mode
        run_async(self.app._play_track_core("/music/song1.mp3"))
        self.app.set_play_similar_mode(True)
        self.assertTrue(self.app.play_similar_mode)

        # Truncate queue to just the active track so next advance will run it dry
        audio_engine.queue = [{"path": "/music/song1.mp3", "title": "Song 1", "artist": "Artist A", "album": "Album 1"}]
        audio_engine.current_index = 0
        audio_engine.play_similar_seed_path = "/music/song1.mp3"

        # Mock graph walker & database lookup for replenishment
        new_walk_tracks = ["/music/walk1.mp3", "/music/walk2.mp3"]
        self.app.db_manager.get_track_full.side_effect = lambda p: {
            "path": p, "title": "Walk Title", "artist": "Walk Artist", "album": "Walk Album"
        }

        # Patch walk to return our mocked walk tracks
        with patch("utils.track_graph.walk", new_callable=AsyncMock) as mock_walk:
            mock_walk.return_value = new_walk_tracks
            
            # 2. Advance the queue dry (should trigger silent replenishment)
            audio_engine.next()
            
            # Wait for loop cycles so the async replenishment task completes
            async def wait_cycles():
                await asyncio.sleep(0.01)
            run_async(wait_cycles())
            
            # Verify replenishment successfully resolved and appended walk tracks
            # Queue size should be 1 (original) + 2 (appended) = 3 tracks
            self.assertEqual(len(audio_engine.queue), 3)
            # It should have skipped to index 1 (the first newly appended walk track)
            self.assertEqual(audio_engine.current_index, 1)
            self.assertEqual(audio_engine.queue[audio_engine.current_index]["path"], "/music/walk1.mp3")

    def test_auto_dj_mode(self):
        # 1. Start sequential playback in normal mode
        run_async(self.app._play_track_core("/music/song1.mp3"))
        self.assertEqual(len(audio_engine.queue), 3)
        self.assertEqual(audio_engine.current_index, 0)

        # Mock taste model and PCA projection space
        import numpy as np
        # Cold taste model mock
        self.app.db_manager.get_taste_model.return_value = None
        self.app.db_manager.get_tracks_with_features.return_value = [
            {"path": "/music/song1.mp3", "title": "Song 1", "artist": "Artist A", "album": "Album 1"},
            {"path": "/music/song2.mp3", "title": "Song 2", "artist": "Artist B", "album": "Album 2"},
            {"path": "/music/song3.mp3", "title": "Song 3", "artist": "Artist C", "album": "Album 3"},
        ]
        self.app.db_manager.load_pca_space.return_value = (
            np.zeros(8, dtype=np.float32),  # means
            np.ones(8, dtype=np.float32),   # stds
            np.eye(8, 3, dtype=np.float32), # projection matrix V_keep
        )
        self.app.db_manager.get_track_full.side_effect = lambda p: next(
            (t for t in self.sample_tracks if t["path"] == p), None
        )

        # 2. Toggle Auto-DJ ON
        self.app.set_auto_dj_mode(True)
        self.assertTrue(self.app.auto_dj_mode)
        
        # Wait for the async task to populate the Auto-DJ queue
        async def wait_cycles():
            await asyncio.sleep(0.01)
        run_async(wait_cycles())

        # Auto-DJ should have populated the queue
        self.assertGreater(len(audio_engine.queue), 0)
        self.assertEqual(audio_engine.queue[0]["path"], "/music/song1.mp3")

        # Play Similar should be mutually exclusive (False)
        self.assertFalse(self.app.play_similar_mode)

        # Toggle Play Similar ON should disable Auto-DJ
        self.app.set_play_similar_mode(True)
        self.assertTrue(self.app.play_similar_mode)
        self.assertFalse(self.app.auto_dj_mode)

        # Toggle Auto-DJ back ON should disable Play Similar
        self.app.set_auto_dj_mode(True)
        self.assertTrue(self.app.auto_dj_mode)
        self.assertFalse(self.app.play_similar_mode)

        # 3. Toggle Auto-DJ OFF
        self.app.set_auto_dj_mode(False)
        self.assertFalse(self.app.auto_dj_mode)

        # Original queue must be restored sequential play
        self.assertEqual(len(audio_engine.queue), 3)
        self.assertEqual(audio_engine.queue[0]["path"], "/music/song1.mp3")
        self.assertEqual(audio_engine.queue[1]["path"], "/music/song2.mp3")
        self.assertEqual(audio_engine.current_index, 0)

    def test_dislike_advance_ownership(self):
        # Mock taste model and PCA projection space
        import numpy as np
        self.app.db_manager.get_taste_model.return_value = None
        self.app.db_manager.get_tracks_with_features.return_value = [
            {"path": "/music/song1.mp3", "title": "Song 1", "artist": "Artist A", "album": "Album 1"},
            {"path": "/music/song2.mp3", "title": "Song 2", "artist": "Artist B", "album": "Album 2"},
            {"path": "/music/song3.mp3", "title": "Song 3", "artist": "Artist C", "album": "Album 3"},
        ]
        self.app.db_manager.load_pca_space.return_value = (
            np.zeros(8, dtype=np.float32),  # means
            np.ones(8, dtype=np.float32),   # stds
            np.eye(8, 3, dtype=np.float32), # projection matrix V_keep
        )
        self.app.db_manager.get_track_full.side_effect = lambda p: next(
            (t for t in self.sample_tracks if t["path"] == p), None
        )

        # Setup initial sequential queue
        run_async(self.app._play_track_core("/music/song1.mp3"))
        self.assertEqual(len(audio_engine.queue), 3)
        self.assertEqual(audio_engine.current_index, 0)

        # 1. Play Similar active: disliking should NOT skip/advance
        self.app.set_play_similar_mode(True)
        self.assertTrue(self.app.play_similar_mode)
        
        # Click dislike (like=False)
        self.app._on_feedback_click(False)
        # Should remain at index 0 (no advance)
        self.assertEqual(audio_engine.current_index, 0)

        # 2. Auto-DJ active: disliking MUST skip/advance to next track
        self.app.set_auto_dj_mode(True)
        self.assertTrue(self.app.auto_dj_mode)
        self.assertFalse(self.app.play_similar_mode)

        # Re-set queue to clear index for clean skip test
        audio_engine.current_index = 0
        
        # Click dislike (like=False)
        self.app._on_feedback_click(False)
        # Auto-DJ should have advanced index to 1 (skipped track)
        self.assertEqual(audio_engine.current_index, 1)

    def test_sigkill_atomic_queue_state_durability(self):
        # 1. Setup a valid queue state file
        run_async(self.app._play_track_core("/music/song1.mp3"))
        self.app._save_queue_state()
        state_file_path = os.path.join(self.test_dir, "queue_state.json")
        self.assertTrue(os.path.exists(state_file_path))
        with open(state_file_path, "r") as f:
            original_data = json.load(f)
        
        # 2. Simulate a crash / SIGKILL during the NEXT write
        # We simulate this by patching os.replace to raise KeyboardInterrupt (which simulates a hard termination exception)
        # and checking that the original file is left completely uncorrupted.
        with patch("os.replace", side_effect=KeyboardInterrupt("Simulated SIGKILL/OSKILL")):
            with self.assertRaises(KeyboardInterrupt):
                self.app._save_queue_state()
        
        # 3. Verify that the original queue_state.json is still present and valid
        self.assertTrue(os.path.exists(state_file_path))
        with open(state_file_path, "r") as f:
            restored_data = json.load(f)
        self.assertEqual(restored_data["queue"][0]["path"], "/music/song1.mp3")

    def test_rapid_concurrency_race_condition_resilience(self):
        # Setup initial sequential play
        run_async(self.app._play_track_core("/music/song1.mp3"))
        
        # We will trigger a backlog of concurrent tasks in parallel using asyncio.gather.
        # This stresses the mutual exclusivity and the generation-counter (`_play_similar_gen`) protection.
        async def toggle_similar_rapidly():
            # Parallel toggles of play similar and shuffle
            for _ in range(5):
                self.app.set_play_similar_mode(True)
                await asyncio.sleep(0.001)
                self.app.toggle_shuffle()
                await asyncio.sleep(0.001)
                self.app.set_play_similar_mode(False)
                await asyncio.sleep(0.001)

        async def cycle_repeat_and_skips():
            # Parallel repeat cycling and skips
            self.app.cycle_repeat = main.StreamripFletApp.cycle_repeat.__get__(self.app, main.StreamripFletApp)
            for _ in range(5):
                self.app.cycle_repeat()
                audio_engine.next()
                await asyncio.sleep(0.001)
                audio_engine.previous()
                await asyncio.sleep(0.001)

        # Run both async loops concurrently to simulate rapid button mashing / multi-thread backlogs
        async def run_stress():
            await asyncio.gather(
                toggle_similar_rapidly(),
                cycle_repeat_and_skips()
            )
        
        run_async(run_stress())
        
        # Verify that despite the stress, play similar state is consistently handled and queue integrity is preserved
        self.assertTrue(len(audio_engine.queue) > 0)
        self.assertIn(audio_engine.repeat_mode, ["none", "all", "one"])

    def test_dj_deprecation_graceful_no_op(self):
        # Even if a deprecated set_auto_dj_mode(True) is invoked, it should not crash the player thread.
        # Verify set_auto_dj_mode toggles gracefully or is a standard safe flow.
        self.app.set_auto_dj_mode(True)
        self.assertTrue(self.app.auto_dj_mode)
        
        # When set_play_similar_mode(True) is triggered, Auto-DJ should turn off as usual.
        self.app.set_play_similar_mode(True)
        self.assertTrue(self.app.play_similar_mode)
        self.assertFalse(self.app.auto_dj_mode)

    def test_chaotic_crash_restoration_loop(self):
        # Helper to simulate a hard SIGKILL and app restart
        def crash_and_restart():
            # 1. Save queue state (auto-saving on state change)
            self.app._save_queue_state()
            
            # 2. Wipe memory completely (simulating process death)
            audio_engine.clear_queue()
            audio_engine._audio = MagicMock()
            audio_engine.is_shuffle = False
            audio_engine.repeat_mode = "none"
            self.app.play_similar_mode = False
            self.app.play_similar_saved_queue = None
            self.app.play_similar_saved_index = None
            self.app.play_similar_saved_shuffle = False
            
            # 3. Reload preferences from flet_prefs.json to simulate cold boot loading
            self.app._prefs = {}
            if os.path.exists(self.app._prefs_path):
                with open(self.app._prefs_path, "r") as fh:
                    content = fh.read()
                    print(f"DIAGNOSTIC: flet_prefs.json content: {content}")
                    self.app._prefs = json.loads(content)
            print(f"DIAGNOSTIC: Loaded self.app._prefs: {self.app._prefs}")
            # Re-apply startup preferences logic
            audio_engine.is_shuffle = bool(self.app._prefs.get("is_shuffle", False))
            audio_engine.repeat_mode = self.app._prefs.get("repeat_mode", "none")
            self.app.play_similar_mode = bool(self.app._prefs.get("play_similar_mode", False))
            self.app.auto_dj_mode = bool(self.app._prefs.get("auto_dj_mode", False))
            
            # 4. Restore state (simulating fresh app boot)
            self.app.is_restoring_session = False
            self.app._read_queue_state = main.StreamripFletApp._read_queue_state.__get__(self.app, main.StreamripFletApp)
            self.app._restore_queue_state_async = main.StreamripFletApp._restore_queue_state_async.__get__(self.app, main.StreamripFletApp)
            run_async(self.app._restore_queue_state_async())

        # Operation 1: Start standard sequential queue
        run_async(self.app._play_track_core("/music/song2.mp3"))
        self.assertEqual(len(audio_engine.queue), 3)
        self.assertEqual(audio_engine.current_index, 1)

        # CRASH 1
        crash_and_restart()
        self.assertEqual(len(audio_engine.queue), 3)
        self.assertEqual(audio_engine.current_index, 1)
        self.assertFalse(self.app.play_similar_mode)

        # Operation 2: Toggle Play Similar Mode ON
        self.app.set_play_similar_mode(True)
        self.assertTrue(self.app.play_similar_mode)
        self.assertEqual(len(self.app.play_similar_saved_queue), 3)

        # CRASH 2
        crash_and_restart()
        self.assertTrue(self.app.play_similar_mode)
        self.assertEqual(len(self.app.play_similar_saved_queue), 3)

        # Operation 3: Skip to next song while in Similar Mode (simulating walk continuation)
        audio_engine.queue = list(audio_engine.queue) + [{"path": "/music/walk1.mp3", "title": "Walk 1", "artist": "Artist W", "album": "Walk Album"}]
        audio_engine.current_index = 3

        # CRASH 3
        crash_and_restart()
        self.assertTrue(self.app.play_similar_mode)
        self.assertEqual(audio_engine.current_index, 3)
        self.assertEqual(audio_engine.queue[3]["path"], "/music/walk1.mp3")

        # Operation 4: Toggle Shuffle ON (should disable Play Similar)
        self.app.toggle_shuffle = main.StreamripFletApp.toggle_shuffle.__get__(self.app, main.StreamripFletApp)
        self.app._toggle_shuffle_async = main.StreamripFletApp._toggle_shuffle_async.__get__(self.app, main.StreamripFletApp)
        run_async(self.app._toggle_shuffle_async())
        self.assertTrue(audio_engine.is_shuffle)
        self.assertFalse(self.app.play_similar_mode)

        # CRASH 4
        crash_and_restart()
        self.assertTrue(audio_engine.is_shuffle)
        self.assertFalse(self.app.play_similar_mode)

        # Operation 5: Cycle Repeat Mode to 'one'
        self.app.cycle_repeat = main.StreamripFletApp.cycle_repeat.__get__(self.app, main.StreamripFletApp)
        self.app.cycle_repeat() # none -> one
        self.assertEqual(audio_engine.repeat_mode, "one")

        # CRASH 5
        crash_and_restart()
        self.assertEqual(audio_engine.repeat_mode, "one")
        self.assertTrue(audio_engine.is_shuffle)

        # Operation 6: Toggle Shuffle OFF (restores regular sequential queue)
        run_async(self.app._toggle_shuffle_async())
        self.assertFalse(audio_engine.is_shuffle)

        # CRASH 6
        crash_and_restart()
        self.assertFalse(audio_engine.is_shuffle)

    def test_ridiculous_os_kill_interleaved_lifecycle(self):
        # Hyper-strict fuzzing test that simulates OS SIGKILLs interleaved in between every single skip, toggle, and mode transition.
        def crash_and_restart():
            # 1. Save queue state (auto-saving on state change)
            self.app._save_queue_state()
            
            # 2. Wipe memory completely (simulating process death)
            audio_engine.clear_queue()
            audio_engine._audio = MagicMock()
            audio_engine.is_shuffle = False
            audio_engine.repeat_mode = "none"
            self.app.play_similar_mode = False
            self.app.play_similar_saved_queue = None
            self.app.play_similar_saved_index = None
            self.app.play_similar_saved_shuffle = False
            
            # 3. Reload preferences from flet_prefs.json to simulate cold boot loading
            self.app._prefs = {}
            if os.path.exists(self.app._prefs_path):
                with open(self.app._prefs_path, "r") as fh:
                    self.app._prefs = json.load(fh)
            
            # Re-apply startup preferences logic
            audio_engine.is_shuffle = bool(self.app._prefs.get("is_shuffle", False))
            audio_engine.repeat_mode = self.app._prefs.get("repeat_mode", "none")
            self.app.play_similar_mode = bool(self.app._prefs.get("play_similar_mode", False))
            self.app.auto_dj_mode = bool(self.app._prefs.get("auto_dj_mode", False))
            
            # 4. Restore state (simulating fresh app boot)
            self.app.is_restoring_session = False
            self.app._read_queue_state = main.StreamripFletApp._read_queue_state.__get__(self.app, main.StreamripFletApp)
            self.app._restore_queue_state_async = main.StreamripFletApp._restore_queue_state_async.__get__(self.app, main.StreamripFletApp)
            run_async(self.app._restore_queue_state_async())

        # Step 1: Initialize regular queue and start playing
        run_async(self.app._play_track_core("/music/song2.mp3"))
        self.assertEqual(len(audio_engine.queue), 3)
        self.assertEqual(audio_engine.current_index, 1)

        # OS KILL & RESTORE
        crash_and_restart()
        self.assertEqual(len(audio_engine.queue), 3)
        self.assertEqual(audio_engine.current_index, 1)
        self.assertFalse(self.app.play_similar_mode)

        # Step 2: Cycle Repeat Mode to 'one'
        self.app.cycle_repeat = main.StreamripFletApp.cycle_repeat.__get__(self.app, main.StreamripFletApp)
        self.app.cycle_repeat() # none -> one
        self.assertEqual(audio_engine.repeat_mode, "one")

        # OS KILL & RESTORE
        crash_and_restart()
        self.assertEqual(audio_engine.repeat_mode, "one")

        # Step 3: Toggle Play Similar Mode ON
        self.app.set_play_similar_mode(True)
        self.assertTrue(self.app.play_similar_mode)

        # OS KILL & RESTORE
        crash_and_restart()
        self.assertTrue(self.app.play_similar_mode)
        # Note: Repeat mode is preserved during Play Similar mode load
        self.assertEqual(audio_engine.repeat_mode, "one")

        # Step 4: Cycle Repeat Mode: one -> all -> none (so next skip will trigger walk replenishment instead of seeking to 0)
        self.app.cycle_repeat() # one -> all
        self.app.cycle_repeat() # all -> none
        self.assertEqual(audio_engine.repeat_mode, "none")

        # OS KILL & RESTORE
        crash_and_restart()
        self.assertEqual(audio_engine.repeat_mode, "none")
        self.assertTrue(self.app.play_similar_mode)

        # Step 5: Advance/Skip to next (triggering play similar walk replenishment)
        # Truncate queue to just the active track so next advance runs it dry
        audio_engine.queue = [{"path": "/music/song2.mp3", "title": "Song 2", "artist": "Artist B", "album": "Album 2"}]
        audio_engine.current_index = 0
        audio_engine.play_similar_seed_path = "/music/song2.mp3"
        self.app._session_bad_paths = []
        self.app._on_similar_continue = main.StreamripFletApp._on_similar_continue.__get__(self.app, main.StreamripFletApp)
        self.app._similar_auto_continue_queue = main.StreamripFletApp._similar_auto_continue_queue.__get__(self.app, main.StreamripFletApp)
        audio_engine.bind(on_similar_continue=self.app._on_similar_continue)

        new_walk_tracks = ["/music/walk1.mp3", "/music/walk2.mp3"]
        self.app.db_manager.get_track_full.side_effect = lambda p: {
            "path": p, "title": "Walk Title", "artist": "Walk Artist", "album": "Walk Album"
        }

        with patch("utils.track_graph.walk", new_callable=AsyncMock) as mock_walk:
            mock_walk.return_value = new_walk_tracks
            
            # Skip triggers replenishment
            audio_engine.next()
            
            # Wait for replenishment tasks
            async def wait_cycles():
                await asyncio.sleep(0.01)
            run_async(wait_cycles())

        # Verify walk tracks were appended
        self.assertEqual(len(audio_engine.queue), 3)
        self.assertEqual(audio_engine.current_index, 1)
        self.assertEqual(audio_engine.queue[1]["path"], "/music/walk1.mp3")

        # OS KILL & RESTORE
        crash_and_restart()
        self.assertTrue(self.app.play_similar_mode)
        self.assertEqual(len(audio_engine.queue), 3)
        self.assertEqual(audio_engine.current_index, 1)
        self.assertEqual(audio_engine.queue[1]["path"], "/music/walk1.mp3")

        # Step 6: Toggle Shuffle ON (disables play similar, shuffles Regular queue)
        self.app.toggle_shuffle = main.StreamripFletApp.toggle_shuffle.__get__(self.app, main.StreamripFletApp)
        self.app._toggle_shuffle_async = main.StreamripFletApp._toggle_shuffle_async.__get__(self.app, main.StreamripFletApp)
        run_async(self.app._toggle_shuffle_async())
        self.assertTrue(audio_engine.is_shuffle)
        self.assertFalse(self.app.play_similar_mode)

        # OS KILL & RESTORE
        crash_and_restart()
        self.assertTrue(audio_engine.is_shuffle)
        self.assertFalse(self.app.play_similar_mode)

        # Step 7: Skip next in Shuffle mode
        audio_engine.next()
        self.assertIn(audio_engine.current_index, [0, 1, 2])
        shuffled_index = audio_engine.current_index

        # OS KILL & RESTORE
        crash_and_restart()
        self.assertTrue(audio_engine.is_shuffle)
        self.assertEqual(audio_engine.current_index, shuffled_index)

        # Step 8: Toggle Shuffle OFF (restores regular sequential queue behind the current track)
        run_async(self.app._toggle_shuffle_async())
        self.assertFalse(audio_engine.is_shuffle)

        # OS KILL & RESTORE
        crash_and_restart()
        self.assertFalse(audio_engine.is_shuffle)
        self.assertEqual(len(audio_engine.queue), 4)

    def test_jarvis_init_caching_and_concurrency(self):
        # Stress-test Jarvis (AssistantView) cache-gating, edge rebuilds, and concurrency controls
        from ui.views.assistant import AssistantView
        
        # Instantiate AssistantView
        view = MagicMock(spec=AssistantView)
        view.app = self.app
        view.page = self.mock_page
        view._init_started = False
        view._init_greeted = False
        view._runner = None
        view._history_list = []
        view._set_banner = MagicMock()
        view._append_bubble = AsyncMock()
        view.chat_memory = MagicMock()
        
        # Bind the real _do_init and _init_assistant methods
        view._do_init = AssistantView._do_init.__get__(view, AssistantView)
        view._init_assistant = AssistantView._init_assistant.__get__(view, AssistantView)
        
        # Set up mock database and graph status
        status = {
            "total_tracks": 10,
            "artist_edges": 5,
            "album_edges": 5,
            "acoustic_edges": 5,
        }
        self.app.db_manager.get_tracks_missing_features = AsyncMock(return_value=[])
        
        # 1. Test cache hit (silent path bypass)
        view.chat_memory.load_graph_state.return_value = {
            "total_tracks": 10,
            "missing_count": 0
        }
        
        with patch("utils.track_graph.graph_status", new_callable=AsyncMock) as mock_status, \
             patch("utils.track_graph.build_metadata_edges", new_callable=AsyncMock) as mock_meta, \
             patch("utils.track_graph.build_acoustic_edges", new_callable=AsyncMock) as mock_acoustic:
            
            mock_status.return_value = status
            
            # Run initialization
            run_async(view._do_init())
            
            # Verify cache hit: No edge rebuilds were triggered
            mock_meta.assert_not_called()
            mock_acoustic.assert_not_called()
            
        # 2. Test cache miss (silent rebuild triggered)
        view.chat_memory.load_graph_state.return_value = {
            "total_tracks": 8, # track count mismatch -> cache miss!
            "missing_count": 0
        }
        
        with patch("utils.track_graph.graph_status", new_callable=AsyncMock) as mock_status, \
             patch("utils.track_graph.build_metadata_edges", new_callable=AsyncMock) as mock_meta, \
             patch("utils.track_graph.build_acoustic_edges", new_callable=AsyncMock) as mock_acoustic:
            
            mock_status.return_value = status
            
            # Run initialization
            run_async(view._do_init())
            
            # Verify cache miss: Metadata and acoustic edge builders were invoked and cache was updated
            mock_meta.assert_called_once()
            mock_acoustic.assert_called_once()
            view.chat_memory.save_graph_state.assert_called_with(10, 0)
            
        # 3. Test concurrency protection (_init_started lock)
        # We trigger multiple parallel calls to _init_assistant and verify that only one gets executed
        view._init_started = False
        
        call_count = [0]
        async def mock_do_init_slow():
            call_count[0] += 1
            await asyncio.sleep(0.01)
        view._do_init = mock_do_init_slow
        
        async def run_parallel_init():
            await asyncio.gather(
                view._init_assistant(),
                view._init_assistant(),
                view._init_assistant()
            )
            
        run_async(run_parallel_init())
        
        # Verify that concurrency guard completely blocked race conditions
        self.assertEqual(call_count[0], 1)

    def test_search_rapid_selection_queue_integrity(self):
        # Stress-test rapid user track selections from Search results context under shifting queue modes.
        self.app.db_manager.get_tracks_by_album.return_value = self.sample_tracks
        
        # Simulate rapid selections in different search contexts
        async def select_tracks():
            # Selection 1: click track 1 in standard search context
            await self.app._play_track_core("/music/song1.mp3", source=("search", "query"))
            self.assertEqual(audio_engine.queue[audio_engine.current_index]["path"], "/music/song1.mp3")
            
            # Selection 2: toggle play similar ON
            self.app.set_play_similar_mode(True)
            self.assertTrue(self.app.play_similar_mode)
            
            # Selection 3: click track 2 from search results in middle of play similar walks
            await self.app._play_track_core("/music/song2.mp3", source=("search", "query"))
            self.assertEqual(audio_engine.queue[audio_engine.current_index]["path"], "/music/song2.mp3")
            
            # Selection 4: toggle shuffle ON (should turn Play Similar OFF)
            await self.app._toggle_shuffle_async()
            self.assertTrue(audio_engine.is_shuffle)
            self.assertFalse(self.app.play_similar_mode)
            
            # Selection 5: click track 3 from search results (should shuffle entire library)
            await self.app._play_track_core("/music/song3.mp3", source=("search", "query"))
            self.assertEqual(len(audio_engine.queue), 3)
            self.assertTrue(audio_engine.is_shuffle)
            
        run_async(select_tracks())

if __name__ == "__main__":
    unittest.main()
