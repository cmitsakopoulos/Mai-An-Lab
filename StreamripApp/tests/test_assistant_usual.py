import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Stub heavy modules before project imports to avoid loading numpy/Flet during tests
def _stub_module(name: str, attrs: dict | None = None):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        if attrs:
            for k, v in attrs.items():
                setattr(mod, k, v)
        sys.modules[name] = mod

_stub_module("utils.dsp", {"FEATURES_VERSION": 3})
_stub_module("utils.track_graph")
_stub_module("flet")
_stub_module("flet_core")

from utils import assistant_intent as ai
from utils.assistant_runner import AssistantRunner

# Stubs
class _FakeEngine:
    def __init__(self):
        self.queue = []
    def set_queue(self, tracks, start_index=0):
        self.queue = list(tracks)

class _FakeDB:
    def __init__(self, tracks):
        self._tracks = tracks
    async def get_most_played(self, limit=20):
        return self._tracks[:limit]
    async def record_playback(self, path, action, seed_path=None):
        pass

class TestAssistantUsual(unittest.TestCase):
    def test_intent_parsing(self):
        phrases = [
            "play the usual",
            "queue up the usual",
            "play my usual",
            "give me my usual",
        ]
        for p in phrases:
            with self.subTest(phrase=p):
                intent = ai.parse(p)
                self.assertEqual(intent.name, ai.INTENT_PLAY_THE_USUAL)

    def test_handler_plays_track(self):
        import asyncio
        loop = asyncio.get_event_loop()
        
        sample_tracks = [{
            "path": "/music/track1.mp3",
            "title": "Yesterday",
            "artist": "The Beatles",
            "album": "Help!",
            "duration": 125.0,
        }]
        
        engine = _FakeEngine()
        db = _FakeDB(sample_tracks)
        runner = AssistantRunner(db_manager=db, audio_engine=engine)
        
        intent = ai.parse("play the usual")
        
        async def run_test():
            return await runner.dispatch(intent)
            
        res = loop.run_until_complete(run_test())
        self.assertTrue(res.success)
        self.assertEqual(len(engine.queue), 1)
        self.assertEqual(engine.queue[0]["path"], "/music/track1.mp3")
        self.assertIn("Yesterday", res.spoken)
        self.assertIn("The Beatles", res.spoken)

    def test_handler_no_history_fails(self):
        import asyncio
        loop = asyncio.get_event_loop()
        
        engine = _FakeEngine()
        db = _FakeDB([])
        runner = AssistantRunner(db_manager=db, audio_engine=engine)
        
        intent = ai.parse("play the usual")
        
        async def run_test():
            return await runner.dispatch(intent)
            
        res = loop.run_until_complete(run_test())
        self.assertFalse(res.success)
        self.assertIn("enough play history", res.spoken)

if __name__ == "__main__":
    unittest.main()
