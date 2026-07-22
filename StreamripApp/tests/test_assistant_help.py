import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import assistant_intent as ai
from utils.assistant_runner import AssistantRunner

def _run(coro):
    return asyncio.run(coro)

class TestAssistantHelp(unittest.TestCase):
    def setUp(self):
        self.runner = AssistantRunner(db_manager=None, audio_engine=None)

    def test_help_returns_success(self):
        intent = ai.Intent(name="help", query="help", raw="help")
        resp = _run(self.runner._handle_help(intent))
        self.assertTrue(resp.success)
        self.assertIn("Playback", resp.displayed)
        self.assertIn("Queue", resp.displayed)

if __name__ == "__main__":
    unittest.main()
