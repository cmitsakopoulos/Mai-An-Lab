# Mai An Lab: Flet → Flutter port map

*Status: 2026-11-24. Branch `flutter-port` (on origin up to phase 3g; the
Settings, Network and phase 4a commits are local). Phase 0 (backbone)
done. Phase 1 (phone player) is written and audited against the Flet sources
(§9). Phase 2 (stations) is written and matches the Python output exactly on
a real library (§4, phase 2). Phase 3's code is complete: the Genres, Artists and
Albums views, the migrations the port now owns, the play-ledger drain, the
library scan (with tinytag ported to Dart), playlists, the full
long-press menu, selection mode and Delete Track, the tab bar with
the Search tab's local landing cards, Settings with the config.toml
writer, and the Acoustic Network view (§4, phase 3). Phase 4 (sync,
Android side) is under way: the Companion protocol and discovery, tested
against Flet's own server, notifications, the sync manager, and the
download queue and dock that bring the Mac's transfers onto the phone
(§4, phase 4). Desktop
tests pass (`flutter test`: 479, including the real-library golden tests
that need the gitignored goldens; analyzer clean in Dart's strict modes);
debug and release APKs build.
It is installed on the phone (2026-11-24, with the 2026-11-02 library, an
older build: no scan, no Settings yet). The user reports problems found in use; fix
them, or record intended differences in §7. Nothing ships from this port yet. 41 of the 90 Flet features in §11
are ported. Third-party code the port uses or copies is listed in §12.*

**Next session: start at §10 (open items). §11 lists every Flet feature and
where it stands.** To put the port on the phone: `tool/install_phone.sh`
(§8).

The Flet app (`StreamripApp/`) is the reference. It is tested (16.9k lines of
tests) and it ships. Every behaviour here is checked against it, not against a
spec. Where the port deliberately differs, §7 records the difference.

**Size of the job**

| Area | Python (lines) |
|---|---|
| `main.py` | 3,287 |
| `utils/` | 23,922 |
| `utils/sync/` | 3,265 |
| `ui/` (views, player, widgets, tokens) | 19,970 |
| **App total** | **~50,400** |
| Tests | 16,879 |
| `flet_audio_service` extension, Dart (already Flutter-native) | 1,878 |
| `flet_audio_service` extension, Kotlin (already Android-native) | 1,283 |

The `flet_audio_service` extension changes the estimate. Its Dart and Kotlin
already run inside Flutter today, so most of the Android OS layer has been
written and tested. The port reuses it rather than rewriting it.

---

## 0. Ground rules

1. **The Flet app ships until cutover (§5).** The port lives in
   `mai_an_flutter/` on `flutter-port`; master stays Python-only. The latest
   Flet code on master is the ground truth: Flet changes are committed on
   master, then master is merged into `flutter-port` (never the other way
   until cutover). At cutover the branches swap roles: `flutter-port`
   becomes master, and the Flet code continues on a `flet` branch (the Mac
   keeps running it until phase 8, and it stays the reference for anything
   not yet ported).
2. **The data contract is the interface (§2).** Both apps read the same
   `library.db`. Python owned the schema, migrations and every write until
   phase 3. Now the port migrates and writes its own copy exactly as
   db_manager does (`data/library_schema.dart`, golden-tested); it opens any
   other database (a `LIBRARY_DB` override, the test goldens) read-only.
3. **Parity is proved with Python's own outputs.** A test pins reference values
   that the Python function printed. `track_query_test.dart` already does this
   for `_kmer_similarity` and `_safe_fts`. Larger behaviours (stations, intents,
   search ranking) get golden JSON exported by a small Python script from a
   fixed `state_backups` bundle.
4. **Settings, animations and polish are not phases.** Each phase builds the
   settings rows and transitions for its own screens. The Flet UI's motion
   inventory is about 70 sites in total: 34 `ft.Animation`, 25 `animate_*`,
   3 `AnimatedSwitcher`, 1 `Shimmer`. Each maps one-to-one onto a Flutter
   implicit animation.

---

## 1. Android: how the app sits on the OS

"Flet today" means what the shipping app does and has been tested to do.
"Port" is the plan.

Status key:
- ✅ done in the port
- 🧪 written and built, not yet verified on the phone
- ♻️ reuse existing Dart or Kotlin
- 🔨 to build
- ⚠️ decision or risk

| # | Concern | Flet today | Port | Phase | |
|---|---|---|---|---|---|
| 1 | **Package identity** | `com.mitsakopoulos.maianlab.mai_an_lab`, signed with `~/.android/debug.keystore`, versionCode 1 | Same base ID, plus `applicationIdSuffix ".port"` until cutover. Without the suffix, `flutter run` installs over the Flet app, or uninstalls it and wipes its data. | 0 | ✅ |
| 2 | **Private data dir** | `FLET_APP_STORAGE_DATA` = `<appSupport>/data` = `/data/user/0/<pkg>/files/data`. Python also points HOME at it. | Same layout: `getApplicationSupportDirectory()/data`. `library.db` is there now. Other files move over in their phases (§2). | 0 | ✅ |
| 3 | **Process model** | The Python process outlives the Flet UI session. The engine, `_AUTOPLAY`, `_QUEUE_STATE` and `_ENRICH_STATE` are per-process, and a new `StreamripFletApp` attaches to them. | One Dart isolate. `MainActivity` extends `AudioServiceActivity`, so the engine survives the activity being destroyed. State lives in controllers above the widget tree, which removes the session/process split. | 1 | 🧪 |
| 4 | **Background playback** | `audio_service` `AudioService` (foreground type `mediaPlayback`), `MediaButtonReceiver`, `MediaBrowserService`. `AudioPlayerHandler` lives in the extension's Dart. | Lifted into `lib/player/audio_handler.dart`, Flet bridge removed. The epoch/ack protocol shrank to an in-process op chain in `PlayerController` (ordered ops, push supersedes, index adopted only when the row matches, rate-limited re-push on divergence). | 1 | 🧪 |
| 5 | **Audio focus, interruptions, headphones unplugged** | `audio_session` `.music()`, interruption stream, user play/pause supersedes a pending interruption | Comes with #4 | 1 | 🧪 |
| 6 | **Resume after process death** | `native_resume.json` (`setResumePath`), `queue_state.json`, `queue_pos.json` snapshotted every few seconds | Same files and the same JSON (`lib/player/queue_store.dart`), so a cutover install resumes the user's queue. `_choose_resume` ported with its tests. | 1 | 🧪 |
| 7 | **Notifications** | `POST_NOTIFICATIONS` requested at runtime (Android 13+). Kotlin `ProgressNotifications` and alert notifications. Tapping one navigates via `takePendingNavigate`. | Keep the Kotlin (`packages/maian_native`). Playback-error cards work. Job progress (`core/progress_notify.dart`) and tap routing (`core/notification_router.dart`) since phase 4b; each target screen registers when it is ported. | 1–4 | 🧪 |
| 8 | **Long tasks with the screen off** | Kotlin `LibraryTaskService`, foreground type `dataSync`. The first job starts it and the last one stops it. | Keep the Kotlin. ⚠️ Android 15+ caps `dataSync` foreground services at about 6 h per 24 h. Long offloads or downloads must handle the timeout callback. Verify on device. | 3–4 | ♻️⚠️ |
| 9 | **Reading music files** | `MANAGE_EXTERNAL_STORAGE` (all-files access) + `READ_MEDIA_AUDIO` + legacy storage permissions. Plain file paths under `/storage/emulated/0/Music`. | Same permissions via `permission_handler`, requested before the player starts (as Flet did), minus RECORD_AUDIO until phase 5. ⚠️ Play Store policy rejects all-files access, so sideload only, as now. | 1 | 🧪 |
| 10 | **Music folder rules** | `storage_paths.py`: never return an app-private folder, downloads follow the library folder, fallbacks are never written back | `scan/storage_paths.dart` (resolvers and `is_app_private`; the platform and sandbox roots are passed in) and `scan/library_folder.dart` (`sync_config_to_ui`'s inputs: `library_path` pref, `[downloads] folder`). Python's tests ported (`storage_paths_test.dart`); `pick_download_dir` comes with downloads (6). | 3 | 🧪 |
| 11 | **Path identity** | Absolute, platform-specific paths: phone `/storage/emulated/0/Music/…`, Mac `/Users/…/Desktop/Music/…`. `play_counts.track_path` is the join key. `normalize_nfc` is applied only where a file is *named* by the app: downloads (`streamrip_api`, `queue_controller`) and files received by the sync server. | The scanner stores `os.scandir` paths unnormalised, and so does the port (golden-tested), so phase 3 needs no normaliser. Mac transfers do (phase 4d): `core/nfc.dart` is `unicodedata.normalize("NFC")` from tables generated out of Python's unicodedata (`tool/gen_nfc_table.py`), equal to Python on 6,142 strings; downloads (6) reuse it. Search already strips NFD combining marks (`fold`). | 4, 6 | 🧪 |
| 12 | **Artwork** | Kotlin `resolveArtwork`: embedded art via `MediaMetadataRetriever`, then the sidecar file, cached by sha1 | Keep the Kotlin. The handler resolves it for the current and next rows; sidecar lookup is lazy (per folder, not per queue row). | 1 | 🧪 |
| 13 | **Audio analysis** | Kotlin `decodePcm` (MediaCodec, to mono PCM) + `dsp.py` (numpy, 906 lines). Usually offloaded to the Mac (sync). | Keep the decoder. The feature maths becomes Dart, or never runs on the phone (§6, decision B). | 6 | ⚠️ |
| 14 | **EQ and loudness boost** | `set_eq_band_gain` and `set_loudness_boost` in the extension (just_audio Android effects). `reapply_dsp` runs on every track change: config.toml `[dsp]` preset + Dynamism (boost and EQ contour from the track's play_counts features). | In the handler (`setEqBandGain`, `setLoudnessBoost`). `player/dsp.dart` ports `reapply_dsp` with Python reference values; the controller applies it on every row change, including background auto-advance. Now Playing shows the Dynamism badge. Gains set before the device's EQ bands load are applied when they arrive. No settings UI yet (the Flet-written config is honoured). | 1 | 🧪 |
| 15 | **Voice** | `flutter_tts`, `speech_to_text`, `RECORD_AUDIO`, `<queries>` for `RecognitionService` | Same plugins | 5 | ♻️ |
| 16 | **Plain HTTP to the Mac** | `usesCleartextTraffic="true"` app-wide | Nothing to declare: dart:io sockets don't go through Android's cleartext policy (this SDK's dart:io has no such check). INTERNET is declared. Confirm on the phone with the first real pairing. | 4 | 🧪 |
| 17 | **LAN discovery** | UDP query every 2 s on port 54545, answered by unicast; beacons dropped (no multicast lock: pyjnius isn't in the build) | `sync/discovery.dart` (`RawDatagramSocket`), with `MainActivity`'s `acquireMulticastLock` and `defaultRouteIpv4` (§4, phase 4a). ⚠️ Recheck local-network permission rules before raising targetSdk past 36. | 4 | 🧪⚠️ |
| 18 | **Lifecycle** | `on_app_lifecycle_state_change`: suspend UI updates in the background, prefill the autoplay buffer on hide, run assistant and companion background hooks, pick up listens finished in the background | `AppLifecycleListener`. Flutter stops drawing frames by itself. Done: the position ticker stops, the position is saved and the auto-play buffer is topped up to 24 on hide. Listens reach auto-play in-process (§7), so there is no catch-up to do for it. The ledger drain runs on show, at start and 3 s after each listen (phase 3, 🧪). Still to come: assistant and companion hooks (4–5). | 1–4 | 🔨 |
| 19 | **Edge-to-edge, predictive back** | Flet/Flutter defaults | targetSdk 36 forces both. Back on the root screen calls `moveTaskToBack` (MainActivity channel `mai_an/app`); predictive back is enabled. Still to audit: edge-to-edge insets in the sheets. | 1 | 🧪 |
| 20 | **16 KB memory pages** (Android 15+ devices) | Flet build | ✅ Checked: every `.so` in the port's APK has 16 KB LOAD alignment, and the zip is 16 KB-aligned | 0 | ✅ |
| 21 | **Haptics, links, clipboard, file picker** | `HapticFeedback` (about 15 call sites, configurable intensity), `UrlLauncher`, `FilePicker` (1) | `core/haptics.dart` reads `[haptics]` (on/off, per-action intensity, main.py's defaults); used by the track menu (long_press, swipe_queue): 🧪. `url_launcher`, `file_selector` when needed: 🔨. | as needed | 🧪🔨 |
| 22 | **APK size** | CPython + site-packages (about 72 MB of site-packages before trimming) | Release, arm64 only: 24.7 MB (16 KB-aligned). Debug, all ABIs: 171 MB. Release builds skip lint (`checkReleaseBuilds = false`): lint crashes on the symlinked Kotlin in `maian_native` (§8); restore it when that directory moves in. Release (R8) not yet run on the phone. | 1 | 🧪 |
| 23 | **macOS** | Same Python codebase | No Xcode on this Mac (15.7.3). Build in CI or after an Xcode install. The Mac keeps the Flet app until phase 8. | 8 | ⚠️ |

---

## 2. Data contract

Everything sits in the private data dir (`<appSupport>/data`) unless stated.
The port reuses each file's exact format, so a cutover install inherits it.

| File | Owner today | Port reads | Port writes |
|---|---|---|---|
| `library.db` (main database; schema below) | `db_manager.py` | phase 0 ✅ | phase 3: migrations and the ledger drain ✅ (desktop and phone) |
| `.config/streamrip/config.toml` (Android; on the Mac it is under `Library/Application Support/streamrip/`). Holds `general.library_sort`, `appearance.accent_color`, `[dsp]`, `[haptics]`, `[downloads] folder` and credentials. | `config.py`, `streamrip_api.update_config_params` | phase 1 ✅ (sort, accent, `[dsp]`, `[haptics]`: `core/app_config.dart`); 3 ✅ (`[downloads] folder`, `[landing]`, `appearance.show_*`) | 3 ✅: created from the template when missing, and Settings' writes, byte for byte as tomlkit 0.15.2 leaves them (`core/config_file.dart`, golden-tested); the rest of the keys with the settings that own them |
| `queue_state.json`, `queue_pos.json`, `native_resume.json` | main.py, engine | 1 ✅ | 1 ✅ (`autoplay_held`, the queue auto-play set aside: 2 ✅) |
| `.cache/queue_regular.json`, `.cache/queue_shuffle.json` (the queue set aside while shuffling the library) | main.py | 1 ✅ | 1 ✅ |
| `flet_prefs.json` (`_save_pref`; shuffle, repeat, similar-play prefs) | main.py | 1 ✅ | 1–2 ✅ (`is_shuffle`, `repeat_mode`, `play_similar_mode`; merge-write as in Flet) |
| `play_ledger.jsonl` (one line per finished listen, written by the audio handler) | handler; drained by `play_ledger.py` | 3 (drain) | 1 ✅ (same format and ids). Drained into `play_counts` / `playback_history` by `data/play_ledger.dart` (3 ✅, on the phone too). |
| `companion_transfers.json` | sync | 4 | 4 |
| `.onboarded` | onboarding | 7 | 7 |
| `recent_searches.json` | search | 4 | 4 |
| `downloads.db`, `failed_downloads.db` | streamrip layer | 6 | 6 |
| `temp/` (artwork cache, `.nomedia`) | `filepath_utils` | not needed ✅ | not needed ✅ (on Android Flet already used the Kotlin cache, `cacheDir/np_art`, which the port shares) |
| `mai_an_lab_state_*.zip` (export bundle, written to `/sdcard/Download`) | `state_export.py` | dev tool ✅ (`tool/push_db.sh`) | 4 |

**`library.db` schema version.** `PRAGMA user_version` holds
`GEOMETRY_VERSION` (currently 2). When the stored value is older, Python clears
every `pca_coords`. The port refuses to walk older coordinates for the same
reason (`StaleGeometryException`). Raise `kGeometryVersion` whenever the Python
constant moves.

**Tables, and the phase that first needs each:**

| Phase | Tables |
|---|---|
| 0 | `artists`, `albums`, `tracks`, `play_counts` (coordinates) |
| 1 | `play_counts` (DSP features: `energy`, `beat_strength`, `spectral_contrast`) ✅ |
| 2 | `artist_enrichment` (country, genres, source), `genre_affinity` (the NPMI model), `play_counts` (`bpm`, `energy`, `timbre`/`features_version` for "is this seed analysed"), `playback_history` (recent plays seed auto-play's history on a cold start). All read-only. ✅ |
| 3 | `playback_history`, `play_counts` (counts), written by the ledger drain |
| 3 | `track_neighbors` ✅: read by the Network view (`data/network_queries.dart`); its artist tier written by `build_metadata_edges` (ported, golden-tested; called where Flet calls it, phases 4 and B). The assistant reads it too (5); stations don't. |
| 3 | `playlists`, `playlist_tracks` ✅ (`data/playlists.dart`), `fts_search`. FTS5 is kept in step by 11 triggers stored in the database, so port writes stay consistent for free. |
| 5 | `artist_ai_research` |
| 6 | `artist_releases`, `release_sync_log` |
| 7 | `metadata_audit_dismissed`, `artist_aliases`, `artist_alias_rejections` |

`track_partitions` is only ever cleared (main.py's graph reset); nothing the
port needs reads it. `autoplay_hidden` no longer exists (Python drops it).

---

## 3. Inventory: each Flet module and what happens to it

Verdicts:
- **Reuse:** the code already runs in Flutter or Android.
- **Port:** a line-by-line translation, with parity tests.
- **Rewrite:** same behaviour, Flutter-shaped code.
- **Keep on Mac:** stays Python.
- **Drop:** not carried over.

| Flet module | Lines | Verdict | Phase |
|---|---|---|---|
| `flet_audio_service` (Dart + Kotlin) | 3,161 | Reuse (strip the Flet bridge) ✅ (`player/audio_handler.dart`, `packages/maian_native`; phone 🧪) | 1 |
| `audio_engine.py` (Android path) | 1,463 | Rewrite: thin controller over #4 ✅ (`player/player_controller.dart`; `reapply_dsp` in `player/dsp.dart`; phone 🧪) | 1 |
| `queue_layout.py` + the queue half of `audio_engine.py` | ~500 | Port ✅ (`player/play_queue.dart`, `queue_entry.dart`) | 1 |
| main.py playback: `_play_track_core`, shuffle toggle, `cycle_repeat`, queue/position saves, restore, `_save_pref` | ~700 | Port ✅ (`player/player_controller.dart`, `queue_store.dart`) | 1 |
| `play_ledger.py` (drain into the database) | 126 | Port ✅ (`data/play_ledger.dart`, with `record_listens`; golden-tested; runs on the phone) | 3 |
| `queue_controller.py` (the DOWNLOAD queue, despite the name), `history_manager.py` (download history) | ~530 | Port. The queue ✅ (`downloads/download_queue.dart`, phase 4d; its tests ported); the source workflows and `history_manager` with downloads | 4, 6 |
| `ui/player/now_playing`, `mini_player`, `queue_sheet` | 1,087 | Rewrite ✅ (`player/ui/`). The auto-play chain is shown disabled until phase 2. | 1 |
| `ui/player/quality_selector` (download quality) | 168 | Rewrite | 6 |
| `db_manager.py`, read half | ~2,000 | Port, one method at a time on demand | 0–3 |
| `db_manager.py`, write half and migrations | ~2,000 | Port. Migrations ✅ (`data/library_schema.dart`: `get_connection` + `initialize`, golden-tested on fresh, legacy and real databases); `record_listens` ✅; the scan's writes ✅; playlists ✅ (`data/playlists.dart`, golden-tested); the rest with its features | 3 |
| `ui/views/library.py` | 4,915 | Rewrite, one tab at a time (tracks ✅ rows, sort, search; view bar, genres, artists, albums ✅ (`library/browse_*`, `genre_colors.dart`); playlists, the long-press menu, selection mode ✅ (`library/track_menu.dart`, `library_dialogs.dart`, `selection.dart`); the Acoustic Network ✅ (`library/network_*`)) | 1–3 |
| `track_graph.py` (stations: coordinate graph, artist-graph cache, `Station`, `open_station`, `walk`), `artist_graph.py`, `autoplay.py`, `genre_taxonomy.py`, `credit_keys` (in `metadata_enrich`) | ~2,100 | Port ✅ (`stations/`, `player/autoplay.dart`, `core/py_text.dart`; golden-tested) | 2 |
| `track_graph.py` builders (`build_acoustic_edges`, `build_genre_affinity`, `build_metadata_edges`, `bulk_analyze_library`), `genre_similarity.py` (builds the NPMI model) | ~500 | Port where Flet calls them (§10 4f): `build_metadata_edges` ✅ (`data/network_queries.dart`, golden-tested; no caller yet: Flet runs it after an import, the readiness build, analysis and Settings' recompute, i.e. phases 4 and B), the acoustic build with offload/import (4), analysis with decision B | 3, 4, 6 |
| `harmonic.py` | 132 | Not needed by stations (key was removed from the geometry); port only if a feature needs it | n/a |
| `ui/player/autoplay_sheet.py` | 156 | Rewrite ✅ (`player/ui/autoplay_sheet.dart`) | 2 |
| `library_scanner.py`, `storage_paths.py` + `tinytag` 2.3.2 (the tag reader the APK bundles, ~2,500 lines, MIT) | ~2,900 | Port ✅ (`scan/library_scanner.dart`, `scan/tinytag.dart`, `scan/storage_paths.dart`, `data/scan_writes.dart`; golden-tested on a synthetic corpus and the Mac's music folder) | 3 |
| `tagger.py`, `metadata_editor.py` (mutagen) | ~240 | No Flet feature edits tags. `tagger.tag_file` runs only after a download (`streamrip_api`): port with downloads (decision A), tag writer candidates in §12. `extract_artwork` runs only off Android (the Kotlin resolver serves the phone): Mac | 6, 8 |
| `sync/client`, `discovery`, `send`, parts of `manager` | ~1,900 | Port (Android side). Client and listener ✅ (`sync/companion_client.dart`, `sync/discovery.dart`; tested against Flet's own server); the manager ✅ (`sync/sync_manager.dart`); its UI and `send.py`'s menu entry with stage e (§4, phase 4) | 4 |
| `sync/server.py`, `transcoder.py` (ffmpeg) | ~1,340 | Keep on Mac until phase 8 | 8 |
| `ui/views/sync_hub.py` | 1,012 | Rewrite | 4 |
| `state_export.py` | 294 | Port (bundle import/export) | 4 |
| `llm_engine.py` (Gemini/Ollama over an OpenAI-compatible API) | 231 | Port: plain HTTP + JSON | 5 |
| `llm_tools.py`, `assistant_runner.py`, `assistant_intent.py`, `chat_memory.py`, `web_research.py` | ~4,500 | Port, with golden intent and tool-call tests | 5 |
| `ui/views/assistant.py` | 861 | Rewrite | 5 |
| `qobuz.py`, `deezer.py`, `streamrip_api.py`, `streamrip_search.py`, `downloadable.py`, `search_results.py`, `source_metadata.py`, `new_releases.py` + deezer-py, streamrip, pycryptodomex | ~3,000 + libraries | ⚠️ Port or keep on Mac (§6, decision A) | 6 |
| `ui/views/search.py`, `ui/player/download_dock.py` | 3,806 | Rewrite. The landing's local cards ✅ (`search/`, `data/landing_queries.dart`, golden-tested); the dock ✅ (`downloads/download_dock.dart`, 4d); the rest with the sources | 3, 4, 6 |
| `dsp.py`, `pca_engine.py` | 1,044 | ⚠️ Port to Dart, or keep on Mac (§6, decision B) | 6 |
| `metadata_enrich`, `metadata_ai`, `metadata_audit`, `metadata_qobuz`, `ui/player/metadata_workbench.py` | ~5,000 | Port last (desktop-heavy) | 7 |
| `ui/views/settings.py` | 3,932 | Rewrite, spread across phases (rule 4). Phase 3 ✅ (`settings/`): the hub and its search, Storage, Appearance, Audio & DSP (curve, presets), Haptics, Permissions, Database Management, Advanced (caches, raw TOML), About. The other groups with their phases (§11). | all |
| `ui/player/onboarding_modal.py` | 895 | Rewrite, smaller | 7 |
| `ui/widgets.py`, `ui/tokens.py` | 769 | Rewrite (tokens ✅) | as needed |
| `audio_engine_macos.py` (pyobjc / AVFoundation) | 1,311 | Rewrite for the Mac | 8 |
| main.py, the app shell (everything but playback): splash, tab bar and startup tab ✅ (`app_shell.dart`: Search and Library), back navigation and confirm-pop, Settings as a pushed page ✅ (`settings/settings_page.dart`), startup jobs (state-zip auto-import, state auto-export, enrichment pass, new-releases refresh, graph readiness build, cache pruning, companion start), maintenance actions (wipe, clear index/DSP, caches ✅ `data/maintenance.dart`; fresh-install reset: decision C), delete track/playlist ✅, notification routing, success sound | ~2,500 | Rewrite, each piece with the feature behind it (§11) | 3–7 |
| `config.py` (streamrip config: load, defaults, `repair_config`) + `streamrip_api.py`'s `load_config`, `update_config_params`, `ensure_config_exists` | 552 | Read half for the keys in use (`core/app_config.dart`). Writes ✅ (`core/config_file.dart`: `update_config_params` as tomlkit edits the text, the template copy of `ensure_config_exists`). Its repair half (`fill_missing` from a newer template) when the template next changes (§7). | 1, 3, then all |
| `progress_notify.py` (job notifications: progress, summary text, retry action, task-service start/stop) | 239 | Port ✅ (`core/progress_notify.dart`, phase 4b): a Dart wrapper over the Kotlin `showProgressNotification`, with the throttle, summary and retry rules; the metadata and analysis helpers with 7 and B. A scan the user starts posts nothing (in-app banner only); the scan's alert ("n tracks not analysed yet") follows only the automatic scan after a download or transfer. First users: sync (4), then downloads (6), analysis (B), metadata (7). | 4 |
| `ui/player/dialogs.py` (confirm and text-input dialogs) | 100 | Rewrite ✅ (`library/library_dialogs.dart`: playlist editor, name dialog, delete confirmations) | 3 |
| `search_history.py` (`recent_searches.json`), `text_match.py` | 81 | Port with Search | 6 |
| streamrip-derived helpers: `client.py`, `metadata_objects.py`, `ssl_utils.py`, `exceptions.py`, `filepath_utils.py` | ~330 | Go with the source clients (decision A) | 6 |
| `mock_search.py`, `genre_eval.py`, `error_boundary.py` | ~400 | Drop or fold in (Flutter has `ErrorWidget` and zones). Demo search mode depends on `mock_search` (decision C). | n/a |
| Dev scripts: `check_spoofer.py`, `configure_paths.py`, `scratch_test_search.py`, `tools/*.py` (enrich_artists, dev_fake_features, eval_ai_metadata, eval_metadata_fusion, simulate_notifications_live) | ~1,000 | Keep on Mac: they read `library.db` or call Python modules directly; none ships in the APK | n/a |
| Build and device scripts: `build_android.sh`, `fresh_build_android.sh` (+ `.ps1`), `_adb_common.sh`, `usb_probe.sh` | n/a | Replaced by `flutter build` + `tool/install_phone.sh`; `push_db.sh` stands in for the state reinject. A bundle-based reinstall (fresh_build's flow) comes with bundle import (4). | 1 ✅, 4 |
| `StreamripApp/tests/` | 16,879 | The reference behaviour: each module's tests are ported with it (as `queue_store`, `dsp` and `autoplay` were), or replaced by goldens | all |

---

## 4. Phases

Each phase ends when its parity tests pass and it is in daily use on the
phone. Rough sizes are in Dart lines, excluding tests.

| # | Phase | Done when | Size |
|---|---|---|---|
| 0 | **Backbone** ✅ | Reads `library.db` read-only on a background isolate. Tracks tab with Flet's sort modes and search semantics. Coordinate graph. Android build. | 1k |
| 1 | **Phone player** 🧪 (code done, not yet confirmed in use) | Plays the phone's real library with the screen off. Notification, lockscreen and headset controls work. The queue survives process death in Flet's JSON format. Permissions work. | 4–5k |
| 2 | **Stations** 🧪 (code done, not yet confirmed in use) | "Play Similar" matches Flet's stations: golden tests on 20 fixed seeds from a `state_backups` bundle. Autoplay sheet. | 3k |
| 3 | **Library writes** | Ledger drain, scan, playlists, delete. The port owns migrations (respects `user_version`). Remaining library tabs (the Network view with its metadata edges), selection mode, the full long-press menu, the tab bar and the Search tab's local landing cards. §11 has the list. | 5k |
| 4 | **Sync (Android side)** | Phone ↔ Flet-Mac pairing, transfers and DSP offload. Bundle import and export (incl. the boot-time auto-export and auto-import). | 3k |
| 5 | **Assistant** | Golden intent and tool-call tests pass. Voice. | 4k |
| 6 | **Sources and analysis** | Search, previews, downloads, "New from your artists", on-phone analysis: as far as decisions A and B put them on the phone | 4–6k |
| 7 | **Metadata tools, onboarding** | Feature parity, incl. the automatic enrichment pass | 4k |
| 8 | **macOS** | Mac player on Flutter; sync server moves over. Needs Xcode or CI. | 3k |

### Phase 2: stations

**How parity is proved.** `tool/export_station_goldens.py` (run with
`~/miniconda3/bin/python`) runs the Python station code and writes two files:

- `test/goldens/stations_synthetic.json` (committed): taxonomy, credit keys,
  tag similarity, and an ArtistGraph + Drift over a synthetic library that
  hits every case (untagged, collab credit row, namesake, hand-set row, Greek
  script, no tempo, no tags at all). `test/stations_synthetic_test.dart`.
- `tools/state_backups/goldens/<bundle>/` (gitignored: personal data): the
  library.db Python read, and for 20 seeds the Play Similar walk (20 tracks)
  and an auto-play-shaped station (take, accept, reject, take ×4), plus every
  artist's culture and profile flags. `test/station_real_golden_test.dart`
  picks it up automatically (or `STATION_GOLDEN=…`) and skips when absent.

On the 2026-11-02 phone bundle (1216 tracks, 287 artists, 327 tags) every
walk and every station step is identical to Python's. Things that made that
possible, kept in the code:

- the coordinate rows come from Python's exact SQL, so row order (and with it
  artist order and tie-breaks) is the same;
- matrices are float32 like numpy's, and the scalar rounding follows numpy's
  NEP 50 rules (`_f32` in `artist_graph.dart`);
- `core/py_text.dart` reproduces `str.lower` (final sigma), `str.isalnum`,
  `str.title` and NFKD-with-marks-dropped (a table generated by
  `tool/gen_nfkd_table.py` from Python's `unicodedata`, plus Hangul
  arithmetic), which `credit_keys` and the genre tokens depend on.

Rerun the export after any change to the Python station code, then
`flutter test`.

**Wiring.** `LibraryController` builds the `StationSource` (coordinate graph
+ artist graph) off the UI isolate after the track list loads (~0.2 s on the
Mac for 1216 tracks; a walk takes ~11 ms). `AutoPlay` drives
`PlayerController` through the same edits Flet's engine exposes (set aside,
insert after, replace rows, extend). Now Playing's chain is a plain toggle
(Flet's working tree), dimmed while no track has coordinates, with the art
border while on; the queue sheet shows the AUTO-PLAY header, which opens the
Auto-play sheet; long-press has Start radio.

### Phase 3: library views (read-only part)

The Genres, Artists and Albums views and the view bar, built before the
writes because they only read (step 4e of §10).

**How parity is proved.** `tool/export_library_goldens.py` (after
`export_station_goldens.py`, whose `library.db` copy it reads) writes:

- `test/goldens/library_synthetic.json` (committed): library.py's
  `_genre_group` and `_genre_color` over about 140 tags.
  `test/genre_colors_test.dart`.
- `tools/state_backups/goldens/<bundle>/library_views.json` (gitignored):
  every Albums, Artists and Genres sort, and a sample of expansions (artists
  of a genre, their albums in it, a genre's play queue, albums of an artist,
  tracks of an album). `test/library_views_golden_test.dart`.

On the 2026-11-02 bundle (635 albums, 287 artists, 18 genres) every list and
expansion is identical to Python's, row order included: the queries are
db_manager's SQL (`data/browse_queries.dart`), with the double-quoted string
literals single-quoted.

**What it does.** The view bar follows `appearance.show_*` (defaults:
Genres, Artists, Tracks; Albums is off by default, as in Flet) and
`pill_style`. Genre → artists (in that genre) → albums (in that genre) →
tracks; tapping a track plays its album from it. The genre row's play
button queues the genre newest first. Album long-press: Play Album, Add
Album to Queue. Search uses the one box, with the Tracks view's semantics
(§7).

### Phase 3: migrations and the ledger drain (the first write)

Step 4b of §10. Under decision 4a the port writes only its own copy of
`library.db`; `tool/push_db.sh` (and `install_phone.sh --db`) replace it,
discarding those writes, and now stop the app first, since it holds the file
open read-write in WAL mode.

**Migrations.** `migrateLibrary` (`data/library_schema.dart`) is
`DatabaseManager(path)` + `initialize()`, which main.py runs on every
start: the connection pragmas (WAL, foreign keys), `get_connection()`'s
twelve migrations, then `initialize()`'s schema and triggers, in Python's
order, so a fresh file comes out the same as Python's (including its
quirks: `play_counts.cluster_id` only appears on the second open, because
its migration runs before the table exists). Only the port's own database is
opened writable (`LibraryRepository.open(…, writable: true)`); the
`LIBRARY_DB` override and the test goldens stay read-only.

**Drain.** `LedgerDrain` (`data/play_ledger.dart`) is `drain_ledger`: rename
to `.draining`, settle 0.5 s, parse, `record_listens` in one transaction,
delete; a leftover `.draining` first. Scheduled as in Flet: 3 s after each
listen the handler logs (Flet: each track change), immediately on show
(Flet: resume) and once the library is open at start (Flet: native ready).
Coalescing, and a failure keeps the file. Auto-play doesn't take the
drained lines (it already had each listen in-process, §7).

**How parity is proved.** `tool/export_db_goldens.py` writes
`test/goldens/db_synthetic.json` (committed): the schema and every row
after Python opens a fresh file (once, twice) and two legacy shapes (old:
no DSP columns, single-key `track_partitions`, retired tables; mid:
geometry v1 coordinates, features-v4 blobs, a `feature_cache`, enrichment
before provenance), plus three ledger drains (broken lines, streams, bare
paths, string numbers, short tracks, a crash replay) and Python's
`round(x, 1)` on tie cases. `test/db_schema_test.dart`,
`test/play_ledger_test.dart`. It also writes
`tools/state_backups/goldens/<bundle>/library_migrated.db` (gitignored):
`test/db_schema_real_test.dart` migrates its own copy of the real library
and every table must equal Python's. All identical.

### Phase 3: the library scan

Step 4c of §10. `LibraryScanner` (`scan/library_scanner.dart`) is
library_scanner.py: walk the library folder (`os.scandir` order; symlinked
files count, symlinked folders don't), drop rows whose file is gone, read
tags for new files and files whose mtime moved by over 1 s, 50 at a time on
a background isolate, insert 500 per transaction, and for 200 files or more
drop the insert trigger and rebuild counts and search at the end
(`begin/end_bulk_import`). The writes are db_manager's, with its SQL
(`data/scan_writes.dart`), including artist aliases (`artist_tag`). The
folder comes from `storage_paths` (§1 row 10).

Tags are read by a line-by-line Dart port of **tinytag 2.3.2**
(`scan/tinytag.dart`, MIT, §12), the version the Flet APK bundles, so
titles, durations and bitrates come out to the last bit. Images and the
ASF/AIFF parsers are left out (the scanner never reaches them).

The UI is library.py's: the refresh button (accent) next to sort, the
banner with the bar and status line under the view bar, "Scan complete.
Indexed n items." / "Library is up to date.", the empty-library state, and
a reload of every view and the station graphs afterwards.

**How parity is proved.** `tool/export_scan_goldens.py` (tinytag pinned in
`tool/.pydeps`, see the script) builds `test/goldens/tags/` (committed,
~100 KB: every container, ID3 v1/v2.2/v2.3/v2.4 in each text encoding,
extended headers, SYLT, PRIV, TXXX, vorbis comments in FLAC/Ogg/Opus/Ogg
FLAC, MP4 AAC/ALAC with freeform atoms, RIFF INFO and ID3 chunks, broken
files) and writes:

- `test/goldens/scan_synthetic.json`: tinytag on each corpus file
  (`test/tinytag_test.dart`), and two scan cases
  (`test/scan_test.dart`): a nested tree with symlinks, unsupported and
  untagged files, an alias and whitespace in names, then deletes, a
  replaced file, a touch under 1 s and a new file; and a 205-file tree
  that takes the bulk path. Counts and every table identical.
- `tools/state_backups/goldens/mac_music/` (gitignored): tinytag on all
  454 files of `~/Desktop/Music` and Python's scan of it
  (`test/scan_real_test.dart`). Every tag identical; every table
  identical except `added_date` below 1 ms (§7).

### Phase 3: playlists, the long-press menu, selection, delete

Step 4d of §10. Playlists are a fifth view in
the bar (library.py's order: Playlists, Genres, Artists, Albums, Tracks;
Flet's initial-view rule puts it last). A playlist row opens onto its
tracks, each with a drag handle and a remove button; a drag moves a track
within its own playlist only. The row's edit button opens Customize
Playlist (name, nine colours), its delete button the red confirmation.
"Create New Playlist" closes the list (not while searching); with no
playlists the tab shows "No playlists yet." and CREATE PLAYLIST. Sort:
Name (A–Z), Date Created (default).

The long-press menu is Flet's: the track card (sidecar cover, title,
"artist · album", format, duration, bitrate), Select…, Play Next, Add to
Queue, Start Radio, Add to Playlist, Delete Track. Redownload (decision A)
and Send (phase 4) come with their features. Select… opens selection mode
in every view: rows get checkboxes, the bar at the bottom has cancel, the
count, Play (the picks, in order, right after the playing track, and
plays), Add to Queue and Add to Playlist. Delete Track asks, stops the
track if it plays, drops its queue rows, deletes the file, then its
database rows, and reloads every view.

**How parity is proved.** `tool/export_playlist_goldens.py` writes
`test/goldens/playlists_synthetic.json` (committed): 29 db_manager calls
in sequence on one database (create, a NOCASE name clash, Greek names,
adds, a duplicate, a path not in the library, moves including clamped and
empty ones, removes, `delete_tracks_by_paths`, an orphan row that a move
clears, renames with a clash, colours, deletes, a missing id), and after
each: the result or `str(exc)`, both tables, both listings and every
playlist's tracks. Plus the playlist search on ASCII queries (§7).
`test/playlists_test.dart`: all identical. `browse_controller_test`,
`player_controller_test` and `library_screen_test` cover the view, the
selection's Play and Add to Queue, and the whole menu → selection →
playlist → delete flow on real files.

### Phase 3: the tab bar and the Search landing

Step 4g of §10. `AppShell` (`app_shell.dart`) is main.py's `_build_ui`
for the tabs the port has: Search and Library, the mini player above
Flet's tab bar (no pill, hairline border, accent indicator at 0x55), a
220/160 ms fade between tabs, each tab keeping its state. The startup tab
is `general.startup_page` (§7). Back leaves the Library's selection mode,
then sends the app to the background, on either tab.

The Search tab (`search/`) has search.py's landing: Most Listened Tracks
(tap: `play_track(path)`, i.e. the Library's tracks view as it stands),
Most Listened Artists and Genres (tap: the Library on that view, filtered
to the name), Library at a Glance (tap: that view, unfiltered; a view
hidden in Appearance falls back to Tracks with Flet's note). `[landing]`
switches each card; the cards are rebuilt on show when empty or over 60 s
old, as in Flet.

**How parity is proved.** `tool/export_library_goldens.py` now also runs
`get_most_played`, `get_most_played_artists`, `get_most_played_genres`
and `get_library_counts` at limits 5 and 50: on a synthetic library in
`library_synthetic.json` (multi-genre albums, Greek and junk tokens,
"rock" beside "Rock", count-0 rows, a played path not in the library,
ties on count and on plays; `test/landing_queries_test.dart`) and on the
real bundle (`library_views.json`; `library_views_golden_test.dart`).
All identical. `test/app_shell_test.dart` covers the refresh rule, the
config keys and the links into the Library.

### Phase 3: Settings

Step 4h of §10. `settings/` is settings.py for the groups whose features
the port has, pushed over the tabs from the gear in the Library and Search
headers, or from the empty library's ENTER PATHS (Storage). Back goes from
a group to the hub, then out. The hub keeps Flet's sections and its
search (direct matches, then the weighted cosine with one-edit typos);
Companion & Sync, Authentication, AI Assistant and Metadata come with
their phases and are left out of both.

- **Storage & Paths**: the resolved folders (`sync_config_to_ui`), SAVE
  PATHS when edited (empty and app-private paths refused; then a scan),
  and Flet's Android folder navigator (saved at once, no scan). Default
  Download Quality waits for decision A; its value is written back as read.
- **Appearance**: startup page, sort, pill style, the five landing
  switches, the library tabs, twelve accents; SAVE when anything differs
  from the file. Changes reach the running app at once (Flet restarts the
  UI): view bar, landing cards, accent, pills, haptics.
- **Audio & DSP**: Dynamism (greyed when nothing is analysed, the boost
  card from the player), the EQ switch, system and custom presets, the
  curve, sliders and gain fields, saving, applying and deleting presets.
  Gains reach the player as they move. The device's EQ bands come from the
  handler (`equalizerInfo`).
- **Haptic Feedback**, **Permissions** (statuses, refreshed on return from
  the system page), **Database Management** (clear index, clear DSP, wipe;
  the compute pipeline waits for decision B), **Advanced** (album-art and
  preview caches, the raw TOML editor; export/import with phase 4, the
  developer switches with decision C), **About** (with the licence page).

**config.toml.** Every write is `update_config_params`, which re-reads the
file, sets the keys with tomlkit and dumps it, keeping comments, spacing
and order. `core/config_file.dart` makes the same edits to the text:
values replaced in place, new keys after a table's last key, new tables at
the end, and `custom_presets` in whichever form the file has it (an inline
table, rendered by a translation of tomlkit's `InlineTable.as_string`, a
`[dsp.custom_presets]` table, or a new one). A missing file starts as the
template (`assets/config_template.toml`, utils/config.toml). Writes go
through one queue and the file is re-read after each (`ConfigController`).

**How parity is proved.** `tool/export_config_goldens.py` (tomlkit 0.15.2
pinned in `tool/.pydeps`) runs Flet's own `update_config_params` through
Settings' write sequences (settings.py's appearance, general, paths, DSP,
preset save/pick/delete, haptics and demo-search writes) on the template
and on the newest phone bundle's config with its credentials blanked, and
through 103 small edge files (comments, CRLF, missing newlines, multi-line
values, quoted keys, sub-tables, inline and table presets, escapes,
floats). `test/goldens/config_synthetic.json` (committed); 192 writes,
`test/config_file_test.dart`: every file identical, byte for byte.
`tool/export_settings_goldens.py` runs settings.py's search on 86 queries
(`test/goldens/settings_search.json`, `test/settings_search_test.dart`):
identical. `test/settings_test.dart` covers each group's writes, the
maintenance actions on a real schema, and the changes reaching the app.

### Phase 3: the Acoustic Network view

Step 4i of §10. library.py's Network view, first in the view bar when
`appearance.show_network` is on (Settings → Appearance now has the
switch, with Flet's EXPERIMENTAL tag). Its graph is the seed's
`track_neighbors` (in practice the artist tier: the acoustic tier is no
longer written, so the "neighbourhood" is same-artist tracks) placed by
their coordinates, with a station walk from the selection laid over it.

- `data/network_queries.dart`: `has_pca_coords`, the "any analysed
  track" seed, `get_neighbors`, `get_tracks_pca_coords_for_paths` and
  `build_metadata_edges` (+ `replace_neighbors_bulk`), same SQL.
- `library/network_layout.dart`: the builder's node and edge half
  (seed, neighbours, the ring for neighbours without coordinates, walk
  steps), the focal-centred uniform projection pinned per build,
  `_declutter_net_nodes`, `_apply_walk_seq` (merge, let only newcomers
  move, prune), `_rebuild_walk_edges`, the legend. The declutter branches
  on distances, so it uses `pyHypot` (CPython's `vector_norm`, §12):
  a plain `sqrt(x²+y²)` is an ulp off and moves nodes.
- `library/network_controller.dart`: `load_library`'s network branch
  (`buildNetwork`), `_compute_walk_seq` (explore: `StationSource.walk`;
  live: auto-play's buffer, else the queue ahead), the readout's play /
  explore-from-here / walk-from-here, density and steps, the mode chip,
  Play and Next, the row menu (play from here, reseed, walk here, add to
  playlist), and `_sync_network_now_playing` (live re-anchors after
  200 ms or reseeds after 350 ms; explore never moves).
- `library/network_view.dart`: the canvas (`_emit_net_shapes` as a
  CustomPainter, sizes counter-scaled by the zoom), InteractiveViewer
  (0.8–5×, 240 px margin, fit button), tap-to-inspect, the legend, the
  readout, the pulsing now-playing ring, the list panel with collapse,
  the setup card when nothing is analysed.

**How parity is proved.** `tool/export_network_goldens.py` runs
library.py's own `_build_interactive_network_canvas` and
`_apply_walk_seq` on a LibraryView built without a page, and keeps the
nodes, edges, projection and selection it leaves. Synthetic
(`test/goldens/network_synthetic.json`, `test/network_synthetic_test.dart`):
`build_metadata_edges` at k=3 and 30 beside acoustic rows, the queries,
ten load scenarios (clumps, coincident and collinear points, neighbours
missing or without coordinates, no seed, a playing focal node, a single
node, nothing to draw, far outliers) and four re-walks, and 407
`math.hypot` values. Real (`network.json`, gitignored;
`test/network_real_golden_test.dart`): all 13,788 artist edges, and 8
seeds × densities 12/24/36 with the walk (through the ported stations),
the layout and a 15-step re-walk. All identical to the bit, including
SQLite's tie order on equal weights. `test/network_view_test.dart`
covers the controller's actions, both modes following playback, and the
screen.

### Phase 4: sync, Android side

The phone's half of Companion & Sync, against the Flet Mac (its server
stays Python until phase 8). Stages, one commit each:

| | Stage | Flet source | |
|---|---|---|---|
| a | Protocol: the client, multipart uploads, discovery (query, beacons, /24 sweep), the Kotlin for the route address and the multicast lock, the permissions | `sync/client.py`, `sync/discovery.py` (listener half) | ✅ |
| b | Progress and alert notifications: a Dart `ProgressNotifier` over the Kotlin `showProgressNotification` (throttle, summary, cancel and Retry buttons, the task service), `post_alert`, notification routing to the screens that exist | `progress_notify.py`, main.py `_on_notification_navigate` | ✅ |
| c | The manager: enable and idle auto-off, USB and saved-address probe, heartbeat, PIN snooze, Find Mac (probe, query, sweep) and its messages, manual connect, unpair, background and resume hooks, auto-accept, the Send to Mac worker and acks; the device name | `sync/manager.py`, `sync/send.py` | ✅ (wired into the app with e) |
| d | Transfers onto the phone: the download queue for the `companion` source (`.part`, size check, no-overwrite names, `clean_filename`, NFC, `pick_download_dir`, features stored, one quiet scan after the queue drains, Android media scan) and the dock as far as transfers need it | `queue_controller.py`, `streamrip_api.download` (companion branch), `download_dock.py` | ✅ |
| e | UI: Settings → Companion & Sync and the sync hub, the PIN and transfer dialogs, Send to Mac in the long-press, selection and album menus | `ui/views/sync_hub.py`, manager dialogs, `send.py` | 🔨 |
| f | State bundles: export (Settings, and the boot auto-export to Downloads and the library folder), import (Settings, and `*_import.zip` at boot, with `.onboarded`), upload to the Mac | `state_export.py`, main.py `_heavy_init`, `_auto_export_state_snapshot` | 🔨 |
| g | DSP offload and the acoustic build: features stored in batches, then `build_metadata_edges` and `build_acoustic_edges` (the SVD build, golden-tested against numpy), and the startup readiness build | sync_hub's offload, `track_graph.py`, `pca_engine.redundant_raw_features`, main.py `_ensure_graph_built_async` | 🔨 |

**Stage a.** `sync/companion_client.dart` is client.py call for call:
ping, pair, the offload (cache probe, then one upload per track, features
handed to the caller in batches of 10 and flushed on any exit, three
consecutive network failures end it), bundle upload, Send to Mac's push,
pending transfers, acks, and `stream_url`. Errors keep Python's split
(`CompanionAuthError` for PermissionError, `CompanionHttpError` for
RuntimeError, dart:io's own for the network) and its timeouts (whole-call
limits for ping, pair and the polls; connect 10 s and 180 s per read stall
for transfers). `sync/multipart.dart` streams uploads with a
Content-Length, so a FLAC never sits in memory; the file name goes as
`filename*` (§7). `sync/discovery.dart` is the listener: one UDP socket on
54545 (an ephemeral port if taken), the query broadcast every 2 s, the
sender's address preferred over the advertised one, a server re-reported
at most every 10 s, `scan_once`, and `sweep_subnet`. Android: the default
route's address (`defaultRouteIpv4`) and a WifiManager.MulticastLock
(Flet had none, §7) from `MainActivity`; INTERNET, ACCESS_NETWORK_STATE
and CHANGE_WIFI_MULTICAST_STATE in the manifest. `core/py_text.dart`
gained `pyQuote` (`urllib.parse.quote`).

**Stage b.** `core/progress_notify.dart` is progress_notify.py:
`ProgressNotifier` (one notification per job; about one update a second,
the first and last always posted; `stage`; a summary, or nothing when
nothing was shown; a Retry-style button that opens the app at a target),
the Cancel button dispatched to the newest notifier of its job (the
extension's per-job handler table), `post_alert`, `cancel_alert` and
`format_eta`. The Kotlin already starts and stops LibraryTaskService with
the first and last running job; `MaianNative.showProgressNotification`
is the new call into it. The metadata and analysis helpers come with
phases 7 and B. `core/notification_router.dart` is
`_on_notification_navigate`: taps go to the screen named by
"screen:action", and a cold-start tap waits for its screen to register.
The Library registers now (`library`, from the scan alert); `sync` and
`downloads` with stages d and e. `test/progress_notify_test.dart` ports
test_progress_notify.py and the notifier scenarios.

**Stage c.** `sync/sync_manager.dart` is manager.py without its widgets:
the PIN and transfer dialogs, snackbars, prefs, the download queue and
the features lookup belong to a `SyncHost` (the app's comes with stage
e), and the dialogs call back (`PinPrompt.submit`, `TransferPrompt.download`,
`pinPromptClosed`). Everything else follows Python: the 4 s probe of the
USB tunnel (127.0.0.1) and the saved address, beacons, one connection
attempt at a time (a user tap waits its turn), the PIN prompt and its
two-minute snooze, the 15 s heartbeat (two misses drop the link; an
unauthorized reply clears the token), the 60 s background poll with its
one "transfers" card, the 15-minute idle auto-off (held by long jobs and,
for up to an hour, by transfers in flight), Find Mac (round trip, probe,
query, /24 sweep, and its four messages), manual connect with
`_explain_connect_error`, unpair, auto-accept (until a transfer has
failed twice) or the dialog once per new set, acks only for files on
disk, and the Send to Mac worker (one batch, extended by later taps; the
`sync` notification; three network failures end it; the summary). The
device name is `getprop ro.product.model`, as in Flet.
`data/feature_store.dart` is server.py's `stored_features`.
`test/sync_manager_test.dart` (21 tests, on a virtual clock) covers
those paths; `test/companion_protocol_test.dart` adds the manager against
Flet's server: manual connect, a wrong and a right PIN over "USB",
auto-accept, the stream URL, the ack reaching the Mac, and Send to Mac.
`MaianNative` and discovery now ask `Platform.isAndroid`: widget tests
report `defaultTargetPlatform` as Android.

**Stage d.** `downloads/download_queue.dart` is queue_controller.py:
jobs and their states, `enqueue_many` (the manager's `TransferQueue`),
remove, retry, clear, the bounded history, the worker with its batch
notification (`download`; summary, Retry for non-Mac failures, Cancel
clears the queue), three attempts with 5 s and 10 s waits, cancel between
chunks and during the waits, features stored with the file (NFC path;
`data/feature_store.dart` `persistFeaturesJob`, server.py
`persist_features`), the success haptic, each Mac transfer's outcome
reported to the manager, and one quiet scan a second after the queue
drains. `downloads/companion_download.dart` is `download`'s companion
branch: the file name from the Mac's path (`.m4a` for AAC), `.part` then
rename, the size check, "already on this device", no-overwrite names
(`name (2).ext`), the 401/404 texts, a 120 s read stall and no total cap.
`downloads/file_names.dart` holds `clean_filename` and `normalize_nfc`;
`scan/storage_paths.dart` gained `pick_download_dir`. The dock
(`downloads/download_dock.dart`) is the pill above the mini player and the
sheet; `AppDownloadHost` connects the queue to the folders, the library,
the haptics and (with stage e) the manager. The `downloads` notification
target opens the sheet; `downloads:retry` re-queues the last batch's
failures. `LibraryController.scan(quiet: true)` shows no banner and no
snackbar, then posts library.py's "not analysed yet" card (without its
Analyse button: decision B). `MainActivity` has `scanMediaFile`
(MediaScannerConnection). Tests: `download_queue_test.dart` (Flet's
test_download_queue.py and the workflow), `file_names_test.dart` (NFC,
`clean_filename` and pathlib's name parts against Python),
`storage_paths_test.dart` (Flet's pick tests), `download_dock_test.dart`,
`scan_controller_test.dart` (the quiet scan, features round trip), and in
`companion_protocol_test.dart` the download against Flet's server next
to Python's own `download()` into a twin folder: same results, same files,
same first and last progress reports, for an NFD name, illegal
characters, a re-send, a taken name, no token and a file never queued.

**How parity is proved (stage a).** `tool/companion_test_server.py` runs
Flet's own `CompanionServer` hermetically (scratch feature cache and
bundle folder, beacon on loopback and a private UDP port, a deterministic
stand-in for feature extraction) and Flet's own client on command.
`test/companion_protocol_test.dart` (13 tests, skipped without the Flet
Python env): pairing with a wrong and a right PIN, the device name as the
Mac records it, 401s as PermissionError, pending transfers identical to
Python's, `stream_url` identical on 15 transfer and token shapes, the
stream download byte for byte, acks, Send to Mac (Artist/Album filing,
features stored, duplicate, the batch announced once), the offload storing
exactly Python's rows in Python's batches (cache hit, stale cache row,
undecodable file, missing file, a batch of 10), cancel, bundle upload,
discovery by query, the sweep, and `quote` on 6 strings.
`test/discovery_test.dart`: which datagrams the listener takes.

---

## 5. Cutover (Android)

The port is built to replace the Flet app in place, keeping all user data.

1. Remove `applicationIdSuffix ".port"`.
2. Sign with the **same key as the Flet APKs** (today `~/.android/debug.keystore`).
   A different key makes Android refuse the update. ⚠️ **Not done yet:** back
   that keystore up now; losing it means users have to uninstall.
3. Set versionCode above every Flet build (Flet ships 1).
4. On first launch the port finds `files/data/library.db`, config, queue and
   prefs exactly where Flet left them (§2), so no migration step is needed.
5. Fallback: a state bundle export and import (`state_export.py` format).
6. Branches swap (rule 1): `flutter-port` becomes master, the Flet code
   continues on `flet`.

Moving to a real release keystore is a separate, later decision. It forces one
uninstall and reinstall; the bundle export/import covers the data.

---

## 6. Open decisions

- **A. Downloads on the phone?** If the Mac downloads and the phone receives
  over sync, phase 6's source clients (Qobuz, Deezer decryption, streamrip)
  never need porting. That is the highest-risk code: reverse-engineered APIs
  with no upstream to follow.
- **B. Analysis on the phone?** If the Mac computes features and coordinates
  (as offload already does), there's no DSP port and no Rust anywhere.
- **C. What to drop?** Candidates: onboarding (rebuild smaller), `pca_engine`
  and `genre_eval` diagnostics, `mock_search` (and with it the demo search
  mode), Flet-specific plumbing (`error_boundary`). `progress_notify` is not
  a candidate: its notifications are a feature (§3).

---

## 7. Parity log: where the port differs from Flet today

| Area | Flet | Port | Resolution |
|---|---|---|---|
| Similar tracks | `track_graph.open_station` + `artist_graph` Drift + `credit_keys` | The same, ported; identical output on the golden seeds | Fixed in phase 2. The interim "Nearest by sound" sheet is gone; long-press has Start radio, as in Flet. |
| Auto-play's listen signal | Ledger lines drained into the database every track change / resume (3 s debounce); skips older than 180 s ignored | The audio handler hands each listen to auto-play as it writes the ledger line | Intentional: same rule (`is_listen`), no file round trip, nothing goes stale. The ledger file is still written for the phase 3 drain. |
| Queue edits while nothing is loaded | Sent to the native player, which fails them (RangeError) and is re-pushed on play | Not sent; the first play pushes the whole queue | Intentional (a restored track missing on disk, or after stop). |
| Search matching | FTS5 prefix match (unicode61, accent-blind), then `LIKE`, then bigram closest match | Every query word as a substring, accent-blind, same punctuation stripping, then the same closest-match fallback | Intentionally broader: it finds everything FTS finds, plus mid-word hits. Closest-match scoring is identical and tested. |
| Search timing | 300 ms debounce, SQL per query | In-memory, per keystroke | Faster. Nothing to fix. |
| Tie order within a sort | Unspecified (SQLite) | By id | Deterministic. Nothing to fix. |
| Initial sort | `config.toml` `general.library_sort` | Reads `general.library_sort` (was "always date") | Fixed in phase 1. |
| Shuffle on/off | `set_queue` reloads the playing track from 0:00 and starts playback even when paused; the ledger logs it as an early skip | The track keeps its position and play/pause state (`keepListen`) | Intentional: the Flet behaviour is a side effect, not a feature. |
| Restored track missing on disk | Not preloaded, and Play then does nothing | Not preloaded; the first Play loads it at the saved offset | Intentional. |
| Queue rows | `_play_track_core` rows carry no `duration` | They carry `duration` | Harmless to the Flet reader; the slider has the right length before the decoder reports. |
| Microphone permission | Asked at first launch with the rest | Not asked until voice exists (phase 5) | Intentional. |
| Long-press on a track | Flet's full context menu | Play next, Add to queue, Start radio (haptic tick as in Flet) | The rest arrives with the features behind it (Select…, playlists, send, redownload, delete). |
| Notification taps | Routed to the target tab | Routed (`core/notification_router.dart`) to the screens that exist: the Library; sync and downloads with phase 4d–e, the workbench with 7 | Each target with its screen. A tap on a target not ported yet is held, not acted on. |
| DSP after background auto-advance | `reapply_dsp` runs only while the Python session hears the player, so tracks reached with the session torn down keep the previous track's settings | Applied on every row change | Intentional. |
| Track row | Format badge, Flet subtitle rules, tint on the playing row | Same | Fixed in the phase 0–1 audit. |
| Sort in Artists and Genres | One `sort_mode` for every view, coerced on each switch (Tracks → Artists → Tracks loses the Tracks sort; Genres keeps an unrelated mode and shows no tick) | Tracks and Albums share one sort, as in Flet; Artists and Genres each keep their own, starting at A–Z | Intentional. |
| Album long-press → Play Album | Passes `("album", album, artist)`, which `_play_track_core` unpacks as `(artist, album)`: outside shuffle the query finds nothing and it says "No playable media found." | Plays the album from its first track | Port is right. The Flet fix is one line (`library.py` `_open_album_context_menu`, swap the tuple); not made on master yet. |
| Search in Albums, Artists, Genres | `LIKE` on the album title / artist / genre name, plus albums and artists whose tracks match FTS; then closest match | The Tracks view's semantics (every word, accent-blind, substring) on the same fields and through the tracks; Genres by name only, as in Flet; the same closest-match scoring | Intentional, as for Tracks. |
| Network: rebuild while a walk is selected outside the neighbourhood (a walk step, then a density change) | The walk is computed from the selection, but the selection has no node (only seed, neighbours and walk steps get one), so it resets to the seed and the walk is drawn from the seed | The selection joins the graph (as a walk-step node), so the drawn walk starts where it was computed | Port is right; library.py's own comment says that was the intent. Flet fix: `_add` the `walk_seed` row in `_build_interactive_network_canvas`; not made on master yet. |
| Network zoom | Node sizes are re-counter-scaled once, when the pinch ends (re-sending the shapes per frame was the bridge cost) | Counter-scaled on every frame of the pinch | Intentional: no bridge; same result at rest. |
| Network step pills | The number drawn at a guessed width (canvas text can't be measured) | Centred in its pill | Cosmetic. |
| Network without coordinates | Setup card with a Compute DSP Features button | The card, without the button | Decision B. |
| Network: a rebuild while a re-walk is pending | The re-walk can land on the new graph afterwards | The rebuild cancels it (the rebuild computes the walk itself) | Intentional. |
| Fix artist info, Curate in Workbench | Buttons on the artist and genre rows | Not shown | Phase 7, with the workbench. |
| Opening the database | `get_connection` + `initialize` commit after each migration step | The same steps in one transaction | Intentional: a failure leaves the file as it was. Same result otherwise (golden-tested). |
| Ledger line that is JSON but not an object (`[1]`, `5`) | `raw.get` raises outside the try: the drain fails, and the `.draining` file fails again on every later drain | Dropped like a torn line | Intentional. The handler never writes one. |
| Drain triggers | Each track change (3 s), resume, native ready | Each listen logged (3 s), show, library open | Same moments: a track change is when the outgoing listen is logged. |
| Scan: `tracks.added_date` | `st_mtime` (nanoseconds, as a double) | `FileStat.modified` (dart:io has milliseconds only) | Differs by under 1 ms; the scanner rereads a file only when its mtime moved by over 1 s, so a Flet-scanned library is not reread. Exact on whole-second mtimes (the synthetic goldens). |
| Scan: a row with `added_date` NULL | `abs(None - mtime)` raises and the scan dies | Treated as changed: the file is reread | Intentional. Only very old databases have such rows. |
| Scan: ASF/AIFF content in an `.aac` file | tinytag's ASF or AIFF parser | Skipped as unreadable | Not ported (§4, the scan); never seen in a library. |
| Search tab without source credentials | "Setup Required" prompt; no landing cards (they show only once Qobuz or Deezer is set up, or demo mode is on) | The landing cards always | Intentional: the port has no source search yet (decision A), and the cards only read the library. |
| Search tab header | Title, source switch, connection signal, recent searches, settings shortcut, search box, result tabs | The title and the settings shortcut | The rest with the sources (6). |
| Startup tab | `general.startup_page`: Jarvis, Search, Library or Settings | Search, else Library | Jarvis (5) isn't ported; Settings is a pushed page, not a tab. Settings' dropdown offers Search and Library, as Flet's does. |
| Empty library | "ENTER PATHS" opens Settings → Storage | The same | Fixed in phase 3 (Settings). |
| Untagged files on every scan | Never get a row, so each scan rereads them and counts them in "Indexed n items." | The same | Kept for parity. |
| Playlist search | `LIKE` on the name (ASCII case only), then closest match | The other views' semantics: every word, accent- and case-blind for every script; same closest-match scoring | Intentional, as for Tracks. Golden-tested on ASCII queries, where both agree. |
| Playlist rows after a reload | Opened by a tap: drag handle and remove button. Re-opened by `load_library` (after an add): plain rows, no handle | Always the handle and the remove button | Port is right; Flet's generator omits `playlist_id`. |
| Playlist count after removing a track | The row's "n tracks" stays stale until the next reload | Updated at once | Port is right. |
| Add several tracks to a playlist | One transaction per track | One transaction for all | Same rows (golden-tested); a failure adds none instead of some. |
| Saving the playlist editor with a taken name | The IntegrityError is lost in the task; nothing is shown | "Could not save playlist: UNIQUE constraint failed: playlists.name" | Intentional. |
| Back in selection mode | Leaves the app (only Search's selection mode is closed by back) | Leaves selection mode | Intentional: back closes the innermost state, as Flet does for Search. |
| Writes on a read-only library (`LIBRARY_DB` override) | n/a (Flet always owns its database) | Add to Playlist and Delete Track are disabled | Dev-only case. |
| Library reload after a scan or delete | The snackbar follows `load_library` (no graph rebuild there) | Rows reload, the snackbar shows, the station graphs rebuild behind | Same order as Flet. |
| Settings → Appearance's Default Library Sort | The dropdown shows `library_sort` but saves `general.default_sort`, which nothing reads, so the sort never changes | Writes `default_sort` as Flet does, plus `library_sort`, and re-sorts the Library at once | Port is right. The Flet fix: write `library_sort` in `_save_appearance_settings`; not made on master yet. |
| Applying Settings | Appearance restarts the UI (`restart_ui`); a raw TOML save applies at the next start | Any config change (Settings or the raw editor) reaches the view bar, landing cards, accent, pills and haptics at once | Intentional. |
| Settings hub | Twelve groups | The eight whose features exist (§4, Settings); Companion & Sync, Authentication, AI Assistant and Metadata with phases 4, A, 5 and 7 | Until then. |
| Settings rows waiting on features | Default Download Quality, the Jarvis tab switch, compute DSP/PCA, export/import, demo search, debug play counts, fresh-install reset | Not shown; values the hidden rows would save are written back as read | With decisions A, B, C and phases 4, 5. |
| EQ drag and slider writes | `update_config_params` on every drag event of the curve and the sliders | The player gets each step; the file is written when the drag ends | Intentional: same file afterwards, without a write per frame. |
| EQ curve and slider haptics | `trigger_haptic("light")` on every event | On each 0.1 dB step (slider: each value change, as Flet) | Same feel without a buzz per pixel. |
| Settings labels | Only the switch toggles | Tapping a switch's label toggles it too | Small usability fix. |
| Permissions page | Statuses; its text points at an OPEN SYSTEM SETTINGS button that isn't there (and the GRANT buttons never show) | Statuses, refreshed on return, and the OPEN SYSTEM SETTINGS button the text describes | Port is right. |
| Album Cache button | Clears Python's `temp/` and in-memory cache; on Android the Kotlin artwork cache (`cacheDir/np_art`) stays | Clears `temp/` and `np_art` | Port is right: `np_art` is the cache Android uses. |
| About | GitHub opens in the browser; credits Flet | The link is copied (no `url_launcher` yet); credits Flutter; a Licenses button (`showLicensePage`) | Until a browser hand-off is needed elsewhere. |
| Folder pickers off Android | macOS/Linux native picker, Windows FilePicker | Type the path (the browse button is Android only) | Phase 8 (macOS). |
| config.toml repair | `ensure_config_exists` fills sections and keys a newer template adds (`fill_missing`) and bumps `misc.version`, on every load | Only the missing-file case (template copy); a complete config is left as is, which is all the repair does today (golden-checked) | Port `fill_missing` the next time utils/config.toml gains keys. |
| Send to Mac: non-ASCII file names | aiohttp's client percent-encodes `filename="…"` and its server never decodes it, so the Mac saves "Άλφα.flac" as "%CE%86%CE%BB%CF%86%CE%B1.flac" (confirmed against Flet's own server) | Sends the name as `filename*=utf-8''…`, which the server decodes: the Mac saves "Άλφα.flac" | Port is right. Flet fix (master): send the name in a form the server decodes (`filename*`, or `FormData(quote_fields=False)` in `push_track`); not made yet. |
| The phone's own address (`resolve_local_ip`) | The kernel's route to 8.8.8.8 (a UDP "connect") | The active network's IPv4 link address (ConnectivityManager), the same answer; off Android, the first non-tunnel IPv4 address | dart:io can't connect a UDP socket. |
| Success after a download | `play_success_notification`: the vibrate haptic, and a ringtone through pyjnius, which the APK doesn't have (it fails silently) | The vibrate haptic | Same on the phone. The Mac's sound comes with phase 8. |
| A saved transfer on Android | chmod 666 (777 on the folder), `am broadcast MEDIA_SCANNER_SCAN_FILE` (ignored since Android 10) and `content insert` from the app's shell | MediaScannerConnection.scanFile, no chmod (shared storage's permissions are FUSE's, not the file's mode) | Port is right: the file reaches MediaStore. |
| "Not analysed yet" card after a quiet scan | Analyse button (on-phone analysis) | The card without the button | Decision B. |
| Quiet scan while the Library is hidden | The reload is deferred until the tab shows | Reloaded at once | Flutter rebuilds only what is visible; nothing to defer. |
| Download errors from the network | aiohttp's text ("Cannot connect to host …") | dart:io's (`SocketException: …`) | Different words, same place (the dock's status line). |
| Discovery on Android | Hears only the Mac's unicast replies to its query (no multicast lock without pyjnius) | Holds a MulticastLock while listening, so the Mac's 2 s broadcast beacons arrive as well | Intentional (§1 row 17): finds the Mac sooner on networks that pass broadcasts. |

---

## 8. Dev workflow

- **Device:** Android phone over USB, or Wireless debugging.
  `tool/install_phone.sh` builds, installs and starts the port without a
  terminal session attached (`--help` for the flags):
  - `--db [bundle.zip|library.db]`: copy a library in first (debug builds
    only; see below). The port's database is its own copy (decision 4a):
    rerun `--db` to pick up a newer bundle, which discards what the port
    wrote (its drained play counts).
  - `--release`: smooth build for everyday listening. Not debuggable, so
    `--db` needs a debug build installed at that moment: `--db` once, then
    `--release` (the data is kept; both are signed with the same key).
  - `--grant` (skip the permission prompts), `--logs` (follow logcat),
    `--wifi IP:PORT` (adb connect first), `--no-build`.
  For hot reload, `flutter run` in `mai_an_flutter/` (`r` reload, `R`
  restart).
- **Data:** `tool/push_db.sh` copies the newest phone state bundle from
  `tools/state_backups/` into the port: its `library.db`, and its
  `config.toml` (replacing what the port's Settings wrote). Its paths are Android paths, so tracks
  resolve on the phone. Pass a `library.db` or `.zip` path to use another one.
  The Mac's live database is `StreamripApp/.flet/storage/data/library.db`
  (Mac paths: browsable, not playable).
- **Native code:** `packages/maian_native` compiles the Flet extension's
  Kotlin, resources and manifest in place (`src/main/kotlin` is a symlink to
  `flet_audio_service/src/flutter/flet_audio_service/android/src/main/kotlin`),
  so a fix there reaches both apps. At cutover, move that directory in.
- **Desktop tests:** `flutter test`. Add `LIBRARY_DB=… flutter test
  test/real_library_test.dart` to run against a real database.
- **Goldens:** each `tool/export_*_goldens.py` runs with
  `~/miniconda3/bin/python` from `mai_an_flutter/`. The scan and config
  exports need the APK's tinytag and tomlkit apart from the dev env (which
  has streamrip's tomlkit 0.7.2):
  `~/miniconda3/bin/pip install --target tool/.pydeps tinytag==2.3.2 tomlkit==0.15.2`
  (gitignored). Generated tables: `tool/gen_nfkd_table.py`, `tool/gen_nfc_table.py`,
  `tool/gen_casefold_table.py` (`str.casefold`, `str.isdecimal`).

---

## 9. Phase 0–1 audit (2026-11-23)

Every Flet source in the phase 0–1 scope was read against the port:
the extension's `AudioPlayerHandler` and wrapper, `audio_engine.py`,
`queue_layout.py`, main.py's playback, persistence, shuffle, repeat and
lifecycle code, `now_playing.py`, `mini_player.py`, `queue_sheet.py`, and the
tracks part of `library.py` and `db_manager.get_all_tracks`.

Matched line for line: the handler (ledger, resume record, art, skip on
error, idle release, audio focus, notification config), the queue model and
every mutation, the op ordering and divergence repair, `_choose_resume` and
all five persistence files, shuffle on/off with the set-aside queue, repeat,
the position save loop, permissions, and the mini player, Now Playing and
queue sheet layouts and texts.

Gaps found and fixed in this pass:

1. Per-track DSP (`reapply_dsp`): EQ preset and Dynamism were not applied at
   all, and the Dynamism badge was missing (§1 row 14).
2. Haptics ignored `[haptics]`; the track menu now uses Flet's actions and
   intensities, and Play next / Add to queue give a haptic tick, not a
   snackbar, as in Flet.
3. Track rows lacked the format badge, Flet's subtitle rules and the
   playing-row tint; durations were rounded instead of truncated.
4. Missing title/artist/album fell back to "Unknown artist/album" instead of
   Flet's `basename` / "Unknown".
5. The "stopped" playback-failure snackbar used different wording.
6. The queue sheet hid the kept queue after a stop.

Still out of scope by phase (not gaps): the auto-play chain (2), the
tracks tab's selection mode and playlist rows (3), Start radio, Add to
playlist and Delete in the long-press menu (2–3), notification routing,
the ledger drain (3), EQ settings UI (with the settings phases).

---

## 10. Open items (handoff)

Last session: 2026-11-24. Commits on `flutter-port`: `4a116b9` (phases 0–1),
`c6ff28d` (audit fixes), `73ca39e` (plan update), then phase 2 (stations)
with the install script, and a merge of master (the Flet changes, `2ec2865`).
Phase 3 so far is seven commits, one per stage: strict analyzer and format,
Python primitives, migrations and the ledger drain, the scan engine, the
views' and playlists' queries, the library UI (views, scan UI, playlists,
long-press menu, selection, delete), and the tab bar with the Search
landing; then this plan; all pushed to origin. Then Settings (`e946579`)
and the Network view, local. Then phase 4a (the Companion protocol), 4b (notifications), 4c (the
sync manager) and 4d (the download queue and dock), local.

**Done**
- [x] Phase 0: backbone (read-only DB isolate, tracks tab, sort, search,
      coordinate graph, Android build, 16 KB pages).
- [x] Phase 1 code: audio handler, player controller, queue model, Flet
      persistence files, prefs, player UI, permissions, lifecycle hide/show,
      back-to-background.
- [x] Phase 0–1 audit against the Flet sources, and its six fixes (§9).
- [x] Port moved to its own branch, `flutter-port`.
- [x] Phase 2 code: golden export script, taxonomy, credit keys, artist
      graph, drift, stations, auto-play controller, chain button, queue
      sheet header, Auto-play sheet, Start radio, background prefill. All 20
      golden seeds match Python exactly (§4, phase 2).
- [x] Flet working-tree changes (plain-toggle chain, one-list workbench)
      committed on master and merged into `flutter-port`. They are the ground
      truth the phase 2 port was written against.
- [x] `tool/install_phone.sh`: build, install, copy a library in, start
      (§8). Release builds work (lint workaround, §1 row 22): 24.7 MB.
- [x] Full feature inventory of the Flet app (§11) and the modules §3 was
      missing (app shell, config, progress notifications, dialogs, dev and
      build scripts, tests).
- [x] Phase 3, read-only part: view bar, Genres, Artists, Albums, genre
      colours; goldens identical to Python (§4, phase 3). `pyTitle` now
      applies Final_Sigma as `str.title` does.
- [x] Phase 3b: the port owns migrations (`data/library_schema.dart`) and
      drains the play ledger (`data/play_ledger.dart`); goldens identical to
      Python on synthetic and real databases (§4, phase 3, second part).
      `push_db.sh` stops the app before replacing its database.
- [x] Installed on the phone (debug, 2026-11-02 library, permissions
      granted). The ledger drain runs there; its writes matched Python's.
- [x] Phase 3c: the library scan, tinytag 2.3.2 ported, storage paths;
      identical to Python on a synthetic corpus and on the Mac's 454-track
      music folder (§4, phase 3, the scan).
- [x] Phase 3d (first part): playlists (view, editor, add/remove/reorder,
      delete), the full long-press menu, selection mode, Delete Track;
      playlist writes identical to Python's (§4, phase 3, playlists).
- [x] Analyzer in Dart's strict modes (`strict-casts`, `strict-inference`,
      `strict-raw-types`; §12). `dart format` reflowed a few committed
      files; no code change in them.
- [x] Phase 3 plan checked against Flet (2026-11-24): no tag editor and
      no graph rebuild after a scan exist there (4d, 4f).
- [x] Phase 3g: tab bar (Search, Library) and the Search landing's local
      cards; queries identical to Python (§4, phase 3, the tab bar).
- [x] Phase 3h: Settings (hub, search, Storage, Appearance, Audio & DSP,
      Haptics, Permissions, Database Management, Advanced, About) and the
      config.toml writer, byte-identical to tomlkit on 192 writes (§4,
      phase 3, Settings). `push_db.sh` brings the bundle's config.toml.
      Release APK builds (25.7 MB).
- [x] Phase 3i: the Acoustic Network view and `build_metadata_edges`;
      identical to library.py's own builder on synthetic scenarios and on
      24 real seed × density cases with re-walks, and to Python's 13,788
      artist edges (§4, phase 3, the Acoustic Network view). Release APK:
      26.0 MB.
- [x] Phase 4 planned in seven stages (§4, phase 4). Stage a: the
      Companion client, multipart uploads and discovery, against Flet's
      own CompanionServer run hermetically (`tool/companion_test_server.py`);
      the Kotlin for the route address and the multicast lock. Found a
      Flet bug on the way: Send to Mac saves non-ASCII names
      percent-encoded (§7).
- [x] Phase 4b (progress notifications, tap routing) and 4c (the sync
      manager, on a virtual clock and against Flet's server).
- [x] Phase 4d: the download queue and dock, the companion download
      (identical files and results to Python's `download()` against
      Flet's server), NFC from Python's tables, `pick_download_dir`, the
      quiet scan and its "not analysed" card (§4, phase 4). Release APK:
      26.1 MB.

**To do, in order**
1. [ ] Install the current build on the phone: `tool/install_phone.sh
       --db` (now also brings the bundle's config.toml, so the accent, EQ
       and tabs match the Flet app). The user reports problems found in
       use. Check on the phone: the EQ curve and sliders, the folder
       navigator, Permissions after the system page, Clear Index; the
       Network view (turn it on in Appearance): pinch and pan, taps on
       small nodes, the pulse, Now Playing mode across track changes.
2. [ ] Back up `~/.android/debug.keystore` (§5) somewhere off this Mac.
3. [ ] Push `master` (one commit ahead of origin: `2ec2865`, already in
       `flutter-port`'s history). ~~`flutter-port`~~ pushed 2026-11-24.
4. [ ] Phase 3, in this order (§11 has the rows):
       a. ~~Decide how the two apps share writes until cutover~~. Decided
          2026-11-24: the Flet app's database stays the real one. The port
          writes only its own copy (data dir `.port`), a sandbox that
          `--db` replaces, discarding the port's writes. Its migrations
          are tested against copies until cutover.
       b. ~~Ledger drain and the migrations~~ (done).
       c. ~~Scan + `storage_paths`~~ (done).
          `progress_notify` moved to phase 4, NFC to 4 and 6 (§1 row 11,
          §3): the scan needs neither.
       d. ~~Playlists, delete, the rest of the long-press menu, selection
          mode~~ (done). ~~Tag edits~~: not a Flet feature (checked
          2026-11-24). `tagger.tag_file` only tags downloads (6, decision
          A); `extract_artwork` only serves the Mac (8). §3 updated.
       e. ~~Albums, Artists, Genres tabs; tab visibility~~ (done).
          Playlists with d.
       f. ~~Graph rebuild after a scan~~: Flet doesn't do one (checked
          2026-11-24). After a scan it reloads, schedules enrichment (7)
          and, after a quiet scan, posts the "not analysed" alert (4). New
          tracks have no features until analysed (decision B), so nothing
          in the geometry changes. The builders go where Flet calls them:
          `build_metadata_edges` (writes `track_neighbors`, read by the
          Network view and the assistant) with the Network view (3);
          `build_acoustic_edges` (an SVD; needs numpy-equal maths, cosine
          is sign-invariant per component) with the first thing that
          brings new features: sync offload or bundle import (4), or
          on-phone analysis (B), plus the startup readiness check and
          Settings' Recompute PCA; `build_genre_affinity` with it and
          with enrichment (7).
       g. ~~Search-tab landing cards from local data~~ (done, with the tab
          bar: §4, phase 3, the tab bar and Search landing).
       h. ~~Settings as a pushed page and its rows for all of the above~~
          (done: §4, phase 3, Settings).
       i. ~~The Network view with `build_metadata_edges`~~ (done: §4,
          phase 3, the Acoustic Network view). Phase 3's list is complete;
          what remains of it is phone verification (item 1).
       j. Phase 4 (sync, Android side) started 2026-11-24, before
          decisions A–C: none of its stages depends on them (transfers
          from the Mac matter more if A keeps downloads there). Stages a
          (protocol), b (notifications), c (the manager) and d (the
          download queue and dock) done. Next: e (the UI, which also
          wires the manager into the app and its lifecycle, and sets
          `AppDownloadHost.sync`); the table in §4, phase 4 has the order. Stage f and g bring the
          first callers of `build_metadata_edges`.
5. [ ] Decisions A, B, C (§6) before phase 6 is planned in detail.
6. [ ] Flet, on master: the album menu's Play Album tuple,
       Appearance's sort writing `library_sort`, the Network view's
       walk-seed node, and Send to Mac's file names (§7).
7. [ ] Decide on `assets/config_template.toml`: it is streamrip's
       GPL-3.0 template plus the app's sections (§12). The Flet APK already
       ships it; keep it, or write the port's own template.

**Waiting on a later phase** (not gaps): see §11, every 🔨 row.

---

## 11. Feature inventory: everything the Flet app does

Every user-visible feature and background job of the Flet app (master,
2026-11-24), with where it stands in the port. §3 is by module; this is by
feature, so nothing hides inside a large file. When a Flet feature is added,
add its row here.

Status: ✅ done · 🧪 written, not yet confirmed in use · 🔨 to build
(phase) · ⚠️ depends on a decision (§6) · ✗ dropped.

**Counts:** 41 of the 90 rows are ported (✅ or 🧪); 49 are to build or to decide. The ported rows are the player, the stations, the library views (the Acoustic Network included), playlists, the long-press menu and selection mode, the ledger drain, the scan, the tab bar, the Search landing's local cards, the download dock, and Settings for all of these.

### App shell (`main.py`)

| Feature | Flet source | Port |
|---|---|---|
| Accent colour from config | `_apply_accent` | ✅ |
| Haptic feedback, per-action intensity | `trigger_haptic` | 🧪 |
| Playback-error snackbar or notification card | `_on_playback_error_toast` | 🧪 |
| Back: close the open sheet, leave Settings, then to the background | `navigate_back`, `_on_confirm_pop` | 🧪 root screen, selection mode, Settings group → hub → out; grows with each screen |
| Lifecycle: stop UI work when hidden, prefill auto-play | `_on_lifecycle` | 🧪 (§1 row 18) |
| Tab bar (Jarvis optional, Search, Library), Settings as a pushed page, startup tab from `startup_page` | `_build_ui`, `_switch_tab` | 🧪 Search and Library, startup tab, fade, back, Settings (§7); Jarvis 🔨 5 |
| Splash while the app loads | `_build_splash` | 🔨 3, only if startup is slow on the phone |
| Notification taps routed to a screen (metadata sync, suggestions, download retry, transfers) | `_on_notification_navigate` | 🧪 the router, the Library, downloads and their Retry; transfers 🔨 4e; workbench 🔨 7 |
| Success sound after a download | `play_success_notification` | 🧪 the haptic (Flet's Android sound never played, §7); Mac 🔨 8 |
| Onboarding on first launch | `check_onboarding` | 🔨 7 |

### Startup and background jobs

| Job | Flet source | Port |
|---|---|---|
| Position save loop; queue saved on every mutation | `_position_save_loop`, `_on_queue_mutated` | 🧪 |
| Auto-play history from recent plays | `autoplay.load_history` | 🧪 |
| Ledger drain into `play_counts` / `playback_history` (track change, resume) | `_schedule_ledger_drain`, `play_ledger.py` | 🧪 |
| Similarity-graph readiness build (edges, coordinates) | `_ensure_graph_built_async`, `track_graph` builders | 🔨 4: it fires only when features exist without coordinates, i.e. after an offload or import; until then the port reads Python-built coordinates |
| Cache pruning | `_prune_caches_async` | 🔨 3 (album art); 6 (previews). The manual clears are in Settings → Advanced 🧪 |
| State bundle auto-export on every boot (Downloads and library folder) | `_auto_export_state_snapshot` | 🔨 4. The Mac offload and `fresh_build_android.sh` depend on it; Flet keeps doing it while installed |
| State bundle auto-import (`*_import.zip`) on boot, then graph rebuild | `_heavy_init` | 🔨 4 |
| Companion auto-discovery | `companion_manager.start` | 🔨 4 |
| Artist-metadata enrichment pass (MusicBrainz / Qobuz; after scans and once per launch; retries; notification) | `_enrich_metadata_async`, `metadata_enrich.enrich_pending` | 🔨 7. Stations read its `artist_enrichment`; until it runs, new artists borrow a profile from their sound neighbours |
| New-releases refresh (daily gate) | `ensure_new_releases_refresh`, `new_releases.py` | 🔨 6 |

### Player

| Feature | Port |
|---|---|
| Mini player, Now Playing, queue sheet (reorder, remove, Clear, Playing Next block) | 🧪 |
| Background playback, notification, lockscreen, headset, audio focus | 🧪 |
| Resume after process death (Flet's three files) | 🧪 |
| Shuffle with the set-aside queue; repeat modes | 🧪 |
| Per-track EQ and Dynamism; Dynamism badge | 🧪 |
| Artwork: embedded, sidecar | 🧪 |
| Auto-play: chain toggle, AUTO-PLAY header, Auto-play sheet, refills, skip/listen feedback, background prefill | 🧪 |
| Artwork from a URL (previews) | 🔨 6 |
| Quality selector sheet (download quality) | ⚠️ A |
| Download dock (progress of every download, from any tab) | 🧪 (Mac transfers; source downloads ⚠️ A) |

### Library tab (`ui/views/library.py`)

| Feature | Port |
|---|---|
| Tracks: rows, format badge, subtitle rules, sort modes, search, playing-row tint | ✅ / 🧪 |
| Long-press: Play next, Add to queue, Start radio | 🧪 |
| Long-press: Add to playlist (existing or new), Delete (file + database), Select multiple | 🧪 |
| Long-press: Send to the Mac | 🔨 4 |
| Long-press: Redownload | ⚠️ A |
| Selection mode: play, queue, add to playlist, send | 🧪 (send 🔨 4) |
| Albums: expandable rows; album menu (play, queue, send) | 🧪 (send 🔨 4) |
| Artists: artist → albums → tracks; edit-metadata button | 🧪 (editor 🔨 7) |
| Genres: genre groups and colours, play a genre, curate in the workbench | 🧪 (curate 🔨 7) |
| Playlists: create, rename and recolour, delete, reorder (drag), remove a track | 🧪 |
| Acoustic Network: 2-D map of the coordinates; density and step chips; zoom; tap to select; walk from a node, reseed; play or queue the walk; now-playing pulse; list panel with its own menu | 🧪 (golden-tested layout; phone check pending) |
| Which tabs show (Appearance setting) | 🧪 |
| Scan the library folder: progress, summary, "n tracks not analysed yet" | 🧪 (the alert follows the quiet scan after transfers, without Analyse: B) |
| Compute DSP on the phone (cancellable, notification) | ⚠️ B |
| Empty-library state with "enter paths" | 🧪 |

### Search tab (`ui/views/search.py`)

| Feature | Port |
|---|---|
| Landing: library stats card | 🧪 |
| Landing: Most Listened Tracks / Artists / Genres, opening in the Library | 🧪 |
| Landing: New From Your Artists shelf (public Deezer API + MusicBrainz, cached in `artist_releases`), manual refresh | 🔨 6 |
| Recent searches | 🔨 6 |
| Local-library hits in the results, ownership badges | 🔨 6 |
| Qobuz / Deezer search: hero result, album and artist cards, pagination, source switch, missing-credentials banner | ⚠️ A |
| Stream previews (progress card, cancel) | ⚠️ A |
| Download a result or a selection (quality sheet, dock, success sound, scan afterwards) | ⚠️ A |
| Demo mode (sample results) | ⚠️ C |

### Jarvis tab (`ui/views/assistant.py`, `assistant_*`, `llm_*`)

| Feature | Port |
|---|---|
| Chat with the LLM agent; tool calls into library and player; intents | 🔨 5 |
| Voice: push-to-talk, listening/thinking states, spoken replies (toggle) | 🔨 5 |
| Chat memory, clear chat, background/resume hooks | 🔨 5 |
| Web research for artists | 🔨 5 |

### Companion & Sync (`ui/views/sync_hub.py`, `utils/sync/`)

| Feature | Port |
|---|---|
| Android: find the Mac (UDP beacon), pair, unpair, manual connect, diagnosis | 🔨 4 |
| Android: check for and download transfers from the Mac | 🔨 4 (the manager and the download queue are done; the UI and the wiring are stage e) |
| Android: DSP offload to the Mac (progress notification, cancel from it) | 🔨 4 |
| Android: upload a state bundle | 🔨 4 |
| Mac: server toggle, LAN address, clear pending, reset pairing, open backups | Flet on the Mac until 8 |

### Settings (`ui/views/settings.py`), built with each phase (rule 4)

| Group | Contents | Port |
|---|---|---|
| Hub | Groups list, fuzzy settings search | 🧪 (search golden-tested) |
| Companion & Sync | Opens the sync hub | 🔨 4 |
| Storage & Paths | Library folder, downloads folder, default download quality | 🧪 folders and the Android navigator; ⚠️ A (quality) |
| Authentication | Qobuz (email/ID, password or token, app ID and secret), Deezer ARL | ⚠️ A |
| AI Assistant | Enable, provider and API key, model list, test connection, personality presets (built-in and custom) | 🔨 5 |
| Appearance | Accent colour, startup page, Jarvis tab, library tab visibility, landing sections | 🧪 (the Jarvis switch 🔨 5) |
| Audio & DSP | EQ on/off, presets, five bands, Dynamism | 🧪 |
| Haptic Feedback | On/off, per-action intensity | 🧪 |
| Metadata | Opens the workbench | 🔨 7 |
| Permissions | Status and grant buttons, how-to | 🧪 (statuses, the system-settings button) |
| Database Management | Wipe database, clear index, clear DSP | 🧪 |
| Database Management | Compute / recompute DSP, recompute PCA | ⚠️ B |
| Advanced | Raw TOML editor, preview and album-art cache clear | 🧪 |
| Advanced | State export / import | 🔨 4 |
| Advanced | Demo search, debug play counts, fresh-install reset | ⚠️ C |
| About | Version, credits, links | 🧪 (GitHub link copied, §7; licence page) |

### Metadata tools (phase 7)

| Feature | Port |
|---|---|
| Metadata workbench: one confidence-ordered list of every artist, search, review suggestions, sync (MusicBrainz, Qobuz, AI) | 🔨 7 |
| Artist metadata editor (country, genres, hand-set flag) | 🔨 7 |
| Genre curation | 🔨 7 |
| Metadata audit and dismissals | 🔨 7 |
| Artist aliases and alias rejections | 🔨 7 |
| AI artist research | 🔨 7 |
| Tag writing to files (only downloaded files, by `tag_file`; no editor) | ⚠️ A |

### Onboarding (phase 7)

| Feature | Port |
|---|---|
| Setup wizard: folders, credentials, appearance, startup tab | 🔨 7 |
| `.onboarded` marker; a restored install is not a first run | 🔨 7 |

### macOS (phase 8)

| Feature | Port |
|---|---|
| Mac player (AVFoundation engine) | 🔨 8 |
| Sync server and ffmpeg transcoder | 🔨 8 |

---

## 12. Third-party code and sources

Code the port copies, translates or depends on, for attribution in the
app's About page and the repository's notices. Add a row whenever outside
code or a published template comes in, before it is committed.

**Ported or copied into the source tree**

| What | Where in the port | Source | License |
|---|---|---|---|
| tinytag 2.3.2: the ID3, MPEG, FLAC, Ogg, WAVE and MP4 parsers, translated line by line | `lib/scan/tinytag.dart` (licence text in the header) | https://github.com/tinytag/tinytag (PyPI `tinytag==2.3.2`) | MIT, © 2014-2026 Tom Wallroth, Mat (mathiascode) et al. |
| `flet_audio_service` extension (audio handler, Kotlin notifications, artwork, PCM decoder) | `lib/player/audio_handler.dart`, `packages/maian_native` | This repository (the Flet app's own extension) | the project's |
| Unicode data tables (NFKD fold, casefold, decimal digits) generated from Python's `unicodedata` | `lib/core/nfkd_fold_table.dart`, `lib/core/py_unicode_tables.dart` | Unicode Character Database 15.1, via CPython | Unicode License v3 (data) |
| tomlkit 0.15.2: `InlineTable.as_string`, translated; the rest of its editing behaviour reproduced from its outputs | `lib/core/config_file.dart` (licence text above `_InlineTable`) | https://github.com/python-poetry/tomlkit (PyPI `tomlkit==0.15.2`) | MIT, © 2018 Sébastien Eustace |
| CPython's `math.hypot` (`vector_norm` in Modules/mathmodule.c: scaling, lossless squaring and summation, one differential correction), translated | `lib/core/py_num.dart` `pyHypot` | https://github.com/python/cpython (3.13) | PSF License v2 |
| config.toml template: streamrip's `config.toml` plus the app's `[dsp]`, `[haptics]`, `[assistant]`, `[appearance]` sections (utils/config.toml, copied as is) | `assets/config_template.toml` | https://github.com/nathom/streamrip, via the Flet app | GPL-3.0 (streamrip). The Flet APK ships the same file; decide before release (§10 item 7) |
| streamrip-derived helpers (filename cleaning; in the Flet app's `filepath_utils.py`, credited there) | `clean_filename` only, written from its behaviour (drop `\/*?:"<>|`, cut at 255 UTF-8 bytes, strip, NFC), not translated: `lib/downloads/file_names.dart` | https://github.com/nathom/streamrip | GPL-3.0: no streamrip code copied; check before porting anything else from it |
| Unicode canonical decompositions, combining classes and composition exclusions, generated from Python's `unicodedata` | `lib/core/nfc_table.dart` (with `lib/core/nfc.dart`) | Unicode Character Database 15.1, via CPython | Unicode License v3 (data) |

**Templates and guidance adopted**

| What | Where | Source | License |
|---|---|---|---|
| Strict analyzer modes (`strict-casts`, `strict-inference`, `strict-raw-types`) on top of `flutter_lints` | `analysis_options.yaml` | dart.dev, "Customizing static analysis" (enabling additional type checks) | BSD-3-Clause (Dart SDK docs and `lints`) |
| `ReorderableListView` with `ReorderableDragStartListener` handles for playlist reordering | `library/browse_view.dart` | Flutter SDK (material) | BSD-3-Clause |

**Dependencies (pub.dev)**: `sqlite3`, `path_provider`, `audio_service`,
`just_audio`, `audio_session`, `permission_handler`, `toml`. Their licences
ship in the app through Flutter's `LicenseRegistry` (`showLicensePage`), so
Settings → About opens it (Licenses).

**Candidates for coming steps** (found 2026-11-24, not yet evaluated)

| Need | Candidate | Notes |
|---|---|---|
| Tag writing (`tagger.py`, with downloads, 6) | [`metatagger`](https://pub.dev/packages/metatagger) (pure Dart: ID3v2, vorbis comments, iTunes ilst); [`metadata_god`](https://pub.dev/documentation/metadata_god/latest/) and [`haudiotagger`](https://pub.dev/packages/haudiotagger) (Rust lofty via flutter_rust_bridge); [`flutter_taglib`](https://pub.dev/documentation/flutter_taglib/1.3.2/) (TagLib over FFI) | Pure Dart keeps the build free of Rust (decision B's spirit). Whatever is chosen must leave tags that mutagen and tinytag read back the same. |

