import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.queue_controller import (
    QueueController, QUEUED, DOWNLOADING, DONE, FAILED, CANCELLED, HISTORY_LIMIT,
)


def _item(i=1, source="qobuz"):
    return {
        "id": str(i), "media_type": "track", "source": source,
        "ui_title": f"Track {i}", "ui_subtitle": "Artist", "image": "",
    }


def _controller():
    app = MagicMock()
    app.target_folder = "/tmp"
    return QueueController(app)


class TestChipDesyncRegression(unittest.TestCase):
    """Two enqueues landing in the same tick both saw is_processing False
    (it is set inside the worker task, which has not run yet), and the second
    wiped the first item's chip while both jobs stayed queued — the queue
    display desynced in exactly the multi-download case it existed for."""

    def test_rapid_enqueues_all_survive(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None   # keep jobs pending
            q.enqueue(_item(1))
            q.enqueue(_item(2))
            q.enqueue(_item(3))
            return q
        q = asyncio.run(run())
        self.assertEqual(len(q.download_queue), 3)
        self.assertEqual(q.active_count, 3)
        self.assertEqual([j["title"] for j in q.download_queue],
                         ["Track 1", "Track 2", "Track 3"])

    def test_every_job_gets_a_distinct_id(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            return q, q.enqueue_many([_item(i) for i in range(5)])
        q, jobs = asyncio.run(run())
        self.assertEqual(len({j["id"] for j in jobs}), 5)


class TestJobModel(unittest.TestCase):
    def test_job_denormalises_display_fields(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            return q.enqueue(_item(1, source="deezer"), quality_tier="hires")
        job = asyncio.run(run())
        self.assertEqual(job["state"], QUEUED)
        self.assertEqual(job["source"], "deezer")
        self.assertEqual(job["title"], "Track 1")
        self.assertEqual(job["quality_label"], "HIRES")
        # Deezer has no 24-bit tier; hires must resolve to its FLAC value.
        self.assertEqual(job["metadata"]["quality"], 2)

    def test_hires_quality_differs_per_source(self):
        q = _controller()
        self.assertEqual(q._quality_int("qobuz", "hires"), 4)
        self.assertEqual(q._quality_int("deezer", "hires"), 2)
        self.assertEqual(q._quality_int("deezer", "mp3"), 1)

    def test_enqueue_many_notifies_once_for_the_batch(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            calls = []
            q.add_listener(lambda: calls.append(1))
            q.enqueue_many([_item(i) for i in range(40)])
            return calls
        # 40 selected tracks must not schedule 40 separate UI passes.
        self.assertEqual(len(asyncio.run(run())), 1)


class TestRemovalAndRetry(unittest.TestCase):
    def test_remove_pending_job(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            jobs = q.enqueue_many([_item(i) for i in range(3)])
            q.remove(jobs[1]["id"])
            return q, jobs
        q, jobs = asyncio.run(run())
        self.assertEqual([j["id"] for j in q.download_queue],
                         [jobs[0]["id"], jobs[2]["id"]])
        self.assertEqual(jobs[1]["state"], CANCELLED)

    def test_remove_in_flight_job_requests_cancellation(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            job = q.enqueue(_item(1))
            q._pending.remove(job)
            q.current_job = job
            q.remove(job["id"])
            return q
        q = asyncio.run(run())
        self.assertTrue(q._cancel_event.is_set())

    def test_retry_requeues_a_failed_job(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            job = q.enqueue(_item(1))
            q._pending.clear()
            job["state"] = FAILED
            q._push_history(job)
            fresh = q.retry(job["id"])
            return q, job, fresh
        q, job, fresh = asyncio.run(run())
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh["state"], QUEUED)
        self.assertEqual(fresh["percent"], 0.0)
        self.assertNotEqual(fresh["id"], job["id"])
        self.assertEqual(len(q.download_queue), 1)

    def test_retry_refuses_an_active_job(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            job = q.enqueue(_item(1))
            return q.retry(job["id"])
        self.assertIsNone(asyncio.run(run()))

    def test_clear_cancels_everything_and_records_it(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            jobs = q.enqueue_many([_item(i) for i in range(3)])
            count = q.clear()
            return q, jobs, count
        q, jobs, count = asyncio.run(run())
        self.assertEqual(count, 3)
        self.assertEqual(q.download_queue, [])
        self.assertTrue(q._cancel_event.is_set())
        self.assertTrue(all(j["state"] == CANCELLED for j in jobs))
        # Cleared jobs remain visible as outcomes rather than vanishing.
        self.assertEqual(len(q._history), 3)

    def test_history_is_bounded(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            for i in range(HISTORY_LIMIT + 15):
                job = q.enqueue(_item(i))
                q._pending.clear()
                q._push_history(job)
            return q
        self.assertEqual(len(asyncio.run(run())._history), HISTORY_LIMIT)

    def test_clear_finished_leaves_active_jobs_alone(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            done = q.enqueue(_item(1))
            q._pending.remove(done)
            done["state"] = DONE
            q._push_history(done)
            q.enqueue(_item(2))
            q.clear_finished()
            return q
        q = asyncio.run(run())
        self.assertEqual(q._history, [])
        self.assertEqual(q.active_count, 1)


class TestViewDecoupling(unittest.TestCase):
    """The controller used to call app.search_view.* directly, which is why
    downloads were invisible from every other tab."""

    def test_controller_never_references_a_view(self):
        """Checked against the AST, not the text: the class docstring names
        search_view when explaining why the coupling was removed."""
        import ast
        import inspect
        from utils import queue_controller

        tree = ast.parse(inspect.getsource(queue_controller))
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertNotIn("search_view", attrs)
        self.assertNotIn("download_dock", attrs)
        # Sanity: the walk really does see app attribute access.
        self.assertIn("show_snackbar", attrs)

    def test_controller_imports_no_ui_module(self):
        """It used to import flet and ui.tokens purely to build chip widgets."""
        import ast
        import inspect
        from utils import queue_controller

        tree = ast.parse(inspect.getsource(queue_controller))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertNotIn("flet", imported)
        self.assertFalse([m for m in imported if m.startswith("ui.")], imported)

    def test_listeners_are_notified_on_state_change(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            calls = []
            q.add_listener(lambda: calls.append(1))
            job = q.enqueue(_item(1))
            q._set(job, status="Downloading", percent=50)
            q.remove(job["id"])
            return calls
        self.assertGreaterEqual(len(asyncio.run(run())), 3)

    def test_a_failing_listener_does_not_break_the_queue(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            q.add_listener(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            ok = []
            q.add_listener(lambda: ok.append(1))
            q.enqueue(_item(1))
            return q, ok
        q, ok = asyncio.run(run())
        self.assertEqual(len(q.download_queue), 1)
        self.assertEqual(len(ok), 1)


class TestJobsOrdering(unittest.TestCase):
    def test_active_first_then_pending_then_newest_finished(self):
        async def run():
            q = _controller()
            q._ensure_worker = lambda: None
            old = q.enqueue(_item(0)); q._pending.remove(old)
            old["state"] = DONE; q._push_history(old)
            newer = q.enqueue(_item(1)); q._pending.remove(newer)
            newer["state"] = DONE; q._push_history(newer)
            cur = q.enqueue(_item(2)); q._pending.remove(cur)
            cur["state"] = DOWNLOADING; q.current_job = cur
            q.enqueue(_item(3))
            return q
        titles = [j["title"] for j in asyncio.run(run()).jobs]
        self.assertEqual(titles, ["Track 2", "Track 3", "Track 1", "Track 0"])


if __name__ == "__main__":
    unittest.main()
