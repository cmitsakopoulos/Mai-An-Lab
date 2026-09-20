import os
import time
import asyncio
import logging
import itertools

from utils.error_boundary import JobCancelledException
from utils.streamrip_api import download, get_default_download_path

logger = logging.getLogger(__name__)

# ── Job states ───────────────────────────────────────────────────────────────
QUEUED      = "queued"
DOWNLOADING = "downloading"
DONE        = "done"
FAILED      = "failed"
CANCELLED   = "cancelled"

TERMINAL_STATES = (DONE, FAILED, CANCELLED)

# How many finished jobs to keep so the dock can show outcomes instead of
# silently vanishing rows. Bounded: this list lives for the whole session.
HISTORY_LIMIT = 30


class QueueController:
    """Owns download jobs and their lifecycle. Knows nothing about any view.

    It previously reached directly into `app.search_view` to drive a progress
    card, which is why downloads were invisible from every other tab and why
    the controller could not be reasoned about on its own. Observers now
    register with `add_listener`; the dock is just one of them.
    """

    def __init__(self, app):
        self.app = app
        self._pending: list[dict] = []
        self._history: list[dict] = []
        self.current_job: dict | None = None
        self.is_processing = False

        self._cancel_event = asyncio.Event()
        self._job_lock = asyncio.Lock()
        self._worker_task: asyncio.Task | None = None
        self._ids = itertools.count(1)
        self._listeners: list = []
        self._scan_needed = False

    # ── observation ─────────────────────────────────────────────────────────
    def add_listener(self, fn) -> None:
        """Register a zero-arg callable fired whenever queue state changes."""
        if fn not in self._listeners:
            self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                logger.exception("queue listener failed")

    # ── introspection ───────────────────────────────────────────────────────
    @property
    def download_queue(self) -> list[dict]:
        """Jobs still waiting. Kept for existing call sites."""
        return self._pending

    @property
    def jobs(self) -> list[dict]:
        """Everything the dock renders: active first, then waiting, then done."""
        active = [self.current_job] if self.current_job else []
        return active + list(self._pending) + list(reversed(self._history))

    @property
    def active_count(self) -> int:
        """Jobs that have not reached a terminal state."""
        return len(self._pending) + (1 if self.current_job else 0)

    @property
    def is_idle(self) -> bool:
        return self.active_count == 0

    def find(self, job_id: str) -> dict | None:
        for job in self.jobs:
            if job and job.get("id") == job_id:
                return job
        return None

    # ── quality resolution ──────────────────────────────────────────────────
    def _quality_int(self, source: str, tier: str) -> int | None:
        src = (source or "").lower()
        if tier == "mp3":   return 1
        if tier == "cd":    return 2
        if tier == "hires":
            if src == "qobuz":  return 4
            if src == "tidal":  return 3
            if src == "deezer": return 2
        return None

    # ── job construction ────────────────────────────────────────────────────
    def _make_job(self, item_data: dict, quality_tier: str) -> dict:
        source     = item_data.get("source", "qobuz")
        media_type = item_data.get("media_type", "track")
        item_id    = item_data.get("id", "")
        url        = item_data.get("url") or f"https://www.{source}.com/{media_type}/{item_id}"

        meta = dict(item_data)
        meta["quality"]       = self._quality_int(source, quality_tier)
        meta["quality_label"] = quality_tier.upper()

        return {
            "id":       f"dl{next(self._ids)}",
            "url":      url,
            "metadata": meta,
            # Denormalised for the dock so it never reaches into metadata.
            "source":     source,
            "media_type": media_type,
            "title":      meta.get("ui_title") or meta.get("name") or "Unknown",
            "artist":     meta.get("ui_subtitle") or meta.get("artist") or "",
            "image":      meta.get("image") or meta.get("image_url") or "",
            "quality_label": meta["quality_label"],
            "state":    QUEUED,
            "percent":  0.0,
            "status":   "Queued",
            "detail":   "",
        }

    # ── public API ──────────────────────────────────────────────────────────
    def enqueue(self, item_data: dict, quality_tier: str = "mp3") -> dict:
        job = self._make_job(item_data, quality_tier)
        self._pending.append(job)
        self._ensure_worker()
        self._notify()
        return job

    def enqueue_many(self, items: list[dict], quality_tier: str = "mp3") -> list[dict]:
        """Batch entry point. One notification and one worker check for the
        whole batch rather than N of each, so selecting 40 tracks does not
        schedule 40 redundant UI passes."""
        jobs = [self._make_job(item, quality_tier) for item in items]
        self._pending.extend(jobs)
        if jobs:
            self._ensure_worker()
            self._notify()
        return jobs

    def _ensure_worker(self) -> None:
        if not self._worker_task or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

    def remove(self, job_id: str) -> bool:
        """Drop one job. Cancels it if it is the one in flight."""
        if self.current_job and self.current_job.get("id") == job_id:
            self.cancel_current()
            return True
        for i, job in enumerate(self._pending):
            if job.get("id") == job_id:
                job["state"] = CANCELLED
                job["status"] = "Removed"
                self._pending.pop(i)
                self._push_history(job)
                self._notify()
                return True
        # Already finished: drop it from history outright.
        for i, job in enumerate(self._history):
            if job.get("id") == job_id:
                self._history.pop(i)
                self._notify()
                return True
        return False

    def retry(self, job_id: str) -> dict | None:
        """Re-queue a finished job. Previously a failure was a dead end and the
        user had to find the item in search results again."""
        job = self.find(job_id)
        if not job or job.get("state") not in TERMINAL_STATES:
            return None
        self._history = [j for j in self._history if j.get("id") != job_id]
        fresh = dict(job)
        fresh["id"]      = f"dl{next(self._ids)}"
        fresh["state"]   = QUEUED
        fresh["percent"] = 0.0
        fresh["status"]  = "Queued"
        fresh["detail"]  = ""
        self._pending.append(fresh)
        self._ensure_worker()
        self._notify()
        return fresh

    def clear_finished(self) -> int:
        count = len(self._history)
        self._history.clear()
        self._notify()
        return count

    def clear(self) -> int:
        """Cancel everything, in flight included."""
        count = self.active_count
        for job in self._pending:
            job["state"] = CANCELLED
            job["status"] = "Cancelled"
            self._push_history(job)
        self._pending.clear()
        self._cancel_event.set()
        self._notify()
        self.app.show_snackbar(f"Queue cleared ({count} item{'s' if count != 1 else ''}).")
        return count

    def cancel_current(self) -> None:
        self._cancel_event.set()
        self.app.show_snackbar("Cancellation requested…")

    # ── worker ──────────────────────────────────────────────────────────────
    def _push_history(self, job: dict) -> None:
        self._history.append(job)
        if len(self._history) > HISTORY_LIMIT:
            del self._history[: len(self._history) - HISTORY_LIMIT]

    async def _worker_loop(self):
        self.is_processing = True
        self._notify()
        try:
            while self._pending:
                job = self._pending.pop(0)
                self.current_job = job
                job["state"]  = DOWNLOADING
                job["status"] = "Starting…"
                self._cancel_event.clear()
                self._notify()

                await self._workflow(job)

                self._push_history(job)
                self.current_job = None
                self._notify()

                if self._pending:
                    # Brief beat between jobs so a burst does not look like one
                    # continuous blur of progress.
                    await asyncio.sleep(0.4)
        finally:
            self.is_processing = False
            self._worker_task = None
            self._cancel_event.clear()
            self._notify()
            # Re-index ONCE, after the whole queue drains. This used to run per
            # completed job, so a 20-track batch kicked off 20 full library
            # scans — each one rebuilding the library list and forcing a
            # page-wide update, on top of the download's own progress traffic.
            if self._scan_needed:
                self._scan_needed = False
                self._schedule_library_scan()

    def _schedule_library_scan(self) -> None:
        """Import whatever landed, shortly after the queue goes quiet."""
        view = getattr(self.app, "library_view", None)
        if view is None:
            return

        async def _deferred_scan():
            await asyncio.sleep(1.0)
            try:
                # Quiet: the user asked for a download, not a re-index.
                view.start_scan(quiet=True)
            except Exception:
                logger.exception("post-download library scan failed")

        asyncio.create_task(_deferred_scan())

    def _set(self, job: dict, *, status=None, percent=None, detail=None, state=None):
        """Mutate job state and tell observers. Progress prose is deliberately
        source-agnostic: which backend a job uses is rendered as data (the
        source badge), never interpolated into a status string."""
        if status  is not None: job["status"]  = status
        if percent is not None: job["percent"] = percent
        if detail  is not None: job["detail"]  = detail
        if state   is not None: job["state"]   = state
        self._notify()

    async def _workflow(self, job: dict):
        url      = job.get("url")
        metadata = job.get("metadata", {})
        target   = self.app.target_folder or get_default_download_path()
        last_update = [0.0]

        def progress_hook(data):
            now = time.time()
            pct = data.get("percent")
            if pct is None or pct >= 100 or (now - last_update[0] > 0.25):
                last_update[0] = now
                self._set(
                    job,
                    status=(data.get("status", "") or "").capitalize() or "Downloading",
                    percent=pct,
                    detail=data.get("message", ""),
                )

        try:
            await asyncio.to_thread(os.makedirs, target, exist_ok=True)
            for attempt in range(3):
                try:
                    async with self._job_lock:
                        if self._cancel_event.is_set():
                            raise JobCancelledException()
                    self._set(
                        job,
                        status="Initializing…",
                        percent=5,
                        detail="Contacting API…" if attempt == 0
                        else f"Contacting API… (attempt {attempt + 1} of 3)",
                    )

                    await download(
                        url, target,
                        progress_callback=progress_hook,
                        quality=metadata.get("quality"),
                        stop_event=self._cancel_event,
                    )
                    break
                except JobCancelledException:
                    raise
                except Exception as exc:
                    if attempt < 2:
                        wait = 5 * (2 ** attempt)
                        self._set(job, status="Retrying", percent=0,
                                  detail=f"{exc}. Retrying in {wait}s…")
                        for _ in range(wait * 2):
                            if self._cancel_event.is_set():
                                raise JobCancelledException()
                            await asyncio.sleep(0.5)
                    else:
                        raise Exception(f"Failed after 3 attempts: {exc}") from exc

            self._set(job, state=DONE, status="Finished", percent=100,
                      detail="Download complete")
            # NOT routed through safe_update: this plays a sound and fires a
            # haptic and mutates no control at all, but an untargeted entry in
            # a flush widens that whole flush back to a full page.update() —
            # re-syncing every control in every cached tab. It was the one
            # guaranteed full-tree sync on the completion path.
            try:
                self.app.play_success_notification()
            except Exception:
                logger.exception("success notification failed")
            self._scan_needed = True

        except JobCancelledException:
            self._set(job, state=CANCELLED, status="Cancelled", percent=0,
                      detail="Aborted by user")
        except Exception as exc:
            self._set(job, state=FAILED, status="Failed", percent=0, detail=str(exc))
