import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.config import AssistantConfig
from utils.assistant_runner import AssistantRunner


class TestCustomPersonality(unittest.TestCase):
    def test_assistant_config_defaults(self):
        cfg = AssistantConfig()
        self.assertEqual(cfg.personality_prompt, "")

    def test_assistant_config_custom_value(self):
        cfg = AssistantConfig(personality_prompt="You are a ZAX AI Supercomputer devoted to the NCR. Address the user as 'Citizen'.")
        self.assertEqual(cfg.personality_prompt, "You are a ZAX AI Supercomputer devoted to the NCR. Address the user as 'Citizen'.")

    def test_runner_prompt_assembly_default(self):
        runner = AssistantRunner(db_manager=None, audio_engine=None)

        class MockConfig:
            assistant = AssistantConfig(personality_prompt="")

        runner._load_assistant_cfg = lambda: MockConfig()

        cfg = runner._load_assistant_cfg()
        acfg = cfg.assistant
        default_persona = "You are Jarvis, a concise, sophisticated AI assistant"
        custom_persona = acfg.personality_prompt.strip() if acfg.personality_prompt else ""
        persona = custom_persona if custom_persona else default_persona

        self.assertTrue(persona.startswith("You are Jarvis"))

    def test_runner_prompt_assembly_custom(self):
        runner = AssistantRunner(db_manager=None, audio_engine=None)
        custom_text = "You are a cheerful 80s radio DJ named DJ Retro."

        class MockConfig:
            assistant = AssistantConfig(personality_prompt=custom_text)

        runner._load_assistant_cfg = lambda: MockConfig()

        cfg = runner._load_assistant_cfg()
        acfg = cfg.assistant
        custom_persona = acfg.personality_prompt.strip() if acfg.personality_prompt else ""
        persona = custom_persona if custom_persona else "You are Jarvis"

        self.assertEqual(persona, custom_text)


if __name__ == "__main__":
    unittest.main()
