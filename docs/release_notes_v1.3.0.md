# Release Notes — Mai-An Lab v1.3.0

This release introduces an interactive acoustic network graph view, direct audio preview streaming, enhanced queue replenishment for similarity-based play, startup performance and battery optimizations, and a hardened Android playback bridge.

## New Features & Key Highlights

### Interactive Acoustic Network Graph View
* **Acoustic Network Canvas**: Integrates an interactive 2D force-directed layout representation of the similarity graph directly inside the Library tab. Toggled via the "Acoustic Network" appearance switch in settings.
* **Dual Navigation Modes**: Renders in "Local" mode (plots the current playing/seed track and its 1-hop nearest neighbors) or "Walk" mode (visualizes the actual sequence traversed during similarity walks).
* **High-Fidelity Interactivity**: Nodes are color-coded by genre community, support smooth pan/zoom gestures, and double-clicking any track node immediately starts its playback. Requires analyzing the library first to compute the PCA coordinates.

### Direct Track Preview Streaming & Progress UI
* **Direct Network Streaming**: Resolves authenticated stream URLs (MP3 128kbps) directly from the Qobuz API. Plays previews instantly over the network without disk-write operations.
* **Preview Progress Card**: Adds a dedicated loading card in the search tab stack displaying real-time connection status (i.e., resolving, downloading, error reports).
* **Granular Cancellation**: Provides a cancel button on the progress card that instantly halts active lookup tasks or interrupts the background download thread.
* **Preview Download Backup**: Preserves the original file-downloading preview mechanism as a robust fallback. If streaming URL resolution fails, it automatically downloads the preview file and displays download percentage progress.

### Reinforced Similarity Walk Queue (Similar Mode)
* **8-Track Seed Queue**: Initiating "Similar Mode" now immediately seeds the upcoming queue with 8 unique recommendations (up from 4), giving the user a longer horizon of upcoming tracks.
* **Proactive Block-Replenishment**: Monitors upcoming tracks and triggers proactive replenishment when the buffer falls below 4 tracks, querying the similarity graph and auto-appending new recommendations back to 8 tracks.
* **Deduplicated Multi-Track Walks**: Extends the community walk algorithm to fetch and filter candidate recommendations in batches, ensuring candidates are deduplicated against the active queue before appending.
* **Robust Walk Recovery**: Re-seeds a full 8-track buffer block if the queue is completely exhausted by the user.

### Cold Boot & Battery Optimizations
* **Deferred Library Imports**: Defers loading of heavy mathematical/scientific libraries (i.e., NumPy) until database PCA operations or SQL queries are actually executed. This shaves ~1-2 seconds off cold boot time.
* **Splash Screen Battery Fix**: Terminates background splash animation coroutines immediately after the UI mounts, eliminating idle CPU cycle consumption.
* **Batched Queue Replenishment**: Similar-mode block replenishment now appends the whole recommendation block in a single queue update — one queue-sheet rebuild and one coalesced disk write — instead of repeating both once per track (previously up to 8× per refresh).
* **Single-Call Native Queue Insert**: A new `add_queue_items` batch method on the audio bridge lets a multi-track append cross the Python↔Dart boundary in one call rather than one round-trip per track, with the live source left intact (no playback interruption).

### Incremental Metadata Enrichment & NPMI Genre Similarity
* **Incremental Artist Enrichment**: Exposes a MusicBrainz API client that incrementally resolves missing artist-level metadata (countries, areas, and genre tags) at rate-limited intervals.
* **Precomputed NPMI Genre Model**: Rebuilds a Normalized Pointwise Mutual Information (NPMI) model of cross-genre relationships from the enrichment cache. This is consumed by the similarity walker's metadata gate to evaluate transitions between different artists, facilitating cleaner, more sonic-aligned community structures.

### Automatic Cache Pruning & Footprint Control
* **Background Cache Pruning**: Introduces automatic, background pruning of temporary artwork and search preview caches when the app starts or returns to the foreground.
* **Artwork Cache Bounding**: Binds the temporary artwork directory to keep only the 100 most recently modified artwork images.
* **Search Preview TTL**: Automatically deletes search preview folders/files older than 24 hours to prevent mobile disk footprint ballooning.

### Audio & DSP Settings Hub
* **Dynamism Enhancement**: Toggle Dynamism Enhancement directly from Settings. Renders a live decibel boost monitor card in the settings panel that scales dynamically based on the current track's acoustic features.
* **Dynamism Availability Guards**: Dynamically disables and dims the Dynamism Enhancement switch (opacity 0.4) when acoustic feature vectors are missing, displaying helpful inline warning labels (e.g., *"Library is empty — add tracks first"* or *"{N} tracks lack DSP features — run Jarvis Analyser first"*).
* **Manual 5-Band Equalizer**: Toggles the manual 5-band EQ (60Hz, 230Hz, 910Hz, 4000Hz, 14000Hz) with gains ranging $\in [-15.0, +15.0]\text{ dB}$. Includes preset category toggles (System vs. Custom) with visual layout guarding, keeping advanced custom controls hidden until activated.
* **Keyboard-Editable Gain Inputs**: Allows typing precise decibel gain values into text boxes that automatically parse formatting (e.g. `+3.5 dB`), validate bounds, and clamp inputs, automatically syncing back to the graphic sliders and native audio engine.


### Android Playback Robustness
* **Instant Error Propagation**: The audio bridge now reports player and handler failures (i.e. an unplayable stream URL) to Python immediately as error events instead of letting callers wait out a timeout. Track preview falls back to download the moment streaming fails rather than after a multi-second stall — errors are surfaced without blocking the method channel.
* **Defensive Source & Queue Handling**: Bare filesystem paths are routed through file-based audio sources so ExoPlayer no longer mistakes them for schemeless network URIs, and the playback sequence stream tolerates malformed/null media tags instead of crashing the listener and freezing future queue updates.
* **Processing-State Mirror**: The engine now tracks the player's load/buffer/ready/completed state directly, giving UI and wait logic a precise signal instead of inferring readiness from position changes.

## Bug Fixes & Refactoring
* **Removal of Legacy Mood Logic & Taste Model**: Completely purged all custom/default mood profiles (e.g. `chill`, `happy`, `tonal`) and the regression-based taste modeling from the database, UI, and playback engine. These features were removed due to high complexity and client performance overhead that made them too difficult to optimize efficiently on mobile devices. Queue recommendations are now centered entirely on the acoustic similarity graph.
* **macOS Stream Playback Fix**: Remote preview streams now play on macOS. AVPlayer's asset loader is driven by the main run loop, but the player was being created on the Python worker thread, leaving it stuck in a perpetual "unknown" state (metadata and artwork appeared, but no audio). Player creation is now marshalled onto the main thread so the run loop adopts and advances it.
* **DSP Offloader — Fragmented-MP4 Recovery**: The host-side feature extractor (`tools/dsp_offload.py`) now handles fragmented MP4 files whose `moov` initialization atom sits at the *end* of the file (e.g. some FLAC-in-MP4 tracks). ffmpeg reads sequentially and aborted on the first fragment before reaching the trailing `moov` (`trun ... no tfhd was found`), even though these files play fine on-device because Android's extractor locates the `moov` regardless of position. On decode failure the offloader now relocates `moov` to the front (a pure byte copy — no re-encode) and retries, so these tracks finally get features computed instead of being silently re-attempted every run.
* **Stop Event Integration**: Connected Flet cancellation events directly to Streamrip's low-level file download loop to allow clean interruption of partial download tasks.
* **Cleanups**: Unused preview cache scanners and residual temp assets are cleaned up systematically.
