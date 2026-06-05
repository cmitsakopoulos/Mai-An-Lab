"""Unit tests for the Active DSP Pipeline integration, presets, and toggles.
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
    "flet.canvas",
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
    "matplotlib",
    "matplotlib.pyplot",
    "seaborn",
    "certifi"
]

original_modules = {}
main = None
audio_engine = None

def setUpModule():
    global main, audio_engine
    
    # 1. Back up and mock external modules
    for mod in stub_modules:
        if mod in sys.modules:
            original_modules[mod] = sys.modules[mod]
        mock_mod = MagicMock()
        sys.modules[mod] = mock_mod

    # 2. Add parent dir to path
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

    # 3. Isolate environment directories
    temp_dir = tempfile.mkdtemp(prefix="streamrip_test_")
    os.environ["HOME"] = temp_dir
    os.environ["XDG_CONFIG_HOME"] = temp_dir
    os.environ["XDG_CACHE_HOME"] = os.path.join(temp_dir, ".cache")
    os.makedirs(os.environ["XDG_CACHE_HOME"], exist_ok=True)

    # 4. Import the real app modules under mocked environment
    import main as m
    from utils.audio_engine import audio_engine as ae
    main = m
    audio_engine = ae

def tearDownModule():
    for mod in stub_modules:
        if mod in original_modules:
            sys.modules[mod] = original_modules[mod]
        else:
            sys.modules.pop(mod, None)

    # Unload any app modules imported during this test to prevent caching of mock references
    to_remove = []
    for mod in list(sys.modules.keys()):
        lower_mod = mod.lower()
        if "utils" in lower_mod or "ui" in lower_mod or "main" in lower_mod or "streamrip" in lower_mod:
            to_remove.append(mod)
    for mod in to_remove:
        sys.modules.pop(mod, None)

# Helper to run async tests
def run_async(coro):
    return asyncio.run(coro)


class TestDSPPipeline(unittest.TestCase):
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

        # Mock AudioEngine's native parts
        audio_engine._page = self.mock_page
        audio_engine._audio = MagicMock()
        audio_engine._audio.set_loudness_boost = AsyncMock()
        audio_engine._audio.set_eq_band_gain = AsyncMock()
        audio_engine.clear_queue()
        audio_engine.is_shuffle = False
        audio_engine.repeat_mode = "none"

        # Mock StreamripFletApp
        self.app = MagicMock(spec=main.StreamripFletApp)
        self.app.page = self.mock_page
        self.app.db_manager = AsyncMock()
        self.app._prefs = {}
        self.app._prefs_path = os.path.join(self.test_dir, "flet_prefs.json")
        self.app.show_snackbar = MagicMock()
        self.app.safe_update = lambda fn: fn()

        # Write blank/default config file
        self.config_path = os.path.join(self.test_dir, "config.toml")
        with open(self.config_path, "w") as f:
            f.write("[dsp]\n")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    @patch("utils.streamrip_api.get_config_path")
    def test_dsp_defaults_and_toggles(self, mock_config_path):
        mock_config_path.return_value = self.config_path
        
        # Test default track transition DSP logic: no-op by default
        track = {"path": "/music/song1.mp3", "energy": 0.3, "brightness": 0.7}
        audio_engine.queue = [track]
        audio_engine.current_index = 0
        
        # Re-mock/spy on audio_engine methods
        audio_engine.set_loudness_boost = MagicMock()
        
        # Trigger transition
        audio_engine._sync_metadata_for_current()
        
        # By default (since dsp is not enabled in config), normalization boost should be 0.0
        # and equalizer bands should be set to 0.0
        audio_engine.set_loudness_boost.assert_called_with(0.0)
        self.assertEqual(audio_engine._eq_gains, [0.0, 0.0, 0.0, 0.0, 0.0])

    @patch("utils.streamrip_api.get_config_path")
    def test_dynamism_active(self, mock_config_path):
        mock_config_path.return_value = self.config_path
        
        # Enable dynamism in config
        from utils.streamrip_api import update_config_params
        update_config_params({
            "dsp": {
                "dynamism_enabled": True
            }
        })
        
        # Scenario 1: Maximum energy, beat strength, and spectral contrast -> full boost (EQ disabled)
        track_high = {"path": "/music/high.mp3", "energy": 1.0, "beat_strength": 1.0, "spectral_contrast": 0.36}
        audio_engine.queue = [track_high]
        audio_engine.current_index = 0
        audio_engine.set_loudness_boost = MagicMock()
        audio_engine._sync_metadata_for_current()
        # score = 0.4 * 1.0 + 0.3 * 1.0 + 0.3 * 0.8 = 0.94
        # dyn_offsets = [0.94 * 3.0, 0.94 * 1.5, 0.0, 0.94 * 1.0, 0.94 * 2.5] = [2.82, 1.41, 0.0, 0.94, 2.35]
        self.assertAlmostEqual(audio_engine._eq_gains[0], 2.82)
        self.assertAlmostEqual(audio_engine._eq_gains[1], 1.41)
        self.assertAlmostEqual(audio_engine._eq_gains[2], 0.0)
        self.assertAlmostEqual(audio_engine._eq_gains[3], 0.94)
        self.assertAlmostEqual(audio_engine._eq_gains[4], 2.35)
        # loudness boost = 1.0 + 3.0 * 0.94 = 3.82 dB
        audio_engine.set_loudness_boost.assert_called_with(3.82)

        # Scenario 2: Minimum energy, beat strength, and spectral contrast (EQ disabled)
        track_low = {"path": "/music/low.mp3", "energy": 0.0, "beat_strength": 0.0, "spectral_contrast": 0.25}
        audio_engine.queue = [track_low]
        audio_engine.current_index = 0
        audio_engine.set_loudness_boost = MagicMock()
        audio_engine._sync_metadata_for_current()
        # norm_contrast = (0.25 - 0.2) / 0.2 = 0.25
        # score = 0.3 * 0.25 = 0.075 -> rounded to 0.07
        # dyn_offsets = [0.07 * 3.0, 0.07 * 1.5, 0.0, 0.07 * 1.0, 0.07 * 2.5] = [0.21, 0.105, 0.0, 0.07, 0.175]
        self.assertAlmostEqual(audio_engine._eq_gains[0], 0.21)
        self.assertAlmostEqual(audio_engine._eq_gains[1], 0.105)
        self.assertAlmostEqual(audio_engine._eq_gains[2], 0.0)
        self.assertAlmostEqual(audio_engine._eq_gains[3], 0.07)
        self.assertAlmostEqual(audio_engine._eq_gains[4], 0.175)
        # loudness boost = 1.0 + 3.0 * 0.07 = 1.21 dB
        audio_engine.set_loudness_boost.assert_called_with(1.21)

        # Scenario 3: EQ and Dynamism both enabled -> Exclusive precedence routing
        update_config_params({
            "dsp": {
                "equalizer_enabled": True,
                "active_preset": "Rock",
                "dynamism_enabled": True
            }
        })
        audio_engine.queue = [track_high]
        audio_engine.current_index = 0
        audio_engine.set_loudness_boost = MagicMock()
        audio_engine._sync_metadata_for_current()
        # Verify EQ gains are exactly the Rock preset gains, NOT modified by dynamism offsets
        self.assertEqual(audio_engine._eq_gains, [4.0, 2.0, -2.0, 2.0, 4.0])
        # Loudness boost is still applied for dynamism
        audio_engine.set_loudness_boost.assert_called_with(3.82)

    @patch("utils.streamrip_api.get_config_path")
    def test_equalizer_presets_active(self, mock_config_path):
        mock_config_path.return_value = self.config_path
        
        # Enable equalizer with "Rock" preset
        from utils.streamrip_api import update_config_params
        update_config_params({
            "dsp": {
                "equalizer_enabled": True,
                "active_preset": "Rock"
            }
        })
        
        track = {"path": "/music/song1.mp3"}
        audio_engine.queue = [track]
        audio_engine.current_index = 0
        
        audio_engine.set_loudness_boost = MagicMock()
        audio_engine._sync_metadata_for_current()
        
        # Verify Rock gains are applied: [4.0, 2.0, -2.0, 2.0, 4.0] dB
        self.assertEqual(audio_engine._eq_gains, [4.0, 2.0, -2.0, 2.0, 4.0])

    @patch("utils.streamrip_api.get_config_path")
    def test_equalizer_custom_presets(self, mock_config_path):
        mock_config_path.return_value = self.config_path
        
        # Enable equalizer with custom preset "My Custom Setup"
        from utils.streamrip_api import update_config_params
        update_config_params({
            "dsp": {
                "equalizer_enabled": True,
                "active_preset": "My Custom Setup",
                "custom_presets": {
                    "My Custom Setup": [1.0, 2.0, 3.0, 4.0, 5.0]
                }
            }
        })
        
        track = {"path": "/music/song1.mp3"}
        audio_engine.queue = [track]
        audio_engine.current_index = 0
        
        audio_engine.set_loudness_boost = MagicMock()
        audio_engine._sync_metadata_for_current()
        
        # Verify custom gains are applied
        self.assertEqual(audio_engine._eq_gains, [1.0, 2.0, 3.0, 4.0, 5.0])


    @patch("utils.streamrip_api.get_config_path")
    def test_haptic_feedback_config_toggle(self, mock_config_path):
        mock_config_path.return_value = self.config_path
        
        # Test default: haptic feedback should be enabled by default (falls back to True)
        from utils.streamrip_api import load_config
        cfg = load_config()
        self.assertTrue(cfg.get("haptics", {}).get("haptic_feedback_enabled", True))
        
        # Test disabled haptic feedback config persistence
        from utils.streamrip_api import update_config_params
        update_config_params({
            "haptics": {
                "haptic_feedback_enabled": False
            }
        })
        cfg2 = load_config()
        self.assertFalse(cfg2.get("haptics", {}).get("haptic_feedback_enabled", True))

    @patch("utils.streamrip_api.get_config_path")
    @patch("sys.platform", "linux")  # mock non-darwin
    def test_trigger_haptic_non_darwin(self, mock_config_path):
        import asyncio
        mock_config_path.return_value = self.config_path

        app = MagicMock(spec=main.StreamripFletApp)
        # Bind the async method under test to the mock app instance
        app._trigger_haptic_async = main.StreamripFletApp._trigger_haptic_async.__get__(
            app, main.StreamripFletApp
        )
        app.haptic = AsyncMock()

        # Case 1: Config has haptic enabled = True, custom intensities
        from utils.streamrip_api import update_config_params
        update_config_params({
            "haptics": {
                "haptic_feedback_enabled": True,
                "eq_drag_intensity": "light",
                "swipe_queue_intensity": "medium",
                "swipe_dismiss_intensity": "medium",
                "long_press_intensity": "heavy"
            }
        })

        asyncio.run(app._trigger_haptic_async("swipe_queue"))
        app.haptic.medium_impact.assert_called_once()
        app.haptic.heavy_impact.assert_not_called()

        app.haptic.medium_impact.reset_mock()
        asyncio.run(app._trigger_haptic_async("long_press"))
        app.haptic.heavy_impact.assert_called_once()

        # Case 2: Config has haptic enabled = False
        update_config_params({
            "haptics": {
                "haptic_feedback_enabled": False
            }
        })
        app.haptic.medium_impact.reset_mock()
        app.haptic.heavy_impact.reset_mock()

        asyncio.run(app._trigger_haptic_async("swipe_queue"))
        app.haptic.medium_impact.assert_not_called()

        # Case 3: trigger_haptic (sync wrapper) dispatches via page.run_task
        app.page = MagicMock()
        app.trigger_haptic = main.StreamripFletApp.trigger_haptic.__get__(app, main.StreamripFletApp)
        app.trigger_haptic("long_press")
        app.page.run_task.assert_called_once()


    @patch("utils.streamrip_api.get_config_path")
    @patch("sys.platform", "darwin")  # mock darwin
    def test_trigger_haptic_darwin_no_op(self, mock_config_path):
        mock_config_path.return_value = self.config_path
        
        app = MagicMock(spec=main.StreamripFletApp)
        app.trigger_haptic = main.StreamripFletApp.trigger_haptic.__get__(app, main.StreamripFletApp)
        app.haptic = MagicMock()
        
        from utils.streamrip_api import update_config_params
        update_config_params({
            "haptics": {
                "haptic_feedback_enabled": True,
                "swipe_queue_intensity": "medium"
            }
        })
        
        app.trigger_haptic("swipe_queue")
        app.haptic.medium_impact.assert_not_called()



class TestSettingsEqualizerUI(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="streamrip_test_")
        main.DATA_DIR = self.test_dir
        
        self.mock_page = MagicMock()
        self.app = MagicMock(spec=main.StreamripFletApp)
        self.app.page = self.mock_page
        self.app.target_folder = self.test_dir
        self.app.library_folder = self.test_dir
        self.app.show_snackbar = MagicMock()
        self.app.safe_update = lambda fn: fn()
        self.app.trigger_haptic = MagicMock()
        self.app.play_similar_mode = False
        self.app.auto_dj_mode = False
        
        class DummyControl:
            def __init__(self, *args, **kwargs):
                self.visible = True
                self.value = ""
                self.content = MagicMock()
                self.controls = MagicMock()
                self.options = []
            def update(self):
                pass
            def __getattr__(self, name):
                return MagicMock()
                
        sys.modules["flet"].Container = DummyControl
        sys.modules["flet"].View = DummyControl
        sys.modules["flet"].GestureDetector = DummyControl
        sys.modules["flet"].Row = DummyControl
        sys.modules["flet"].Column = DummyControl

        import importlib
        if "ui.widgets" in sys.modules:
            importlib.reload(sys.modules["ui.widgets"])
        if "ui.views.settings" in sys.modules:
            importlib.reload(sys.modules["ui.views.settings"])

        self.config_path = os.path.join(self.test_dir, "config.toml")
        with open(self.config_path, "w") as f:
            f.write("[dsp]\n")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    @patch("utils.streamrip_api.get_config_path")
    def test_settings_equalizer_radio_and_keyboard(self, mock_config_path):
        mock_config_path.return_value = self.config_path
        
        # Stub ft.dropdown.Option to behave as a real class so we can test options
        class MockOption:
            def __init__(self, key, text=None):
                self.key = key
                self.text = text
        sys.modules["flet"].dropdown.Option = MockOption
        
        from ui.views.settings import SettingsView
        
        view = SettingsView(self.app)
        
        # Set mock properties since Flet is fully mocked/stubbed
        view._eq_preset_type_radio.value = "system"
        view._eq_preset_dropdown.options = []
        view._eq_preset_dropdown.value = "Flat"
        
        view._equaliser_switch = MagicMock()
        view._equaliser_switch.value = True
        
        # Trigger visibility update for system preset mode
        view._refresh_custom_presets_list()
        self.assertEqual(view._eq_preset_type_radio.value, "system")
        self.assertFalse(view._custom_preset_save_row.visible)
        self.assertFalse(view._custom_presets_list_container.visible)
        
        view._eq_preset_type_radio.value = "custom"
        mock_event = MagicMock()
        mock_event.control.value = "custom"
        view._on_preset_type_change(mock_event)
        
        # Verify visibility updates to True for custom preset mode
        self.assertTrue(view._custom_preset_save_row.visible)
        self.assertTrue(view._custom_presets_list_container.visible)
        
        # Verify changing back to system hides custom preset UI
        view._eq_preset_type_radio.value = "system"
        mock_event = MagicMock()
        mock_event.control.value = "system"
        view._on_preset_type_change(mock_event)
        self.assertFalse(view._custom_preset_save_row.visible)
        self.assertFalse(view._custom_presets_list_container.visible)
        
        # Restore mock settings to custom for keyboard testing
        view._eq_preset_type_radio.value = "custom"
        mock_event = MagicMock()
        mock_event.control.value = "custom"
        view._on_preset_type_change(mock_event)
        
        option_keys = [opt.key for opt in view._eq_preset_dropdown.options]
        self.assertIn("Custom", option_keys)
        self.assertNotIn("Flat", option_keys)
        
        mock_textfield = MagicMock()
        mock_textfield.value = "5.5 dB"
        
        view._on_gain_text_field_submit(0, "5.5 dB", mock_textfield)
        self.assertEqual(view._eq_bands[0]["gain"], 5.5)
        self.assertEqual(mock_textfield.value, "+5.5 dB")
        
        view._on_gain_text_field_submit(0, "20", mock_textfield)
        self.assertEqual(view._eq_bands[0]["gain"], 15.0)
        self.assertEqual(mock_textfield.value, "+15.0 dB")
        
        view._on_gain_text_field_submit(0, "-20.5 dB", mock_textfield)
        self.assertEqual(view._eq_bands[0]["gain"], -15.0)
        self.assertEqual(mock_textfield.value, "-15.0 dB")
        
        view._eq_bands[0]["gain"] = 2.0
        view._on_gain_text_field_submit(0, "invalid-gain-value", mock_textfield)
        self.assertEqual(view._eq_bands[0]["gain"], 2.0)
        self.assertEqual(mock_textfield.value, "+2.0 dB")


    @patch("utils.streamrip_api.get_config_path")
    def test_dynamism_boost_ui_updates(self, mock_config_path):
        mock_config_path.return_value = self.config_path
        
        from ui.views.settings import SettingsView
        view = SettingsView(self.app)
        
        view._dynamism_boost_card = MagicMock()
        view.update_loudness_boost(3.5)
        self.assertTrue(view._dynamism_boost_card.visible)
        self.assertEqual(view._dynamism_boost_card.content.controls[2].value, "+3.5 dB")
        
        view.update_loudness_boost(0.0)
        self.assertFalse(view._dynamism_boost_card.visible)
        
        from ui.player.now_playing import NowPlayingSheet
        np = NowPlayingSheet(self.app)
        np._dynamism_badge = MagicMock()
        
        np.update_loudness_boost(2.4)
        self.assertTrue(np._dynamism_badge.visible)
        self.assertEqual(np._dynamism_badge.content.controls[3].value, "+2.4 dB")
        
        np.update_loudness_boost(0.0)
        self.assertFalse(np._dynamism_badge.visible)


if __name__ == "__main__":
    unittest.main()

