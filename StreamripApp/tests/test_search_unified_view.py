"""Tests for the Unified Search Experience and Mock Search generator.

Verifies:
1. Mock search results generator produces valid multi-type payloads.
2. Top Result (Hero Card) selection logic (exact, prefix, and fallback matching).
3. Unified view layout builder produces hero card, top tracks, album shelf, and artist shelf.
4. Smooth view-mode switching from unified 'all' mode to specialized 'tracks', 'albums', 'artists'.
5. Pagination bar visibility control (hidden in 'all' view, shown when pages > 1 in filtered views).
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft

from ui.views.search import SearchView
from utils.mock_search import get_mock_search_results


def _build_test_search_view(loop=None):
    v = SearchView.__new__(SearchView)
    v.app = MagicMock()
    v.app.db_manager = MagicMock()
    v.app.safe_update = lambda fn, *a, **kw: fn()
    v.page = MagicMock()
    if loop:
        v.page.run_task = lambda fn: (loop.run_until_complete(fn()) if callable(fn) else loop.run_until_complete(fn))
    else:
        v.page.run_task = lambda fn: None
    v.selected_source = "qobuz"
    v.view_mode = "all"
    v.current_page = 0
    v.total_pages = 1
    v.items_per_page = 35
    v.expanded_nodes = set()
    v.selection_mode = False
    v.selected_keys = set()
    v._selected_records = {}
    v._source_cache = {}
    v._cache_query = {}
    v._local_matches = {}
    v._local_library_expanded = False
    v._local_task = None
    v._remote_error = None
    v._remote_pending = False
    v.current_search_id = 1
    v._progress_spacer = None
    v._pending_local_section = None
    v._search_indicator = ft.ProgressRing(visible=False)
    v._search_field = ft.TextField(value="burial")
    v._results_list = ft.ListView()
    v._view_tabs_row = ft.Row()
    v._setup_prompt = ft.Container()
    v._empty_label = ft.Container()
    v._error_label = ft.Container()
    v._landing_container = ft.Container()
    v._pagination_bar = ft.Container()
    v._error_detail = ft.Text()
    v._page_label = ft.Text()
    v._prev_page_btn = ft.IconButton()
    v._next_page_btn = ft.IconButton()
    v.cached_results = {"track": [], "album": [], "artist": []}
    return v


class TestSearchUnifiedView(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        engine = patch("ui.views.search.audio_engine").start()
        engine.current_track = ""
        engine.current_artist = ""
        self.addCleanup(patch.stopall)

    def test_mock_search_results_structure(self):
        results = get_mock_search_results("Burial", source="qobuz")
        self.assertGreater(len(results), 0)

        types = {r.get("media_type") for r in results}
        self.assertIn("track", types)
        self.assertIn("album", types)
        self.assertIn("artist", types)

        # Empty or whitespace query returns empty list
        self.assertEqual(get_mock_search_results(""), [])
        self.assertEqual(get_mock_search_results("   "), [])

    def test_pick_hero_result_priorities(self):
        v = _build_test_search_view()
        results = get_mock_search_results("Burial", source="qobuz")
        for r in results:
            v.cached_results[r["media_type"]].append(r)

        # Exact match for artist
        hero_artist = v._pick_hero_result("Burial")
        self.assertIsNotNone(hero_artist)
        self.assertEqual(hero_artist["media_type"], "artist")
        self.assertEqual(hero_artist["name"], "Burial")

        # Exact match for album
        hero_album = v._pick_hero_result("Untrue")
        self.assertIsNotNone(hero_album)
        self.assertEqual(hero_album["media_type"], "album")

        # Exact match for track
        hero_track = v._pick_hero_result("Archangel")
        self.assertIsNotNone(hero_track)
        self.assertEqual(hero_track["media_type"], "track")

    def test_build_unified_controls_sections(self):
        v = _build_test_search_view()
        results = get_mock_search_results("Burial", source="qobuz")
        for r in results:
            v.cached_results[r["media_type"]].append(r)

        controls = v._build_unified_controls()
        self.assertGreater(len(controls), 3)

        # First main control is the Top Result / Hero Card
        hero_card = controls[0]
        self.assertIsInstance(hero_card, ft.Container)

        # Verify pagination bar is suppressed in "all" view
        v._update_pagination_ui()
        self.assertFalse(v._pagination_bar.visible)

    def test_switching_view_mode_rebuilds_categories(self):
        v = _build_test_search_view()
        results = get_mock_search_results("Burial", source="qobuz")
        for r in results:
            v.cached_results[r["media_type"]].append(r)

        # Switch to tracks view
        v.view_mode = "tracks"
        track_controls = v._page_controls()
        self.assertGreater(len(track_controls), 0)

        # Switch to albums view
        v.view_mode = "albums"
        album_controls = v._page_controls()
        self.assertGreater(len(album_controls), 0)

        # Switch back to all view
        v.view_mode = "all"
        all_controls = v._page_controls()
        self.assertGreater(len(all_controls), 0)

    def _skeleton_phase(self, v):
        """The list as start_search leaves it while the remote half is out."""
        from ui.widgets import SkeletonRow
        v._remote_pending = True
        v._progress_spacer = ft.Container(height=v._PROGRESS_CARD_SPACE)
        v._results_list.controls = [v._progress_spacer, *(SkeletonRow() for _ in range(3))]

    def test_local_matches_sit_between_progress_spacer_and_skeletons(self):
        from ui.widgets import SkeletonRow
        v = _build_test_search_view(self.loop)
        v.current_search_id = 10
        self._skeleton_phase(v)

        async def _fake_local(q):
            return {"track": [{"title": "Archangel", "artist": "Burial", "path": "/p/1"}], "album": [], "artist": []}

        v._load_local_matches = _fake_local
        self.loop.run_until_complete(v._show_local_matches("burial", 10))

        # Spacer first (the progress card overlays it), then the library bar,
        # then the skeletons: nothing renders behind the card or the bar.
        controls = v._results_list.controls
        self.assertIs(controls[0], v._progress_spacer)
        self.assertIs(controls[1], v._pending_local_section)
        self.assertTrue(all(isinstance(c, SkeletonRow) for c in controls[2:]))
        self.assertEqual(len(controls), 5)

        # When remote search lands, the list is rebuilt without the spacer
        v._on_results([])
        self.loop.run_until_complete(asyncio.sleep(0.01))
        self.assertFalse(any(c is v._progress_spacer for c in v._results_list.controls))

    def test_progress_spacer_covers_the_progress_card(self):
        v = SearchView.__new__(SearchView)
        card = 8 * 2 + 2 + 18 + 2 + 2 * 15  # padding, border, title, gap, 2-line detail
        self.assertGreaterEqual(v._PROGRESS_CARD_SPACE, card + 6)  # + bottom margin

    def test_auth_error_shows_error_card_not_results(self):
        v = _build_test_search_view(self.loop)
        v._show_search_error = MagicMock()
        v._on_results({"error": "Qobuz rejected the request (HTTP 401). Invalid credentials"})
        self.loop.run_until_complete(asyncio.sleep(0.01))

        v._show_search_error.assert_called_once()
        self.assertIn("401", v._show_search_error.call_args[0][0])
        self.assertEqual(sum(len(items) for items in v.cached_results.values()), 0)

    def test_hero_card_builds_for_album_not_in_library(self):
        v = _build_test_search_view()
        album = next(r for r in get_mock_search_results("Burial")
                     if r["media_type"] == "album" and not r["is_in_library"])
        self.assertIsInstance(v._build_hero_result_card(album), ft.Container)

    def test_local_library_expandable_dropdown_toggle(self):
        v = _build_test_search_view(self.loop)
        v._local_matches = {
            "track": [{"title": "Near Dark", "artist": "Burial", "path": "/p1"}],
            "album": [],
            "artist": [],
        }

        # Initially collapsed
        self.assertFalse(v._local_library_expanded)
        section = v._build_local_section()
        self.assertIsNotNone(section)
        # Check that header has Show (1) and items column is hidden
        col = section.content
        header = col.controls[0]
        divider = col.controls[1]
        items_container = col.controls[2]
        self.assertFalse(divider.visible)
        self.assertFalse(items_container.visible)

        # Toggle to expand
        v._toggle_local_library()
        self.assertTrue(v._local_library_expanded)
        expanded_section = v._build_local_section()
        self.assertTrue(expanded_section.content.controls[1].visible)
        self.assertTrue(expanded_section.content.controls[2].visible)

        # Toggle to collapse again
        v._toggle_local_library()
        self.assertFalse(v._local_library_expanded)
        collapsed_again = v._build_local_section()
        self.assertFalse(collapsed_again.content.controls[1].visible)
        self.assertFalse(collapsed_again.content.controls[2].visible)

    def test_toggle_during_skeleton_phase_swaps_section_in_place(self):
        from ui.widgets import SkeletonRow
        v = _build_test_search_view(self.loop)
        self._skeleton_phase(v)
        v._local_matches = {
            "track": [{"title": "Near Dark", "artist": "Burial", "path": "/p1"}],
            "album": [],
            "artist": [],
        }
        section = v._build_local_section()
        v._results_list.controls.insert(1, section)
        v._pending_local_section = section

        v._toggle_local_library()
        controls = v._results_list.controls
        self.assertTrue(v._local_library_expanded)
        self.assertIsNot(controls[1], section)
        self.assertIs(controls[1], v._pending_local_section)
        self.assertTrue(controls[1].content.controls[2].visible)
        # skeletons untouched
        self.assertEqual(len(controls), 5)
        self.assertTrue(all(isinstance(c, SkeletonRow) for c in controls[2:]))

    def test_hero_card_source_badge_omitted(self):
        v = _build_test_search_view()
        results = get_mock_search_results("Burial", source="qobuz")
        hero = results[0]
        card = v._build_hero_result_card(hero)

        # Collect text values in the hero card
        texts = []
        def _walk(c):
            if isinstance(c, ft.Text) and c.value:
                texts.append(c.value)
            for attr in ("content", "controls"):
                child = getattr(c, attr, None)
                if isinstance(child, list):
                    for x in child: _walk(x)
                elif isinstance(child, ft.Control):
                    _walk(child)
        _walk(card)

        # Hero card should have TOP RESULT but must not have redundant QOBUZ / DEEZER source tags
        self.assertTrue(any("TOP RESULT" in t for t in texts))
        self.assertNotIn("QOBUZ", texts)
        self.assertNotIn("DEEZER", texts)

    def test_pick_hero_result_track_prefix_and_multi_type(self):
        v = _build_test_search_view()
        results = get_mock_search_results("Burial", source="qobuz")
        for r in results:
            v.cached_results[r["media_type"]].append(r)

        # Searching for track prefix "Archan" should pick track "Archangel" as hero
        hero_track = v._pick_hero_result("Archan")
        self.assertIsNotNone(hero_track)
        self.assertEqual(hero_track["media_type"], "track")
        self.assertIn("Archangel", hero_track["ui_title"])

    def test_open_in_library_normalizes_tracks_mode(self):
        v = _build_test_search_view()
        lib_mock = MagicMock()
        lib_mock._search_field = ft.TextField()
        lib_mock._lib_clear_btn = ft.Container()
        lib_mock.view_mode = "playlists"
        v.app.library_view = lib_mock

        v._open_in_library("track", "Archangel")

        # Must normalize to "tracks" (plural); show_filtered does the rest,
        # once, so the Library loads once
        lib_mock.show_filtered.assert_called_once_with("tracks", "Archangel")
        lib_mock._set_view_mode.assert_not_called()
        v.app.switch_tab.assert_called_with(2)

    def _filtered_lib(self, appearance):
        from ui.views.library import LibraryView
        lib = LibraryView.__new__(LibraryView)
        lib.app = MagicMock()
        lib._search_field = ft.TextField()
        lib._lib_clear_btn = ft.Container()
        lib._set_view_mode = MagicMock()
        lib._load_appearance = lambda: appearance
        return lib

    def test_hidden_albums_tab_lands_on_tracks_not_playlists(self):
        # show_albums defaults to off; the album name used to land on Playlists
        lib = self._filtered_lib({"show_albums": False})
        lib.show_filtered("albums", "Blue Train")
        lib._set_view_mode.assert_called_once_with("tracks")
        self.assertEqual(lib.search_query, "Blue Train")  # tracks match album titles
        lib.app.show_snackbar.assert_called_once()

    def test_hidden_genres_tab_drops_the_unmatchable_filter(self):
        lib = self._filtered_lib({"show_genres": False})
        lib.show_filtered("genres", "Jazz")
        lib._set_view_mode.assert_called_once_with("tracks")
        self.assertEqual(lib.search_query, "")

    def test_visible_tab_opens_directly(self):
        lib = self._filtered_lib({"show_albums": True})
        lib.show_filtered("albums", "Blue Train")
        lib._set_view_mode.assert_called_once_with("albums")
        lib.app.show_snackbar.assert_not_called()


class TestDemoMode(unittest.TestCase):
    """Opt-in sample data (Settings → Advanced → Demo Search Results)."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        engine = patch("ui.views.search.audio_engine").start()
        engine.current_track = ""
        engine.current_artist = ""
        self.addCleanup(patch.stopall)

    def _cfg(self, demo, creds=False):
        cfg = {"appearance": {"demo_search": demo}}
        if creds:
            cfg["qobuz"] = {"email_or_userid": "me", "password_or_token": "tok"}
        return patch("utils.streamrip_api.load_config", lambda: cfg)

    def test_flag_parsing(self):
        v = _build_test_search_view()
        for val, want in ((True, True), ("true", True), ("0", False), (None, False), (False, False)):
            self.assertEqual(v._demo_mode({"appearance": {"demo_search": val}}), want)
        self.assertFalse(v._demo_mode({}))

    def test_demo_skips_the_credentials_gate_only_when_on(self):
        v = _build_test_search_view()
        with self._cfg(False):
            self.assertIsNotNone(v._credentials_missing("qobuz"))
        with self._cfg(True):
            self.assertIsNone(v._credentials_missing("qobuz"))

    def test_demo_delivers_sample_results_through_the_normal_callbacks(self):
        v = _build_test_search_view()
        v._DEMO_LATENCY_S = 0
        got, progress = [], []
        self.loop.run_until_complete(v._deliver_demo_results(
            "burial", got.append, lambda s, d: progress.append(s)))
        self.assertEqual(len(progress), 1)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0] and all(r["demo"] for r in got[0]))

    def test_demo_records_are_never_downloaded_or_previewed(self):
        v = _build_test_search_view()
        demo = get_mock_search_results("burial")[0]
        v._download(demo)
        v.app.quality_selector_sheet.show.assert_not_called()
        v.app.show_snackbar.assert_called_once()

        real = {"id": "1", "media_type": "track", "title": "x"}
        v._download(real)
        v.app.quality_selector_sheet.show.assert_called_once_with(real)

        v._selected_records = {"a": demo, "b": real}
        v._download_selection()
        v.app.quality_selector_sheet.show_batch.assert_not_called()

    def test_error_state_button_turns_demo_on_and_reruns(self):
        v = _build_test_search_view()
        v.page.run_task = MagicMock()
        v.app.settings_view._demo_search_switch = ft.Switch(value=False)
        written = {}
        with patch("utils.streamrip_api.update_config_params",
                   lambda p: written.update(p) or True):
            v._enable_demo_and_retry()
        self.assertEqual(written, {"appearance": {"demo_search": True}})
        self.assertTrue(v.app.settings_view._demo_search_switch.value)
        v.page.run_task.assert_called_once_with(v.start_search)

    def test_settings_switch_applies_without_the_save_bar(self):
        from ui.views.settings import SettingsView
        sv = SettingsView(MagicMock())
        sv._build_advanced_group()
        sv._build_appearance_group()
        sv._current_subpage_name = "Appearance"
        sv._baseline_subpage_state = sv._get_subpage_state("Appearance")
        sv._demo_search_switch.value = True
        written = {}
        with patch("utils.streamrip_api.update_config_params",
                   lambda p: written.update(p) or True):
            sv._demo_search_switch.on_change(None)
        self.assertEqual(written, {"appearance": {"demo_search": True}})
        # not part of the dirty state, so no "Save Appearance" bar for it
        self.assertEqual(sv._get_subpage_state("Appearance"), sv._baseline_subpage_state)
        sv.app.search_view.refresh_setup_state.assert_called_once()

    def test_demo_children(self):
        from utils.mock_search import get_mock_children
        results = get_mock_search_results("burial")
        artist = next(r for r in results if r["media_type"] == "artist")
        album = next(r for r in results if r["media_type"] == "album")
        self.assertTrue(all(r["media_type"] == "album" for r in get_mock_children(artist)))
        self.assertTrue(all(r["media_type"] == "track" for r in get_mock_children(album)))
        self.assertEqual(get_mock_children(results[0]), [])


if __name__ == "__main__":
    unittest.main()

