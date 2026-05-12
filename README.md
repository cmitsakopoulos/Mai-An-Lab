# Mai An Lab: Streamrip on your phone

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform: Android](https://img.shields.io/badge/Platform-Android-green.svg)]()
[![Built with Flet](https://img.shields.io/badge/Built%20with-Flet-blue.svg)](https://flet.dev)
[![Engine: Streamrip](https://img.shields.io/badge/Engine-Streamrip-orange.svg)](https://github.com/nathom/streamrip)

Mai An Lab is a high-fidelity music client that brings the full **[streamrip](https://github.com/nathom/streamrip)** download engine to Android, wrapped in a lightweight, glassmorphic player built on [Flet](https://flet.dev/). It is the first port of streamrip to a real mobile runtime.

The rest of the app is built around making that catalogue feel native: a fast indexer, a triggers-driven SQLite/FTS5 search layer, a customizable player, and a small experimental auto-playlist engine borrowed from bioinformatics clustering.

> [!NOTE]
> I am a bioinformatician by training, not a professional software engineer. This project is a passion-driven exploration of audio engineering and DSP. As such, it is a "living" passion project rather than a production-grade product, and it may not always follow standard enterprise coding patterns. It is a work of love, and I am learning as I go!

---

## Documentation (Wiki)

For detailed information on the internals of Mai An Lab, please refer to the following documentation:

- [🏗️ System Architecture & Database Design](./docs/Architecture.md)
- [🏝️ Auto-Playlist Engine & DSP Deep-Dive](./docs/Auto_Playlist_Engine.md)
- [🛠️ Build & Deployment Guide](#build-and-deployment)

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

- **Lightweight, customizable player** — A glassmorphic Flet UI with micro-animations and a modular design-token system you can re-skin without touching layout code.
- **Fast indexing and search** — Recursive library scans use hierarchical in-memory caches and a bulk-import mode that drops triggers during ingest, indexing 10k+ tracks in seconds.
- **Efficient SQL database** — A single `aiosqlite` connection with WAL journaling and a 64 MB page cache ensures the UI never blocks on heavy indexing tasks.
- **Personalized UI** — Choose your default startup page, re-skin with a single accent color, and manage advanced Streamrip TOML settings directly in-app.

---

## Build and Deployment

### Prerequisites (Windows)
To build the application for Android from a Windows machine, you must ensure the following toolchain is installed:
1. **Python 3.11+**: Required for the Flet build engine.
2. **Flutter SDK**: Install the latest stable version and ensure `flutter` is in your `PATH`.
3. **Android SDK & NDK**: Latest Command Line Tools with `ANDROID_HOME` set.
4. **Flet**: Install via `pip install flet`.

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

---

## Permissions
- `INTERNET` — streaming and downloading.
- `READ_EXTERNAL_STORAGE`, `WRITE_EXTERNAL_STORAGE`, `MANAGE_EXTERNAL_STORAGE` — filesystem access.
- `READ_MEDIA_AUDIO` — library scanning.
- `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_MEDIA_PLAYBACK` — background audio.
- `POST_NOTIFICATIONS` — media controls.
- `WAKE_LOCK` — prevents sleep during downloads.

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
