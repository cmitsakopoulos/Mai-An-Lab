import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ui.views.settings import SettingsView, SettingSearchEntry


class TestSettingsSearch(unittest.TestCase):
    def test_search_registry_entries(self):
        class MockApp:
            page = None
            def safe_update(self, fn):
                pass
            def show_snackbar(self, msg):
                pass

        view = SettingsView(app=MockApp())
        registry = view._search_registry

        self.assertGreater(len(registry), 5)
        titles = [e.title for e in registry]
        self.assertIn("AI Assistant (Jarvis)", titles)
        self.assertIn("Audio & DSP", titles)
        self.assertIn("Storage & Paths", titles)

    def test_search_matching_keywords(self):
        class MockApp:
            page = None
            def safe_update(self, fn):
                pass

        view = SettingsView(app=MockApp())
        registry = view._search_registry

        # Search query 'gemini'
        q = "gemini"
        matches = [
            e for e in registry
            if q in e.title.lower()
            or q in e.subtitle.lower()
            or q in e.category.lower()
            or any(q in kw.lower() for kw in e.keywords)
        ]
        self.assertTrue(any(e.title == "AI Assistant (Jarvis)" for e in matches))

        # Search query 'eq'
        q = "eq"
        matches = [
            e for e in registry
            if q in e.title.lower()
            or q in e.subtitle.lower()
            or q in e.category.lower()
            or any(q in kw.lower() for kw in e.keywords)
        ]
        self.assertTrue(any(e.title == "Audio & DSP" for e in matches))

    def test_clear_search(self):
        class MockApp:
            page = None
            def safe_update(self, fn):
                pass

        view = SettingsView(app=MockApp())
        view._search_input.value = "gemini"
        view._clear_search()
        self.assertEqual(view._search_input.value, "")

    def test_mark_dirty_and_floating_save_bar(self):
        class MockApp:
            page = None
            def __init__(self):
                self.saved = False
            def safe_update(self, fn):
                if fn: fn()
            def show_snackbar(self, msg):
                pass
            def restart_ui(self, target_tab=2):
                pass

        app = MockApp()
        view = SettingsView(app=app)
        
        self.assertFalse(view._apply_visuals_container.visible)
        
        # Test _mark_dirty shows container and sets active save handler
        called = []
        def handler():
            called.append(True)
            
        view._mark_dirty(handler, label="SAVE TEST")
        self.assertTrue(view._apply_visuals_container.visible)
        self.assertEqual(view._active_save_handler, handler)
        
        # Test clicking floating save executes handler and hides bar
        view._on_floating_save_click()
        self.assertTrue(called[0])
        self.assertFalse(view._apply_visuals_container.visible)
        self.assertIsNone(view._active_save_handler)

    def test_appearance_settings_triggers_save_bar(self):
        class MockApp:
            page = None
            def safe_update(self, fn):
                if fn: fn()

        app = MockApp()
        view = SettingsView(app=app)
        view._build_appearance_group()
        
        # Triggering on_change on appearance switch should mark view dirty
        view._on_appearance_change()
        self.assertTrue(view._apply_visuals_container.visible)
        self.assertEqual(view._active_save_handler, view._save_appearance_settings)

    def test_reverting_setting_hides_save_button(self):
        class MockApp:
            page = None
            target_folder = ""
            library_folder = ""
            def safe_update(self, fn):
                if fn: fn()

        app = MockApp()
        view = SettingsView(app=app)
        view._show_sub_page("AI Assistant", view._build_assistant_group())
        
        initial_model = view._assistant_model_dropdown.value
        self.assertFalse(view._apply_visuals_container.visible)

        # Modify model -> Save button appears
        view._assistant_model_dropdown.value = "gemini-3.6-flash"
        view._check_dirty("AI Assistant", view._save_assistant_settings)
        self.assertTrue(view._apply_visuals_container.visible)

        # Revert model back to baseline -> Save button automatically disappears!
        view._assistant_model_dropdown.value = initial_model
        view._check_dirty("AI Assistant", view._save_assistant_settings)
        self.assertFalse(view._apply_visuals_container.visible)

    def test_dropdown_event_trigger_and_revert(self):
        import flet as ft
        class MockApp:
            page = None
            def safe_update(self, fn):
                if fn: fn()

        app = MockApp()
        view = SettingsView(app=app)
        view._show_sub_page("AI Assistant", view._build_assistant_group())
        initial_model = view._assistant_model_dropdown.value

        # Simulate Flet event when user picks a dropdown item
        e_change = ft.ControlEvent(control=view._assistant_model_dropdown, name="select", data="gemini-3.6-flash")
        view._assistant_model_dropdown.on_select(e_change)
        self.assertTrue(view._apply_visuals_container.visible)

        # Simulate Flet event when user picks the original dropdown item back
        e_revert = ft.ControlEvent(control=view._assistant_model_dropdown, name="select", data=initial_model)
        view._assistant_model_dropdown.on_select(e_revert)
        self.assertFalse(view._apply_visuals_container.visible)


if __name__ == "__main__":
    unittest.main()
