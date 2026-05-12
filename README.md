# Mai An Lab: Streamrip on your phone

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform: Android](https://img.shields.io/badge/Platform-Android-green.svg)]()
[![Built with Flet](https://img.shields.io/badge/Built%20with-Flet-blue.svg)](https://flet.dev)
[![Engine: Streamrip](https://img.shields.io/badge/Engine-Streamrip-orange.svg)](https://github.com/nathom/streamrip)

Mai An Lab is a high-fidelity music client that brings the full **[streamrip](https://github.com/nathom/streamrip)** download engine to Android, wrapped in a lightweight, glassmorphic player built on [Flet](https://flet.dev/). It is the first port of streamrip to a real mobile runtime — Qobuz catalogue browsing, lossless downloads (FLAC up to 24-bit / 192 kHz), tagging, and credentialed session management all run inside an embedded CPython interpreter via `serious-python`.

The rest of the app is built around making that catalogue feel native: a fast indexer, a triggers-driven SQLite/FTS5 search layer, a customizable player, and a small experimental auto-playlist engine borrowed from bioinformatics clustering.

---

## Table of Contents

- [Verified Environment](#verified-environment)
- [Headline Features](#headline-features)
- [UI/UX & Customization](#uiux--customization)
- [Mobile Performance Tuning](#mobile-performance-tuning)
- [Architecture](#architecture)
- [Database & Search](#database--search)
- [Auto-Playlist Engine](#auto-playlist-engine)
- [Build and Deployment](#build-and-deployment)
- [Permissions](#permissions)
- [License](#license)
- [Disclaimer](#disclaimer)
- [Credits](#credits)

---

## Verified Environment

This application has been strictly tested and verified on the following hardware/software stack:
- **Device**: Google Pixel 8
- **OS**: Android 16 (Developer Preview)
- **Build Number**: CP1A.260405.005
- **Baseline Performance**: ~15-20% CPU usage during active FLAC playback.

---

## Built With

| Component | Technology |
|---|---|
| **Logic** | [Serious Python](https://github.com/flet-dev/serious-python) (CPython 3.11 ARM64) |
| **UI Framework** | [Flet](https://flet.dev/) (Flutter backend) |
| **Audio Engine** | [Just Audio](https://pub.dev/packages/just_audio) + ExoPlayer |
| **Persistence** | [AioSQLite](https://github.com/omnilib/aiosqlite) (SQLite + FTS5) |
| **Analytics** | [NumPy](https://numpy.org/) (Pure-Python DSP) |

---

## Headline Features

> [!IMPORTANT]
> **Streamrip on Mobile** — This project is a custom-patched port of `streamrip 2.1.0`. We've stripped the `aiodns` C-extensions and relaxed dependency bounds to make the entire stack survive the Flutter/Android build process. Qobuz is supported natively out of the box.

- **Lightweight, customizable player** — A glassmorphic Flet UI with micro-animations, a live EQ visualizer, and a modular design-token system you can re-skin without touching layout code. Audio playback runs through a custom Python↔Dart bridge backed by **ExoPlayer**.
- **Fast indexing and search** — Recursive library scans use hierarchical in-memory caches and a bulk-import mode that drops triggers during ingest, indexing 10k+ tracks in seconds.
- **Efficient SQL database** — A single `aiosqlite` connection with WAL journaling and a 64 MB page cache ensures the UI never blocks on heavy indexing tasks.

---

## UI/UX & Customization

The app features a modular design-token system that allows for deep visual and functional personalization:

- **Dynamic Accent Engine** — A centralized color-token system allows users to re-skin the entire interface (buttons, progress bars, highlights) via a single primary accent color.
- **Glassmorphic Aesthetic** — Extensive use of blurred surfaces and semi-transparent overlays creates a premium, layered feel that adapts to the current track's artwork.
- **Tailored Navigation** — Users can choose their default startup page (e.g., Tracks vs. Playlists) and toggle specific landing-page sections like "Most Listened" or "Library Stats".
- **Advanced Configuration** — A built-in TOML editor provides direct access to the underlying Streamrip configuration file for power users.

---

## Mobile Performance Tuning

To achieve a stable 60 FPS UI on Android while running a full CPython backend, several mobile-specific optimizations were implemented:

- **Targeted UI Refreshes** — The app bypasses Flet's global `page.update()` for the high-frequency playback heartbeat. By refreshing only the specific player controls, we reduce background CPU spikes by ~60%.
- **Throttled Position Heartbeat** — Playback position mirroring is strictly throttled to **1.0s** intervals. This minimizes bridge chatter and preserves battery life.
- **Zero-Cost Indicators** — Heavy Python-driven animation loops were replaced with static or GPU-accelerated native indicators (ProgressRings), keeping the background CPU baseline to a minimum.

---

## Architecture

| Component | Role |
|-----------|------|
| [StreamripApp](./StreamripApp) | UI, download queue, library indexer, search, playlist engine. |
| [db_manager.py](./StreamripApp/utils/db_manager.py) | SQLite + FTS5 layer: schema, triggers, async transactions. |
| [audio_engine.py](./StreamripApp/utils/audio_engine.py) | Player state, queue management, ExoPlayer workarounds. |
| [flet_audio_service](./flet_audio_service) | Python ↔ Dart ↔ Kotlin bridge for system-level media controls. |

---

## Database & Search

The catalogue lives in a single SQLite file managed by `DatabaseManager`.

**Performance**
- **Async concurrency** — `aiosqlite` with a shared connection and WAL mode.
- **Mobile-tuned cache** — 64 MB page cache to keep hot metadata off slow mobile storage.
- **FTS5 search** — Diacritic-folding prefix matches via `unicode61`. Prefix queries stay snappy as the catalogue grows.
- **Smart Triggers** — A suite of SQL triggers keeps aggregate counts (per-artist track/album counts) in sync on every mutation.

---

## Auto-Playlist Engine

> [!TIP]
> **Bioinformatics Origins** — This engine was transplanted from bioinformatics research. Instead of clustering gene-expression profiles, we cluster *songs* in a 42-dimensional feature space using Markov Clustering (MCL). The engine identifies tracks that flow toward the same attractor as your seed, sequencing them into a smooth listening arc.

**The DSP Pipeline:**

```text
audio file ──► MediaCodec / ffmpeg ──► mono int16 PCM @ 22050 Hz  (middle 90 s)
                                                  │
                                                  ▼
                                       Pure-numpy DSP analyser
                                                  │
        ┌─────────────────────┬───────────────────┼────────────────────┬─────────────────────┐
        ▼                     ▼                   ▼                    ▼                     ▼
 RMS energy (dB)      Spectral centroid    Spectral rolloff   Onset-flux autocorr   Mel-filterbank STFT
                       (brightness)         (85th-pct freq)    + 120-BPM prior              │
                                                                │                            ▼
                                                                ▼                  log → DCT-II → MFCC
                                                          BPM + beat strength               │
                                                                                  ┌──────────┼──────────┐
                                                                                  ▼          ▼          ▼
                                                                              MFCC mean   MFCC std   chroma
                                                                                (13)        (13)      (12)
                                                                                                       ▲
                                                                                FFT bins → pitch class ┘
                                                                                  (mod 12, A=440 ref)
                                                  │
                                                  ▼
                       42-D feature vector per track  ── z-scored, axis-weighted (sound-profile bias)
                                                  │
                                                  ▼
                                   Gaussian-kernel similarity graph
                                                  │
                                                  ▼
                                   Markov Clustering (MCL: expand → inflate → prune)
                                                  │
                                                  ▼
                             Seed's attractor row → cluster of similar tracks
                                                  │
                                                  ▼
                       Resize to user-chosen length, sequence by greedy nearest-neighbour
```
---

## Build and Deployment

### Prerequisites (Windows)
To build the application for Android from a Windows machine, you must ensure the following toolchain is installed:
1. **Python 3.11+**: Required for the Flet build engine.
2. **Flutter SDK**: Install the latest stable version and ensure `flutter` is in your `PATH`.
3. **Android SDK & NDK**: 
   - Install the latest Command Line Tools.
   - Set the `ANDROID_HOME` environment variable to your SDK path.
   - Ensure the `build-tools`, `platform-tools`, and `ndk` packages are installed via `sdkmanager`.
4. **Flet**: Install the latest Flet library via `pip install flet`.

### Android Build
Built with `serious-python` to embed CPython into the Flutter/Flet runtime. 

> [!NOTE]
> This project uses a custom local extension (`flet_audio_service`). To ensure the path is resolved correctly on your machine, use the provided build scripts:

**Windows (PowerShell):**
```powershell
cd StreamripApp
.\build_android.ps1
```

**Linux / Mac (Shell):**
```bash
cd StreamripApp
chmod +x build_android.sh
./build_android.sh
```

> [!TIP]
> If you encounter Gradle file locking or transform errors, run `gradle --stop` and use the `--clear-cache` flag with the build scripts to force a clean dependency resolution.

---

## Permissions
- `INTERNET` — streaming and downloading.
- `READ_EXTERNAL_STORAGE`, `WRITE_EXTERNAL_STORAGE`, `MANAGE_EXTERNAL_STORAGE` — full filesystem access for music library management.
- `READ_MEDIA_AUDIO` — high-fidelity local library scanning.
- `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_MEDIA_PLAYBACK` — persistent background audio playback and service management.
- `POST_NOTIFICATIONS` — media transport controls in the notification shade.
- `WAKE_LOCK` — prevents the CPU from sleeping during active downloads or analysis tasks.

---

## License

Distributed under the MIT License. See `LICENSE` for details.

---

## Disclaimer

This is a modified version of the original Streamrip project. I take no credit for the original code. I just modified it to my needs and added a music player on top of it. 

As the creator of Streamrip notes: 
> **I will not be responsible for how you use streamrip. By using streamrip, you agree to the terms and conditions of the Qobuz API.**

---

## Credits

Thank you to the open-source communities of Streamrip and Flet for providing the tools and libraries that make my special interest possible!
