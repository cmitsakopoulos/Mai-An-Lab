"""Guards Android system back / desktop Escape navigation.

Flet 0.86 wraps every view — including the automatic root view — in a Flutter
PopScope. The app runs on that single root view, so with `can_pop` left at its
default True the back gesture pops the only route and kills the app from
anywhere, including halfway down the Settings hierarchy. Setting can_pop=False
routes the gesture to `on_confirm_pop` instead, which is why no `page.views`
migration was needed.

The load-bearing invariant is that `confirm_pop()` is answered on EVERY path:
the Dart side parks on a completer with a 5-minute timeout and cancels the pop
if nothing arrives, which would leave the user unable to leave the app at all.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import StreamripFletApp
from ui.views.settings import SettingsView


def _app(current_tab=3, subpage=None, previous_tab=2):
    """A stub carrying only what navigate_back() touches."""
    app = MagicMock()
    app._current_tab = current_tab
    app._previous_tab = previous_tab
    app.settings_view._current_subpage_name = subpage
    # Bind the real implementations onto the stub.
    app.navigate_back = lambda: StreamripFletApp.navigate_back(app)
    return app


class TestNavigateBack(unittest.TestCase):
    def test_subpage_returns_to_hub_without_switching_tabs(self):
        app = _app(current_tab=3, subpage="Storage")

        self.assertTrue(app.navigate_back())
        app.settings_view._show_hub.assert_called_once()
        app._switch_tab.assert_not_called()

    def test_hub_returns_to_originating_tab(self):
        app = _app(current_tab=3, subpage=None, previous_tab=1)

        self.assertTrue(app.navigate_back())
        app._switch_tab.assert_called_once_with(1)
        app.settings_view._show_hub.assert_not_called()

    def test_hub_never_bounces_back_into_settings(self):
        # A corrupt _previous_tab must not make back a no-op loop.
        app = _app(current_tab=3, subpage=None, previous_tab=3)

        self.assertTrue(app.navigate_back())
        app._switch_tab.assert_called_once_with(2)

    def test_main_tabs_do_not_consume_back(self):
        # Returning False is what lets Android background the app normally.
        for tab in (0, 1, 2):
            with self.subTest(tab=tab):
                app = _app(current_tab=tab)
                self.assertFalse(app.navigate_back())
                app._switch_tab.assert_not_called()

    def test_missing_settings_view_falls_through_to_tab_switch(self):
        app = _app(current_tab=3, subpage=None, previous_tab=0)
        app.settings_view = None

        self.assertTrue(app.navigate_back())
        app._switch_tab.assert_called_once_with(0)


class TestPreviousTabTracking(unittest.TestCase):
    """_switch_tab must record where Settings was entered from."""

    def _real_switch_tab_app(self, current_tab):
        app = MagicMock()
        app._current_tab = current_tab
        app._previous_tab = 2
        app._view_cache = {}
        # safe_update runs the mutation inline so the real body executes.
        app.safe_update = lambda fn: fn()
        app._get_nav_index.return_value = 0
        return app

    def test_records_originating_tab(self):
        for origin in (0, 1, 2):
            with self.subTest(origin=origin):
                app = self._real_switch_tab_app(origin)
                StreamripFletApp._switch_tab(app, 3)
                self.assertEqual(app._previous_tab, origin)
                self.assertEqual(app._current_tab, 3)

    def test_reentering_settings_from_settings_is_not_self_referential(self):
        # library.py deep-links Settings→Storage while already on Settings.
        app = self._real_switch_tab_app(2)
        StreamripFletApp._switch_tab(app, 3)
        self.assertEqual(app._previous_tab, 2)

        StreamripFletApp._switch_tab(app, 3)
        self.assertEqual(app._previous_tab, 2, "must not become 3")

    def test_switching_between_main_tabs_leaves_previous_untouched(self):
        app = self._real_switch_tab_app(2)
        StreamripFletApp._switch_tab(app, 1)
        self.assertEqual(app._previous_tab, 2)


class TestShowHubClearsSubpage(unittest.TestCase):
    """Regression guard: the marker was set but never cleared."""

    def test_show_hub_clears_current_subpage_name(self):
        class MockApp:
            page = None
            def safe_update(self, fn):
                pass
            def show_snackbar(self, msg):
                pass

        view = SettingsView(app=MockApp())
        view._current_subpage_name = "Storage"
        view._baseline_subpage_state = {"a": 1}

        view._show_hub()

        self.assertIsNone(view._current_subpage_name)
        self.assertIsNone(view._baseline_subpage_state)


class TestConfirmPop(unittest.IsolatedAsyncioTestCase):
    """confirm_pop() must be answered exactly once on every path."""

    def _pop_app(self, navigate_back):
        app = MagicMock()
        recorded = []

        async def _confirm_pop(should_pop):
            recorded.append(should_pop)

        view = MagicMock()
        view.confirm_pop = _confirm_pop
        app.page.views = [view]
        app.navigate_back = navigate_back
        return app, recorded

    async def test_consumed_back_keeps_app_open(self):
        app, recorded = self._pop_app(lambda: True)
        await StreamripFletApp._on_confirm_pop(app, None)
        self.assertEqual(recorded, [False])

    async def test_unconsumed_back_lets_app_close(self):
        app, recorded = self._pop_app(lambda: False)
        await StreamripFletApp._on_confirm_pop(app, None)
        self.assertEqual(recorded, [True])

    async def test_exception_still_answers_and_defers_to_system(self):
        # If this ever stops answering, back dies for 5 minutes on device.
        def _boom():
            raise RuntimeError("resolver blew up")

        app, recorded = self._pop_app(_boom)
        await StreamripFletApp._on_confirm_pop(app, None)
        self.assertEqual(recorded, [True])

    async def test_confirm_pop_failure_is_swallowed(self):
        # A dead session must not surface as an unhandled task exception.
        app = MagicMock()

        async def _confirm_pop(should_pop):
            raise RuntimeError("session destroyed")

        view = MagicMock()
        view.confirm_pop = _confirm_pop
        app.page.views = [view]
        app.navigate_back = lambda: True

        await StreamripFletApp._on_confirm_pop(app, None)  # must not raise


if __name__ == "__main__":
    unittest.main()
