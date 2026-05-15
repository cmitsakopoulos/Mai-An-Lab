# Development Reference: Bridge & Pipelines

This document provides technical details for the specialized Android-native bridges and background processing pipelines in Mai-An Lab.

## 1. The Python ↔ Android Bridge

Mai-An Lab uses a multi-layered bridge to interface a CPython backend with Android system services.

### Layered Architecture
1. **Python Layer (`audio_engine.py`)**; maintains the playback queue and dispatches commands to the `FletAudioService` instance.
2. **Bridge Layer (`flet_audio_service.py`)**; a custom Flet plugin that serializes Python commands into JSON and sends them via Flutter MethodChannels.
3. **Dart Layer (`flet_audio_service.dart`)**; receives MethodChannel calls and interfaces with the `just_audio` and `audio_service` plugins.
4. **Kotlin Layer (`FletAudioServicePlugin.kt`)**; handles Android-specific hardware tasks like `MediaCodec` PCM decoding and Foreground Service lifecycle management.

### Background Audio & Foreground Service
To prevent the Android OS from killing the Python process during background playback, the Kotlin layer initializes an **Android Foreground Service** with a `MediaSession`. This ensures:
- The app maintains a persistent notification.
- Playback controls work from the lock screen and Bluetooth devices.
- The Python interpreter remains active in the background.

---

## 2. DSP & PCM Extraction Pipeline

The "Playlist Creation" engine requires precise acoustic features, which are extracted directly on the Android device using hardware-accelerated codecs.

### Native Decoding Flow
1. **Request**; Python sends a `decode_pcm` request with a file path to the Kotlin bridge.
2. **MediaExtractor**; the Kotlin layer uses `MediaExtractor` to identify the track's format and locate the audio stream.
3. **MediaCodec**; the hardware decoder (`MediaCodec`) extracts raw PCM samples at a target sample rate of **22,050 Hz**.
4. **PCM Buffer**; the raw bytes are sent back to Python via the bridge as a list of integers.
5. **Feature Extraction**; Python (via `numpy`) processes the PCM buffer to extract the 43D feature vector (BPM, Energy, MFCCs, Chroma).

### Performance Considerations
- **Hardware-Accelerated**; by using `MediaCodec`, decoding is significantly faster and more battery-efficient than software-based decoders like FFmpeg.
- **Batched Processing**; to avoid UI stutter, DSP analysis is performed on a background thread in Kotlin and a separate `asyncio` task in Python.

---

## 3. Streaming & Search Implementation

Mai-An Lab integrates a modified version of **Streamrip 2.1.0** to handle remote metadata and downloads.

### Qobuz Integration
- **Direct API Access**; the app communicates directly with Qobuz's REST API using `aiohttp`.
- **Token Management**; user IDs and tokens are stored in the local `recent_searches.json` and `config.toml`, allowing for persistent sessions without storing plaintext passwords.
- **Concurrent Fetching**; search results for Tracks, Albums, and Artists are fetched in parallel using `asyncio.gather` to minimize network latency.

### Download Architecture
- **Worker Thread**; downloads are executed on a dedicated background thread to prevent blocking the Flet event loop.
- **Atomic Renames**; tracks are downloaded to a `.tmp` file and only renamed to their final `.flac` or `.mp3` extension after a successful checksum verification and metadata tag injection.
- **Automatic Indexing**; once a download completes, the `LibraryScanner` is triggered to immediately add the new track to the local SQLite database.

---

## 4. Gapless Playback & Queue Mutations

Mai-An Lab leverages the advanced capabilities of the `just_audio` Flutter plugin to provide a seamless playback experience.

### ConcatenatingAudioSource
The playback queue is managed via a `ConcatenatingAudioSource`. This allows the Android `ExoPlayer` instance to "see" the next track before the current one finishes.
- **Gapless Support**; the next track is pre-buffered in the background, ensuring zero latency between songs.
- **In-Place Mutations**; the bridge supports `add_queue_item`, `remove_queue_item`, and `move_queue_item` commands that modify the live sequence without tearing down the player. Playback continues uninterrupted while the user reorders or appends to the queue.

### Lazy Preparation
To conserve memory and bandwidth on Android, the queue uses `useLazyPreparation: true`.
- **Behavior**; only the current and immediate next tracks are fully prepared. Subsequent tracks in a large playlist remain in an idle state until they approach the playback head.
