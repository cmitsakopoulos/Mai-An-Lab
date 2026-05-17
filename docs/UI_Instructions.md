# UI Instructions: Library & Search

This document provides factual instructions for using the Mai-An Lab user interface, detailing the implementation of the Library and Search modules.

## 1. Library Management

The Library view (accessible via the **Library** tab) serves as the primary interface for managing and playing locally indexed music.

### 1.1 Indexing Music
- **Initial State**: If the database is empty, a prompt appears: "Your music library is currently empty. Index your folders to start listening."
- **Scan Button**: The **INDEX LIBRARY** button (or the Refresh icon in the top-right) triggers a recursive scan of the configured music folders.
- **Progress Tracking**: A progress bar and status label appear at the top of the Library view during an active scan.

### 1.2 View Modes & Navigation
The Library supports four distinct view modes, accessible via tabs at the top:
- **Playlists**: Lists user-created and imported playlists.
- **Artists**: Lists all unique artists. Expanding an artist reveals their albums.
- **Albums**: Lists all albums. Expanding an album reveals its tracks.
- **Tracks**: A flat list of all indexed tracks.

### 1.3 Searching & Filtering
- **Real-time Filtering**: The search bar at the top of the Library view filters the current list in real-time.
- **Debounced Input**: Search queries are debounced by **300ms** to ensure UI responsiveness on large libraries.
- **Clear Search**: A close icon ('X') appears when text is present, allowing for instant clearing of the filter.
- **Automatic Collapse**: When the search query changes, all expanded nodes (Artists/Albums) are automatically collapsed to maintain a clean view.

### 1.4 Sorting
The Sort menu (accessible via the Sort icon) provides context-aware options:
- **Artists**: Sort by Name (A–Z), Most Tracks, or Most Albums.
- **Playlists**: Sort by Name (A–Z) or Date Created.
- **Albums/Tracks**: Sort by Date Added, Artist (A–Z), Album (A–Z), or Track (A–Z).

---

## 2. Search Functionality

The Search view (accessible via the **Search** tab) mimics the Library's layout to provide a consistent user experience while browsing remote sources (e.g., Qobuz).

### 2.1 Design Consistency
The Search view intentionally replicates the Library's aesthetic:
- **Unified Search Bar**: The search field uses the same visual container, colors, and behavior as the Library search.
- **Tabbed Results**: Results are grouped into **Tracks**, **Albums**, and **Artists** tabs, matching the Library's organization.
- **Result Cards**: ListTile items use the same typography, spacing, and icon sets as Library items.

### 2.2 Source Integration & High-Performance Page Pagination
- **Default Source**: Qobuz is currently the primary search provider.
- **Scroll Boundary Pagination**: Removed all rigid per-instance limits. Results are structured into dynamic, paginated lists. Scrolling past the bottom boundary automatically loads the next page, while scrolling to the top boundary transitions back to the previous page.
- **Slide-In Animation**: To deliver a premium tactile feel, swapping pages instantly teleports the scroll position off-screen and glides the new page into view using a hardware-accelerated **Slide-In Offset Animation**.
- **Tree-Containment Performance**: By rendering only one active page at a time (typically 20 items) rather than appending thousands of search cards, the Flet widget tree remains tiny, completely preventing memory leaks, layout lag, or battery drain.
- **Safe Ceiling Protection**: A general max results ceiling is actively enforced under the hood to secure local memory, preventing the DOM from choking during extremely deep searches.
- **Credentials**: If Qobuz credentials are missing, a "Setup Required" prompt directs the user to Settings.
- **Search History**: Recent searches are stored in `recent_searches.json` and can be accessed via a history sheet.

### 2.3 Audio Previews & Downloads
- **Previews**: Tracks in search results can be previewed before downloading. A "Progress Ring" appears during buffering, followed by a "Stop" icon during playback.
- **Download Button**: A download icon (visible on track and album cards) adds the selection to the global download queue.
- **In Library Awareness**: If a search result is already present in the local library, the download icon is replaced by a **cyan check circle**, and the download action is disabled.

---

## 3. Global UI Conventions

### 3.1 Media Type Color Coding
The UI uses a consistent color palette to differentiate media types:
- **Artist**: `LIB_ARTIST_COLOR` (Violet/Purple accents)
- **Album**: `LIB_ALBUM_COLOR` (Indigo/Blue accents)
- **Track**: `LIB_TRACK_COLOR` (Teal/Cyan accents)
- **Playlist**: `LIB_PLAYLIST_COLOR` (Amber/Orange accents)

### 3.2 Playback Indicators
- **Active Track Highlight**: The currently playing track is highlighted with a subtle background color (`0.12` opacity of the media type accent).
- **Icon Mutation**: The leading icon on a track changes from a "Play" outline to a "Pause" or "Playing" indicator when active.

### 3.3 Download Progress
- **Progress Card**: When a download is active, a persistent card appears at the bottom of the Search view.
- **Status Updates**: Displays the current job status (e.g., "Downloading...", "Converting..."), percentage, and metadata.
- **Queue Management**: Users can cancel the current job or clear the pending queue directly from this card.

---

## 4. Gestures & Context Menus

The Mai-An Lab interface utilizes gestures to streamline playback control and metadata management.

### 4.1 Track Gestures (Library)
Interactions with tracks in the Library view support advanced gestures:
- **Swipe Right**: Swiping from left-to-right on any track row immediately adds that track to the **"Next Up"** position in the queue. Visual feedback includes a Cyan background and a "Next Up" icon.
- **Long Press**: Pressing and holding a track row opens the **Track Context Menu**, providing several advanced options:
    - **Play Next**: Queues the track to play after the current song.
    - **Add to Queue**: Appends the track to the end of the global queue.
    - **Add to Playlist**: Opens a selector to add the track to a custom playlist.
    - **Edit Metadata**: Launches the metadata editor for manual tag adjustments. Writes updates directly to the file header and immediately propagates new values across the database index.
    - **Delete Song**: Removes the audio file permanently from local storage and cleanses the track row and relationships instantly from the database and active playlists.
    - **Redownload**: Triggers a remote search (Qobuz) to find and download a replacement or higher-quality version of the track. Once downloaded, the library engine automatically performs an atomic index rescan in the background to seamlessly integrate the replacement.

### 4.2 Player Gestures (Now Playing)
The full-screen player supports intuitive touch controls for navigation:
- **Artwork Swipe**:
    - **Swipe Left**: Skips to the **Next** track in the queue.
    - **Swipe Right**: Returns to the **Previous** track.
- **Artwork Tap**: Toggles between **Play** and **Pause**. A transient overlay icon confirms the action.
- **Sheet Dismissal**: The entire player sheet is **draggable**, allowing users to swipe down from anywhere on the background to collapse the player.

### 4.3 Playlist Management
- **In-Place Reordering**: Within a playlist, tracks feature **Up/Down arrows** for optimistic reordering.
- **Quick Removal**: The **Minus icon** allows for instant removal of a track from the playlist without rebuilding the entire library list.

### 4.4 Gesture-Driven Push-to-Talk (PTT) Voice System
Jarvis integrates advanced gesture-based push-to-talk microphone interactions to deliver a tactile and conversational experience:
- **Press & Hold (Tap Down)**:
  * Triggers the microphone voice input engine.
  * The mic icon immediately turns **vibrant red** (`#FF4444`) with the tooltip changing to *"Release to Send"*.
  * A pulsating, animated *"Listening, sir..."* bubble is appended at the bottom of the chat list, keeping active focus on current input.
- **Release (Tap Up / Cancel)**:
  * Instantly halts the voice capturing engine.
  * Automatically finishes the Speech-to-Text session, packages the transcribed phrase, submits it to Jarvis, and resets the icon back to a sleek, passive cyan.
  * Ensures zero raw hardware delays, letting you speak naturally and stop dictation instantly by letting go of the control.
- **Scroll Tracking**:
  * Chat lists leverage native GPU-accelerated scrolling. The viewport smoothly tracks and slides down for new messages *only* if the user is currently at the bottom.
  * If browsing history, new updates append silently in the background, preserving historical reading positions without aggressive snapping.

