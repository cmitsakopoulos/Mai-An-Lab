# UI Instructions: Library & Search

This document outlines the user interface components, behavior, and customization options for the Mai-An Lab application.

---

## 1. Library Management

The **Library** tab manages and plays locally indexed music.

### 1.1 Indexing Music
- **Scanning**: Recursive scans are triggered via the **INDEX LIBRARY** button or the top-right Refresh icon.
- **Progress**: A top-anchored progress bar and status label track active scans.

### 1.2 View Modes & Navigation
The Library supports five views, toggled via settings:
- **Playlists**: Lists custom and imported playlists.
- **Artists**: Lists artists; expanding reveals albums.
- **Albums**: Lists albums; expanding reveals tracks.
- **Tracks**: A flat, paginated list of all indexed tracks.
- **Network**: An interactive representation of the acoustic similarity space built from PCA graph embeddings. Features include:
  * **Split Layout**: Displays the 2D force-directed similarity graph on top and a scrollable, ordered track list below. A chevron toggle collapses or expands the graph to maximize list area.
  * **Modes (`[ Local | Walk ]`)**:
    - **Local**: Renders the seed track and its $K$ nearest acoustic neighbors, color-coded by primary genre category. Multi-genre tags parse their leading primary segment (`Pop` $\rightarrow$ Soft Pink `#FF80AB`, `Rock/Alt` $\rightarrow$ Bright Cyan `#40C4FF`, `Hip-Hop` $\rightarrow$ Amber Gold `#FFD740`, `Electronic` $\rightarrow$ Neon Lime `#76FF03`).
    - **Walk**: Visualizes sequential similarity walk trajectories with directed arrowheads pointing step-by-step through the acoustic space.
  * **Controls & Aesthetics**:
    - **Top Control Header**: Segmented mode tabs, compact PopupMenu selector (`Density: 24` in Local mode, `Steps: 10` in Walk mode), and Follow Live Playing Track toggle.
    - **Native GPU Pan and Zoom**: Uses Flutter's `InteractiveViewer` for smooth canvas navigation, eliminating bridge and Python event-loop overhead during gestures.
    - **Ordered Enqueue**: Batch *Play* or *Add* actions preserve trajectory order (Play replaces sequentially, Add appends via `queue_extend`) instead of falling back to database index order.
  * **Selected Track Actions**:
    - Selecting a node highlights the track in the ordered list below.
    - Track actions include Play Now, Add to Queue, Reseed Local, Start Walk Here, and Add to Playlist.

### 1.3 Searching & Filtering
- **Real-Time Filter**: The top search bar filters the current view instantly, debounced by 300ms to ensure responsiveness. Tap the 'X' button to clear.
- **Fuzzy Search Fallback**: If FTS5 and prefix-based `LIKE` queries return no direct results, the library search falls back to a 2-gram (k-mer) similarity matching algorithm to surface the closest matching tracks, albums, or artists (score threshold $\ge 0.25$).

### 1.4 Sorting
Context-aware sort options:
- **Artists**: Sort by Name, Most Tracks, or Most Albums.
- **Playlists**: Sort by Name or Date Created.
- **Albums / Tracks**: Sort by Date Added, Artist, Album, or Track.

### 1.5 Infinite Scroll & Viewport Loading
- **Continuous Viewport Ingestion**: Lists dynamically load items as the user scrolls, eliminating fixed 35-item page slicing and manual "Tap to load..." boundary cards.
- **Memory Optimization**: Off-screen widgets are recycled by Flet's underlying Flutter ListView, keeping the memory footprint minimal across large catalogs.

---

## 2. Auto-Play Station & Recommendations

Controls that guide queue replenishment and dynamic similarity journeys.

### 2.1 Auto-Play Station Controls
- **Station Toggle**: Activated via the chain icon in the Now Playing sheet; long-press toggles Auto-Play on/off.
- **Variety Selector**: A segmented pill selector in the Auto-Play sheet toggles between:
  - *Deterministic*: Reproducible nearest-neighbor traversal.
  - *Random*: Rank-sampled candidate selection inside legs, lift-weighted hops, and a shuffled entry track.
- **Visual Indicators**: Artwork carries a distinct cyan border in the Mini Player and Now Playing sheet when Auto-Play is active.
- **Queue Partitioning**: Tracks are strictly partitioned:
  ```text
  [ Playing Track ] → [ Up-Next Picks ] → [ Auto-Play Buffer ] → [ Held Queue ]
  ```
  Manual *Play Next* and *Add to Queue* picks land ahead of the station buffer. Disabling Auto-Play drops only the station buffer, leaving the user queue intact.
- **Negative Feedback Signals**: Swiping/removing a recommendation permanently hides it from Auto-Play (recorded in the `autoplay_hidden` DB table). Skipping a track early triggers negative feedback, ending the current genre leg after two strikes.

---

## 3. Search, Streaming & Acquisition

The **Search** tab (implemented in [search.py](file:///Users/chrismitsacopoulos/Desktop/Mai-An-Lab/StreamripApp/ui/views/search.py)) supports remote catalog queries across Qobuz and Deezer.

### 3.1 Dual-Source Architecture
- **Source Switch**: A segmented control above results toggles between Qobuz and Deezer. Each source retains its query state and scroll position across switches.
- **Visual Distinctions**: Search rows display source-specific branding (Deezer rows carry a purple accent `#A259FF`).
- **Infinite Scroll**: Remote query results load continuously via infinite scroll, replacing legacy page buttons and ghost cards.

### 3.2 Previews & Streaming
- **Qobuz Streaming Previews**: Resolves authenticated HTTP stream URLs (128 kbps MP3) to play previews instantly over the network without disk writes.
- **Deezer Decrypted Previews**: Because Deezer CDN audio is Blowfish-encrypted and unstreamable over standard HTTP, previews fall back to a fast, temporary on-the-fly decrypted download to internal cache before playback.
- **Quality Ceiling & Plan Fallback**: Deezer delivers up to 16-bit / 44.1 kHz FLAC (CD quality); 24-bit Hi-Res master tiers are exclusive to Qobuz. If a chosen quality tier is unavailable on an account, the client falls back to 320 kbps or 128 kbps MP3 automatically.
- **Jarvis Catalog Limitation**: The Jarvis voice assistant and LLM tools exclusively search and download via Qobuz. Deezer catalog exploration and downloads are accessed exclusively through this Search view.

### 3.3 Download Dock & Batch Acquisition
- **Integrated Download Dock (`download_dock.py`)**: A floating action dock displays active download speed, progress bars, and quality badges. The dock auto-collapses when idle.
- **Multi-Select Download**: Long-pressing search cards activates multi-select mode, enabling batch acquisition of multiple tracks or albums at a selected quality tier.
- **Automatic Library Rescan**: Completed downloads pass checksum validation and are indexed into the SQLite database via a single-file WAL transaction within milliseconds.

---

## 4. Global UI Conventions

- **Color Coding**: ACCENT colors identify item types:
  - **Artist**: Purple (`LIB_ARTIST_COLOR`)
  - **Album**: Blue (`LIB_ALBUM_COLOR`)
  - **Track**: Cyan (`LIB_TRACK_COLOR`)
  - **Playlist**: Amber (`LIB_PLAYLIST_COLOR`)
- **Active Highlights**: Playing tracks are highlighted with a low-opacity background accent and dynamic leading equalizer icons.

---

## 5. Gestures & Controls

- **Library Tracks**:
  - *Swipe Right*: Adds track to the **"Next Up"** queue slot.
  - *Long Press*: Context menu for *Play Next*, *Add to Queue*, *Add to Playlist*, *Edit Metadata*, *Delete Song*, and *Redownload*.
- **Now Playing Sheet**: Swipe left/right on artwork to skip/prev; tap artwork to play/pause; drag down to collapse.
- **Playlist Management**: Up/down arrows for manual track reordering; minus icon for immediate removal.
- **Jarvis Push-to-Talk (PTT)**:
  - *Press & Hold*: Starts capturing voice. Tooltip updates to *"Release to Send"* and icon turns red.
  - *Release*: Halts capturing, performs STT, submits text, and resets color.
  - *Greetings & Status*: Session introductions change dynamically by system time. Jarvis appends library metrics (track count, active edges) beneath greetings.
  - *Expiration*: Thread history persists across tabs but clears after 15 minutes of inactivity. The trash bin icon manually purges history.
- **Media Notification Controls**:
  - *Standard Controls*: Previous, Play/Pause, Stop, and Next are available directly in the system notification shade and lock screen.
  - *Replenish/Refresh Queue*: A custom refresh action button in the media notification triggers a background queue replenishment, automatically rebuilding/filling the similar tracks queue based on the current track's profile and user tastes.

---

## 6. Onboarding & First-Use Wizard

On first launch, the application presents an interactive multi-step wizard (`onboarding_modal.py`):
- **Slide 1 (Welcome & Features)**: Orientation covering the acoustic similarity engine, dual-source streaming/downloading, and the Jarvis assistant.
- **Slide 2 (Music Sources & Paths)**: Automatic detection of public Music directories, folder path configuration (guarded against Android sandbox fallbacks), and initial credential input for Qobuz and Deezer.
- **Slide 3 (Personalization)**: Theme accent selection (Gold, Cyan, Violet, Rose, Mint) and default startup view configuration (Library, Search, Jarvis).

---

## 7. Settings & Customization Hub

The settings screen features a categorized menu linking to modular sub-panels, preceded by a real-time **Settings Search Bar** that indexes all setting labels and descriptions with Levenshtein fuzzy matching:

### 7.1 Account & Streaming Sources
- **Qobuz Authentication**: User ID, Auth Token, App ID, and App Secret configuration. Supports authenticated streaming and guest-signed catalog search.
- **Deezer Authentication**: Browser ARL cookie configuration for search, decrypted previews, and direct downloads. Standard username/password login is not supported due to Deezer API requirements.

### 7.2 Storage & Directory Paths
- **Folder Configuration**: Independent directory settings for the local library index and download destination.
- **Public Storage Enforcement**: Automatically resolves paths to public storage (e.g. `/sdcard/Music/Streamrip`), explicitly preventing fallbacks into restricted app-private sandboxes on Android 11+.
- **Default Download Quality**: Option to enforce a default quality tier (FLAC, MP3 320, MP3 128), bypassing the quality selector dialog.

### 7.3 Auto-Play Management & Learned Feedback Pane
- **Variety Mode**: Toggle default station behavior between *Deterministic* (closest-first acoustic order) and *Random* (rank-sampled picks inside legs with lift-weighted hops).
- **Auto-Play Learning & Memory View (`HiddenTracksPane` in `ui/views/autoplay_hidden.py`)**: Tapping the "Hidden from Auto-play & Blocked hops" card opens a dedicated management subview:
  - *Hidden from Auto-play*: Lists every track manually removed from auto-play recommendations. Each entry displays a restore button, alongside a header "Restore all" action. Tracks remain in the library index at all times; they are only excluded from automated recommendation pools.
  - *Blocked Genre Hops*: Displays directed genre jumps that auto-play has permanently blocked following negative user feedback (consecutive early skips or removals occurring within the hop window). Includes individual hop unblock buttons and a global "Reset all" action to restore full transition freedom.

### 7.4 Library Intelligence & Metadata Workbench
- **Metadata Workbench Launcher**: Direct access to the artist provenance board (`metadata_workbench.py`). Displays library-wide tagging coverage, triggers multi-source synchronization (MusicBrainz → file tags → Qobuz), reviews low-confidence matches, and executes bulk scene tag assignments.
- **Database Curation Scope**: Workbench edits are committed directly to the application's SQLite database (`artist_enrichment` and `artists` tables) to shape Auto-Play journey graphs and catalog filters. The Workbench **does not rewrite physical audio file tags on disk**.

### 7.5 Appearance & Navigation
- **Segmented Tab Customization**: Granular control over visible Library tabs (Playlists, Artists, Albums, Tracks, Network), allowing users to limit active tabs (e.g. to 3 views) for ergonomic Cupertino segmented navigation.
- **Theme Accents & Defaults**: Custom accent color selection, startup view defaults, and library sort preferences.

### 7.6 Audio & DSP
- **5-Band Manual Equalizer**: Bands at 60Hz, 230Hz, 910Hz, 4000Hz, and 14000Hz with gains $\in [-15.0, +15.0]\text{ dB}$.
- **Preset Filtering & UI Guarding**: Filter between System Presets (Flat, Rock, Pop, Jazz, Classical, Electronic, Bass Booster, Vocal Booster) and Custom Presets, with custom preset creation tools visible only in custom mode.
- **Keyboard-Editable Gains**: Slider values can be adjusted via keyboard text fields that parse formatting (e.g. `+3.5 dB`), validate bounds, and clamp inputs.
- **Dynamism Enhancement**: Real-time gain enhancement with live dB boost monitor badge and card. Dynamically disabled with warning labels when acoustic features are missing.

### 7.7 Haptic Feedback & Vibrations
- Modify vibration intensities (`Selection Click`, `Light`, `Medium`, `Heavy`, `Vibrate (Long)`, `None`) for EQ sliders, node inspection, and navigation gestures.

### 7.8 Advanced Tools & Maintenance
- **Permissions**: Request runtime Android permissions (Notifications, Audio, Storage, Microphone).
- **Database Maintenance**: Table-specific purges (Index, DSP features, or Full DB reset) and manual feature extraction triggers.
- **Raw Config Editor**: Direct editing of the Streamrip TOML configuration.
- **State Export / Import (`state_export.py`)**: Export or import self-contained state bundles (`mai_an_lab_state_<timestamp>.zip`) containing the SQLite WAL database snapshot, TOML config, and search token history. Allows transferring library coordinates and graph caches across devices via `/sdcard/Download` or `~/Downloads` without rescanning.
