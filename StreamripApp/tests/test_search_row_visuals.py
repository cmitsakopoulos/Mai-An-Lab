import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft

from ui.views.search import SearchView
from ui.widgets import AnimatedEntry, STAGGER_LIMIT, STAGGER_STEP_MS


def _view():
    v = SearchView.__new__(SearchView)
    v.app = MagicMock()
    v.page = MagicMock()
    v.expanded_nodes = set()
    v.selected_source = "qobuz"
    v.selection_mode = False
    v.selected_keys = set()
    v._selected_records = {}
    v._results_list = ft.ListView()
    v._selection_count = ft.Text()
    v._selection_download_btn = ft.Container()
    v._selection_bar = ft.Container()
    v._view_tabs_row = ft.Row()
    v._header_slot = ft.AnimatedSwitcher(content=v._view_tabs_row)
    return v


def _rec(i=1, m_type="track", image="", source="qobuz"):
    return {"id": str(i), "media_type": m_type, "source": source,
            "ui_title": f"Item {i}", "ui_subtitle": "Artist",
            "ui_detail": "Album  ·  2024", "image": image}


class TestLeadingVisual(unittest.TestCase):
    """Result rows carry a typed icon, never cover art.

    A page of 35 rows meant 35 remote image fetches plus 35 decodes on every
    page turn and re-render, which cost more on device than the thumbnails were
    worth. Artwork stays where it is cheap and already cached: the library
    list, the mini-player and the download dock.
    """

    def setUp(self):
        self.engine = patch("ui.views.search.audio_engine").start()
        self.engine.current_track = ""
        self.engine.current_artist = ""
        self.addCleanup(patch.stopall)

    def _leading(self, rec, view=None):
        v = view or _view()
        return v._result_card(0, rec).content.leading.controls[1]

    def test_no_image_control_even_when_artwork_is_available(self):
        ctrl = self._leading(_rec(image="https://example.test/a.jpg"))
        self.assertNotIsInstance(ctrl, ft.Image)
        self.assertIsInstance(ctrl, ft.Icon)

    def test_row_tree_contains_no_images_at_all(self):
        v = _view()
        entry = v._result_card(0, _rec(image="https://example.test/a.jpg"))

        def walk(ctrl):
            yield ctrl
            for attr in ("content", "leading", "title", "subtitle", "trailing"):
                child = getattr(ctrl, attr, None)
                if isinstance(child, ft.Control):
                    yield from walk(child)
            for child in (getattr(ctrl, "controls", None) or []):
                if isinstance(child, ft.Control):
                    yield from walk(child)

        self.assertFalse([c for c in walk(entry) if isinstance(c, ft.Image)])

    def test_icon_reflects_the_media_type(self):
        self.assertEqual(self._leading(_rec(m_type="album")).icon, ft.Icons.ALBUM_ROUNDED)
        self.assertEqual(self._leading(_rec(m_type="artist")).icon, ft.Icons.PERSON_ROUNDED)
        self.assertEqual(self._leading(_rec(m_type="track")).icon, ft.Icons.MUSIC_NOTE_ROUNDED)

    def test_selection_mode_replaces_the_icon_with_a_tick(self):
        v = _view()
        rec = _rec(image="x.jpg")
        v.enter_selection(rec)
        self.assertEqual(self._leading(rec, v).icon, ft.Icons.CHECK_CIRCLE_ROUNDED)

    def test_unselected_row_in_selection_mode_shows_an_empty_tick(self):
        v = _view()
        v.enter_selection(_rec(1))
        self.assertEqual(self._leading(_rec(2), v).icon,
                         ft.Icons.RADIO_BUTTON_UNCHECKED_ROUNDED)


class TestSourceBadge(unittest.TestCase):
    """ui_source_color was computed by the searcher and never rendered. The
    badge is what allows every status string to stay backend-agnostic."""

    def setUp(self):
        self.engine = patch("ui.views.search.audio_engine").start()
        self.engine.current_track = ""
        self.engine.current_artist = ""
        self.addCleanup(patch.stopall)

    def test_badge_shows_the_record_source(self):
        v = _view()
        tile = v._result_card(0, _rec(source="deezer")).content
        badge = tile.subtitle.controls[0]
        self.assertEqual(badge.content.value, "DEEZER")

    def test_badge_colour_differs_per_source(self):
        v = _view()
        q = v._source_badge(_rec(source="qobuz")).content.color
        d = v._source_badge(_rec(source="deezer")).content.color
        self.assertNotEqual(q, d)

    def test_badge_falls_back_to_the_selected_source(self):
        v = _view()
        v.selected_source = "deezer"
        badge = v._source_badge({"id": "1", "media_type": "track"})
        self.assertEqual(badge.content.value, "DEEZER")


class TestSubtitleComposition(unittest.TestCase):
    def setUp(self):
        self.engine = patch("ui.views.search.audio_engine").start()
        self.engine.current_track = ""
        self.engine.current_artist = ""
        self.addCleanup(patch.stopall)

    def test_artist_is_dropped_when_it_repeats_the_title(self):
        v = _view()
        rec = {"id": "1", "media_type": "album", "source": "qobuz",
               "ui_title": "Radiohead", "ui_subtitle": "Radiohead",
               "ui_detail": "2024", "image": ""}
        text = v._result_card(0, rec).content.subtitle.controls[1].value
        self.assertEqual(text, "2024")

    def test_both_parts_are_joined_when_they_differ(self):
        v = _view()
        text = v._result_card(0, _rec()).content.subtitle.controls[1].value
        self.assertIn("Artist", text)
        self.assertIn("Album", text)


class TestStagger(unittest.TestCase):
    def test_early_rows_are_staggered(self):
        a = AnimatedEntry(ft.Text("x"), stagger_index=0)
        b = AnimatedEntry(ft.Text("x"), stagger_index=3)
        self.assertEqual(a.stagger_delay, 0)
        self.assertEqual(b.stagger_delay, 3 * STAGGER_STEP_MS)

    def test_late_rows_are_not_staggered(self):
        """A stagger running the full length of a 35-row page reads as lag."""
        late = AnimatedEntry(ft.Text("x"), stagger_index=STAGGER_LIMIT + 5)
        self.assertEqual(late.stagger_delay, 0)

    def test_opting_out_yields_no_delay(self):
        self.assertEqual(AnimatedEntry(ft.Text("x")).stagger_delay, 0)
        self.assertEqual(AnimatedEntry(ft.Text("x"), stagger_index=None).stagger_delay, 0)


class TestStatusCardsOverlay(unittest.TestCase):
    """As column children the cards occupied layout space, so the result list
    jumped down when a search started and back when it ended."""

    def test_cards_are_not_direct_children_of_the_root_column(self):
        import inspect
        src = inspect.getsource(SearchView.__init__)
        root = src[src.index("self._root = ft.Column("):]
        stack_at = root.index("ft.Stack(")
        for name in ("self._search_progress_card", "self._preview_progress_card"):
            self.assertIn(name, root)
            # Both must appear only after the Stack begins.
            self.assertGreater(root.index(name), stack_at, name)


if __name__ == "__main__":
    unittest.main()
