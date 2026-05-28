import os
import time
import asyncio
import logging
import flet as ft
from ui.tokens import CYAN, DIM
from utils.error_boundary import JobCancelledException
from utils.streamrip_api import download, get_default_download_path

logger = logging.getLogger(__name__)

class QueueController:
    def __init__(self, app):
        self.app = app
        self._queue = asyncio.Queue()
        self._pending_items = [] 
        self.is_processing = False
        self._cancel_event = asyncio.Event()
        self.current_job: dict | None = None
        self._job_lock = asyncio.Lock()
        self._status_chips: list[ft.Control] = []
        self._worker_task: asyncio.Task | None = None

    @property
    def download_queue(self) -> list[dict]:
        """Compatibility property for UI rendering."""
        return self._pending_items

    # ── quality resolution ──────────────────────────────────────────────────
    def _quality_int(self, source: str, tier: str) -> int | None:
        src = source.lower()
        if tier == "mp3":   return 1
        if tier == "cd":    return 2
        if tier == "hires":
            if src == "qobuz":  return 4
            if src == "tidal":  return 3
            if src == "deezer": return 2
        return None

    # ── public API ──────────────────────────────────────────────────────────
    def enqueue(self, item_data: dict, quality_tier: str = "mp3"):
        source     = item_data.get("source", "qobuz")
        media_type = item_data.get("media_type", "track")
        item_id    = item_data.get("id", "")
        url        = item_data.get("url") or f"https://www.{source}.com/{media_type}/{item_id}"

        meta = item_data.copy()
        meta["quality"]       = self._quality_int(source, quality_tier)
        meta["quality_label"] = quality_tier.upper()

        chip = ft.Container(
            width=12, height=12,
            border_radius=6,
            bgcolor=DIM + "44",
            animate_opacity=300,
        )
        self._status_chips.append(chip)
        job = {"url": url, "metadata": meta, "chip": chip}
        
        self._pending_items.append(job)
        self._queue.put_nowait(job)
        
        self.app.search_view.update_chips(self._status_chips)
        self.app.refresh_queue_ui()

        if not self.is_processing:
            if not self._worker_task or self._worker_task.done():
                self._worker_task = asyncio.create_task(self._worker_loop())
        else:
            self.app.show_snackbar(f"Added to queue ({quality_tier.upper()}).")

    def clear(self):
        count = len(self._pending_items)
        # Drain queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

        self._pending_items.clear()
        self._status_chips.clear()
        self._cancel_event.set()
        
        self.app.refresh_queue_ui()
        self.app.search_view.update_chips([])
        self.app.search_view.hide_progress_card()
        self.app.show_snackbar(f"Queue cleared ({count} items).")

    def cancel_current(self):
        self._cancel_event.set()
        self.app.show_snackbar("Cancellation requested…")

    async def _worker_loop(self):
        self.is_processing = True
        while not self._queue.empty():
            self.current_job = await self._queue.get()
            if self.current_job in self._pending_items:
                self._pending_items.remove(self.current_job)
            
            self._cancel_event.clear()
            
            chip = self.current_job.get("chip")
            if chip: chip.bgcolor = CYAN
                
            self.app.refresh_queue_ui()
            self.app.search_view.show_progress_card()

            await self._workflow()
            
            self._queue.task_done()
            
            # Transition delay
            delay = 1 if self._cancel_event.is_set() else 3
            await asyncio.sleep(delay)
            self._ui(self.app.search_view.hide_progress_card)
            self.current_job = None
            self.app.refresh_queue_ui()

        self.is_processing = False
        self._worker_task = None

    # ── background workflow ─────────────────────────────────────────────────
    async def _workflow(self):
        url      = self.current_job.get("url")
        metadata = self.current_job.get("metadata", {})
        target   = self.app.target_folder or get_default_download_path()
        last_update = [0.0]

        def progress_hook(data):
            now = time.time()
            pct = data.get("percent")
            if pct is None or pct >= 100 or (now - last_update[0] > 0.25):
                last_update[0] = now
                status  = data.get("status", "")
                message = data.get("message", "")
                self._ui(lambda s=status, p=pct, m=message:
                         self.app.search_view.update_progress(s.capitalize(), p, m))

        try:
            await asyncio.to_thread(os.makedirs, target, exist_ok=True)
            for attempt in range(3):
                try:
                    async with self._job_lock:
                        if self._cancel_event.is_set():
                            raise JobCancelledException()
                    self._ui(lambda a=attempt: self.app.search_view.update_progress(
                        "Initializing…", 5, f"Connecting to Qobuz API… (Attempt {a + 1})"))
                    
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
                        self._ui(lambda w=wait, e=str(exc): self.app.search_view.update_progress(
                            "Retrying", 0, f"Error: {e}. Retrying in {w}s…"))
                        for _ in range(wait * 2):
                            if self._cancel_event.is_set():
                                raise JobCancelledException()
                            await asyncio.sleep(0.5)
                    else:
                        raise Exception(f"Failed after 3 attempts: {exc}") from exc

            chip = self.current_job.get("chip")
            if chip: chip.bgcolor = "#4CAF50" # Green
            
            self._ui(lambda: self.app.search_view.update_progress(
                "Finished", 100, "Download completed successfully!"))

            # Automatically trigger library scan ~1s after download completes to import the new song
            if hasattr(self.app, "library_view") and self.app.library_view:
                async def _deferred_scan():
                    await asyncio.sleep(1.0)
                    self.app.library_view.start_scan()
                asyncio.create_task(_deferred_scan())

        except JobCancelledException:
            self._ui(lambda: self.app.search_view.update_progress(
                "Cancelled", 0, "Download aborted by user."))
            chip = self.current_job.get("chip")
            if chip: chip.bgcolor = "#F44336" # Red
        except Exception as exc:
            self._ui(lambda e=exc: self.app.search_view.update_progress(
                "Failed", 0, str(e)))
            chip = self.current_job.get("chip")
            if chip: chip.bgcolor = "#F44336"

    def _ui(self, fn):
        try:
            self.app.safe_update(fn)
        except Exception:
            logger.exception("UI update error")
