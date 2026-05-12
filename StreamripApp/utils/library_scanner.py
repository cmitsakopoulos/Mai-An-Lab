import os
import asyncio
import logging
try:
    from tinytag import TinyTag
except ImportError:
    TinyTag = None

logger = logging.getLogger(__name__)

class LibraryScanner:
    """
    Asynchronous library scanner that recursively scans a directory for audio files.
    Optimized to use synchronous batch reading to minimize thread context-switching overhead.
    """
    def __init__(self, target_folder, db_manager, progress_callback=None, completion_callback=None):
        self.target_folder = os.path.abspath(os.path.expanduser(target_folder)) if target_folder else ""
        self.db_manager = db_manager
        self.progress_callback = progress_callback
        self.completion_callback = completion_callback
        self.stop_requested = False
        self.supported_extensions = ('.mp3', '.flac', '.m4a', '.wav', '.ogg', '.opus', '.aac', '.mp4')

    def _read_tags_batch(self, batch_info):
        """
        Synchronously parses a batch of files using TinyTag.
        Runs in a background thread to avoid GIL contention and context-switching overhead.
        """
        parsed_tracks = []
        for path, mtime in batch_info:
            if self.stop_requested: break
            try:
                tag = TinyTag.get(path)
                if not tag.title and not tag.artist:
                    continue
                
                ext = os.path.splitext(path)[1][1:].upper()
                track_data = {
                    'title': tag.title or os.path.basename(path),
                    'artist': tag.albumartist or tag.artist or "Unknown Artist",
                    'album': tag.album or "Unknown Album",
                    'year': tag.year,
                    'genre': tag.genre,
                    'duration': tag.duration or 0.0,
                    'path': path,
                    'format': ext,
                    'added_date': mtime,
                    'bitrate': tag.bitrate or 0,
                    'track_num': tag.track
                }
                parsed_tracks.append(track_data)
            except Exception as e:
                logger.warning(f"Failed to parse {path}: {e}")
        return parsed_tracks

    async def run(self):
        if not TinyTag:
            logger.error("TinyTag library missing. Cannot scan.")
            await self._dispatch_completion(0, 0)
            return

        if not self.target_folder or not os.path.exists(self.target_folder):
            logger.warning("Target folder does not exist or is empty.")
            await self._dispatch_completion(0, 0)
            return

        db_state = await self.db_manager.get_path_mtime_map()
        
        if self.progress_callback:
            await self._ui_callback(-1, "Calculating library size...")

        # os.walk is blocking, so run it in a thread
        disk_files = await asyncio.to_thread(self._get_disk_files)
        
        if self.stop_requested: return

        deleted_paths = [p for p in db_state if p not in disk_files]
        if deleted_paths:
            await self.db_manager.delete_tracks_by_paths(deleted_paths)

        to_process = []
        for path, mtime in disk_files.items():
            if path not in db_state or abs(db_state[path] - mtime) > 1.0:
                to_process.append((path, mtime))
        
        total_to_process = len(to_process)
        if total_to_process == 0:
            await self.db_manager.prune_orphans()
            await self._dispatch_completion(0, 0)
            return

        # For large scans, drop the per-row INSERT trigger and rebuild counts/FTS
        # in a single bulk pass at the end — dramatically faster for big libraries.
        bulk_mode = total_to_process >= 200
        if bulk_mode:
            await self.db_manager.begin_bulk_import()

        processed = 0
        batch_size = 500

        try:
            for i in range(0, total_to_process, batch_size):
                if self.stop_requested: break

                chunk = to_process[i:i + batch_size]

                # Send the entire chunk to the thread pool in one go
                batch_results = await asyncio.to_thread(self._read_tags_batch, chunk)

                # Update DB and UI
                if batch_results:
                    await self.db_manager.insert_tracks_batch(batch_results)

                processed += len(chunk)
                if self.progress_callback:
                    p = (processed / total_to_process) * 100
                    msg = f"Imported {processed} of {total_to_process} tracks"
                    if chunk:
                        msg += f": {os.path.basename(chunk[-1][0])}"
                    await self._ui_callback(p, msg)
        finally:
            if bulk_mode:
                await self.db_manager.end_bulk_import()

        await self.db_manager.checkpoint()
        await self.db_manager.prune_orphans()
        await self._dispatch_completion(processed, 0)

    def _get_disk_files(self):
        disk_files = {}
        for root, _, files in os.walk(self.target_folder):
            if self.stop_requested: break
            for f in files:
                if f.lower().endswith(self.supported_extensions):
                    p = os.path.join(root, f)
                    try:
                        disk_files[p] = os.path.getmtime(p)
                    except: pass
        return disk_files

    async def _ui_callback(self, p, msg):
        if self.progress_callback:
            if asyncio.iscoroutinefunction(self.progress_callback):
                await self.progress_callback(p, msg)
            else:
                self.progress_callback(p, msg)

    async def _dispatch_completion(self, count, skipped_count):
        if self.completion_callback:
            if asyncio.iscoroutinefunction(self.completion_callback):
                await self.completion_callback(count, skipped_count)
            else:
                self.completion_callback(count, skipped_count)

    def stop(self):
        self.stop_requested = True