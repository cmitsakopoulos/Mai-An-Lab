# Mai-An Lab: Dual-Platform Streamrip Music Player (Android & macOS)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform: Android](https://img.shields.io/badge/Platform-Android%2011%2B-green.svg)](https://developer.android.com/about/versions/11)
[![Platform: macOS](https://img.shields.io/badge/Platform-macOS%20Desktop-lightgrey.svg)](https://www.apple.com/macos/)
[![Python: 3.11](https://img.shields.io/badge/Python-3.11-blue.svg)](https://www.python.org/)
[![Built with Flet](https://img.shields.io/badge/Built%20with-Flet-blue.svg)](https://flet.dev)
[![Engine: Streamrip](https://img.shields.io/badge/Engine-Streamrip-orange.svg)](https://github.com/nathom/streamrip)

[**Latest Release**](https://github.com/cmitsakopoulos/Mai-An-Lab/releases/latest)

Mai-An Lab is a dual-platform music player and streamrip download client for Android and macOS. It embeds the streamrip download toolset in an interactive interface built with Flet.

The player implements native engine fallbacks; background services and hardware speech engines on mobile, and an Apple `AVAudioPlayer` loop on desktop. It features a Jarvis styled voice assistant, SQLite library search, and an auto-playlist engine based on $k$-NN acoustic similarity.

---

## Interactive UI Panes

---

### Search & Downloader

Search across streaming endpoints, preview snippets, and download albums or tracks to your system directory with visual progress tracking.

<p align="center">
  <img src="assets/search_example.gif" width="48%" alt="Search & Download Pane" style="border-radius: 12px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5); border: 1px solid rgba(255, 255, 255, 0.1);">
</p>

---

### Music Library & Playback Controls

Browse your catalog by artist, album, or track through SQL joins. The playback pane sits alongside the library, providing full queue management, scrubbing, and volume control.

> [!WARNING]
> **Preset Moods are heavily under construction.** The built-in profiles (`chill`, `happy`, `tonal`, etc.) use library-relative percentile scoring and are functional, but the mood definitions and threshold calibration are actively being revised. Expect behaviour to change between versions.

- **Automatic Preset Moods**: Start playlists using built-in mood profiles that score tracks against library-wide percentile distributions to match the sonic character of your collection.
- **Acoustic Islets (Custom Moods)**: Create custom moods from a single exemplar track centroid ($C = T_{\text{exemplar}}$). The player walks a 60-D $k$-NN similarity graph and selects tracks satisfying the cosine similarity threshold ($\cos(T_i, C) \ge \theta_{\text{islet}}$) for tempo-aligned playback.

<p align="center">
  <img src="assets/library_example.gif" width="48%" alt="Music Library Pane" style="border-radius: 12px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5); border: 1px solid rgba(255, 255, 255, 0.1);">
  &nbsp;
  <img src="assets/playback_pane_example.gif" width="48%" alt="Playback Controls Pane" style="border-radius: 12px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5); border: 1px solid rgba(255, 255, 255, 0.1);">
</p>

---

### Jarvis Voice Assistant

Use voice controls or chat to walk similarity graphs, search tracks, manage queues, make playlists, save custom Acoustic Islets, and trigger downloads — all via local keyword parsing and audio focus ducking. Say "Hello" and see what happens.

<p align="center">
  <img src="assets/idiot_example.gif" width="48%" alt="Jarvis Voice Assistant" style="border-radius: 12px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5); border: 1px solid rgba(255, 255, 255, 0.1);">
</p>

---

### PCA & Covariance Analysis *(v1.1.0)*

After every library rebuild the PCA engine generates four diagnostic figures (written to `<library>/pca_report/`) showing the full and pruned feature spaces. These plots expose which acoustic features survive the unsupervised Pearson correlation cleaving pass ($|r| \ge 0.85$) and how the surviving dimensions separate your tracks in the orthogonal projection.

**Correlation Heatmaps** — Pearson $r$ across all 8 raw features (left: full space, right: after redundant-feature pruning):

<p align="center">
  <img src="assets/covariance_heatmap_full.png" width="48%" alt="Covariance Heatmap — Full 8-Feature Space" style="border-radius: 12px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5); border: 1px solid rgba(255, 255, 255, 0.1);">
  &nbsp;
  <img src="assets/covariance_heatmap_pruned.png" width="48%" alt="Covariance Heatmap — Pruned Feature Space" style="border-radius: 12px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5); border: 1px solid rgba(255, 255, 255, 0.1);">
</p>

**PCA Biplots** — Tracks projected onto PC1/PC2; arrows are feature loading vectors coloured by energy (left: full, right: pruned after cleaving):

<p align="center">
  <img src="assets/pca_scatter_full.png" width="48%" alt="PCA Biplot — Full 8-Feature Space" style="border-radius: 12px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5); border: 1px solid rgba(255, 255, 255, 0.1);">
  &nbsp;
  <img src="assets/pca_scatter_pruned.png" width="48%" alt="PCA Biplot — Pruned Feature Space" style="border-radius: 12px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5); border: 1px solid rgba(255, 255, 255, 0.1);">
</p>

See [PCA Engine & Unsupervised Feature Cleaving](./docs/Auto_Playlist_Engine.md#8-pca-engine--unsupervised-feature-redundancy-cleaving-v110) for the double-pass SVD implementation details.

---

## Dual-Platform Technicalities

Mai-An Lab implements specialized subsystems on each operating system to handle execution without relying on native C-extension blocks.

### macOS Native Audio Engine
Desktop playback uses Apple `AVAudioPlayer` (`AVFoundation` via dynamic `pyobjc-framework-AVFoundation` loading) running in the Python process.
- **Thread Safety**: Uses a `threading.RLock` queue synchronizer to coordinate playlist updates and track transitions.
- **Background Polling**: A 10 Hz daemon polling thread checks track completion to trigger the next track in the queue.
- **Zero Subprocess Overhead**: Eliminates command-line subprocess decoders, reducing CPU overhead and zombie processes.
- **State Dispatching**: Coordinates sub-millisecond track position updates with the Flet UI thread via a 5 Hz throttled dispatch loop.

### Android Native Audio & Assistant Services
Mobile playback utilizes Flutter's modular background architecture to interface with system-level resources. Importantly, a trial and error born backend, using Dart and Kotlin, was built as part of a custom Flet extension. See:
- **Background Media Services**: Interfaces with `just_audio` and Android ExoPlayer via a custom `flet_audio_service` wrapper to manage background playback.
- **Power Management**: Integrates wake locks to prevent the CPU from sleeping during downloads.
- **Speech Subsystem**: Uses native Android `SpeechRecognizer` (Speech-to-Text) and `TextToSpeech` engines with audio focus ducking to parse commands offline.

---

## Headline Features

> [!IMPORTANT]
> **Portability & Build Parity**; this project is a modified port of `streamrip 2.1.0`. All native C-extension compilation dependencies are removed, allowing the same Python codebase to target Android (ARM64) and macOS (Intel & Apple Silicon) with zero-configuration build compatibility. Qobuz is supported.

- **Acoustic Auto-Playlists**; extracts a 52-D timbre representation combined with 8-D structural attributes to form a 60-D vector space. Features library-relative preset moods and custom Acoustic Islets integrated with a $k$-NN similarity graph solver for tempo-aligned music queues.
- **Unsupervised PCA Engine** *(v1.1.0)*; double-pass SVD with automatic Pearson correlation cleaving prunes acoustically redundant scalar features before the 3-D projection is committed. The Mood EQ hides zero-weight features and the projection geometry adapts to each library. After every rebuild, four diagnostic PNG figures (full and pruned correlation heatmap + biplot scatter) are written to `<library>/pca_report/`.
- **Jarvis Voice Control**; parses spoken intent with boundary-anchored patterns and voice hesitation stripping to trigger similarity walks and downloads.
- **Glassmorphic UI**; a responsive interface leveraging Flet containers, backdrop filters, custom color palettes, and transitions.
- **SQLite Indexing**; uses an `aiosqlite` connection with WAL journaling and a 64 MB page cache to index tracks without blocking the UI thread.
- **App Configuration**; edit Streamrip TOML settings, manage startup views, and configure theme colors directly in the application.

---

## Documentation (Wiki)

For information on the internals of Mai-An Lab, refer to the following documentation:

- [System Architecture & Database Design](./docs/Architecture.md)
- [Jarvis Voice Assistant & Background Pipelines](./docs/Development_Reference.md#5-jarvis-voice-assistant--background-pipelines)
- [Auto-Playlist Engine & DSP Deep-Dive](./docs/Auto_Playlist_Engine.md); includes the host-side **DSP Offload** workflow (`tools/dsp_offload.py`), which moves library-wide feature extraction off the mobile device and onto your computer over ADB.
- [PCA Engine & Unsupervised Feature Cleaving](./docs/Auto_Playlist_Engine.md#8-pca-engine--unsupervised-feature-redundancy-cleaving-v110) *(v1.1.0)*; double-pass SVD, correlation pruning, on-device visualization report.
- [UI Instructions: Library & Search](./docs/UI_Instructions.md)
- [Build & Deployment Guide](#running--deployment)

---

## Verified Environment

This application is verified on the following hardware/software stack:
- **Devices**: Google Pixel 8 and MacBook Air (2020) M1.
- **OS**: Android 16 (Fuck if I know) and MacOS Sequoia (15.7.3 (24G419))
- **Build Number**: CP1A.260405.005

**Performance Baseline**: ~20% baseline CPU usage. View pagination limits overhead during scrolling; search and playback execute without blocking the UI.

---

## Built With

| Component | Technology |
|---|---|
| **Logic** | [Serious Python](https://github.com/flet-dev/serious-python) (CPython 3.11) |
| **UI Framework** | [Flet](https://flet.dev/) (Flutter backend) |
| **Android Audio Engine** | [Just Audio](https://pub.dev/packages/just_audio) + ExoPlayer |
| **macOS Audio Engine** | Native `AVAudioPlayer` (`AVFoundation` via [pyobjc-framework-AVFoundation](https://pypi.org/project/pyobjc-framework-AVFoundation/)) |
| **Persistence** | [AioSQLite](https://github.com/omnilib/aiosqlite) (SQLite + FTS5) |
| **Analytics** | [NumPy](https://numpy.org/) (Pure-Python DSP) |

---

## Running & Deployment

### Running Natively on macOS

You can run the music player and indexer natively on macOS.

1. **Install Prerequisites (Optional)**:
   * **FFmpeg**: Only required if you plan to run the host-side **DSP Offload** script (`tools/dsp_offload.py`) or fallback decoding. Install via Homebrew: `brew install ffmpeg`.
   
2. **Install Python dependencies**:
   ```bash
   cd StreamripApp
   pip install -r requirements.txt
   ```
   *(This installs `pyobjc-framework-AVFoundation` on macOS to support native CoreAudio playback).*

3. **Launch the player**:
   ```bash
   flet run main.py
   ```

---

### Building for Android

### Prerequisites

To build the application for Android, ensure the following toolchain is installed:

| Tool | Requirement |
|---|---|
| **Python** | 3.11+ (must match Serious Python's embedded interpreter) |
| **Flutter SDK** | Latest stable; `flutter` must be in your `PATH` |
| **Android SDK & NDK** | Latest Command Line Tools; `ANDROID_HOME` must be set |
| **Flet** | Install via `pip install flet` |
| **ADB** | Included with Android SDK Platform Tools |

### Android Build

Built with `serious-python` to embed CPython into the Flutter/Flet runtime.

> [!NOTE]
> This project uses a custom local extension (`flet_audio_service`). To ensure the path is resolved correctly, use the provided build scripts:

**Windows (PowerShell):**
```powershell
cd StreamripApp
.\build_android.ps1
```

**macOS / Linux (Shell):**
```bash
cd StreamripApp
chmod +x build_android.sh
./build_android.sh
```

> [!IMPORTANT]
> **Rebuilds**; these scripts wipe the `build/` directory and `.gradle/` cache, kill hung Java processes, and uninstall previous versions from your device to ensure a clean installation. Heavy but will never fail -- ensure you have symlinks enabled on Windows...ask Claude because Windows is DOGSHIT.

---

## Permissions

| Permission | Purpose | Android Version |
|---|---|---|
| `INTERNET` | Streaming media and metadata | All |
| `READ_MEDIA_AUDIO` | Required for indexing and accessing local music files | 13+ |
| `MANAGE_EXTERNAL_STORAGE` | Required for recursive indexing of music folders (All Files Access) | 11+ |
| `FOREGROUND_SERVICE` | Required for persistent background playback | All |
| `FOREGROUND_SERVICE_MEDIA_PLAYBACK` | Specific service type required for background media playback | 14+ |
| `POST_NOTIFICATIONS` | Required to show playback controls in the notification shade | 13+ |
| `WAKE_LOCK` | Prevents the CPU from sleeping during downloads | All |
| `READ_EXTERNAL_STORAGE` | Legacy filesystem access (superseded by `READ_MEDIA_AUDIO`) | < 13 |

---

## Developer Note

> [!NOTE]
> I am a bioinformatician by training, not a professional software engineer. This is a non-professionally driven passion project; please take this into consideration when encountering issues. Any advice or willingness to contribute is appreciated.

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

> [!IMPORTANT]
> **Thanks to the open-source communities of Streamrip and Flet** for providing the foundational tools and libraries that make this project possible.

## Extra Credits

NO THANKS to MICROSLOP, for creating the worst piece of software to ever exist. Windows is DISGUSTING; a DISGRACE that deserves no place in the pages of history. Even the worst country on earth decided to mandate Linux for government offices; why can't we get the UN to ban it completely? Why does everything have to run on that horrible excuse of an (non)operating "system"? Using Windows means you're getting cucked by a silicone slab that zaps you. Pay thousands to get a proper piece of hardware and the worst thing to ever happen to humanity CUCKS you out of 350000% CPU/RAM so that copilot can laugh at you while it steals your credit card information. MICROSLOP should be classified as a terrorist organisation. Thank you for reading!