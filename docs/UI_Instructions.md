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

### 1.3 Searching & Filtering
- **Real-Time Filter**: The top search bar filters the current view instantly, debounced by **300ms** to ensure responsiveness. Tap the 'X' button to clear.
- **Fuzzy Search Fallback**: If FTS5 and prefix-based `LIKE` queries return no direct results, the library search falls back to a 2-gram (k-mer) similarity matching algorithm to surface the closest matching tracks, albums, or artists (score threshold $\ge 0.25$).

### 1.4 Sorting
Context-aware sort options:
- **Artists**: Sort by Name, Most Tracks, or Most Albums.
- **Playlists**: Sort by Name or Date Created.
- **Albums / Tracks**: Sort by Date Added, Artist, Album, or Track.

### 1.5 Pagination & Transitions
- **Dynamic Slicing**: Lists are paginated at **35 items per page** to keep the memory footprint low.
- **Transitions**: Changing pages triggers a hardware-accelerated slide-in animation (**100ms** animation, **80ms** layout sleep).
- **Ghost Cards**: Clickable, glassmorphic cards ("Tap to load...") appear at list boundaries to navigate to adjacent pages. Horizontal swipe gestures are disabled to avoid conflicts.

---

## 2. Playback Taste Modeling & Play Similar

Controls that guide queue generation and recommendation behavior.

### 2.2 Play Similar & Continuous Playback Walks
Pure acoustic walks over the $k$-NN graph, decoupled from the taste model regressor for strict sonic consistency.
- **Centralized Manager**: Tapping *Play Similar* instantly toggles the mode, bypassing confirmations.
- **Visual Indicators**: Album artwork displays a **vibrant Cyan border** (`#00FFFF`) in the Mini Player and Now Playing card.
- **Shuffle Exclusivity**: Play Similar and Shuffle modes are mutually exclusive.
- **Non-Destructive Queue Backup**: Stores the original active queue when Play Similar is enabled and restores it dynamically when disabled, preserving active playback.
- **Continuous Replenishment**: Maintains an 8-song buffer ahead of the currently playing track. If the number of upcoming tracks in the queue drops below 4, the engine automatically walks and appends new similar tracks to replenish the buffer.
- **Anti-Skip Avoidance**: Skipping a song early appends it to a session-level skip avoidance list (`_session_bad_paths`).

---

## 3. Search Functionality

The **Search** tab (implemented in [search.py](file:///Users/chrismitsacopoulos/Desktop/Mai-An-Lab/StreamripApp/ui/views/search.py)) mimics the Library layout for remote query results (Qobuz).

### 3.1 Design & Pagination
- **Consistency**: Uses matching search bars, accents, Result Cards, and a slide-in animation.
- **Discrete Pagination**: Slices remote query results into **35 items per page** with identical chevrons and interactive ghost cards.
- **Dropdown Pagination**: Expanding nodes (artists or albums) loads nested children. Includes a *"Load More"* button inside the dropdown list.

### 3.2 Previews & Downloads
- **Previews**: Stream short previews. Displays buffering progress rings and play/stop indicators.
- **Downloads**: Download tracks/albums directly. Shows a cyan check icon if the item is already present in the local library.
- **HTTPS Connection Signal**: On search submission, a cellular-style connection signal next to the Qobuz subtitle progressively lights up as the connection goes through handshake phases (DNS $\rightarrow$ TCP $\rightarrow$ TLS $\rightarrow$ HTTP). Once successfully established, it turns green with full bars and remains visible to indicate an active online status. To conserve battery, this uses state changes only without continuous frame animations.
- **Connection Progress Card**: Concurrently, a temporary bottom-anchored card displays connection handshake steps (DNS Lookup, TCP Connect, TLS Handshake, HTTP Request, Data Streaming, Processing) and slides away once the connection is established.

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

## 6. Onboarding & First-Use

- **Empty States**: Library tabs show cyan placeholders instructing the user to index their library.
- **Setup Prompts**: If Qobuz credentials are missing, the Search tab is replaced by a setup card pointing the user to Settings.
- **Jarvis Alerts**: If asked to play music on an empty catalog, Jarvis verbally instructs: *"Your library is currently empty, sir. Please configure your music folder first."*

---

## 7. Settings & Customization Hub

The settings screen features a categorized menu linking to sub-panels:

### 7.1 Set-up
- **Authentication**: Credentials (User ID, Auth Token, password hash) for Qobuz.
- **Storage & Paths**: Directory configurations for index folders and download directories.

### 7.2 Customization
- **Appearance**: Adjust startup page, default library sort, toggle landing page stats, toggle visibility of specific library tabs, and choose custom UI accent colors.
- **APPLY VISUALS Button**: Appears conditionally at the bottom when visuals are changed and clears on save.
- **Audio & DSP**: 5-band Equalizer (60Hz, 230Hz, 910Hz, 4000Hz, 14000Hz) with gains $\in [-15\,\text{dB}, +15\,\text{dB}]$, preset management, and **Dynamism Enhancement (Dynamic Punchiness)**:
  - **Preset Filtering & UI Guarding**: Filter between `System Presets` (default profiles) and `Custom Presets` (user-saved configurations). Custom preset creation tools (naming text field and save button) and the list of custom preset cards are guarded and only visible when the custom mode is active.
  - **Keyboard-Editable Gains**: Slider values can be precisely adjusted using keyboard-editable decibel text fields, which automatically handle parsing (e.g. `+5.0 dB`), validate inputs, and clamp values to $[-15.0, 15.0]$ dB.
  - **Real-time Dynamism Monitor**: Displays the active decibel gain boost applied by the Dynamism feature dynamically (via a badge under the track title in Now Playing, and a status card in the Settings panel) when Dynamism is active.
  - **Spectral Contrast Normalization:**
    $$\text{norm\_contrast} = \max\left(0.0, \min\left(1.0, \frac{\text{spectral\_contrast} - 0.2}{0.2}\right)\right)$$
  - **Dynamism Score:**
    $$\text{score} = 0.4 \times \text{energy} + 0.3 \times \text{beat\_strength} + 0.3 \times \text{norm\_contrast}$$
  - **Overall Loudness Boost:**
    $$\text{gain\_db} = 1.0 + 3.0 \times \text{score}$$
  - **Psychoacoustic Contour (Applied only when Manual EQ is disabled):**
    $$\text{dyn\_offsets} = \text{score} \times [3.0, 1.5, 0.0, 1.0, 2.5]\text{ dB}$$
- **Haptic Feedback**: Modify vibration intensities (none, light, medium, heavy) for actions: EQ drag, swipe to queue, swipe to remove, and long press.

### 7.3 Developer & Advanced Tools
- **Permissions**: Request Android permissions (Notifications, Audio, Storage, Manage Storage, Microphone).
- **Database Management**: Wipes database tables (Index, DSP features, Taste weights, or Full DB wipe) and manual triggers to compute DSP features or recompute PCA space.
- **Advanced**: Configure recommendation temperature (softmax exploration divisor $\in [0.01, 0.20]$), clear caches (artwork, preview), edit raw TOML config directly, and debug play count population.
- **State Import / Export**:
  - *Auto-Import startup hook*: Scans downloads directory for `mai_an_lab_state_import.zip` on boot. Overwrites local database (safely deleting stale `-wal` and `-shm` transaction logs to prevent database corruption), updates graph edges (`build_metadata_edges` and `build_acoustic_edges`), rebuilds PCA coords (`optimize_pca_spacing`), and deletes zip.
  - *Manual Backup*: In-app manual import/export buttons to save/restore state.
