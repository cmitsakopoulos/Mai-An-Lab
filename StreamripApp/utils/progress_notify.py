"""System (Android) progress notifications for long-running library jobs.

Track analysis and metadata sync can run for many minutes, often with the app
in the background, where in-app banners and snackbars are invisible. This posts
a native progress notification through the audio extension's
`show_progress_notification` (one per `job`), throttled so a fast loop doesn't
trip Android's notification-update rate limit (updates beyond a few per second
are silently dropped).

A no-op wherever the native service isn't available (macOS, tests, audio
service not yet up), so callers never need to guard.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

logger = logging.getLogger(__name__)

_MIN_INTERVAL_S = 1.0


def _service():
    if sys.platform == "darwin":
        return None  # AVFoundation engine; no native notification bridge
    try:
        from utils.audio_engine import audio_engine
    except Exception:
        return None
    svc = getattr(audio_engine, "audio_service", None)
    return svc if hasattr(svc, "show_progress_notification") else None


class ProgressNotifier:
    def __init__(self, job: str, title: str):
        self.job = job
        self.title = title
        self._last_at = 0.0
        self._last_pct = -1
        # Only a notifier that actually showed something posts a summary, so a
        # no-op run (nothing to analyse / every artist up to date) stays silent.
        self.posted = False

    async def _send(self, content: str, progress: int, total: int, done: bool) -> None:
        svc = _service()
        if svc is None:
            return
        try:
            await svc.show_progress_notification(
                title=self.title, content=content,
                progress=progress, total=total, done=done, job=self.job,
            )
        except Exception as exc:
            # A notification is never worth failing the job over.
            logger.debug("progress notification (%s) failed: %s", self.job, exc)

    def _post(self, content: str, progress: int, total: int, done: bool) -> None:
        # Fire-and-forget so it can be called from sync progress callbacks
        # (metadata_enrich) as well as async ones (bulk_analyze_library).
        if not done:
            self.posted = True
        try:
            asyncio.get_running_loop().create_task(
                self._send(content, progress, total, done))
        except RuntimeError:
            pass  # no running loop (tests / shutdown)

    def update(self, done: int, total: int, content: str) -> None:
        """Post progress. Skipped unless ≥1 s has passed AND the visible
        percentage moved, except for the first and final updates. total <= 0
        shows an indeterminate bar (e.g. a post-processing stage)."""
        now = time.monotonic()
        pct = int(done * 100 / total) if total > 0 else -1
        first = self._last_at == 0.0
        last = total > 0 and done >= total
        if not (first or last):
            if now - self._last_at < _MIN_INTERVAL_S or pct == self._last_pct:
                return
        self._last_at = now
        self._last_pct = pct
        self._post(content, done, total, False)

    def stage(self, content: str) -> None:
        """Switch to an indeterminate bar with a new message, unthrottled."""
        self._last_at = time.monotonic()
        self._last_pct = -1
        self._post(content, 0, 0, False)

    def finish(self, content: str = "") -> None:
        """Replace the progress bar with a short-lived summary (or cancel it
        when `content` is empty). No-op if nothing was ever shown."""
        if self.posted:
            self._post(content, 0, 0, True)


def format_eta(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"~{s}s left"
    if s < 3600:
        return f"~{s // 60}m {s % 60}s left"
    return f"~{s // 3600}h {(s % 3600) // 60}m left"


def dsp_progress_cb(notifier: ProgressNotifier):
    """A bulk_analyze_library progress_cb that drives `notifier`, with ETA."""
    started = time.monotonic()

    def _cb(done, total, _current, failures):
        text = f"{done} / {total} tracks"
        if failures:
            text += f" · {failures} failed"
        if 0 < done < total:
            rate = (time.monotonic() - started) / done
            text += f" · {format_eta(rate * (total - done))}"
        notifier.update(done, total, text)

    return _cb


def metadata_progress_cb(notifier: ProgressNotifier, inner=None):
    """An enrich_library `progress(i, total, name, res)` callback that drives
    `notifier` and then forwards to `inner` (e.g. the workbench banner)."""
    def _cb(i, total, name, res):
        notifier.update(i, total, f"{i} / {total} artists · {name}")
        if inner is not None:
            inner(i, total, name, res)
    return _cb


def metadata_summary_text(summary: dict) -> str:
    status = (summary or {}).get("status") or ""
    enriched = (summary or {}).get("enriched", 0)
    total = (summary or {}).get("total", 0)
    if status.startswith("error"):
        return "Metadata sync failed: " + status.partition(":")[2].strip()
    if status == "aborted":
        return f"Metadata sync stopped — connection lost ({enriched} of {total} done)."
    if status == "cancelled":
        return f"Metadata sync cancelled ({enriched} of {total} done)."
    return f"Metadata updated for {enriched} of {total} artists."
