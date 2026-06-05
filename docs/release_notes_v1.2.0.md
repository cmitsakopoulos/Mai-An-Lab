# Release Notes — Mai-An Lab v1.2.0

This release introduces expanded audio settings, optimized playback flows, UI updates, and framework fixes for Android and macOS.

## New Features & Key Highlights

### Equalizer Controls & Preset Management
* **Preset Filtering**: Toggle buttons filter between System Presets (defaults) and Custom Presets.
* **UI Guarding**: Custom preset fields and lists are hidden when system presets are selected to simplify layout.
* **Keyboard-Editable Gains**: Sliders are paired with validated, auto-formatted text fields for typing precise gains, bounded at [-15.0, 15.0] dB.
* **Auto-Syncing**: Modifying bands automatically toggles the preset mode to Custom.

### Real-Time Dynamism Boost Monitor
* **Dynamic Badges**: Displays the active decibel gain boost in real-time under the track details in Now Playing and next to the toggle in Settings.
* **Zero Overhead**: Updates asynchronously only during track transitions or when computations finish.

### Acoustic Graph & Clustering Enhancements
* **Unified Graph Geometry**: Migrated acoustic similarity from raw timbre cosine distance to a unified Euclidean space of z-scored, PCA-reduced (Kaiser-truncated SVD) feature vectors, reweighted by a Zelnik-Manor self-tuning Gaussian kernel.
* **Mutual k-NN Pruning**: Enforces strict mutual k-NN intersection to prevent cluster centroid "hub" tracks from dominating walks.
* **Louvain Community Detection**: Replaced legacy k-means clustering with the Louvain algorithm run directly on the self-tuning affinity graph. Modularity optimization discovers communities from topology, sharing the walk's geometry.
* **Self-Tuning Custom Islets**: Islets project seed tracks into the unified graph projection and score neighbors using a self-tuning Gaussian affinity bandwidth based on local track density, ensuring islets and walks agree perfectly.
* **Built-in Mood Scoring**: Mood ranking utilizes z-scored ranking differences combined with per-mood pins and exclusions.

### Optimized PageRank Walks & Playback
* **Fast Graph Walks**: Batched 2-hop prefetch loads the local neighborhood graph in one database round-trip, significantly reducing start latency.
* **Modulated Walks**: Incorporates soft cross-community transition penalties via Louvain IDs, MMR-style diversity penalties in the timbre sub-space to prevent cluster traps, and personalized taste re-ranking.
* **Robust Queue Replenishment**: Replaced file path checks with an explicit state flag, ensuring reliable auto-DJ queue replenishment on track advance.
* **One-Touch Desktop DSP Offload**: Upgraded the `auto_offload` workflow by auto-copying the app's state at start up, which the script will automatically access and mutate with DSP features.

### Search Tracing & Connection Progress Card
* **Low-Level Network Instrumentation**: Integrates an `aiohttp.TraceConfig` listener to track real-time connection stages (DNS resolution, TCP socket connection creation, SSL/TLS handshakes, HTTP request headers transmission, and response data chunk streaming).
* **Interactive Progress UI**: Displays a connection progress card inside the search pane showing current connection stages with a smooth slide-and-fade animation while Qobuz queries run.
* **Query Thread Safety**: Implements asynchronous progress updates with strict query ID guarding to drop stale search requests.

### UI & Customization Polishing
* **Jarvis Tab Glitch Fix**: Fixed a visual glitch where the voice assistant tab highlighted momentarily when switching tabs.
* **Layout Scaling**: Spacings and buttons adapt better to variable screen sizes.

## Bug Fixes & Refactoring
* **Flet Core Fixes**: Fixed Flet runtime alignment and padding exceptions.
* **Test Isolation**: Resolved test harness subclass mock inheritance caching.
* **Import Database Safety**: Added automatic removal of stale database journal files (`-wal` and `-shm`) during state imports to prevent SQLite from applying old WAL transactions to a newly mutated database, resolving malformed database issues.
