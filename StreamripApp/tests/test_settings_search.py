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

    def test_appearance_switch_triggers_and_reverts_save_bar(self):
        class MockApp:
            page = None
            def safe_update(self, fn):
                if fn: fn()

        app = MockApp()
        view = SettingsView(app=app)
        view._show_sub_page("Appearance", view._build_appearance_group())
        
        # Triggering on_change on appearance switch marks view dirty
        view._show_jarvis_switch.value = not view._show_jarvis_switch.value
        view._on_appearance_change()
        self.assertTrue(view._apply_visuals_container.visible)
        self.assertEqual(view._active_save_handler, view._save_appearance_settings)

        # Reverting switch back to baseline hides save bar
        view._show_jarvis_switch.value = not view._show_jarvis_switch.value
        view._on_appearance_change()
        self.assertFalse(view._apply_visuals_container.visible)

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

    def test_appearance_switch_flet_event(self):
        import flet as ft
        class MockApp:
            page = None
            def safe_update(self, fn):
                if fn: fn()

        app = MockApp()
        view = SettingsView(app=app)
        view._show_sub_page("Appearance", view._build_appearance_group())
        self.assertTrue(view._show_jarvis_switch.value)

        # Simulate Flet event turning switch off
        e_off = ft.ControlEvent(control=view._show_jarvis_switch, name="change", data="false")
        view._show_jarvis_switch.on_change(e_off)
        self.assertIs(view._show_jarvis_switch.value, False)
        self.assertTrue(view._apply_visuals_container.visible)

        # Simulate Flet event turning switch back on
        e_on = ft.ControlEvent(control=view._show_jarvis_switch, name="change", data="true")
        view._show_jarvis_switch.on_change(e_on)
        self.assertIs(view._show_jarvis_switch.value, True)
        self.assertFalse(view._apply_visuals_container.visible)

    def test_appearance_color_revert(self):
        class MockApp:
            page = None
            def safe_update(self, fn):
                if fn: fn()

        app = MockApp()
        view = SettingsView(app=app)
        view._show_sub_page("Appearance", view._build_appearance_group())
        initial_color = view._selected_accent_color

        # Select a different accent color
        view._on_color_click("#9C27B0", "accent")
        self.assertEqual(view._selected_accent_color, "#9C27B0")
        self.assertTrue(view._apply_visuals_container.visible)

        # Select the initial accent color back
        view._on_color_click(initial_color, "accent")
        self.assertEqual(view._selected_accent_color, initial_color)
        self.assertFalse(view._apply_visuals_container.visible)

    def test_subpage_scroll_reset(self):
        """Opening normal subpages must mount a fresh scroll column at offset 0
        without invoking auto-scroll to bottom."""
        from unittest.mock import MagicMock
        class MockApp:
            page = MagicMock()
            def safe_update(self, fn):
                if fn: fn()

        app = MockApp()
        view = SettingsView(app=app)
        initial_col = view._scroll_column

        # Navigate to Haptic Feedback
        view._show_sub_page("Haptic Feedback", view._build_haptics_group())
        new_col = view._scroll_column

        # Must be a brand new Column instance to discard inherited scroll offsets
        self.assertIsNot(initial_col, new_col)
        self.assertIs(view._scroll_column, new_col)
        # Normal pages should NOT trigger auto-scroll to bottom
        app.page.run_task.assert_not_called()

        # Returning to hub should also instantiate a clean column
        view._show_hub()
        hub_col = view._scroll_column
        self.assertIsNot(new_col, hub_col)
        self.assertIs(view._scroll_column, hub_col)

    def test_about_auto_scroll(self):
        """Opening About specifically schedules auto-scroll to the bottom."""
        from unittest.mock import MagicMock
        class MockApp:
            page = MagicMock()
            def safe_update(self, fn):
                if fn: fn()

        app = MockApp()
        view = SettingsView(app=app)

        view._show_sub_page("About", view._build_about_group())
        app.page.run_task.assert_called_once_with(view._scroll_about_to_bottom)


if __name__ == "__main__":
    unittest.main()

