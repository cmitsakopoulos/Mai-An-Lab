import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import assistant_intent as ai

class TestAssistantAffirmative(unittest.TestCase):
    def test_affirmative_synonyms(self):
        synonyms = [
            "proceed",
            "please proceed",
            "do proceed",
            "make it so",
            "absolutely",
            "definitely",
            "indeed",
            "of course",
            "yes",
            "yeah",
            "sure",
            "go ahead",
            "yesa",
            "yess",
            "go ahead and apply it",
            "apply it",
            "apply suggestion",
            "sure thing",
            "yes please"
        ]
        
        for synonym in synonyms:
            with self.subTest(synonym=synonym):
                intent = ai.parse(synonym)
                self.assertEqual(
                    intent.name, 
                    ai.INTENT_AFFIRMATIVE, 
                    f"Synonym '{synonym}' should parse as INTENT_AFFIRMATIVE but got {intent.name}"
                )

    def test_negative_cases(self):
        negatives = [
            "no",
            "nope",
            "nah",
            "cancel",
            "forget it",
            "nevermind",
            "don't apply",
            "no thanks"
        ]
        
        for neg in negatives:
            with self.subTest(neg=neg):
                intent = ai.parse(neg)
                self.assertEqual(
                    intent.name, 
                    ai.INTENT_NEGATIVE, 
                    f"Negative '{neg}' should parse as INTENT_NEGATIVE but got {intent.name}"
                )

    def test_runner_pending_confirmation_relaxed_resolution(self):
        import asyncio
        from unittest.mock import MagicMock
        from utils.assistant_runner import AssistantRunner, PendingConfirmation

        runner = AssistantRunner(db_manager=MagicMock(), audio_engine=MagicMock())
        called = False

        async def _on_yes():
            nonlocal called
            called = True
            from utils.assistant_runner import AssistantResponse
            return AssistantResponse(spoken="Applied", displayed="Applied")

        runner._pending = PendingConfirmation(
            prompt="Shall I apply?",
            on_yes_msg="Applied",
            on_yes_callback=_on_yes,
        )

        resp = asyncio.run(runner.dispatch_text("yesa"))
        self.assertTrue(called)
        self.assertEqual(resp.displayed, "Applied")

if __name__ == "__main__":
    unittest.main()
