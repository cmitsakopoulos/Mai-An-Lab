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

    async def show_progress_notification(self, **kw):
        self.calls.append(kw)


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


if __name__ == "__main__":
    unittest.main()
