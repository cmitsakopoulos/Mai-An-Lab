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
            "go ahead"
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
            "nevermind"
        ]
        
        for neg in negatives:
            with self.subTest(neg=neg):
                intent = ai.parse(neg)
                self.assertEqual(
                    intent.name, 
                    ai.INTENT_NEGATIVE, 
                    f"Negative '{neg}' should parse as INTENT_NEGATIVE but got {intent.name}"
                )

if __name__ == "__main__":
    unittest.main()
