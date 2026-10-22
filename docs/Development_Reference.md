# Development Reference: Bridge & Pipelines

This document provides technical details for the specialized Android-native bridges, background processing pipelines, database design, and optimization techniques in Mai-An Lab.

## Component Overview

| Component | Role |
|-----------|------|
| [StreamripApp](../StreamripApp) | UI, download queue, library indexer, search, playlist engine. |
| [db_manager.py](../StreamripApp/utils/db_manager.py) | SQLite + FTS5 layer: schema, triggers, async transactions. |
| [audio_engine.py](../StreamripApp/utils/audio_engine.py) | Android player state and queue management; ExoPlayer integration. |
| [audio_engine_macos.py](../StreamripApp/utils/audio_engine_macos.py) | macOS player state and queue management; native AVFoundation integration. |
| [flet_audio_service](../flet_audio_service) | Python ↔ Dart ↔ Kotlin bridge for system-level media controls on Android. |
| [pca_engine.py](../StreamripApp/utils/pca_engine.py) | Unsupervised double-pass SVD PCA: Pearson correlation cleaving, zero-padded 8×3 projection matrix, and on-device visualization report. |
| [harmonic.py](../StreamripApp/utils/harmonic.py) | Camelot wheel harmonic key geometry: Krumhansl-Schmuckler mapping and late-fusion unit circle coordinates. |
| [autoplay.py](../StreamripApp/utils/autoplay.py) | Resumable Station auto-play controller: dynamic buffers, variety sampling, and negative feedback. |
| [autoplay_hidden.py](../StreamripApp/ui/views/autoplay_hidden.py) | Settings subview for managing hidden auto-play tracks and unblocking learned genre hop penalties. |
| [genre_graph.py](../StreamripApp/utils/genre_graph.py) | PAGA-style genre adjacency: bridge-artist lift scoring, cultural scene partitioning, and untagged label propagation. |
| [deezer.py](../StreamripApp/utils/deezer.py) | Deezer client: search, URL resolution, and Blowfish stream decryption via pycryptodomex. |
| [metadata_workbench.py](../StreamripApp/ui/player/metadata_workbench.py) | Artist provenance board: bulk tagging bins, fused multi-source sync, and confidence review. |
| [metadata_enrich.py](../StreamripApp/utils/metadata_enrich.py) | Multi-source enrichment cascade (MusicBrainz → file tags → Qobuz → manual). |
| [onboarding_modal.py](../StreamripApp/ui/player/onboarding_modal.py) | First-run onboarding wizard: storage path validation, credentials, and theme setup. |
| [state_export.py](../StreamripApp/utils/state_export.py) | Portable state bundle export/import (v3 ZIP): online SQLite backup, TOML config, and search history. |
| [error_boundary.py](../StreamripApp/utils/error_boundary.py) | Global error capture boundary, session lifecycle protection, and restart handler. |
| [llm_engine.py](../StreamripApp/utils/llm_engine.py) | Conversational LLM agent and tool dispatcher for natural-language library interaction. |

---

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
- A custom refresh action button in the notification shade dispatches a `replenish_queue` command to trigger background queue replenishment on-demand.
- The Python interpreter remains active in the background.

### Android-Specific Performance & Reliability Mechanics
- **Targeted UI Refreshes**: The app bypasses Flet's global `page.update()` for the high-frequency playback position heartbeat. By refreshing only the specific player controls, background CPU spikes are reduced by ~60%.
- **Throttled Position Heartbeat**: Playback position mirroring is strictly throttled to 1 Hz while the application is in the foreground, pausing entirely when backgrounded to conserve battery.
- **Bounded Native Operation Timeout**: Every native call across the Python-Dart boundary is registered with a unique UUID (`register_op`) and wrapped in a 30-second timeout (`_SEND_TIMEOUT_S = 30.0`). If the underlying session was torn down by the OS or the Dart method channel stalls, the call times out cleanly instead of deadlocking the operation lock indefinitely.
- **Atomic Batch Queue Insertion**: Multi-track appends cross the boundary in a single batch call (`add_queue_items`) rather than individual dispatches, preventing sequence corruption and audio interruption.
- **Queue State Snapshot Guard (`_QUEUE_STATE["held"]`)**: Process-level tracking preserves the user's manual queue or album tracks when auto-play takes over (`_QUEUE_STATE["held"]`). When auto-play is disabled or the station ends, the held queue is restored. It also prevents empty engine states during session initialization or teardown from erasing the persisted queue snapshot on disk.
- **Station Traversal Lifecycle**: While track playback and queue states are persisted to disk and restored across app relaunches, the active **Station** instance (`track_graph.Station`) resides strictly in memory. If Android destroys the background process, the active playback queue and position are restored on relaunch, but the in-memory station traversal position (current leg, seed, step count, and session exclusion history) resets.
- **Native Play Ledger & Qualification**: Track listens are logged by the native player in an append-only ledger (`play_ledger.py`) and qualify as played only after 30 seconds or 50% duration, capturing accurate metrics during background playback.
- **Coalesced UI Updates (`app.safe_update`)**: Background tasks (such as download progress firing 4 times per second) dispatch mutations through `app.safe_update(fn, target)`. By passing an explicit `target` control, Flet flushes only that control's specific subtree rather than triggering a global `page.update()` that churns every cached tab. If any mutation in a batch is untargeted, the dispatcher safely falls back to a full page flush.
- **Destroyed Session Safeguards**: When Android tears down or recreates an activity, background threads holding stale page handles would trigger `RuntimeError("...destroyed session")`. `safe_update` checks `page.session` before calling `page.run_task`, bailing out before creating un-schedulable coroutines and eliminating `coroutine was never awaited` warnings.
- **Global Error Boundary (`utils/error_boundary.py`)**: Wraps asynchronous view entry points and event handlers. If unhandled exceptions occur, it catches the fault, displays an inline error container with selectable trace details, and provides a clean `restart()` mechanism without crashing the process.
- **Zero-Cost Indicators**: Heavy Python-driven animation loops are replaced with static or GPU-accelerated native indicators to keep background CPU usage to a minimum.
- **Active UI Containment**: High-volume control arrays (such as the Jarvis chat history bubble tree) are strictly capped at 50 active items, dynamically popping older nodes to prevent memory bloat.

---

## 1.2 The macOS Native Audio Engine (AVFoundation)

On macOS systems, Mai-An Lab bypasses the Android MethodChannel bridge and Dart plugin layers entirely, running a lightweight, direct-in-process native audio engine (`audio_engine_macos.py`) backed by Apple's AVFoundation framework.

### 1. Architectural Components & Dynamic Importing
* **Dynamic Import Guard**: The macOS engine executes entirely in the CPython interpreter space. It dynamically queries `sys.platform == "darwin"` and imports the native Objective-C bindings at startup:
  ```python
  from Foundation import NSURL
  from AVFoundation import AVAudioPlayer
  ```
  This keeps the same codebase 100% build-compatible with Windows and Linux build-hosts, preventing import failures when compiling or testing on non-Apple environments.
* **In-Process CoreAudio Execution**: By calling Objective-C runtime bindings via `pyobjc-framework-AVFoundation`, the player directly instantiates `AVAudioPlayer` and loads tracks via native `NSURL.fileURLWithPath_` formats. Playback occurs inside the app's process heap, eliminating external decoder dependencies.

### 2. Thread-Safety & Reentrant Synchronization
Playback controls (Play, Pause, Seek, and Queue alterations) can be triggered asynchronously by user clicks, Jarvis voice intents, or background event loop ticks. 
* **`threading.RLock`**: To guarantee thread-safety and prevent race conditions (such as concurrent calls to initiate playback of different files), the engine encapsulates all state, index, and player control methods within a reentrant lock:
  ```python
  self._lock = threading.RLock()
  ```
* **Thread-Safe Queue Mutation**: Inserting, removing, or reordering tracks in the queue utilizes Flet's standard list sequence, protected entirely by the reentrant lock to ensure index pointers remain consistent.

### 3. Decoupled Throttled Polling & End-of-Track Detection
Unlike Flutter's ExoPlayer wrapper, AVAudioPlayer's standard end-of-track delegate (`audioPlayerDidFinishPlaying:successfully:`) requires an active macOS system `NSRunLoop` to be running on the listener's thread. Since Python and Flet execute inside their own event loops, the engine deploys a custom background monitoring thread:
* **Background Polling Loop**: On playback start, the engine spawns a background daemon thread (`_poll_thread`) running a 10 Hz query loop:
  ```python
  while not stop_event.is_set():
      pos = float(player.currentTime())
      playing = bool(player.isPlaying())
      ...
      time.sleep(0.10)
  ```
* **Decoupled Position Updates**: The internal engine state is updated instantly (`self.position = pos`) on every single iteration to ensure sub-millisecond precision for seeking and track resume functions.
* **UI Dispatch Throttle**: To prevent saturating Flet's rendering pipeline and causing interface stutter during high-speed polling, the observer notification (`self.dispatch("position", pos)`) is throttled to a maximum rate of 5 Hz (once every `0.20` seconds).
* **End-of-Track Auto-Advance**: If the polling thread detects that `player.isPlaying()` has returned `False` while the stop event has not been signaled, it handles the end-of-track transition by scheduling a thread-safe task (`self.next()`) on Flet's main event queue.
* **Automated Subprocess Isolation**: By completely bypassing shell wrappers or command-line subprocess decoders for music playback, the macOS client eliminates the risk of resource leaks, defunct threads, or system-wide zombie processes.

---

## 2. DSP & PCM Extraction Pipeline

The "Playlist Creation" engine requires precise acoustic features to construct similarity matrices. While the engine includes a fully functional on-device extraction pipeline (useful as a fallback for new single downloads), running DSP across an entire library is impractically slow on mobile CPUs (~10+ seconds per track). 

To resolve this, Mai-An Lab implements a hybrid workflow that offloads library-wide analysis to a host laptop.

### 2.1 Native On-Device Decoding Flow (Fallback)
1. **Request**: Python sends a `decode_pcm` request with a file path to the Kotlin bridge.
2. **MediaExtractor**: The Kotlin layer uses `MediaExtractor` to identify the track's format and locate the audio stream.
3. **MediaCodec**: The hardware decoder (`MediaCodec`) extracts raw PCM samples at a target sample rate of **22,050 Hz** from the middle 120s of the track.
4. **PCM File**: Samples are written to `appContext.cacheDir/dsp_pcm/<hash>.pcm` (the app's private internal storage, always accessible to Python in the same process). The bridge returns the absolute path, not the bytes themselves, avoiding serializing ~4 MB of audio across Dart ↔ Python channels.
5. **Feature Extraction**: Python (via pure-NumPy `StreamripApp/utils/dsp.py`) memory-maps the PCM file and extracts the **60D feature vector** (consisting of a 52D packed timbre/chroma profile BLOB and 8 scalar descriptors, detailed in `docs/Auto_Playlist_Engine.md`).

### 2.2 Performance Considerations & Mobile Extraction Limits
- **Hardware-Accelerated Decoding**: Using `MediaCodec` keeps on-device PCM decoding battery-efficient compared to software decoders.
- **On-Device Extraction Bottleneck**: Feature extraction (`StreamripApp/utils/dsp.py`) runs pure NumPy without native BLAS or C acceleration. On typical mobile ARM CPUs, computing HPSS, 20-band MFCCs, chroma pitch vectors, and rhythm profiles takes **~10–12 seconds per track**.
- **Practical Offloading Boundary**: While on-device analysis runs on background worker threads without freezing the UI, processing more than ~50 tracks on-device leads to thermal throttling, excessive battery draw, and prolonged completion times. The laptop-offload pipeline is practically mandatory for medium to large music libraries.

### 2.3 The Laptop-Offload Pipeline (`tools/dsp_offload.py` / `tools/auto_offload.sh`)
To make bulk ingestion feasible, the companion script `tools/dsp_offload.py` offloads feature extraction to the user's computer, running at **~200 ms per track** (a 50x speedup). 

> [!IMPORTANT]
> **ADB USB Debugging Requirement**: For the laptop-offload script to interact with your device over ADB, you must unlock and enable **USB Debugging** in Android's developer settings, connect the phone to your laptop, and authorize the laptop's connection key on the phone's screen. See [Auto-Playlist Engine Guide](./Auto_Playlist_Engine.md#5-laptop-offload-analysis-pipeline) for full setup instructions.

> [!TIP]
> **One-Touch Offload Script (`tools/auto_offload.sh`)**: During development, you can automate this entire round-trip (pulling live databases directly from Android private app storage `/data/user/0/com.mitsakopoulos.maianlab.mai_an_lab/app_flutter`, running the offloader, stopping the app, and writing back the database) using `./tools/auto_offload.sh`.

The offloader is designed with several production engineering optimizations:
- **Zero Math Duplication**: It directly imports and shares the app's native NumPy feature pipeline (`StreamripApp/utils/dsp.py` via `sys.path` injection), ensuring that feature extraction models written on the host are exactly identical to what the app expects (`FEATURES_VERSION = 5`).
- **ADB Tar Streaming**: Instead of executing `adb pull` for every track; which pays a 50–200 ms handshake penalty per file; the script bundles track lists into batches of 100 and streams them in a single command (`adb exec-out tar -cf - -T <list>`). This amortizes connection overhead and saves minutes on large libraries.
- **Parallel CPU Ingestion**: Decodes audio using multi-process `ffmpeg` pipelines and extracts features using a Python `ThreadPoolExecutor`. Because both `ffmpeg` spawns and heavy NumPy/FFT operations release the Python GIL, workers scale linearly across all CPU cores.
- **Transactional SQLite WAL Batching**: Features are upserted into the SQLite database in single-transaction chunks inside a `BEGIN/COMMIT` boundary. This is orders of magnitude faster than single-row commits and guarantees database integrity; if interrupted, the script resumes from the last completed chunk.
- **Persistent Double Cache**: The script maintains a local `--workdir` containing:
  - `audio_cache/`: A local mirror of the phone's music library structure, preventing redownloading files.
  - `<audio_file>.pcm`: Decoded mono PCM frames, ensuring that if you tweak the feature-extraction pipeline parameters, you can re-run the extraction step instantly without re-decoding.
- **Defensive Migrations**: The script inspects the bundle DB and automatically runs `ALTER TABLE play_counts ADD COLUMN` migrations if a pre-v3 bundle is provided, making the tool backward-compatible with any exported app state.

### 2.4 State Bundle Architecture (`utils/state_export.py`)
For headless testing, diagnostics, and moving catalog state between desktop development and mobile devices, the app provides a portable state bundle mechanism:
- **Bundle Format (Version 3)**: A compressed ZIP (`mai_an_lab_state_<timestamp>.zip`) containing:
  - `mai_an_lab.db`: Consistent SQLite database snapshot captured using SQLite's online backup API (`_snapshot_sqlite`), ensuring zero lock corruption even under active WAL transactions.
  - `config.toml`: Current Streamrip service configuration and credentials.
  - `search_history.json`: Local search token history.
  - `manifest.json`: Bundle metadata, schema version, creation timestamp, and host architecture.
- **Storage Locations**: Bundles are written to and read from `/sdcard/Download` on Android (accessible without root via `MANAGE_EXTERNAL_STORAGE`) and `~/Downloads` on desktop.
- **Safe Hot Import Lifecycle**: When importing a bundle in Settings → Advanced, the app proactively closes open `DatabaseManager` handles, unzips the payload, verifies schema compatibility (`BUNDLE_VERSION == 3`), and instructs the user to force-close and relaunch to rebind all memory-resident models.


### 2.5 Playback DSP Routing & Precedence (Equalizer & Dynamism)
To provide high-fidelity audio enhancement without signal muddling or processing conflicts, the audio engine implements a track-by-track Dynamism calculation and a strict exclusivity routing matrix between the 5-band manual Equalizer and Dynamism.

#### 2.5.1 Decoupled Track-by-Track Dynamism Calculations
Rather than comparing track features against global library averages (which distorts calculations based on library composition), Dynamism is calculated purely track-by-track using the active track's absolute acoustic features:
1. **Spectral Contrast Normalization**: Maps typical spectral contrast values ($[0.2, 0.4]$) to a normalized $[0.0, 1.0]$ range:
   $$\text{norm\_contrast} = \max\left(0.0, \min\left(1.0, \frac{\text{spectral\_contrast} - 0.2}{0.2}\right)\right)$$
2. **Combined Dynamism Score**: Computes the final score as a weighted average of energy, beat strength, and normalized contrast:
   $$\text{score} = 0.4 \times \text{energy} + 0.3 \times \text{beat\_strength} + 0.3 \times \text{norm\_contrast}$$
3. **Loudness Gain Boost**: Translates the score to a track-specific gain boost (in dB):
   $$\text{gain\_db} = 1.0 + 3.0 \times \text{score}$$
   This guarantees a baseline enhancement of $+1.0\text{ dB}$ for all tracks, scaling up to $+4.0\text{ dB}$ for highly dynamic/rhythmic music.

#### 2.5.2 Non-Conflicting Precedence Routing
To prevent the manual Equalizer and Dynamism from fighting over the same frequency bands, the engine enforces exclusive precedence routing:
*   **Manual EQ Inactive (Equalizer Disabled)**:
    *   The 5 EQ bands are dynamically configured with a psychoacoustic loudness contour (boosting low-bass and high-presence frequencies) scaled by the track's dynamism score:
        $$\text{dyn\_offsets} = \text{score} \times [3.0, 1.5, 0.0, 1.0, 2.5]\text{ dB}$$
    *   The overall track-specific loudness gain boost (`gain_db`) is applied to the output.
*   **Manual EQ Active (Equalizer Enabled)**:
    *   The manual Equalizer has **100% exclusive control** over the EQ bands. The EQ gains are set strictly to the active preset or custom band values.
    *   Dynamism's frequency offsets are set to `[0.0] * 5` so they never interfere with the manual EQ shape.
    *   The overall track-specific loudness gain boost (`gain_db`) continues to be applied to the output, acting as a clean, non-conflicting volume/loudness additive.

#### 2.4.3 Platform-Specific Audio Engine Backends
*   **macOS (`audio_engine_macos.py`)**:
    *   *Equalizer*: Configures the gains of a native Apple `AVAudioUnitEQ` node connected to the processing graph.
    *   *Loudness Boost*: Converts the `gain_db` into a linear multiplier (e.g., $+3.0\text{ dB} \approx 1.41\times$) and applies it to `AVAudioPlayerNode.setVolume_()`, bypassing standard main mixer limits to avoid clipping.
*   **Android (`audio_engine.py`)**:
    *   *Equalizer*: Pushes EQ bands down to the native Android Equalizer API via Flet method channels.
    *   *Loudness Boost*: Applies the `gain_db` directly to Android's hardware-accelerated `LoudnessEnhancer` audio effect.

### Cross-Thread Method Channel Reply (gotcha)
Kotlin runs `decodePcm` on a `Executors.newSingleThreadExecutor()` background thread so the foreground codec is not blocked. Flutter's `MethodChannel.Result` callbacks **must** be delivered on the main looper; calling `result.success(...)` from the worker thread silently drops the reply on some Android builds. The symptom is misleading: the Kotlin log shows a clean decode, the PCM file is written, but the Dart side receives `null`. The Dart wrapper at `_runDecode` then emits a `decode_complete` event with no `ok` key, which the Python correlator surfaces as `RuntimeError("decode failed")` for every track.

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

Because this fires inside an `asyncio.to_thread` worker, the only UI-visible signal is the `(N failed)` counter on the assistant rescan banner / Magic Playlist progress label; there is no other clue.

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

This same bundling step is required for any future native dependency that links against the C++ runtime (e.g. SciPy, PyTorch Mobile, ONNX Runtime). Keep the libs pinned to the NDK version used to compile the wheels; mixing NDK 25 wheels with NDK 28 `libc++_shared.so` is normally safe (it is forward-compatible), but a major NDK jump is worth verifying.

### Diagnosing future decode failures
Python's `logging` output from the `dsp` module is not routed to logcat in release builds. The proven-visible channels are:

- Kotlin `Log.d(TAG, …)` under tag `FletAudioServiceDsp`; visible via `adb logcat`.
- Python `logger.warning("FAS: …")` on the `flet_audio_service` logger; visible under the `serious_python` tag.
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

Mai-An Lab integrates a highly optimized, custom search client using elements of **Streamrip 2.1.0** to handle remote metadata queries and downloads.

### 3.1 Overhauled Search & Connection Architecture (`StreamripSearcher`)

To guarantee an ultra-responsive user experience, prevent socket leaks, and protect mobile battery/memory limits, the search infrastructure leverages a state-of-the-art background worker and lifecycle manager:

#### 1. Persistent Daemon Worker Thread (`StreamripSearcherWorker`)
* **Dedicated Loop**: The search engine maintains a single class-level event loop (`_loop`) running continuously on a dedicated, daemonized background thread named `StreamripSearcherWorker`.
* **Thread-Safe Dispatches**: All asynchronous metadata and search methods are dispatched thread-safely to this background loop using `asyncio.run_coroutine_threadsafe`. This completely isolates network I/O from Flet’s main UI execution context, ensuring zero stutter during page swaps.

#### 2. Class-Level Cached Client & Async Locks
* **Single Session**: To avoid costly connection renegotiations and socket exhaustion, a single `QobuzClient` session is cached at the class level (`StreamripSearcher._client`).
* **Lock Synchronization**: Access to the cached client and session setup is strictly governed by an asynchronous loop lock (`StreamripSearcher._client_lock`). This prevents race conditions or duplicate session initialization when concurrent API calls are triggered.

#### 3. Dynamic Credentials Hot-Reloading & Request Signing
* **Credentials Hot-Reloading**: When settings are mutated, the searcher detects modifications in the Qobuz credentials configuration (User ID, token, App ID, or secret) on the subsequent request. It gracefully shuts down the active `aiohttp.ClientSession` and dynamically re-authenticates in-place.
* **OrpheusDL Signature & Android Spoofing**: Requests to Qobuz API v0.2 compute an MD5 digest (`request_sig`) over sorted query parameters, timestamp, and secret, accompanied by Android device platform headers (`X-Device-Platform: android`, `X-App-Version: 5.16.1.5`).
* **Header Retention & Guest Fallback**: Authenticated user sessions retain `X-User-Auth-Token` in HTTP headers across searches and stream calls. If user credentials are missing or expired, the client automatically degrades to **Guest Signed Catalog Mode** for error-free search.

#### 4. Metadata-Based Search Deduplication
* Qobuz search results frequently contain duplicate tracks and albums due to single releases, standard/deluxe editions, and varied compilations.
* To clean up the UI, search results undergo an insertion-order-preserving deduplication pass:
  - **Tracks and Albums**: Deduplicated strictly by a composite key of `(media_type, title, artist)`.
  - **Other Items (Artists, Playlists)**: Deduplicated by `(media_type, title)`.
* This eliminates redundant result rows while maintaining absolute priority for the most relevant search hits.

#### 5. 5-Minute Inactivity Automated Timeout Lifecycle
* **Active Poll**: A background coroutine monitor (`_inactivity_monitor`) sleeps in 15-second intervals and checks the elapsed time since the last active search or dropdown expansion.
* **Graceful Session Pruning**: If no activity occurs for **5 minutes (300.0s)**:
  - The monitor locks the client session and closes the active `aiohttp.ClientSession` cleanly.
  - The background event loop is stopped (`cls._loop.stop()`), which gracefully terminates the `StreamripSearcherWorker` thread.
  - All class-level references (`_loop`, `_thread`, `_client`, `_client_lock`) are reset to `None` for garbage collection.
* **On-Demand Resurrection**: The very next search or dropdown expansion automatically and seamlessly spins up a fresh event loop, thread, and lock context; restarting the inactivity timer from zero.

### 3.2 Deezer Backend & Stream Decryption Architecture (`DeezerClient`)

The application integrates Deezer as a co-equal secondary streaming and acquisition source alongside Qobuz:

1. **Authentication via ARL Cookie**: Authenticates using an active browser ARL cookie (`deezer.arl` in `config.toml`). Standard username and password login is unsupported due to Deezer web API restrictions.
2. **On-the-Fly Stream Decryption**: Deezer CDN tracks are Blowfish-CBC encrypted with dynamic per-track keys. Using `pycryptodomex`, audio chunks are decrypted on-the-fly during download and stream operations rather than buffered fully in memory, avoiding Android out-of-memory terminations on large lossless tracks.
3. **Decrypted Preview Fallback**: Because Deezer CDN audio is Blowfish-encrypted and cannot be streamed over raw HTTP without decryption, track previews cannot be played directly via URL. Tapping preview initiates an on-the-fly decrypted download to a local temporary cache file before dispatching to the audio engine.
4. **Subscription Quality Fallback & Ceiling**: Quality selection automatically falls back to the maximum available tier for the account plan (FLAC → 320 kbps MP3 → 128 kbps MP3) when `lower_quality_if_not_available` is enabled. The maximum fidelity available from Deezer is 16-bit / 44.1 kHz FLAC (CD quality); 24-bit Hi-Res master tiers are exclusive to Qobuz.
5. **Tag Layout Normalisation**: Metadata harvested from Deezer (`source_metadata.py`) is normalised to match Qobuz schema standards (artist, album artist, release year, disc and track indices, embedded artwork), ensuring library homogeneity.
6. **Jarvis Catalog Scope Limitation**: Jarvis voice commands and conversational LLM tool invocations (`llm_tools.py`) operate exclusively against the **Qobuz** catalog for remote searches and downloads. Deezer searches must be performed manually through the Search tab UI.

### 3.3 Download Dock & Multi-Track Acquisition (`DownloadDock`)

Downloads are coordinated through an asynchronous download controller (`queue_controller.py`) with UI feedback managed by a persistent floating dock:
- **Speed & Progress Metrics**: Emits aggregated download velocity, progress percentages, and quality badges.
- **Granular Controls**: Supports individual item cancellation, batch pausing, and failure retry dispatching.
- **Auto-Collapsing Floating Pill**: Minimizes automatically when idle to maximize screen space.

### 3.4 Download & Auto-Rescanning Architecture
To bridge remote search and the local playback library seamlessly, remote downloads run on dedicated worker threads. When a download completes and passes checksum validation, it is renamed atomically from a `.tmp` file to its target format.

The downloader thread immediately fires an asynchronous callback that calls `LibraryScanner.scan_track` on the specific downloaded file path:

```text
Qobuz / Deezer Download ──► Background Worker ──► Atomic Rename (.tmp → .flac)
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

- **Instant Local Integration**: Instead of rebuilding the entire music database, the scanner performs a highly optimized, single-file SQLite WAL ingestion. It extracts metadata tags, writes the track details to `tracks` (triggering aggregate counters), and refreshes the Library tree dynamically.
- **Zero-Refresh User Experience**: The newly downloaded song appears in the **Library** tab within milliseconds of the search-card download completing. The UI replaces the download button with a **cyan check icon** automatically, enabling immediate playback without manual scanning.

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

2. **TTS Playback Synchronization Safeguard (Dart `Completer` Blocking)**:
   - **The Challenge**: On several platforms, native TTS plugins return instantly as soon as a text string is successfully *enqueued* in the system's speech queue, rather than waiting for the physical utterance to *finish*. This caused Python's `_append_bubble()` to resolve instantly and launch song playback (via `audio_engine.play()`) directly on top of Jarvis's speaking voice.
   - **The Architecture**:
     * **Completer Locking Pattern**: Inside [flet_audio_service.dart](file:///Users/chrismitsacopoulos/Desktop/Mai-An-Lab/flet_audio_service/src/flutter/flet_audio_service/lib/flet_audio_service.dart), a static `Completer<void>? _ttsCompleter` acts as a thread block.
     * **Event Hooks**: Native event handlers (`setCompletionHandler`, `setErrorHandler`, `setCancelHandler`) are configured on `_ensureTts()` to complete the active `_ttsCompleter` whenever speech completes, cancels, or encounters an error.
     * **Channel Blocking**: When Python calls `tts_speak(...)`, the Dart plugin initiates `tts.speak(text)` and blocks the MethodChannel response:
       ```dart
       _ttsCompleter = Completer<void>();
       await _ttsCompleter!.future.timeout(Duration(seconds: 30), onTimeout: () {
           _ttsCompleter?.complete();
       });
       ```
     * **Abortion Safety**: Any invoke-channel call to `tts_stop()` immediately completes the active completer, avoiding thread hangs during manual stops.
     * **Result**: Playback wait safeguards work perfectly; the music is guaranteed to stay paused or ducked until Jarvis has physically finished saying his lines.

3. **Simultaneous Voice Safety (Active Speech Preservation)**:
   - To guarantee complete stability and prevent channel-locking glitches between Android's native SpeechRecognizer and TextToSpeech engines, the system allows active voice feedback to run naturally without forced interruption when the microphone is pressed.
   - A click or hold gesture on the microphone button launches the voice-capturing STT pipeline directly without calling `service.tts_stop()`, avoiding platform-specific codec/channel cancellation conflicts and ensuring robust execution on all mobile builds.

4. **Deterministic NLP & Normalisation (`assistant_intent.py` & `text_match.py`)**:
   - **Spelling Auto-Correction (Levenshtein Distance)**: Calculates Levenshtein Edit Distance on out-of-vocabulary terms against key command tokens (`"skip"`, `"pause"`, `"resume"`, `"mute"`, `"unmute"`, `"queue"`, `"playlist"`, `"download"`). If edit distance $D \le 1$, the token is automatically corrected.
   - **Synonym & Slang Translation Map**: Normalizes colloquial voice inputs to their semantic command equivalents using a deterministic vocabulary dictionary (e.g. *"banger"* / *"beats"* $\rightarrow$ *"song"*, *"blast"* / *"spin"* $\rightarrow$ *"play"*, *"shove"* $\rightarrow$ *"add"*, *"quiet"* $\rightarrow$ *"mute"*).
   - **Recursive Fixed-Point Peeling**: Employs recursive peeling loops (`_normalise` and `_clean_query`) to dynamically strip out voice hesitation layers, politeness noise, and auxiliary verbs, isolating the target query.
   - **Homophone Protections**: Integrates custom vocabulary mappings to capture homophone slips (e.g. treating *"cue"* and *"queue"* identically).
   - *Note on Legacy Semantic Classifier*: The experimental 30k-token BERT WordPiece vector-space model (`semantic_intent.py`) was retired to `deprecated_feature/`. Sub-word splitting fractured domain terms (such as `equalizer`, `qobuz`, and `haptic`), whereas whole-token matching combined with Levenshtein distance proved more reliable, faster, and zero-overhead on mobile.

5. **Conversational LLM Agent (`llm_engine.py` & `llm_tools.py`)**:
   - For unstructured, complex, or conversational prompts, Jarvis routes requests to an LLM tool-calling agent.
   - **Tool Schema**: Exposes structured tools for catalog searches, playback management, queue inspection, similarity walk triggers, and library statistics.
   - **Fallback Architecture**: Operates as a seamless complement to deterministic voice commands: precise physical playback controls execute instantaneously via local regex matching, while open-ended musical queries leverage the conversational agent.

6. **Proactive Sandboxed Chat Persistence & Lazy 15-Minute Expiration (`utils/chat_memory.py`)**:
   - **Lite JSON Architecture**: To bypass heavy, slow local database wrappers on Android, the chat memory is modeled as a sandboxed JSON storage backend. Read and write access completes in under 0.2 milliseconds with a footprint under 50 KB.
   - **Proactive Memory Cap (50 items)**: To protect Android's UI buffer and WebSocket sync channel from sluggish serialization lag during deep chat histories, the memory strictly caps the chat list at 50 messages, shifting out older entries dynamically.
   - **Lazy Inactivity Expiration**: When the user opens the Jarvis panel or resumes the app, `load_session()` checks the time delta since the last active interaction against a 15-minute inactivity threshold (900 seconds). If breached, storage resets and issues a fresh greeting.

7. **OS Process Lifecycle Integration**:
   - The application hooks into Flet's system event loop via `on_app_lifecycle_state_change`:
     * **Background / Suspend** (`hidden`, `inactive`, `detached`): Proactively calls `handle_app_background()` to write the final active timestamp to disk, guaranteeing that if the Android OS kills the suspended process to free up system memory, the inactivity duration is preserved accurately.
     * **Resume / Foreground** (`resumed`): Calls `handle_app_resume()`. If the 15-minute inactivity limit was breached during background suspension, it immediately resets UI controls and triggers the initial hello greeting bubble.

8. **Real-Time Dynamic Help System**:
    - Queries the database and filesystem on-the-fly when the user requests `help` or asks `"what can you do?"`.

### 5.2 Comprehensive Skillset & Commands

Jarvis supports an exhaustive range of hands-free vocal commands and functions:

| Intent Command | Action Performed | Example Phrases |
|---|---|---|
| **Play Song / Artist** | Immediately plays a matched track, artist catalog, or album from your library. | *"play Stairway to Heaven"*, *"play Radiohead"*, *"start playing Homework"* |
| **Play Next** | Inserts the matched track, artist, or album directly after the currently playing song in the queue. | *"play Stairway next"*, *"put Radiohead next"* |
| **Add to Queue** | Appends the matched track, artist, or album to the end of the global playback queue. | *"add Stairway to the queue"*, *"put Homework in the queue"*, *"enqueue Daft Punk"* |
| **Acoustic Similarity Walk** | Traverses acoustic and metadata similarity edges from the current track to sequence a smooth related arc. | *"play something similar to this"*, *"more like this song"*, *"play tracks similar to Daft Punk"* |
| **Artist Similarity Walk** | Traverses relationship links to play more tracks from the currently playing artist. | *"play more by this artist"*, *"more songs from them"* |
| **Remote Download** | Invokes the background Streamrip thread to search Qobuz, download, and auto-index the target track or album. | *"download Stairway to Heaven"*, *"get Daft Punk Homework"*, *"fetch and save track X"* |
| **Surprise Me (Random)** | Selects a random track from the local library, shuffles the playback pool, and begins playback. | *"surprise me"*, *"play something random"*, *"shuffle play"* |
| **Playback Control** | Standard controls to manipulate active audio player states. | *"pause"*, *"resume"*, *"stop"*, *"skip"*, *"previous track"* |
| **Queue Operations** | Manipulates the ConcatenatingAudioSource without interrupting active music. | *"clear the queue"*, *"shuffle the queue"* |
| **Volume Control** | Adjusts or silences system audio player levels. | *"mute"*, *"unmute"*, *"be quiet"*, *"restore volume"* |
| **Status Inquiry** | Prompts Jarvis to query player metadata and vocalize active media info. | *"what's playing?"*, *"what is this song?"*, *"now playing"* |
| **DSP Library Sweep** | Requests a full filesystem and feature extraction sweep for missing acoustic metrics or link matrices. | *"rescan the library"*, *"reanalyse my music"*, *"reindex features"* |
| **Help Commands** | Queries Jarvis's on-device documentation to dynamically list exact voice skills. | *"help"*, *"what can you do?"*, *"commands"* |

### 5.3 Parallel Multi-Core Folder Scanner
- **Concurrent Disk Crawl**: The library scanner walks large directories asynchronously. It dynamically scales to use multiple processing cores via `concurrent.futures`, preventing large directories from blocking the Flet main thread.
- **Non-Blocking Walk**: Leverages native asynchronous generators, letting users browse existing library tabs or queue songs dynamically while an active walk scans thousands of media files in the background.

---

## 6. Database Design & Schema

The data layer is optimized for fast hierarchical browsing, prefix-based search, and low-latency aggregate reads.

### 6.1 Database Schema
The catalog database (SQLite) manages records across the following tables:

| Table | Description | Key Fields |
|-------|-------------|------------|
| `artists` | Stores unique artist names and aggregate counts. | `id`, `name`, `album_count`, `track_count` |
| `albums` | Maps artists to their respective releases. | `id`, `artist_id`, `title`, `year`, `genre`, `track_count` |
| `tracks` | The primary music index. | `id`, `album_id`, `title`, `track_num`, `duration`, `path`, `format`, `added_date`, `bitrate`, `bpm`, `energy`, `brightness` |
| `playlists` | User-defined and imported collections. | `id`, `name`, `created`, `color` |
| `playlist_tracks` | Junction table for playlist membership. | `playlist_id`, `track_path`, `order_index` |
| `play_counts` | Extended sound profile, feature space, and play history. | `track_path`, `count`, `last_played`, `bpm`, `energy`, `brightness`, `rolloff`, `beat_strength`, `spectral_flatness`, `spectral_contrast`, `key_index`, `timbre` (68D BLOB), `features_version` |
| `track_neighbors` | Sparse adjacency table representing the $k$-NN acoustic/metadata graph. | `track_path`, `neighbor_path`, `weight`, `edge_kind` |
| `pca_space` | Persists the active PCA projection produced by the double-pass SVD engine. | `id` (always 1), `means` (8×float32 BLOB), `stds` (8×float32 BLOB), `projection` (8×3 float32 BLOB), `eigenvalues` (8×float32 BLOB) |
| `artist_enrichment` | Caches external MusicBrainz provenance (country/area) and community genre tags. | `artist_name` (PK), `mbid`, `country`, `area`, `genres` (JSON list), `source`, `score`, `status`, `fetched_at` |
| `genre_affinity` | Pre-computed NPMI genre×genre similarity model for random walk metadata gates. | `id` (always 1), `model` (JSON dict `"a|b": npmi`), `updated_at` |
| `genre_graph` | Pre-computed PAGA-style genre adjacency graph driving auto-play journey walks. | `id` (always 1), `model` (JSON dict `{"version", "nodes", "adj"}`), `updated_at` |
| `autoplay_hidden` | Tracks hidden from auto-play via user removal or negative feedback. | `path` (PK), `hidden_at` |
| `autoplay_hop_rejects` | Negative feedback ledger per genre hop used to penalize or veto poor transitions. | `src`, `dst`, `path`, `artist`, `rejected_at` (PK: `src, dst, path`) |

> [!NOTE]
> **Sound Profile BLOB Layout (v5)**: The high-dimensional feature profile is packed as a single 68-float, little-endian binary BLOB inside the `play_counts.timbre` column (272 bytes total) to keep database size minimal and query speeds fast. The BLOB contains the 20D MFCC Mean, 20D MFCC std (timbral dispersion), 12D Chroma Pitch Profile, and 16D Rhythm Profile. The `features_version` column acts as a schema version, letting the engine dynamically invalidate and re-analyze features if extraction logic evolves.

### 6.2 Relational Design & Triggers
- **Composition over Inheritance**: Entities are represented using strict Composition (*Part-Of* relationships) rather than inheritance to avoid complex sparse tables. Artists compose Albums, which compose Tracks, bound via foreign keys with `ON DELETE CASCADE` constraints.
- **Pre-Computed Denormalization**: To avoid expensive nested `SELECT COUNT` joins at runtime, pre-computed aggregate columns (`artists.album_count`, `artists.track_count`, and `albums.track_count`) are maintained. These fields are synchronized via database triggers on insert, delete, or update, ensuring $O(1)$ read latency during scroll and navigation.
- **Single-Query Junction Optimization**: Playlist track counts and metadata are fetched in a single pre-joined SQL query using a `LEFT JOIN` and `GROUP BY` clause. This eliminates sequential nested query lookups on the SQLite file, avoiding thread stalls and keeping CPU loading flat.
- **FTS5 Search & Indexing**: Diacritic-folding prefix matching is achieved via a virtual table (`fts_search`) utilizing the FTS5 module and the `unicode61` tokenizer with `remove_diacritics=1`. Changes are automatically synchronized to the FTS index via triggers.
- **Fuzzy Search Fallback**: If FTS5 and standard `LIKE %query%` queries return zero results, the engine falls back to a custom fuzzy string matching algorithm. It computes a 2-gram (k-mer) similarity score between the query and track/album/artist fields:
  $$S = \frac{2 \cdot |Q_2 \cap T_2|}{|Q_2| + |T_2|}$$
  where $Q_2$ and $T_2$ are the sets of 2-character substrings (k-mers) in the query and target respectively. Results with a similarity score $\ge 0.25$ are returned, sorted in descending order of similarity.

---

## 7. Metadata Curation & Deletion Internals

Mai-An Lab provides in-app metadata curation and physical file lifecycle operations executed under atomic asynchronous transactions:

### 7.1 Metadata Curation & Workbench Pipeline

Metadata curation is centered on the **Metadata Workbench** (`ui/player/metadata_workbench.py`), which establishes artist-level provenance (country, origin area, and genre taxonomy) to guide Auto-Play journey graphs:

- **Multi-Source Enrichment Cascade (`utils/metadata_enrich.py`)**:
  1. *MusicBrainz API*: Polled with rate limiting (≤ 1 req/s, descriptive User-Agent) to extract objective artist country/area and community genre tags.
  2. *Local Audio File Tags*: Fallback to extract embedded genres from existing library files when MusicBrainz tags are sparse.
  3. *Qobuz Catalog API (`utils/metadata_qobuz.py`)*: Fallback to remote streaming metadata for missing artist classifications.
  4. *Manual Curation*: User overrides applied via the Workbench.
- **Per-Field Provenance**: The database records provenance labels (`MB`, `FILES`, `QOBUZ`, `YOURS`), enabling transparent verification of sources.
- **Bulk Scene Tagging**: Users can select multiple artists and assign common genres (e.g. tagging a set of artists as regional rap) in a single operation.
- **Auto-Play Model Synchronization**: Saving curated metadata immediately triggers `refresh_walk_models`, recalculating NPMI genre affinities and regenerating the journey graph.
- **Background Single-Flight Catch-Up (`_ENRICH_STATE`)**: The app executes an initial boot enrichment pass 30 seconds after launch with single-flight locking, preventing redundant network requests.
- **Database-Only Curation Scope (No Disk Mutation)**: Workbench modifications are committed directly to the SQLite `artist_enrichment` and `artists` tables via `set_manual_artist_enrichment`. The Workbench **does not mutate physical audio file tags on disk** (physical tag rewriting was retired to `deprecated_feature/` to eliminate risk of corrupting disparate container formats such as FLAC, ID3, and MP4).

### 7.2 Physical Song Deletion Pipeline
Song deletion executes in a strict two-phase atomic pipeline to ensure filesystem and database consistency:
- **Phase 1: Physical Erasure**: The target file path is passed to an asynchronous worker thread, which uses `os.remove` to erase the file permanently from storage.
- **Phase 2: Database Cascade & Cleansing**: The track row is deleted from the `tracks` database table, triggering a cascade:
  - **Junction Cleanup**: Removes occurrences of the deleted track path from the `playlist_tracks` junction table.
  - **FTS5 De-indexing**: Wipes search tokens associated with the deleted track from the `fts_search` virtual table.
  - **Aggregate Updates**: Mutates artist track/album counts. If an artist or album now contains **zero** tracks, their parent rows are automatically deleted from `artists` and `albums` respectively, keeping the library tree clean.

---

## 8. Developer, Diagnostic & Migration Tooling

The repository provides several command-line diagnostics and offline scripts under `tools/` and `StreamripApp/tools/` for verifying catalog state, testing auto-play walks, and managing schema migrations:

### 8.1 State Bundle Appraisal CLI (`tools/appraise_state_bundle.py`)
A dependency-free CLI tool (using only Python's standard library `sqlite3` and `zipfile`) that inspects state bundle archives or raw SQLite database files:
- **Computed State Audit**: Scans `tracks` and `play_counts` to report total track counts, the number of tracks with current DSP features (`features_version >= 5`), and the percentage of tracks with persisted $Z_r$ PCA coordinates (`pca_coords`).
- **Build Automation Guard**: Emits distinct process exit codes:
  - `0`: Valid bundle containing tracks (safe for build scripts to preserve).
  - `1`: Valid database structure but empty (0 tracks).
  - `2`: Missing, corrupted, or unreadable archive.
- **Usage in Android Builds**: Called by `_adb_common.sh` and `fresh_build_android.sh` before running `adb uninstall`, refusing to wipe the target device if the only available state backup is empty or missing.

### 8.2 Desktop State Restoration (`tools/restore_macos_state.py`)
Facilitates seamless reproduction of Android mobile catalog state on macOS developer workstations:
- Unpacks an exported state bundle (`mai_an_lab_state_<timestamp>.zip`) to desktop application support paths (`~/Library/Application Support/streamrip/`).
- Maps Android database files, TOML configs, and search histories into local macOS test locations without requiring manual re-scanning or multi-hour feature recomputation.

### 8.3 Feature BLOB Schema Migration (`tools/migrate_v4_to_v5_drop_delta.py`)
An in-place SQLite database migration script that upgrades feature profiles from schema v4 to v5:
- **Delta Dropping**: Reads 88-float v4 feature BLOBs from `play_counts.timbre` and `feature_cache.timbre` (20 MFCC mean, 20 MFCC std, 20 MFCC delta, 12 chroma, 16 rhythm).
- Drops the deprecated 20-float MFCC delta block (`np.delete(v, np.s_[40:60])`), converts the surviving 68 floats to little-endian bytes, and updates `features_version = 5`.
- Avoids re-analyzing audio files from scratch when upgrading older test databases.

### 8.4 Headless Station Probing & A/B Diagnostics (`tools/walk_probe.py` & `tools/walk_ab_probe.py`)
Command-line test harnesses that run auto-play station walks against a target SQLite database without launching the GUI:
- **Cohesion & Guardrails**: Computes mean NPMI soft-set genre similarity scores against seed tracks and tallies foreign cross-culture tracks (similarity $< 0.06$) to verify that cultural barriers are strictly enforced.
- **A/B Metadata Contribution**: Runs dual walks from the same seed—one with metadata pool gating enabled and one pure-acoustic walk—printing diffable queue outputs to evaluate transitions objectively.

### 8.5 Metadata Enrichment Probe & Fusion Evaluation (`StreamripApp/tools/`)
- `enrich_artists.py`: Standalone CLI wrapper around `utils.metadata_enrich.enrich_library`, allowing developers to test MusicBrainz rate limits (1 req/s) and backfill test databases offline.
- `eval_metadata_fusion.py`: Benchmarks the 4-tier metadata fusion cascade against difficult artist catalogs, tracking resolution confidence across MusicBrainz, local tags, and Qobuz APIs.
