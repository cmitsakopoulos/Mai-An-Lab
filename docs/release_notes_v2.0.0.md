# Release Notes — Mai-An Lab v2.0.0

This release adds Deezer as a second source next to Qobuz, rebuilds auto-play as a resumable station with PAGA-style genre journeys, makes queue sync between Python and the Android player self-checking, keeps playback and long library jobs alive in the background, introduces an interactive split network canvas, adds infinite scroll, and replaces the metadata wizard with a multi-source workbench.

> [!IMPORTANT]
> The bundled `flet_audio_service` extension moved to **v0.2.21**. Build with `fresh_build_android.sh` (or `.ps1`); an incremental build keeps the old Dart and Kotlin code.

## New Features & Key Highlights

### Deezer Support
* **Second Source**: Search, preview and download from Deezer alongside Qobuz. A Qobuz/Deezer switch sits above the search results, each source keeps its own results when you flip between them, and rows carry a source badge.
* **Sign-In**: Deezer uses the ARL cookie from a logged-in browser session (Settings → Account, or the set-up wizard). You only need credentials for one source; search opens on whichever is set up.
* **Plan-Aware Quality**: If your Deezer plan can't serve the quality you pick, the download drops to the best it can (FLAC → 320 kbps MP3 → 128 kbps MP3) and is saved with the right extension. Turn off `lower_quality_if_not_available` in the config to get an error instead.
* **Decryption While Downloading**: Deezer's encrypted CDN streams are Blowfish-decrypted as they arrive instead of being buffered whole, so a large FLAC can't run an Android phone out of memory.
* **Same Tags Either Way**: Deezer metadata is normalised to the same tag layout as Qobuz (album artist, disc numbers, cover art), so downloads from either source look the same in the library.
* **Optional Dependency**: `deezer-py` and `pycryptodomex` are imported defensively. A build without them still starts and still does Qobuz; Deezer reports what is missing.

### Search & Downloads
* **Download Dock**: A floating dock and queue for downloads, with speed, per-item quality badges, retry and auto-collapse.
* **Multi-Select**: Long-press a result to select several albums or tracks and download them at one quality in one go.
* **Default Quality**: Settings → Storage → Default Download Quality skips the quality sheet entirely.
* **Honest Errors**: A download where no track resolves now says why (for example, a plan without the requested quality) instead of only "No tracks could be resolved".
* **Never Into the App Sandbox**: Library and download folders can no longer resolve to app-private storage that no files app can open on Android 11+. The defaults point at the public Music folder, downloads follow the library folder when their own is unusable, and the folder picker rejects private locations.

### Auto-Play Station
* **Resumable Station**: Auto-play now continues one station instead of re-walking from scratch on every refill. A station never repeats a track it already queued.
* **Deterministic or Random**: The auto-play sheet (tap the chain icon in Now Playing; long-press toggles auto-play) chooses between a repeatable station and a seeded random one.
* **Genre Journeys**: Stations travel in legs inside a genre and hop between genres through artists the two genres share (bridge-artist lift). Hops never cross cultures (for example, from the Greek scene into Western pop).
* **Learns From Skips**: Early skips and removals end a leg early; a hop you keep rejecting gets blocked. Hidden tracks and blocked hops can be reviewed and restored in Settings → Auto-play.
* **Spotify-Style Queue**: The queue reads as *playing → your up-next picks → station → held queue*. Play Next and Add to Queue land ahead of the station, and turning auto-play off drops the station so your own queue plays next.

### Background Playback & Notifications
* **Playback Stays Alive**: Pausing no longer drops the media service, so playback survives transient audio-focus loss and backgrounding. The service releases itself after 10 minutes of silence.
* **Album Art in the Media Notification**: Embedded artwork is read natively (sidecar `cover.jpg` as a fallback) and keeps working while the UI is closed.
* **Library Task Service**: Downloads, metadata sync and track analysis run under a foreground service with a progress notification, so they keep going with the screen off.
* **Accurate Play Counts**: Listens are recorded by the native player in a ledger and counted when you hear at least 30 s or half the track, so auto-play and background listening finally count, and skips are recorded as skips.
* **Resume Where You Left Off**: The native player records the current track and position, and a cold start resumes from whichever record is newer.

### Metadata Workbench
* **One Surface**: The five-step enrichment wizard is replaced by a workbench at Settings → Library Intelligence → Metadata, with coverage, an inline sync, review of uncertain matches and scene-wide batch fixes.
* **Multiple Sources**: Artist metadata now cascades MusicBrainz → the artist's own file tags → Qobuz, with each field showing where it came from.
* **Edits Reach Auto-Play**: Saving metadata now rebuilds the genre models auto-play walks on; before, edits only changed what was displayed.
* **Retries Without Re-Fetching Forever**: Failed lookups retry, alternating between MusicBrainz and Qobuz, and park after three failures until a manual sync. Going offline costs no attempts. New downloads are enriched after each library scan.

### Interactive Split Network Canvas
* **Split Layout**: Replaces the floating single-node inspector with a split view: force-directed graph on top and a scrollable ordered track list below.
* **GPU Pan and Zoom**: Uses native `InteractiveViewer` transforms for smooth canvas navigation without per-frame Python or bridge overhead.
* **Ordered Enqueue**: Enqueuing from network view preserves trajectory order, supporting sequential playback or batch appending.
* **Collapsible Header**: A chevron toggle collapses or expands the graph to maximize track list visibility.

### Infinite Scroll
* **Continuous Viewport Loading**: Replaced fixed 35-item page slicing and ghost cards with smooth infinite scroll across all Library views and remote Search results.

### Set-Up Wizard
* **First-Run Guide**: A multi-step onboarding wizard introduces the app, takes Qobuz and/or Deezer credentials, picks the music folder (with the in-app browser on Android) and sets the theme accent.

### Jarvis LLM Conversational Agent
* **Hybrid Assistant**: Combines offline, deterministic regex intent matching with conversational LLM tool execution for natural-language library queries, playback management, and catalog searches.

## Bug Fixes & Refactoring
* **Queue Sync Self-Check**: The Android player now reports which track it is actually on and how long its queue is. Python checks both and re-pushes the queue when they disagree, fixing the rare "wrong song or cover shown" mismatch. All queue edits go through one ordered pipeline, so removing several rows at once can no longer delete the wrong ones.
* **Queue State Protection**: Introduced `_QUEUE_STATE["held"]` tracking to ensure session teardown does not wipe the persisted queue snapshot before a restore can occur.
* **Native Operation Timeouts**: Bounded native operations at 30 seconds (`_SEND_TIMEOUT_S`) with UUID operation registration to prevent deadlocks when Android tears down sessions mid-dispatch.
* **Session Persistence**: Fixed a lost queue and a silent auto-play after Android killed or recycled the UI. A command sent to a closed UI session could block every later queue change, auto-play could stay convinced a refill was still running, a restored queue never topped up its auto-play tracks, and the saved queue could be deleted before it was loaded.
* **Battery**: The position save writes a 150-byte file instead of the whole queue every 10 s; the position ticker runs at 1 Hz and only while the app is visible; the network view's animation stops when it is off-screen; the mini player reuses the natively resolved artwork.
* **Search Errors**: Connection and credential failures show an inline error card instead of "No results found".
* **Deezer Previews**: Deezer previews skip the stream attempt (its CDN audio is encrypted and can't be streamed) and go straight to the decrypting download, saving about 7 seconds.
* **Genre Taxonomy**: Tags written without separators (`newwave`, `triphop`) are recognised, and artists spanning several genres act as bridges instead of being forced into one.
* **Semantic Intent Retirement**: Retired the legacy WordPiece BERT tokenizer; replaced with whole-word matching and Levenshtein distance for settings search.
* **Swipe-to-Delete Removed**: Queue rows are removed with the remove button only; drag to reorder is unchanged.

## Known Limitations
* **Deezer short links**: `deezer.page.link` share links are not resolved.
* **No Deezer without an account**: an ARL is always required; streamrip's `deezloader` free mode is not supported.
* **Hi-Res on Deezer**: Deezer's highest quality is 16-bit FLAC, so its Hi-Res option downloads CD-quality FLAC.
* **Jarvis searches Qobuz only**: Jarvis voice search and LLM tools do not query or download from Deezer.
* **Metadata Workbench is database-only**: Curating artists in the Workbench writes to the SQLite database index (`artist_enrichment`) to power auto-play and search; it does not write tags to physical audio files on disk.
* **Auto-Play station state is ephemeral**: Station traversal state and hop progression reside in process memory. If Android terminates the process while suspended, the playback position and current queue restore, but the station traversal resets.
* **Sparse library genre hops**: Auto-play genre transitions require bridge artists in the library; collections under ~100 tracks or with siloed genres without crossover artists cannot hop between genres and will cycle or end within the initial genre.
* **On-device DSP extraction**: Pure-NumPy acoustic feature extraction takes ~10–12 seconds per track on mobile CPUs. The host ADB offloader (`tools/dsp_offload.py`) is recommended for libraries larger than 50 tracks.
* **Listen in progress**: If Android kills the app mid-track, that track's listen is not recorded.
