import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft

from ui.views.search import SearchView
from ui.widgets import AnimatedEntry


def _make_view():
    view = SearchView.__new__(SearchView)
    view.app = MagicMock()
    view.page = MagicMock()
    view.expanded_nodes = set()
    view.selected_source = "qobuz"
    view._results_list = ft.ListView()
    view.selection_mode = False
    view.selected_keys = set()
    view._selected_records = {}
    return view


def _trailing_signature(tile: ft.ListTile):
    """Everything about a row's trailing cluster that the eye can see."""
    sig = []
    for ctrl in tile.trailing.controls:
        if isinstance(ctrl, ft.Icon):
            sig.append(("icon", ctrl.icon, ctrl.color, ctrl.size))
        elif isinstance(ctrl, ft.IconButton):
            sig.append(("iconbutton", ctrl.icon, ctrl.icon_color, ctrl.icon_size))
        elif isinstance(ctrl, ft.Container) and isinstance(ctrl.content, ft.Icon):
            sig.append(("boxed", ctrl.content.icon, ctrl.content.color, ctrl.content.size))
        elif isinstance(ctrl, ft.Container) and isinstance(ctrl.content, ft.ProgressRing):
            sig.append(("spinner",))
        else:
            sig.append((type(ctrl).__name__,))
    return sig


class TestRowVisualConsistency(unittest.TestCase):
    """A freshly built row and a refreshed row must look identical.

    _result_card and refresh_results_only used to derive icons independently
    and had drifted apart: build drew PLAY_CIRCLE_FILLED_ROUNDED at size 22 and
    CHEVRON_RIGHT_ROUNDED, while refresh redrew the same slots as
    PLAY_CIRCLE_OUTLINE at size 20 and KEYBOARD_ARROW_RIGHT. Every playback
    event therefore silently restyled the whole result list.
    """

    def setUp(self):
        self.engine = patch("ui.views.search.audio_engine").start()
        self.engine.current_track = ""
        self.engine.current_artist = ""
        self.addCleanup(patch.stopall)

    def _round_trip(self, record):
        view = _make_view()
        entry = view._result_card(0, record)
        tile = entry.content
        before = (_trailing_signature(tile), tile.bgcolor)

        view._results_list.controls = [entry]
        view.refresh_results_only()

        after = (_trailing_signature(tile), tile.bgcolor)
        return before, after

    def test_idle_track_row_survives_refresh(self):
        before, after = self._round_trip(
            {"id": "1", "media_type": "track", "ui_title": "T", "ui_subtitle": "A"}
        )
        self.assertEqual(before, after)

    def test_playing_track_row_survives_refresh(self):
        before, after = self._round_trip(
            {"id": "1", "media_type": "track", "ui_title": "T",
             "ui_subtitle": "A", "preview_state": "playing"}
        )
        self.assertEqual(before, after)

    def test_collapsed_album_row_survives_refresh(self):
        before, after = self._round_trip(
            {"id": "9", "media_type": "album", "ui_title": "Alb", "ui_subtitle": "A"}
        )
        self.assertEqual(before, after)

    def test_expanded_artist_row_survives_refresh(self):
        view = _make_view()
        record = {"id": "7", "media_type": "artist", "ui_title": "Art", "ui_subtitle": ""}
        view.expanded_nodes.add("artist_7")

        entry = view._result_card(0, record)
        tile = entry.content
        before = (_trailing_signature(tile), tile.bgcolor)

        view._results_list.controls = [entry]
        view.refresh_results_only()

        self.assertEqual(before, (_trailing_signature(tile), tile.bgcolor))
        # An expanded row is highlighted, not transparent.
        self.assertNotEqual(tile.bgcolor, "transparent")

    def test_preview_state_change_is_reflected(self):
        """The applier must still actually apply — not just be self-consistent."""
        view = _make_view()
        record = {"id": "1", "media_type": "track", "ui_title": "T", "ui_subtitle": "A"}
        entry = view._result_card(0, record)
        tile = entry.content
        view._results_list.controls = [entry]

        idle = _trailing_signature(tile)
        record["preview_state"] = "playing"
        view.refresh_results_only()

        self.assertNotEqual(idle, _trailing_signature(tile))
        self.assertEqual(tile.trailing.controls[0].content.icon,
                         ft.Icons.PAUSE_CIRCLE_FILLED_ROUNDED)

    def test_chevron_actually_flips_on_expand(self):
        """Guards the silent-no-op rename: Flet 0.86 calls the property `icon`,
        and the old code assigned `.name`, which is accepted and ignored — so
        the disclosure chevron never changed when a node was expanded."""
        view = _make_view()
        record = {"id": "7", "media_type": "artist", "ui_title": "Art", "ui_subtitle": ""}
        entry = view._result_card(0, record)
        tile = entry.content
        view._results_list.controls = [entry]

        self.assertEqual(tile.trailing.controls[-1].icon, ft.Icons.CHEVRON_RIGHT_ROUNDED)

        view.expanded_nodes.add("artist_7")
        view.refresh_results_only()

        self.assertEqual(tile.trailing.controls[-1].icon,
                         ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED)


class TestSourceAgnosticProgress(unittest.TestCase):
    """Progress prose must never name a backend.

    "Connecting to Qobuz API…" was shown for Deezer downloads. Source identity
    belongs in data (the source pill, SOURCE_COLORS), not interpolated prose,
    so adding a backend costs no new strings and no message can go stale.
    """

    def test_search_progress_strings_name_no_backend(self):
        from ui.views import search as search_mod
        for const in (search_mod.PROGRESS_SEARCHING,
                      search_mod.PROGRESS_CONTACTING,
                      search_mod.PROGRESS_CONNECTING):
            self.assertNotIn("qobuz", const.lower())
            self.assertNotIn("deezer", const.lower())

    def test_searcher_progress_callbacks_name_no_backend(self):
        import inspect
        from utils import streamrip_search

        src = inspect.getsource(streamrip_search)
        for line in src.splitlines():
            if "progress_callback(" not in line or "def " in line:
                continue
            self.assertNotIn("qobuz", line.lower(), line.strip())
            self.assertNotIn("deezer", line.lower(), line.strip())
            self.assertNotIn("label", line, line.strip())

    def test_download_retry_message_names_no_backend(self):
        import inspect
        from utils import queue_controller

        body = inspect.getsource(queue_controller.QueueController._workflow)
        code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
        self.assertNotIn("Qobuz", code)
        self.assertNotIn("Deezer", code)


class TestSourceAwareSetupGate(unittest.TestCase):
    """The lock screen read only the Qobuz config section, so a Deezer-only
    user was held behind "Setup Required" permanently with results cleared."""

    def _view(self):
        view = _make_view()
        view._source_bar = MagicMock()
        return view

    def test_deezer_only_user_is_configured(self):
        view = self._view()
        cfg = {"qobuz": {}, "deezer": {"arl": "abc"}}
        self.assertEqual(view._configured_sources(cfg), ["deezer"])

    def test_qobuz_only_user_is_configured(self):
        view = self._view()
        cfg = {"qobuz": {"email_or_userid": "u", "password_or_token": "t"}, "deezer": {}}
        self.assertEqual(view._configured_sources(cfg), ["qobuz"])

    def test_no_credentials_at_all(self):
        view = self._view()
        self.assertEqual(view._configured_sources({"qobuz": {}, "deezer": {}}), [])

    def test_selected_source_falls_back_to_a_usable_one(self):
        """Selecting Qobuz with only Deezer configured must not dead-end."""
        view = self._view()
        view.selected_source = "qobuz"
        view._setup_prompt = ft.Container()
        view._results_list = ft.ListView()
        view._empty_label = ft.Container()
        view._landing_container = ft.ListView()
        view._search_field = ft.TextField(value="")

        cfg = {"qobuz": {}, "deezer": {"arl": "abc"}, "landing": {}}
        with patch("utils.streamrip_api.load_config", return_value=cfg):
            view.refresh_setup_state(update=False)

        self.assertEqual(view.selected_source, "deezer")
        self.assertFalse(view._setup_prompt.visible)

    def test_credentials_missing_is_actionable_and_names_the_source(self):
        view = self._view()
        cfg = {"qobuz": {}, "deezer": {"arl": "abc"}}
        with patch("utils.streamrip_api.load_config", return_value=cfg):
            self.assertIsNone(view._credentials_missing("deezer"))
            msg = view._credentials_missing("qobuz")
        # Errors DO name the backend: the remedy differs per source.
        self.assertIn("Qobuz", msg)
        self.assertIn("Settings", msg)


if __name__ == "__main__":
    unittest.main()
