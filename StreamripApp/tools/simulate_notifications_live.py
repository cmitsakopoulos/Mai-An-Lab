#!/usr/bin/env python3
"""Interactive Live Notification Center Simulator.

Simulates the Android Notification Center in the terminal without needing an Android device or rebuild.
Renders real-time notification drawer cards with live progress bars, categories, subtext, and action buttons.
Allows testing Cancel buttons and multi-job concurrency interactively.
"""

import asyncio
import os
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import progress_notify as pn


class TerminalNotificationDrawer:
    """Simulates Android's Notification Center UI in the console."""

    ICONS = {
        "download": "⬇️ [DOWNLOAD]",
        "sync": "🔁 [DEVICE SYNC]",
        "dsp": "📊 [AUDIO ANALYSIS]",
        "metadata": "🏷️ [METADATA SYNC]",
        "library": "📚 [LIBRARY TASK]",
    }

    COLORS = {
        "download": "\033[32m",  # Green
        "sync": "\033[36m",      # Cyan
        "dsp": "\033[33m",       # Amber/Yellow
        "metadata": "\033[35m",  # Violet
        "reset": "\033[0m",
        "bold": "\033[1m",
        "dim": "\033[2m",
    }

    def __init__(self):
        self.active_notifications = {}
        self.action_handlers = {}

    def register_notification_action_handler(self, job: str, callback):
        self.action_handlers[job] = callback

    def unregister_notification_action_handler(self, job: str):
        self.action_handlers.pop(job, None)

    async def show_progress_notification(self, title, content, progress, total, done=False, job="dsp"):
        if done and not content:
            self.active_notifications.pop(job, None)
        else:
            self.active_notifications[job] = {
                "title": title,
                "content": content,
                "progress": progress,
                "total": total,
                "done": done,
                "updated_at": time.time(),
            }
        self.render()

    def simulate_cancel(self, job: str):
        handler = self.action_handlers.get(job) or self.action_handlers.get("all")
        if handler:
            print(f"\n⚡ User tapped [Cancel] on {job} notification...")
            res = handler(job, "cancel")
            if asyncio.iscoroutine(res):
                asyncio.create_task(res)

    def render(self):
        c = self.COLORS
        # Clear screen and draw cards
        sys.stdout.write("\033[H\033[J")
        print(f"{c['bold']}=== ANDROID NOTIFICATION CENTER SIMULATOR ==={c['reset']}")
        print(f"{c['dim']}Simulating Android System Notifications (Foreground Service & Action Buttons){c['reset']}\n")

        if not self.active_notifications:
            print(f"{c['dim']}No active notifications in shade.{c['reset']}\n")
            return

        for job, data in self.active_notifications.items():
            col = c.get(job, c["reset"])
            icon = self.ICONS.get(job, "🔔")
            title = data["title"]
            content = data["content"]
            progress = data["progress"]
            total = data["total"]
            done = data["done"]

            pct = int(progress * 100 / total) if total > 0 else 0
            subtext = "Finished" if done else (f"{pct}%" if total > 0 else "Processing…")

            # Progress bar
            bar_len = 28
            if total > 0 and not done:
                filled = int(bar_len * min(1.0, progress / total))
                bar_str = "█" * filled + "░" * (bar_len - filled)
            elif not done:
                bar_str = "▒▒▒ INDETERMINATE ▒▒▒"
            else:
                bar_str = "✓ Complete"

            print(f"{col}┌────────────────────────────────────────────────────────┐{c['reset']}")
            print(f"{col}│{c['reset']} {c['bold']}{icon}{c['reset']}  Mai An Lab • {c['dim']}{subtext}{c['reset']}")
            print(f"{col}│{c['reset']} {c['bold']}{title}{c['reset']}")
            print(f"{col}│{c['reset']} {content}")
            if not done:
                print(f"{col}│{c['reset']} [{bar_str}] {pct if total > 0 else ''}%")
                print(f"{col}│{c['reset']} [ ✕ Cancel Action ]")
            else:
                print(f"{col}│{c['reset']} Status: {c['bold']}Finished (Auto-dismiss in 12s){c['reset']}")
            print(f"{col}└────────────────────────────────────────────────────────┘{c['reset']}")


async def run_simulation():
    drawer = TerminalNotificationDrawer()

    with patch.object(pn, "_service", return_value=drawer):
        print("Starting simulated background jobs...")

        # 1. Start Downloads
        cancelled_dl = False
        def cancel_dl():
            nonlocal cancelled_dl
            cancelled_dl = True

        dl_notifier = pn.ProgressNotifier("download", "Downloads", on_cancel=cancel_dl)

        # 2. Start Cross-Device Sync (DSP Offload)
        cancelled_sync = False
        def cancel_sync():
            nonlocal cancelled_sync
            cancelled_sync = True

        sync_notifier = pn.ProgressNotifier("sync", "DSP Offload to Mac", on_cancel=cancel_sync)

        # Run concurrent updates
        for step in range(1, 11):
            if not cancelled_dl:
                dl_notifier.update(step * 10, 100, f"Track {step} of 10 · 4.2 MB/s · FLAC 24-bit")
            if not cancelled_sync:
                sync_notifier.update(step * 5, 50, f"Offloading {step * 5}/50 (18 tracks/s) · Mac Wi-Fi")

            await asyncio.sleep(0.3)

            # At step 5, simulate user tapping Cancel on the Sync notification
            if step == 5:
                drawer.simulate_cancel("sync")

        if cancelled_sync:
            sync_notifier.finish("DSP offload cancelled (25 of 50 done).")
        else:
            sync_notifier.finish("DSP offload complete: 50 tracks analysed.")

        dl_notifier.finish("10 downloads finished.")
        await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(run_simulation())
