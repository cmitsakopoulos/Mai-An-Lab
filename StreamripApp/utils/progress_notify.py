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
    def __init__(self, job: str, title: str, on_cancel=None):
        self.job = job
        self.title = title
        self.on_cancel = on_cancel
        self._last_at = 0.0
        self._last_pct = -1
        self._last_content = ""
        # Only a notifier that actually showed something posts a summary, so a
        # no-op run (nothing to analyse / every artist up to date) stays silent.
        self.posted = False
        self._handler = None
        if on_cancel is not None:
            self._register_cancel()

    def set_cancel_handler(self, fn) -> None:
        self.on_cancel = fn
        if fn is not None:
            self._register_cancel()

    def _register_cancel(self) -> None:
        svc = _service()
        if svc and hasattr(svc, "register_notification_action_handler"):
            def _action_handler(job, action):
                if job == self.job and action == "cancel" and self.on_cancel:
                    try:
                        res = self.on_cancel()
                        if asyncio.iscoroutine(res):
                            asyncio.create_task(res)
                    except Exception as ex:
                        logger.warning("on_cancel callback for %s failed: %s", self.job, ex)
            self._handler = _action_handler
            svc.register_notification_action_handler(self.job, _action_handler)

    async def _send(self, content: str, progress: int, total: int, done: bool,
                    action: tuple[str, str] | None = None) -> None:
        svc = _service()
        if svc is None:
            return
        extra = {}
        if action:
            extra = {"action_label": action[0], "action_target": action[1]}
        try:
            await svc.show_progress_notification(
                title=self.title, content=content,
                progress=progress, total=total, done=done, job=self.job,
                # A Cancel button nothing listens to just does nothing on tap.
                cancellable=self.on_cancel is not None,
                **extra,
            )
        except Exception as exc:
            # A notification is never worth failing the job over.
            logger.debug("progress notification (%s) failed: %s", self.job, exc)

    def _post(self, content: str, progress: int, total: int, done: bool,
              action: tuple[str, str] | None = None) -> None:
        # Fire-and-forget so it can be called from sync progress callbacks
        # (metadata_enrich) as well as async ones (bulk_analyze_library).
        if not done:
            self.posted = True
        _spawn(self._send(content, progress, total, done, action))

    def update(self, done: int, total: int, content: str) -> None:
        """Post progress. Throttled to ~1 update/s, but never drops updates when
        content or percentage changes past the interval. total <= 0 shows an
        indeterminate bar."""
        now = time.monotonic()
        pct = int(done * 100 / total) if total > 0 else -1
        first = self._last_at == 0.0
        last = total > 0 and done >= total
        if not (first or last):
            if now - self._last_at < _MIN_INTERVAL_S:
                return
            if pct == self._last_pct and content == self._last_content:
                return
        self._last_at = now
        self._last_pct = pct
        self._last_content = content
        self._post(content, done, total, False)

    def stage(self, content: str) -> None:
        """Switch to an indeterminate bar with a new message, unthrottled."""
        self._last_at = time.monotonic()
        self._last_pct = -1
        self._last_content = content
        self._post(content, 0, 0, False)

    def finish(self, content: str = "", action: tuple[str, str] | None = None) -> None:
        """Replace the progress bar with a short-lived summary (or cancel it
        when `content` is empty). No-op if nothing was ever shown.

        `action` = (label, navigate target), e.g. ("Retry", "downloads:retry"),
        adds a button and keeps the summary until dismissed. The button opens
        the app, so it works even after this run (and its handlers) are gone;
        main._on_notification_navigate carries it out."""
        if self.posted:
            self._post(content, 0, 0, True, action if content else None)
        svc = _service()
        if self._handler is not None and svc and hasattr(svc, "unregister_notification_action_handler"):
            svc.unregister_notification_action_handler(self.job, self._handler)
        self._handler = None


def _spawn(coro) -> None:
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()  # no running loop (tests / shutdown)


def post_alert(key: str, title: str, content: str, *, navigate_to: str = "",
               action: tuple[str, str] | None = None, urgent: bool = False,
               icon: str = "") -> None:
    """Fire-and-forget one-shot card (not a progress bar). `key` names it so a
    newer card replaces an older one. A no-op where there is no native bridge."""
    svc = _service()
    if svc is None or not hasattr(svc, "show_alert_notification"):
        return

    async def _go():
        try:
            await svc.show_alert_notification(
                key=key, title=title, content=content, navigate_to=navigate_to,
                action_label=action[0] if action else "",
                action_target=action[1] if action else "",
                urgent=urgent, icon=icon,
            )
        except Exception as exc:
            logger.debug("alert notification (%s) failed: %s", key, exc)

    _spawn(_go())


def cancel_alert(key: str) -> None:
    """Remove the card posted under `key`, if any."""
    svc = _service()
    if svc is None or not hasattr(svc, "cancel_alert_notification"):
        return

    async def _go():
        try:
            await svc.cancel_alert_notification(key)
        except Exception as exc:
            logger.debug("cancel alert (%s) failed: %s", key, exc)

    _spawn(_go())


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


def metadata_retry_action(summary: dict) -> tuple[str, str] | None:
    """A Retry button for a sync that failed outright or lost its connection.
    Per-artist misses get none: those already retry on their own, rolling
    between sources, and a manual Retry would just repeat them."""
    status = (summary or {}).get("status") or ""
    if status.startswith("error") or status == "aborted":
        return ("Retry", "workbench:sync")
    return None


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
