import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft

from main import StreamripFletApp
from ui.views.search import SearchView


def _view():
    v = SearchView.__new__(SearchView)
    v.app = MagicMock()
    v.page = MagicMock()
    v.selected_source = "qobuz"
    v._source_cache = {}
    v._cache_query = {}
    v._last_scroll_pixels = 0
    v._saved_scroll = 0.0
    v._is_programmatic_scroll = False
    v._results_list = ft.ListView()
    return v


class TestScrollPrimitive(unittest.TestCase):
    """Flet's scroll_to is documented as ineffective on controls that build
    items on demand, and build_controls_on_demand defaults to True — so every
    scroll_to in this view was a no-op that only appeared to work when the
    target was already inside the built window."""

    def test_results_list_builds_eagerly_so_scroll_to_works(self):
        v = SearchView.__new__(SearchView)
        lv = ft.ListView(build_controls_on_demand=False)
        self.assertFalse(lv.build_controls_on_demand)
        # Guard the real construction, not just the concept.
        import inspect
        src = inspect.getsource(SearchView.__init__)
        self.assertIn("build_controls_on_demand=False", src)

    def test_no_magic_bottom_offset_remains(self):
        """Checked against code only: the comment explains what 3250 was."""
        import inspect
        src = inspect.getsource(SearchView.change_page)
        code = "\n".join(
            line.split("#", 1)[0] for line in src.splitlines()
        )
        self.assertNotIn("3250", code)
        # -1 is the documented jump-to-end sentinel.
        self.assertIn("-1 if scroll_to_bottom", code)


class TestScrollPreservation(unittest.TestCase):
    def test_hide_captures_the_current_offset(self):
        v = _view()
        v._last_scroll_pixels = 842.5
        v.on_hide()
        self.assertEqual(v._saved_scroll, 842.5)

    def test_show_schedules_a_restore(self):
        v = _view()
        v._last_scroll_pixels = 500
        v.on_hide()
        v.on_show()
        v.page.run_task.assert_called_once()
        self.assertEqual(v.page.run_task.call_args[0][1], 500.0)

    def test_show_at_top_schedules_nothing(self):
        v = _view()
        v.on_hide()
        v.on_show()
        v.page.run_task.assert_not_called()

    def test_negative_offsets_are_never_saved(self):
        """-1 is Flet's jump-to-end sentinel, not a position to restore to."""
        v = _view()
        v._last_scroll_pixels = -1
        v.on_hide()
        self.assertEqual(v._saved_scroll, 0.0)

    def test_restore_failure_does_not_propagate(self):
        v = _view()
        async def boom(**kwargs):
            raise RuntimeError("detached")
        v._results_list.scroll_to = boom
        asyncio.run(v._restore_scroll(300.0))
        self.assertFalse(v._is_programmatic_scroll)

    def test_restore_clears_the_programmatic_guard(self):
        v = _view()
        seen = {}
        async def ok(**kwargs):
            seen.update(kwargs)
        v._results_list.scroll_to = ok
        asyncio.run(v._restore_scroll(275.0))
        self.assertEqual(seen.get("offset"), 275.0)
        self.assertEqual(v._last_scroll_pixels, 275.0)
        self.assertFalse(v._is_programmatic_scroll)


class TestPerSourceCache(unittest.TestCase):
    """Switching Qobuz<->Deezer used to discard the other source's results, so
    toggling the pill on one query refetched every time."""

    def test_buckets_are_isolated_per_source(self):
        v = _view()
        v.cached_results = {"track": [{"id": "q"}], "album": [], "artist": []}
        v.selected_source = "deezer"
        self.assertEqual(v.cached_results["track"], [])
        v.selected_source = "qobuz"
        self.assertEqual(v.cached_results["track"], [{"id": "q"}])

    def test_mutating_the_bucket_writes_through(self):
        v = _view()
        v.cached_results["track"].append({"id": "1"})
        self.assertEqual(v._source_cache["qobuz"]["track"], [{"id": "1"}])

    def test_cache_is_fresh_only_for_the_same_query(self):
        v = _view()
        v.cached_results = {"track": [{"id": "1"}], "album": [], "artist": []}
        v._cache_query["qobuz"] = "radiohead"
        self.assertTrue(v._cache_is_fresh("qobuz", "radiohead"))
        self.assertFalse(v._cache_is_fresh("qobuz", "portishead"))
        self.assertFalse(v._cache_is_fresh("deezer", "radiohead"))

    def test_empty_results_are_not_treated_as_fresh(self):
        """A source that returned nothing must re-query, not show a blank page."""
        v = _view()
        v._cache_query["qobuz"] = "radiohead"
        self.assertFalse(v._cache_is_fresh("qobuz", "radiohead"))


class TestTabLifecycleDispatch(unittest.TestCase):
    def _app(self, current_tab=2):
        app = MagicMock()
        app._current_tab = current_tab
        app._previous_tab = 2
        app._view_cache = {}
        app._TAB_VIEWS = StreamripFletApp._TAB_VIEWS
        app._tab_lifecycle = lambda i, h: StreamripFletApp._tab_lifecycle(app, i, h)
        app._switch_tab = lambda i: StreamripFletApp._switch_tab(app, i)
        return app

    def test_switching_fires_hide_then_show(self):
        app = self._app(current_tab=2)
        app._switch_tab(1)
        app.library_view.on_hide.assert_called_once()
        app.search_view.on_show.assert_called_once()

    def test_reentering_the_same_tab_fires_nothing(self):
        app = self._app(current_tab=1)
        app._switch_tab(1)
        app.search_view.on_hide.assert_not_called()
        app.search_view.on_show.assert_not_called()

    def test_a_raising_hook_does_not_block_navigation(self):
        app = self._app(current_tab=2)
        app.library_view.on_hide.side_effect = RuntimeError("boom")
        app._switch_tab(1)
        # Navigation still completed.
        self.assertEqual(app._current_tab, 1)
        app.search_view.on_show.assert_called_once()

    def test_views_without_hooks_are_skipped(self):
        app = self._app(current_tab=2)
        app.library_view = object()   # no on_hide attribute
        app._switch_tab(1)
        self.assertEqual(app._current_tab, 1)


if __name__ == "__main__":
    unittest.main()
