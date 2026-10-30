"""ProgressNotifier: throttling, silence on no-op runs, and the callback
adapters for bulk_analyze_library / enrich_library."""

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import progress_notify as pn


class _FakeService:
    def __init__(self):
        self.calls = []
        self.action_handlers = {}

    async def show_progress_notification(self, **kw):
        self.calls.append(kw)

    async def show_alert_notification(self, **kw):
        self.calls.append({"alert": True, **kw})

    async def cancel_alert_notification(self, key):
        self.calls.append({"cancel_alert": key})

    def register_notification_action_handler(self, job, cb):
        self.action_handlers[job] = cb

    def unregister_notification_action_handler(self, job, callback=None):
        if callback is None or self.action_handlers.get(job) is callback:
            self.action_handlers.pop(job, None)


def _drive(fn):
    """Run `fn(notifier)` on a loop and let the scheduled posts land."""
    svc = _FakeService()

    async def main():
        n = pn.ProgressNotifier("dsp", "Analysing")
        fn(n)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    with patch.object(pn, "_service", return_value=svc):
        asyncio.run(main())
    return svc.calls


class TestNotifier(unittest.TestCase):
    def test_burst_is_throttled_but_first_and_last_post(self):
        calls = _drive(lambda n: [n.update(i, 500, f"{i}") for i in range(1, 501)])
        self.assertEqual([c["progress"] for c in calls], [1, 500])
        self.assertTrue(all(c["job"] == "dsp" and not c["done"] for c in calls))

    def test_finish_is_silent_when_nothing_was_shown(self):
        self.assertEqual(_drive(lambda n: n.finish("done")), [])

    def test_finish_after_progress_posts_summary(self):
        calls = _drive(lambda n: (n.update(1, 2, "x"), n.finish("All done")))
        self.assertEqual(calls[-1]["done"], True)
        self.assertEqual(calls[-1]["content"], "All done")

    def test_cancel_button_only_when_something_listens(self):
        svc = _FakeService()

        async def main():
            pn.ProgressNotifier("download", "Downloads").update(1, 2, "x")
            pn.ProgressNotifier("dsp", "Analysing", on_cancel=lambda: None).update(1, 2, "x")
            await asyncio.sleep(0)

        with patch.object(pn, "_service", return_value=svc):
            asyncio.run(main())
        self.assertEqual([(c["job"], c["cancellable"]) for c in svc.calls],
                         [("download", False), ("dsp", True)])

    def test_finish_leaves_a_newer_runs_cancel_handler(self):
        svc = _FakeService()

        async def main():
            old = pn.ProgressNotifier("dsp", "A", on_cancel=lambda: None)
            new = pn.ProgressNotifier("dsp", "B", on_cancel=lambda: None)
            old.finish()
            self.assertIn("dsp", svc.action_handlers)
            new.finish()
            self.assertNotIn("dsp", svc.action_handlers)

        with patch.object(pn, "_service", return_value=svc):
            asyncio.run(main())

    def test_metadata_cb_forwards_to_inner(self):
        seen = []
        calls = _drive(lambda n: pn.metadata_progress_cb(n, lambda *a: seen.append(a))(
            1, 3, "Björk", {"status": "ok"}))
        self.assertEqual(seen, [(1, 3, "Björk", {"status": "ok"})])
        self.assertIn("Björk", calls[0]["content"])

    def test_summary_text(self):
        self.assertIn("failed", pn.metadata_summary_text({"status": "error: timeout"}))
        self.assertEqual(pn.metadata_summary_text({"status": "completed", "enriched": 4, "total": 5}),
                         "Metadata updated for 4 of 5 artists.")

    def test_cancel_handler_registration_and_execution(self):
        svc = _FakeService()
        cancelled = []
        with patch.object(pn, "_service", return_value=svc):
            n = pn.ProgressNotifier("download", "Downloading", on_cancel=lambda: cancelled.append(True))
            self.assertIn("download", svc.action_handlers)
            # Simulate cancel action from native NotificationActionReceiver
            svc.action_handlers["download"]("download", "cancel")
            self.assertEqual(cancelled, [True])
            n.finish("Done")
            self.assertNotIn("download", svc.action_handlers)

    def test_update_posts_when_interval_elapses_even_if_pct_unchanged(self):
        svc = _FakeService()
        with patch.object(pn, "_service", return_value=svc):
            n = pn.ProgressNotifier("dsp", "Analysing")
            async def run():
                n.update(1, 100, "Track 1")
                # Wait past the 1.0s interval, keeping pct at 1%
                n._last_at = 0.0  # simulate elapsed time > 1.0s
                n.update(1, 100, "Track 1 - speed 5 MB/s")
                await asyncio.sleep(0)
            asyncio.run(run())
            self.assertEqual(len(svc.calls), 2)
            self.assertEqual(svc.calls[1]["content"], "Track 1 - speed 5 MB/s")

    def test_finish_with_action_keeps_a_retry_button(self):
        calls = _drive(lambda n: (n.update(1, 2, "x"),
                                  n.finish("2 failed.", action=("Retry", "downloads:retry"))))
        self.assertEqual((calls[-1]["action_label"], calls[-1]["action_target"]),
                         ("Retry", "downloads:retry"))

    def test_empty_summary_drops_the_action(self):
        # Empty content cancels the card; a button on nothing makes no sense.
        calls = _drive(lambda n: (n.update(1, 2, "x"), n.finish("", action=("Retry", "x"))))
        self.assertNotIn("action_label", calls[-1])

    def test_metadata_retry_only_for_whole_run_failures(self):
        self.assertEqual(pn.metadata_retry_action({"status": "error: offline"}),
                         ("Retry", "workbench:sync"))
        self.assertEqual(pn.metadata_retry_action({"status": "aborted"}),
                         ("Retry", "workbench:sync"))
        self.assertIsNone(pn.metadata_retry_action({"status": "completed", "enriched": 3}))
        self.assertIsNone(pn.metadata_retry_action({"status": "cancelled"}))

    def test_post_and_cancel_alert(self):
        svc = _FakeService()

        async def main():
            pn.post_alert("scan", "T", "C", navigate_to="library",
                          action=("Analyse", "library:analyse"), icon="dsp")
            pn.cancel_alert("scan")
            await asyncio.sleep(0)

        with patch.object(pn, "_service", return_value=svc):
            asyncio.run(main())
        self.assertEqual(svc.calls[0]["key"], "scan")
        self.assertEqual(svc.calls[0]["action_target"], "library:analyse")
        self.assertFalse(svc.calls[0]["urgent"])
        self.assertEqual(svc.calls[1], {"cancel_alert": "scan"})

    def test_alerts_are_a_noop_without_the_native_bridge(self):
        with patch.object(pn, "_service", return_value=None):
            pn.post_alert("x", "t", "c")  # no loop, no service: must not raise
            pn.cancel_alert("x")


if __name__ == "__main__":
    unittest.main()
