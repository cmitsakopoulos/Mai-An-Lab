import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft

from main import StreamripFletApp
from utils.queue_controller import QueueController, DONE


def _app():
    """A stub carrying only what the update machinery touches."""
    app = MagicMock()
    app.page = MagicMock()
    app.is_background = False
    app._pending_fns = []
    app._flush_pending = False
    app._update_lock = asyncio.Lock()
    app._flush_updates = lambda: StreamripFletApp._flush_updates(app)
    app._safe_update_handler = lambda fn, t=None: StreamripFletApp._safe_update_handler(app, fn, t)
    return app


class TestTargetedUpdates(unittest.TestCase):
    """A download emits progress ~4x/second. A bare page.update() per tick
    re-syncs every control in every cached tab, which is what made the whole
    UI visibly churn while downloading."""

    def test_targeted_flush_skips_page_update(self):
        app = _app()
        target = MagicMock()
        asyncio.run(app._safe_update_handler(lambda: None, target))
        app.page.update.assert_not_called()
        target.update.assert_called_once()

    def test_untargeted_flush_still_updates_the_page(self):
        app = _app()
        asyncio.run(app._safe_update_handler(lambda: None))
        app.page.update.assert_called_once()

    def test_one_untargeted_caller_widens_the_whole_flush(self):
        """Mixed batches must not leave the untargeted mutation unsynced."""
        async def run():
            app = _app()
            target = MagicMock()
            async with app._update_lock:
                app._pending_fns = [(lambda: None, target), (lambda: None, None)]
            await app._flush_updates()
            return app, target
        app, target = asyncio.run(run())
        app.page.update.assert_called_once()
        target.update.assert_not_called()

    def test_duplicate_targets_are_synced_once(self):
        async def run():
            app = _app()
            target = MagicMock()
            async with app._update_lock:
                app._pending_fns = [(lambda: None, target)] * 4
            await app._flush_updates()
            return target
        self.assertEqual(asyncio.run(run()).update.call_count, 1)

    def test_a_detached_target_does_not_raise(self):
        async def run():
            app = _app()
            target = MagicMock()
            target.update.side_effect = AssertionError("not mounted")
            async with app._update_lock:
                app._pending_fns = [(lambda: None, target)]
            await app._flush_updates()
        asyncio.run(run())   # must not propagate

    def test_backgrounded_app_syncs_nothing(self):
        app = _app()
        app.is_background = True
        target = MagicMock()
        asyncio.run(app._safe_update_handler(lambda: None, target))
        app.page.update.assert_not_called()
        target.update.assert_not_called()

    def test_dock_refresh_names_a_target(self):
        from ui.player.download_dock import DownloadDock
        app = MagicMock()
        app.page = MagicMock()
        dock = DownloadDock(app)
        dock.refresh()
        app.safe_update.assert_called_once()
        self.assertIs(app.safe_update.call_args[1]["target"], dock.pill)


class TestLibraryScanDebounce(unittest.TestCase):
    """start_scan rebuilds the library list and forces a page-wide update. It
    used to run once per completed download, so a 20-track batch kicked off 20
    full scans on top of the download's own progress traffic."""

    def _controller(self):
        app = MagicMock()
        app.target_folder = "/tmp"
        return QueueController(app)

    def test_workflow_does_not_scan_directly(self):
        import ast
        import inspect
        from utils import queue_controller

        # textwrap.dedent: a method's source is indented, which ast rejects.
        import textwrap
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(queue_controller.QueueController._workflow)
        ))
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertNotIn("start_scan", attrs)
        self.assertNotIn("library_view", attrs)

    def test_a_completed_job_only_flags_the_need(self):
        q = self._controller()
        self.assertFalse(q._scan_needed)
        q._scan_needed = True
        self.assertTrue(q._scan_needed)

    def test_drained_queue_scans_exactly_once(self):
        async def run():
            q = self._controller()
            scans = []
            q._schedule_library_scan = lambda: scans.append(1)

            async def fake_workflow(job):
                job["state"] = DONE
                q._scan_needed = True

            q._workflow = fake_workflow
            q.enqueue_many([
                {"id": str(i), "media_type": "track", "source": "qobuz",
                 "ui_title": f"T{i}", "ui_subtitle": "A"}
                for i in range(5)
            ])
            await q._worker_task
            return scans, q
        scans, q = asyncio.run(run())
        self.assertEqual(len(scans), 1)
        self.assertFalse(q._scan_needed)

    def test_no_scan_when_nothing_completed(self):
        async def run():
            q = self._controller()
            scans = []
            q._schedule_library_scan = lambda: scans.append(1)

            async def fake_workflow(job):
                job["state"] = "cancelled"

            q._workflow = fake_workflow
            q.enqueue({"id": "1", "media_type": "track", "source": "qobuz",
                       "ui_title": "T", "ui_subtitle": "A"})
            await q._worker_task
            return scans
        self.assertEqual(asyncio.run(run()), [])

    def test_missing_library_view_is_tolerated(self):
        q = self._controller()
        q.app.library_view = None
        q._schedule_library_scan()   # must not raise


if __name__ == "__main__":
    unittest.main()


class TestCompletionPathIsQuiet(unittest.TestCase):
    """Finishing a download must not force a full-tree sync.

    play_success_notification plays a sound and fires a haptic; it mutates no
    control. Routing it through safe_update put an untargeted entry into the
    flush, which widened that whole flush back to page.update() — the one
    guaranteed full re-sync of every control in every cached tab on completion.
    """

    def test_success_notification_is_not_routed_through_safe_update(self):
        import ast
        import inspect
        import textwrap
        from utils import queue_controller

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(queue_controller.QueueController._workflow)
        ))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "safe_update":
                for arg in node.args:
                    name = getattr(arg, "attr", None)
                    self.assertNotEqual(name, "play_success_notification")

    def test_completion_emits_no_full_page_update(self):
        async def run():
            app = _app()
            app.play_success_notification = MagicMock()
            app.target_folder = "/tmp"
            q = QueueController(app)
            q._schedule_library_scan = lambda: None
            q.add_listener(lambda: app.safe_update(lambda: None, MagicMock()))

            async def fake_workflow(job):
                q._set(job, status="Downloading", percent=50)
                q._set(job, state=DONE, status="Finished", percent=100)
                app.play_success_notification()
                q._scan_needed = True

            q._workflow = fake_workflow
            q.enqueue({"id": "1", "media_type": "track", "source": "qobuz",
                       "ui_title": "T", "ui_subtitle": "A"})
            await q._worker_task
            await asyncio.sleep(0)
            return app
        app = asyncio.run(run())
        app.page.update.assert_not_called()
        app.play_success_notification.assert_called_once()


class TestQuietScan(unittest.TestCase):
    """A post-download scan must not take over the view. Every step of a
    user-initiated scan is a page-wide disturbance: the scan banner, collapsing
    the expanded tree, a progress tick per file, and a full load_library()."""

    def _view(self):
        from ui.views.library import LibraryView
        v = LibraryView.__new__(LibraryView)
        v.app = MagicMock()
        v.page = MagicMock()
        v._is_scanning = False
        v._scan_is_quiet = False
        v._needs_reload = False
        v._cached_unanalysed = None
        v._scan_update_count = 0
        v._scan_progress = ft.ProgressBar()
        v._scan_progress_container = ft.Container(visible=False)
        v._scan_status_lbl = ft.Text()
        v._scan_btn = ft.Button()
        v._empty_label = ft.Container()
        v.expanded_nodes = {"/a", "/b"}
        v._path_to_controls = {"/a": object()}
        return v

    def test_quiet_scan_preserves_the_expanded_tree(self):
        from ui.views.library import LibraryView
        v = self._view()
        LibraryView.start_scan(v, quiet=True)
        self.assertEqual(v.expanded_nodes, {"/a", "/b"})
        self.assertFalse(v._scan_progress_container.visible)

    def test_loud_scan_still_takes_over(self):
        from ui.views.library import LibraryView
        v = self._view()
        LibraryView.start_scan(v, quiet=False)
        self.assertEqual(v.expanded_nodes, set())
        self.assertTrue(v._scan_progress_container.visible)

    def test_quiet_progress_ticks_are_dropped(self):
        from ui.views.library import LibraryView
        v = self._view()
        v._scan_is_quiet = True
        LibraryView._on_scan_progress(v, 50.0, "some track")
        v.app.safe_update.assert_not_called()

    def test_loud_progress_ticks_still_render(self):
        from ui.views.library import LibraryView
        v = self._view()
        LibraryView._on_scan_progress(v, 50.0, "some track")
        v.app.safe_update.assert_called_once()

    def test_quiet_completion_off_tab_defers_the_rebuild(self):
        from ui.views.library import LibraryView
        v = self._view()
        v._scan_is_quiet = True
        v.app._current_tab = 1          # user is on Search
        LibraryView._on_scan_complete(v, 3, 0)
        self.assertTrue(v._needs_reload)
        v.page.run_task.assert_not_called()
        v.app.show_snackbar.assert_not_called()

    def test_quiet_completion_on_tab_rebuilds_immediately(self):
        from ui.views.library import LibraryView
        v = self._view()
        v._scan_is_quiet = True
        v.app._current_tab = 2          # user is watching the library
        LibraryView._on_scan_complete(v, 3, 0)
        self.assertFalse(v._needs_reload)
        v.page.run_task.assert_called_once()

    def test_loud_completion_announces_itself(self):
        from ui.views.library import LibraryView
        v = self._view()
        v.app._current_tab = 1
        LibraryView._on_scan_complete(v, 3, 0)
        v.app.show_snackbar.assert_called_once()
        v.page.run_task.assert_called_once()

    def test_on_show_picks_up_a_deferred_rebuild_once(self):
        from ui.views.library import LibraryView
        v = self._view()
        v._needs_reload = True
        LibraryView.on_show(v)
        v.page.run_task.assert_called_once()
        LibraryView.on_show(v)
        v.page.run_task.assert_called_once()   # not repeated

    def test_post_download_scan_is_quiet(self):
        import ast
        import inspect
        import textwrap
        from utils import queue_controller

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(queue_controller.QueueController._schedule_library_scan)
        ))
        found = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "start_scan"
        ]
        self.assertEqual(len(found), 1)
        self.assertTrue(any(k.arg == "quiet" and k.value.value is True
                            for k in found[0].keywords))
