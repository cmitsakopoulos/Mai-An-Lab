# System Architecture & Database Design

This document covers the structural components of Mai An Lab and the optimizations that allow it to handle large music libraries on mobile devices.

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
- **Async concurrency** — `aiosqlite` with a shared connection and WAL mode.
- **Mobile-tuned cache** — 64 MB page cache to keep hot metadata off slow mobile storage.
- **FTS5 search** — Diacritic-folding prefix matches via `unicode61`. Prefix queries stay snappy as the catalogue grows.
- **Smart Triggers** — A suite of SQL triggers keeps aggregate counts (per-artist track/album counts) in sync on every mutation.

## Mobile Performance Tuning

To achieve a stable 60 FPS UI on Android while running a full CPython backend, several mobile-specific optimizations were implemented:

- **Targeted UI Refreshes** — The app bypasses Flet's global `page.update()` for the high-frequency playback heartbeat. By refreshing only the specific player controls, we reduce background CPU spikes by ~60%.
- **Throttled Position Heartbeat** — Playback position mirroring is strictly throttled to **1.0s** intervals. This minimizes bridge chatter and preserves battery life.
- **Zero-Cost Indicators** — Heavy Python-driven animation loops were replaced with static or GPU-accelerated native indicators, keeping the background CPU baseline to a minimum.
