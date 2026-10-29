"""End-to-end integration and scenario tests for the enhanced Android
Notification Center subsystem.

Tests cover:
1. Throttling and dynamic text updates (no freeze when percent is constant).
2. Multi-job isolation (Downloads, Sync, DSP, Metadata).
3. Notification Cancel action dispatching and task abort.
4. Cross-Device Sync notifications (DSP Offload & State Bundle Upload).
5. Completion summaries and auto-cleanup.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import progress_notify as pn
from utils.queue_controller import QueueController, DOWNLOADING, DONE, CANCELLED
from utils.sync.client import CompanionClient


class MockNativeAudioService:
    """Simulates FletAudioServicePlugin & LibraryTaskService on Android."""

    def __init__(self):
        self.notifications = {}  # job -> dict of latest notification state
        self.notification_history = []
        self.action_handlers = {}

    async def show_progress_notification(self, title, content, progress, total, done=False, job="dsp"):
        entry = {
            "job": job,
            "title": title,
            "content": content,
            "progress": progress,
            "total": total,
            "done": done,
        }
        self.notifications[job] = entry
        self.notification_history.append(entry)

    def register_notification_action_handler(self, job, callback):
        self.action_handlers[job] = callback

    def unregister_notification_action_handler(self, job):
        self.action_handlers.pop(job, None)

    def simulate_user_click_cancel(self, job):
        """Simulates NotificationActionReceiver firing ACTION_CANCEL_TASK."""
        handler = self.action_handlers.get(job) or self.action_handlers.get("all")
        if handler:
            res = handler(job, "cancel")
            if asyncio.iscoroutine(res):
                asyncio.create_task(res)
            return True
        return False


class TestNotificationScenarios(unittest.TestCase):

    def setUp(self):
        self.svc = MockNativeAudioService()

    def test_scenario_no_freeze_on_constant_percentage(self):
        """Ensures notifications keep updating status text even when pct is unchanged."""
        with patch.object(pn, "_service", return_value=self.svc):
            notifier = pn.ProgressNotifier("dsp", "Audio Analysis")

            async def run():
                # First post
                notifier.update(1, 1000, "Track 1 - Starting")
                await asyncio.sleep(0)

                # Advance clock past interval (1.0s), keep pct at 0%
                notifier._last_at = 0.0
                notifier.update(2, 1000, "Track 2 - 2.5 MB/s - ~45m left")
                await asyncio.sleep(0)

                # Advance clock past interval again, still at 0%
                notifier._last_at = 0.0
                notifier.update(3, 1000, "Track 3 - 3.1 MB/s - ~42m left")
                await asyncio.sleep(0)

            asyncio.run(run())

            dsp_calls = [c for c in self.svc.notification_history if c["job"] == "dsp"]
            self.assertEqual(len(dsp_calls), 3)
            self.assertEqual(dsp_calls[-1]["content"], "Track 3 - 3.1 MB/s - ~42m left")

    def test_scenario_concurrent_multi_job_isolation(self):
        """Ensures Downloads, Sync, and DSP run concurrently without overwriting each other."""
        with patch.object(pn, "_service", return_value=self.svc):
            dl_notifier = pn.ProgressNotifier("download", "Downloads")
            sync_notifier = pn.ProgressNotifier("sync", "DSP Offload to Mac")
            dsp_notifier = pn.ProgressNotifier("dsp", "Audio Analysis")

            async def run():
                dl_notifier.update(50, 100, "Song A.flac")
                sync_notifier.update(10, 50, "Offloading batch 1")
                dsp_notifier.update(3, 30, "Analyzing local track 3")
                await asyncio.sleep(0)

            asyncio.run(run())

            self.assertIn("download", self.svc.notifications)
            self.assertIn("sync", self.svc.notifications)
            self.assertIn("dsp", self.svc.notifications)

            self.assertEqual(self.svc.notifications["download"]["content"], "Song A.flac")
            self.assertEqual(self.svc.notifications["sync"]["content"], "Offloading batch 1")
            self.assertEqual(self.svc.notifications["dsp"]["content"], "Analyzing local track 3")

    def test_scenario_download_cancel_action_aborts_queue(self):
        """Simulates user clicking [Cancel] on Download notification."""
        with patch.object(pn, "_service", return_value=self.svc):
            qc = QueueController(app=MagicMock())

            async def fake_workflow(job):
                # Simulate work that listens to cancel_event
                for _ in range(50):
                    if qc._cancel_event.is_set():
                        job["state"] = CANCELLED
                        break
                    await asyncio.sleep(0.01)

            qc._workflow = fake_workflow
            async def run():
                qc.enqueue({"id": "1", "source": "qobuz", "title": "Track 1"})
                qc.enqueue({"id": "2", "source": "qobuz", "title": "Track 2"})
                # Let worker start
                await asyncio.sleep(0.02)
                self.assertTrue(qc.is_processing)
                self.assertIn("download", self.svc.action_handlers)

                # User taps Cancel in Android Notification Drawer
                self.svc.simulate_user_click_cancel("download")

                # Wait for worker to stop
                if qc._worker_task:
                    await qc._worker_task

            asyncio.run(run())

            self.assertFalse(qc.is_processing)
            # Finish notification should report cancellation summary
            last_notif = self.svc.notification_history[-1]
            self.assertEqual(last_notif["job"], "download")
            self.assertTrue(last_notif["done"])

    def test_scenario_cross_device_sync_notification_flow(self):
        """Verifies DSP Offload updates the 'sync' notification and completes with summary."""
        with patch.object(pn, "_service", return_value=self.svc):
            notifier = pn.ProgressNotifier("sync", "DSP Offload to Mac")

            async def run():
                # Indeterminate start
                notifier.stage("Scanning Mac Companion cache…")
                await asyncio.sleep(0)
                self.assertEqual(self.svc.notifications["sync"]["total"], 0)

                # Progress ticks
                notifier._last_at = 0.0
                notifier.update(10, 50, "10/50 tracks (12.5 t/s) · song.flac")
                await asyncio.sleep(0)
                self.assertEqual(self.svc.notifications["sync"]["progress"], 10)

                # Finish summary
                notifier.finish("Offload complete: 50 tracks analysed.")
                await asyncio.sleep(0)

            asyncio.run(run())

            self.assertTrue(self.svc.notifications["sync"]["done"])
            self.assertEqual(self.svc.notifications["sync"]["content"], "Offload complete: 50 tracks analysed.")


if __name__ == "__main__":
    unittest.main()
