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
4. **PCM File**; samples are written to `appContext.cacheDir/dsp_pcm/<hash>.pcm` (the app's private internal storage, always accessible to Python in the same process). The bridge returns the absolute path, not the bytes themselves; this avoids serialising ~4 MB of audio across the Dart↔Python event channel for every track.
5. **Feature Extraction**; Python (via `numpy`) memory-maps the PCM file and extracts the 38D feature vector (BPM, Energy, MFCCs, Chroma).

### Performance Considerations
- **Hardware-Accelerated**; by using `MediaCodec`, decoding is significantly faster and more battery-efficient than software-based decoders like FFmpeg.
- **Batched Processing**; to avoid UI stutter, DSP analysis is performed on a background thread in Kotlin and a separate `asyncio` task in Python.

### Cross-Thread Method Channel Reply (gotcha)
Kotlin runs `decodePcm` on a `Executors.newSingleThreadExecutor()` background thread so the foreground codec is not blocked. Flutter's `MethodChannel.Result` callbacks **must** be delivered on the main looper — calling `result.success(...)` from the worker thread silently drops the reply on some Android builds. The symptom is misleading: the Kotlin log shows a clean decode, the PCM file is written, but the Dart side receives `null`. The Dart wrapper at `_runDecode` then emits a `decode_complete` event with no `ok` key, which the Python correlator surfaces as `RuntimeError("decode failed")` for every track.

The fix is to marshal the reply back via a `Handler(Looper.getMainLooper())`:

```kotlin
private val mainHandler = Handler(Looper.getMainLooper())
…
executor.submit {
    try {
        val out = decodePcm(path)
        mainHandler.post { result.success(out) }
    } catch (t: Throwable) {
        mainHandler.post { result.error("DECODE_FAILED", t.message ?: "unknown", null) }
    }
}
```

### `libc++_shared.so` bundling (gotcha)
NumPy's prebuilt Android wheels (specifically `numpy/fft/_pocketfft_umath.cpython-3xx.so`) are linked against the NDK's C++ shared runtime, `libc++_shared.so`. The Flet/`serious_python` build pipeline does **not** ship this library automatically. With it missing the analyser will reach `extract_features_from_pcm`, then die on the first `np.fft.rfft` call:

```
ImportError: dlopen failed: library "libc++_shared.so" not found:
  needed by …/numpy/fft/_pocketfft_umath.cpython-312.so
```

Because this fires inside an `asyncio.to_thread` worker, the only UI-visible signal is the `(N failed)` counter on the assistant rescan banner / Magic Playlist progress label — there is no other clue.

**Resolution.** Bundle `libc++_shared.so` for every ABI the app ships into the `flet_audio_service` plugin's `jniLibs` tree:

```
flet_audio_service/src/flutter/flet_audio_service/android/src/main/jniLibs/
├── arm64-v8a/libc++_shared.so
├── armeabi-v7a/libc++_shared.so
├── x86/libc++_shared.so
└── x86_64/libc++_shared.so
```

Source files come from the NDK at
`$NDK/toolchains/llvm/prebuilt/<host>/sysroot/usr/lib/<triple>/libc++_shared.so`
where `<triple>` maps to the Android ABI:

| ABI            | NDK triple                |
|----------------|---------------------------|
| `arm64-v8a`    | `aarch64-linux-android`   |
| `armeabi-v7a`  | `arm-linux-androideabi`   |
| `x86`          | `i686-linux-android`      |
| `x86_64`       | `x86_64-linux-android`    |

The Android Gradle Plugin auto-merges `jniLibs/<abi>/*.so` into every APK that depends on the plugin, so no further build configuration is needed. If a second plugin also ships its own copy, add `packagingOptions { pickFirst 'lib/**/libc++_shared.so' }` to the plugin's `build.gradle`.

This same bundling step is required for any future native dependency that links against the C++ runtime (e.g. SciPy, PyTorch Mobile, ONNX Runtime). Keep the libs pinned to the NDK version used to compile the wheels — mixing NDK 25 wheels with NDK 28 `libc++_shared.so` is normally safe (it is forward-compatible), but a major NDK jump is worth verifying.

### Diagnosing future decode failures
Python's `logging` output from the `dsp` module is not routed to logcat in release builds. The proven-visible channels are:

- Kotlin `Log.d(TAG, …)` under tag `FletAudioServiceDsp` — visible via `adb logcat`.
- Python `logger.warning("FAS: …")` on the `flet_audio_service` logger — visible under the `serious_python` tag.
- A direct file write to `/sdcard/Download/<name>.log` if the app has `MANAGE_EXTERNAL_STORAGE` (which it does); pull with `adb pull /sdcard/Download/<name>.log`. This bypasses the logging layer entirely and is the most reliable diagnostic channel on a non-debuggable release APK.

### 2.3 Artwork Caching & Temporary Storage Pipeline

To guarantee smooth, lag-free list scrolling on low-spec Android devices and avoid storage bloat, the app deploys an integrated artwork caching and isolation pipeline.

#### 1. Secure Sandboxed Temp Directory & `.nomedia` Isolation
All transient JPEGs and metadata preview covers are mapped to a dedicated sandboxed subdirectory (`StreamripApp/temp/`) under the app's secure internal storage.
* **Initialization**: The helper `get_temp_artwork_dir()` resolves this path, dynamically creating it if missing.
* **Gallery Isolation**: Immediately places a blank `.nomedia` file inside this directory. This signals Android's scanner to ignore the directory entirely, ensuring transient album covers do not pollute the user's Google Photos or native Gallery apps.

#### 2. Thread-Safe LRU Cache Eviction & Physical Pruning
An in-memory cache `ArtworkCache` (`_ARTWORK_CACHE`) keeps up to **50** hot artwork file paths active.
* **Disk Eviction Loop**: When a 51st track's artwork is cached, the LRU algorithm pops the oldest item. Unlike standard lazy caches, the popped item's file path is immediately processed using `os.remove` inside `ArtworkCache.put`.
* **Thread Safety**: All mutations (`get`, `put`, `clear`) are bound by a threading `Lock` context, preventing race conditions during asynchronous scroll-triggered fetches.

#### 3. Graceful Shutdown Sweep
To prevent progressive filesystem bloating from accumulated cache metadata, the application registers a sweep task in the `on_disconnect` hook. On exit, the routine iterates over the sandboxed `temp` folder and physically erases all temporary files while carefully keeping the `.nomedia` file intact for the next launch.

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


---


---

## 5. Jarvis Voice Assistant & Background Pipelines

Mai-An Lab incorporates a state-of-the-art voice interaction framework powered by a local, deterministic NLP parsing engine, native hardware-accelerated Android services, and a background filesystem processing infrastructure:

### 5.1 Jarvis Voice Assistant Architecture

Jarvis is a zero-latency vocal command center that operates entirely on-device, offering instant tactile control through a multi-tiered pipeline:

1. **Native Hardware-Accelerated Voice Bridges**:
   - **Android Native STT (`SpeechRecognizer`)**: Speech-to-Text leverages Android's highly optimized native **on-device Speech Services** via Kotlin method channels. By utilizing local neural models and hardware-level audio codecs, voice translation is completed with near-zero latency, minimal battery footprint, and 100% user privacy (no audio data is transmitted over the internet).
   - **Android Native TTS (`TextToSpeech`)**: Text-to-Speech utilizes Android’s hardware-optimized **native system synthesizer**. It leverages pre-installed high-fidelity language packages and native audio session focus managers to seamlessly duck playback volume during vocal replies without external library overhead.

2. **Conversational NLP & Normalisation (`assistant_intent.py`)**:
   - **Grammatical Anchoring**: Processes free-form user speech using compiled, boundary-anchored regular expressions. This ensures perfect deterministic reliability without any internet dependencies or heavy machine learning weights.
   - **Recursive Fixed-Point Peeling**: Employs a recursive peeling loop (`_normalise` and `_clean_query`) to dynamically strip out voice hesitation layers, politeness noise, and auxiliary verbs (e.g. *"Yo, um, Jarvis, could you please... please"*), isolating the core query perfectly.
   - **Homophone Protections**: Integrates custom vocabulary mappings to capture homophone slips (e.g. treating *"cue"* and *"queue"* identically).

3. **Core Capabilities & Dispatch Table (`assistant_runner.py`)**:
   - **Playback Control**: Instantly skip, reverse, pause, resume, mute, unmute, or shuffle the playback queue.
   - **Context-Aware Queue Mutations**: Commands like *"play next"* or *"add to queue"* resolve metadata titles against the local SQLite database and splice the selected tracks dynamically into the active audio pipeline.
   - **Acoustic Mood Selection**: Matches moods (e.g., *"play something dark"* or *"play happy tracks"*) directly to the 43-dimensional DSP similarity graph attractors.
   - **Similarity Walkers**: Vocal commands like *"play more like this"* trigger real-time metadata and acoustic similarity edge navigations to queue related music automatically.
   - **Stateful Pending Confirmations**: Supports full recursive confirmation trees (e.g., prompting the user before initiating heavy library re-indexing or graph rescans, running callback state machines on *"yes"* or *"no"* replies).

4. **Vocal Pipeline Integration (`main.py`)**:
   - **TTS Priority Ducking**: The runner coordinates with Flet's system audio services to temporarily dim or pause active music during vocal replies, resuming background playback automatically after Jarvis finishes speaking.

### 5.2 Comprehensive Skillset & Commands

Jarvis supports an exhaustive range of hands-free vocal commands and functions:

| Intent Command | Action Performed | Example Phrases |
|---|---|---|
| **Play Song / Artist** | Immediately plays a matched track, artist catalog, or album from your library. | *"play Stairway to Heaven"*, *"play Radiohead"*, *"start playing Homework"* |
| **Play Next** | Inserts the matched track, artist, or album directly after the currently playing song in the queue. | *"play Stairway next"*, *"put Radiohead next"* |
| **Add to Queue** | Appends the matched track, artist, or album to the end of the global playback queue. | *"add Stairway to the queue"*, *"put Homework in the queue"*, *"enqueue Daft Punk"* |
| **Acoustic Moods** | Triggers bioinformatics-driven Markov Clustering on the 43D DSP graph matching the chosen mood profile. | *"play something chill"*, *"I want intense music"*, *"play some upbeat tunes"* |
| **Acoustic Similarity Walk** | Traverses acoustic and metadata similarity edges from the current track to sequence a smooth related arc. | *"play something similar to this"*, *"more like this song"*, *"play tracks similar to Daft Punk"* |
| **Artist Similarity Walk** | Traverses relationship links to play more tracks from the currently playing artist. | *"play more by this artist"*, *"more songs from them"* |
| **Remote Download** | Invokes the background Streamrip thread to search Qobuz, download, and auto-index the target track or album. | *"download Stairway to Heaven"*, *"get Daft Punk Homework"*, *"fetch and save track X"* |
| **Surprise Me (Random)** | Selects a random track from the local library, shuffles the playback pool, and begins playback. | *"surprise me"*, *"play something random"*, *"shuffle play"* |
| **Playback Control** | Standard controls to manipulate active audio player states. | *"pause"*, *"resume"*, *"stop"*, *"skip"*, *"previous track"* |
| **Queue Operations** | Manipulates the ConcatenatingAudioSource without interrupting active music. | *"clear the queue"*, *"shuffle the queue"* |
| **Volume Control** | Adjusts or silences system audio player levels. | *"mute"*, *"unmute"*, *"be quiet"*, *"restore volume"* |
| **Status Inquiry** | Prompts Jarvis to query player metadata and vocalize active media info. | *"what's playing?"*, *"what is this song?"*, *"now playing"* |
| **DSP Library Sweep** | Requests a full filesystem and feature extraction sweep for missing acoustic metrics or link matrices. | *"rescan the library"*, *"reanalyse my music"*, *"reindex features"* |
| **Help Commands** | Queries Jarvis's on-device documentation to list voice skills. | *"help"*, *"what can you do?"*, *"commands"* |

### 5.3 Parallel Multi-Core Folder Scanner
- **Concurrent Disk Crawl**: The library scanner walks large directories asynchronously. It dynamically scales to use multiple processing cores via `concurrent.futures`, preventing large directories from blocking the Flet main thread.
- **Non-Blocking Walk**: Leverages native asynchronous generators, letting users browse existing library tabs or queue songs dynamically while an active walk scans thousands of media files in the background.
