# System Architecture & Database Design

This document covers the structural components of Mai-An Lab and the optimizations that allow it to handle large music libraries on Android.

## Component Overview

| Component | Role |
|-----------|------|
| [StreamripApp](../StreamripApp) | UI, download queue, library indexer, search, playlist engine. |
| [db_manager.py](../StreamripApp/utils/db_manager.py) | SQLite + FTS5 layer: schema, triggers, async transactions. |
| [audio_engine.py](../StreamripApp/utils/audio_engine.py) | Player state, queue management, ExoPlayer workarounds. |
| [flet_audio_service](../flet_audio_service) | Python ↔ Dart ↔ Kotlin bridge for system-level media controls. |

---

## Database & Search Performance

The catalogue lives in a single SQLite file managed by `DatabaseManager`.

### Storage Strategy
- **Async concurrency**; `aiosqlite` with a shared connection and WAL mode.
- **Android-tuned cache**; 64 MB page cache to keep hot metadata off slow Android storage.
- **FTS5 search**; diacritic-folding prefix matches via `unicode61`. Prefix queries stay snappy as the catalogue grows.
- **Smart Triggers**; a suite of SQL triggers keeps aggregate counts (per-artist track/album counts) in sync on every mutation.
- **Single-Query Junction Optimization**; playlist track counts and metadata are fetched in a single pre-joined SQL query using a `LEFT JOIN` and `GROUP BY` clause. This eliminates sequential nested query lookups on the SQLite file, avoiding thread stalls and reducing CPU loading spikes from erratic peaks to a completely flat, fluid baseline.

## Database Schema

The data layer is optimized for fast hierarchical browsing and prefix-based search.

### Core Tables

| Table | Description | Key Fields |
|-------|-------------|------------|
| `artists` | Stores unique artist names and aggregate counts. | `id`, `name`, `album_count`, `track_count` |
| `albums` | Maps artists to their respective releases. | `id`, `artist_id`, `title`, `year`, `genre` |
| `tracks` | The primary music index. | `id`, `album_id`, `title`, `path`, `bpm`, `energy`, `timbre` (BLOB) |
| `playlists` | User-defined and imported collections. | `id`, `name`, `date_created` |
| `playlist_tracks` | Junction table for playlist membership. | `playlist_id`, `track_path`, `order_index` |
| `play_counts` | Persistence for history and playback-driven features. | `track_path`, `count`, `last_played` |

### Search & Indexing (FTS5)
- **`fts_search`**; a virtual table using the FTS5 module. It indexes `title`, `album`, and `artist` from the tracks table.
- **`unicode61` Tokenizer**; configured with `remove_diacritics=1` to ensure searching "Daft" also matches "Däft".
- **Automatic Sync**; SQL triggers (e.g., `trg_tracks_insert_counts`) automatically propagate changes from the `tracks` table to the FTS index, ensuring search results are never stale.

---

## Android Performance Tuning

To achieve a stable 60 FPS UI on Android while running a full CPython backend, several Android-specific optimizations were implemented:

- **Targeted UI Refreshes**; the app bypasses Flet's global `page.update()` for the high-frequency playback heartbeat. By refreshing only the specific player controls, we reduce background CPU spikes by ~60%.
- **Throttled Position Heartbeat**; playback position mirroring is strictly throttled to **1.0s** intervals. This minimizes bridge chatter and preserves battery life.
- **Zero-Cost Indicators**; heavy Python-driven animation loops were replaced with static or GPU-accelerated native indicators, keeping the background CPU baseline to a minimum.
- **Active UI Containment**; to prevent memory bloat or WebSocket choking under long sessions, high-volume control arrays (like the Jarvis chat history bubble tree) are strictly capped at **50** active items, dynamically popping older nodes.
- **Search Page Slide-In Scroll Pagination**; rather than appending thousands of search result cards in a massive flat list that bloats Flet's control tree and chokes the mobile memory buffer, the SearchView renders only **one active page at a time** (typically 20 items). Swapping pages teleports the scroll offset off-screen instantly and triggers a hardware-accelerated **Slide-In Offset Animation**, keeping the widget tree tiny and UI rendering at a rock-solid 60 FPS.
- **GPU-Threaded Auto-Scroll**; eliminated all async-sleep manual coordinate calculation loops. The chat interface relies 100% on Flet's native `auto_scroll=True` engine, offloading list navigation entirely to the platform's native thread and keeping the python execution loop empty.

## Artwork Caching & Temporary Storage

To minimize disk I/O and prevent UI lag during list scrolling, the app utilizes a multi-tiered artwork caching system.

### In-Memory LRU Cache
A Python-level **Least Recently Used (LRU)** cache (`_ARTWORK_CACHE`) stores up to **50** decoded artwork paths.
- **Fast Path**; if a track or album is displayed and its artwork is in the cache, the path is returned instantly without checking the disk or re-extracting from the audio file.

### Persistent Temporary Storage
Extracted artwork and downloaded images are stored in a `temp` directory within the app's internal storage.
- **Isolation**; these files are excluded from Android's Media Store to prevent them from appearing in the user's Gallery app.
- **Cleanup**; temporary artwork is pruned on app shutdown or when the LRU cache evicts a path, ensuring the app's storage footprint remains lean.

---

## Metadata Curation & Deletion Internals

Mai-An Lab supports direct in-app library modification, writing changes directly to physical files and executing database cascade routines under atomic async contexts:

### 1. Metadata Curation Pipeline
- **Physical Tag Mutations**: When editing a track’s metadata tags, the Python backend locks the physical file and executes header writes using low-level container bindings (e.g. Vorbis comments for `.flac`, ID3 tags for `.mp3`, and MP4 tags for `.m4a`).
- **Database Synchronization**: Following disk write completion, the app launches an asynchronous SQLite transaction. It updates the target `tracks`, `albums`, or `artists` tables while keeping WAL journaling active.
- **Aggregate Recalculation Triggers**: SQL triggers automatically run on the updated track rows, recalculating aggregate stats (like artist track counts and album releases) in real-time to keep the expanded UI listings perfectly synchronized.

### 2. Physical Song Deletion Pipeline
Song deletion executes in a strict two-phase atomic pipeline to ensure file-system and database consistency:
- **Phase 1: Physical Erasure**: The target file path is passed to an asynchronous worker thread, which uses `os.remove` to erase the file permanently from internal or external storage.
- **Phase 2: Database Cascade & Cleansing**: The track row is deleted from the `tracks` database table. A comprehensive cascade structure automatically triggers:
  * **Junction Cleanup**: Removes occurrences of the deleted track path from the `playlist_tracks` junction table.
  * **FTS5 De-indexing**: Wipes search tokens associated with the deleted track from the `fts_search` virtual table.
  * **Aggregate Updates**: Mutates artist track/album counts. If an artist or album now contains **zero** tracks, their parent rows are automatically deleted from `artists` and `albums` respectively, keeping the library tree clean.

---

## Download Auto-Rescanning Pipeline

To bridge remote search and the local playback library seamlessly, downloads automatically trigger non-blocking recursive indexers:

```text
Qobuz Download ──► Background Worker ──► Atomic Rename (.tmp → .flac)
                                                   │
                                                   ▼
                                         LibraryScanner Callback
                                                   │
                                                   ▼
                                       Targeted Indexing Scan
                                                   │
                                                   ▼
                                        SQLite DB WAL Ingestion
                                                   │
                                                   ▼
                                         Live UI Accent Refreshes
```

- **Non-Blocking Execution**: Remote downloads run on dedicated worker threads. When a download completes and passes checksum validation, it is renamed atomically from a `.tmp` file to its target format.
- **LibraryScanner Callback**: The downloader thread immediately fires an asynchronous callback that calls `LibraryScanner.scan_track` on the specific downloaded file path.
- **Instant Local Integration**: Instead of rebuilding the entire music database, the scanner performs a highly optimized, single-file SQLite WAL ingestion. It extracts metadata tags, writes the track details to `tracks` (triggering aggregate counters), and refreshes the Library tree dynamically.
- **Zero-Refresh User Experience**: The newly downloaded song appears in the **Library** tab within milliseconds of the search-card download completing. The UI replaces the download button with a **cyan check icon** automatically, enabling immediate playback without manual scanning.
