import os
import sys
import unittest
from unittest.mock import MagicMock
import flet as ft

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ui.views.assistant import AssistantView


class TestAssistantView(unittest.TestCase):
    def test_header_settings_button(self):
        mock_app = MagicMock()
        mock_app.page = MagicMock()
        view = AssistantView(app=mock_app)
        view.build()

        # Check that _settings_btn is created with expected icon & tooltip
        self.assertIsNotNone(view._settings_btn)
        self.assertEqual(view._settings_btn.icon, ft.Icons.SETTINGS_OUTLINED)
        self.assertEqual(view._settings_btn.tooltip, "Settings")

        # Check placement in header row: _settings_btn should precede _clear_btn (bin icon)
        header_container = view.layout.controls[0]
        header_row = header_container.content
        controls = header_row.controls

        settings_idx = controls.index(view._settings_btn)
        clear_idx = controls.index(view._clear_btn)
        self.assertEqual(settings_idx, clear_idx - 1)

        # Test clicking settings button switches tab to settings (tab 3)
        view._settings_btn.on_click(None)
        mock_app._switch_tab.assert_called_once_with(3)


if __name__ == "__main__":
    unittest.main()
