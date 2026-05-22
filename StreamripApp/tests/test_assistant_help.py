import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Isolate APP_DIR so we don't interfere with or read the user's real custom_moods.json
import types as _types
_cfg = _types.ModuleType("utils.config")
_cfg.APP_DIR = tempfile.mkdtemp(prefix="dsptest_help_")
sys.modules["utils.config"] = _cfg

from utils import track_graph as tg
from utils import assistant_intent as ai
from utils.assistant_runner import AssistantRunner, AssistantResponse

def _run(coro):
    return asyncio.run(coro)

class TestAssistantHelp(unittest.TestCase):
    def setUp(self):
        # Construct AssistantRunner with dummy database and engine
        self.runner = AssistantRunner(db_manager=None, audio_engine=None)
        # Clear custom moods before each test
        if os.path.exists(tg.CUSTOM_MOODS_PATH):
            os.remove(tg.CUSTOM_MOODS_PATH)

    def tearDown(self):
        if os.path.exists(tg.CUSTOM_MOODS_PATH):
            os.remove(tg.CUSTOM_MOODS_PATH)

    def test_help_lists_canonical_moods_alphabetically(self):
        intent = ai.Intent(name="help", query="help", raw="help")
        resp = _run(self.runner._handle_help(intent))

        self.assertTrue(resp.success)
        # Check that canonical moods are sorted alphabetically and present in the response
        sorted_moods = sorted(tg.MOODS.keys())
        expected_moods_str = ", ".join(sorted_moods)

        self.assertIn(f"**Acoustic Moods**: `play [mood]` (Available: {expected_moods_str})", resp.displayed)
        self.assertIn("chill", expected_moods_str)
        self.assertIn("upbeat", expected_moods_str)

    def test_help_no_custom_islets_shows_placeholder(self):
        intent = ai.Intent(name="help", query="help", raw="help")
        resp = _run(self.runner._handle_help(intent))

        expected_islets_str = "None registered yet. Save one by saying *save this as [name]* while a song plays."
        self.assertIn(f"**Custom Islets**: `play [islet]` (Available: {expected_islets_str})", resp.displayed)

    def test_help_with_custom_islets_lists_them(self):
        # Seed custom moods
        tg.save_custom_mood(
            name="Sunday afternoon",
            centroid=[0.1] * 52,
            exemplar_path="/music/sunday.flac"
        )
        tg.save_custom_mood(
            name="Late Night Coding",
            centroid=[0.5] * 52,
            exemplar_path="/music/night.flac"
        )

        intent = ai.Intent(name="help", query="help", raw="help")
        resp = _run(self.runner._handle_help(intent))

        # Names are alphabetized and lowercased by save_custom_mood
        expected_islets_str = "late night coding, sunday afternoon"
        self.assertIn(f"**Custom Islets**: `play [islet]` (Available: {expected_islets_str})", resp.displayed)

if __name__ == "__main__":
    unittest.main()
