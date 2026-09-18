import os
import sys
import unittest
from unittest.mock import MagicMock
import flet as ft

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ui.tokens import LEGACY_ACCENT_MAP, CYAN, BORDER_SUBTLE, DIM, TEXT
from ui.views.library import LibraryView


class TestTrackFormatIndicator(unittest.TestCase):
    def test_legacy_accent_map_coverage(self):
        # Verify legacy flat colors map to Apple Dark Mode vibrant accents
        self.assertEqual(LEGACY_ACCENT_MAP.get("#FFD600"), "#FFD60A")  # Yellow
        self.assertEqual(LEGACY_ACCENT_MAP.get("#9B59B6"), "#BF5AF2")  # Purple
        self.assertEqual(LEGACY_ACCENT_MAP.get("#E67E22"), "#FF9F0A")  # Orange
        self.assertEqual(LEGACY_ACCENT_MAP.get("#2ECC71"), "#30D158")  # Green
        self.assertEqual(LEGACY_ACCENT_MAP.get("#78909C"), "#5E5CE6")  # Indigo
        self.assertEqual(LEGACY_ACCENT_MAP.get("#00BFFF"), "#64D2FF")  # Cyan

    def test_track_row_format_badge_leading(self):
        app = MagicMock()
        view = LibraryView(app=app)
        view.page = MagicMock()

        track_data = {
            "path": "/music/album/song.flac",
            "title": "Midnight Drive",
            "artist": "Synthwave Act",
            "album": "Neon City",
            "format": "FLAC",
            "duration": 215,
            "track_num": 1,
        }

        row = view._track_row(track_data, depth=0)
        tile = view._get_tile(row)
        self.assertIsNotNone(tile)

        # Leading should be a Container format badge (width 40, height 20)
        leading_badge = tile.leading
        self.assertIsInstance(leading_badge, ft.Container)
        self.assertEqual(leading_badge.width, 40)
        self.assertEqual(leading_badge.height, 20)
        self.assertIsInstance(leading_badge.content, ft.Text)
        self.assertEqual(leading_badge.content.value, "FLAC")

        # Trailing should NOT contain format badge (only duration)
        trailing = tile.trailing
        self.assertIsInstance(trailing, ft.Row)
        self.assertEqual(len(trailing.controls), 1)  # Only duration label
        self.assertEqual(trailing.controls[0].value, "3:35")

    def test_track_row_extension_fallback(self):
        app = MagicMock()
        view = LibraryView(app=app)
        view.page = MagicMock()

        track_data_no_format = {
            "path": "/music/song.mp3",
            "title": "Acoustic Solo",
            "artist": "Guitarist",
            "album": "Unplugged",
            "format": "",  # Empty format
            "duration": 180,
        }

        row = view._track_row(track_data_no_format, depth=0)
        tile = view._get_tile(row)
        leading_badge = tile.leading
        self.assertIsInstance(leading_badge, ft.Container)
        self.assertEqual(leading_badge.content.value, "MP3")

    def test_update_row_highlight_format_badge(self):
        app = MagicMock()
        view = LibraryView(app=app)
        view.page = MagicMock()

        track_data = {
            "path": "/music/song.flac",
            "title": "Electronic Beat",
            "artist": "DJ",
            "album": "Club",
            "format": "FLAC",
            "duration": 240,
        }

        row = view._track_row(track_data, depth=0)
        tile = view._get_tile(row)

        # 1. Update highlight to active (playing)
        view._update_row_highlight(row, is_current=True)
        badge = tile.leading
        self.assertEqual(badge.content.color, CYAN)
        self.assertEqual(tile.title.color, CYAN)

        # 2. Update highlight to inactive
        view._update_row_highlight(row, is_current=False)
        self.assertEqual(badge.content.color, DIM)
        self.assertEqual(tile.title.color, TEXT)

    def test_queue_sheet_active_icon_is_play_arrow(self):
        import ui.player.queue_sheet as qs
        audio_engine = qs.audio_engine
        original_queue = audio_engine.queue
        original_idx = audio_engine.current_index
        try:
            audio_engine.queue = [
                {"path": "/music/song1.flac", "track_title": "Song 1", "artist_name": "Artist 1"}
            ]
            audio_engine.current_index = 0

            app = MagicMock()
            app.safe_update = lambda fn: fn()
            sheet = qs.QueueSheet(app=app)
            ctrl = sheet.build()
            sheet.refresh()

            # Verify that active track row was built and doesn't contain equalizer
            self.assertIsNotNone(ctrl)
            # Find the active track row in the reorderable list view
            active_row = sheet._queue_list.controls[0]
            # Dig into container -> Dismissible -> card -> Row -> pos_label -> pos_indicator
            card = active_row.content.content
            row = card.content
            pos_label = row.controls[0]
            pos_indicator = pos_label.content
            self.assertIsInstance(pos_indicator, ft.Icon)
            self.assertEqual(pos_indicator.icon, ft.Icons.PLAY_ARROW_ROUNDED)
        finally:
            audio_engine.queue = original_queue
            audio_engine.current_index = original_idx

    def test_notification_system_snackbar_build(self):
        from ui.widgets import NotificationSystem
        app = MagicMock()
        app.is_background = False
        page = MagicMock()
        app.page = page
        captured_callbacks = []
        app.safe_update = lambda fn: captured_callbacks.append(fn)

        notifier = NotificationSystem(app=app)
        notifier.show("Test notification message", color="#FFD60A")

        # Execute safe_update callback
        self.assertEqual(len(captured_callbacks), 1)
        captured_callbacks[0]()

        # Ensure show_dialog was called with an ft.SnackBar without throwing TypeError
        page.show_dialog.assert_called_once()
        dialog_arg = page.show_dialog.call_args[0][0]
        self.assertIsInstance(dialog_arg, ft.SnackBar)
        self.assertEqual(dialog_arg.bgcolor, "#1C1C1E")


if __name__ == "__main__":
    unittest.main()
