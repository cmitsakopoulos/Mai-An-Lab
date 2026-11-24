# Mai An Lab: Flet → Flutter port map

*Status: 2026-11-24. Branch `flutter-port` (not pushed). Phase 0 (backbone)
done. Phase 1 (phone player) is written and audited against the Flet sources
(§9). Phase 2 (stations) is written and matches the Python output exactly on
a real library (§4, phase 2). Phase 3 is under way: the Genres, Artists and
Albums views, the migrations the port now owns, the play-ledger drain, the
library scan (with tinytag ported to Dart), playlists, the full
long-press menu, selection mode and Delete Track, and the tab bar with
the Search tab's local landing cards (§4, phase 3). Desktop
tests pass (`flutter test`: 264, including the real-library golden tests
that need the gitignored goldens; analyzer clean in Dart's strict modes);
debug and release APKs build.
It is installed on the phone (2026-11-24, with the 2026-11-02 library, an
older build: no scan yet). The user reports problems found in use; fix
them, or record intended differences in §7. Nothing ships from this port yet. 29 of the 90 Flet features in §11
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
| 7 | **Notifications** | `POST_NOTIFICATIONS` requested at runtime (Android 13+). Kotlin `ProgressNotifications` and alert notifications. Tapping one navigates via `takePendingNavigate`. | Keep the Kotlin (`packages/maian_native`). Playback-error cards work. Taps are received and drained but not routed: with one screen there is nowhere to go yet. | 1–3 | 🧪 |
| 8 | **Long tasks with the screen off** | Kotlin `LibraryTaskService`, foreground type `dataSync`. The first job starts it and the last one stops it. | Keep the Kotlin. ⚠️ Android 15+ caps `dataSync` foreground services at about 6 h per 24 h. Long offloads or downloads must handle the timeout callback. Verify on device. | 3–4 | ♻️⚠️ |
| 9 | **Reading music files** | `MANAGE_EXTERNAL_STORAGE` (all-files access) + `READ_MEDIA_AUDIO` + legacy storage permissions. Plain file paths under `/storage/emulated/0/Music`. | Same permissions via `permission_handler`, requested before the player starts (as Flet did), minus RECORD_AUDIO until phase 5. ⚠️ Play Store policy rejects all-files access, so sideload only, as now. | 1 | 🧪 |
| 10 | **Music folder rules** | `storage_paths.py`: never return an app-private folder, downloads follow the library folder, fallbacks are never written back | `scan/storage_paths.dart` (resolvers and `is_app_private`; the platform and sandbox roots are passed in) and `scan/library_folder.dart` (`sync_config_to_ui`'s inputs: `library_path` pref, `[downloads] folder`). Python's tests ported (`storage_paths_test.dart`); `pick_download_dir` comes with downloads (6). | 3 | 🧪 |
| 11 | **Path identity** | Absolute, platform-specific paths: phone `/storage/emulated/0/Music/…`, Mac `/Users/…/Desktop/Music/…`. `play_counts.track_path` is the join key. `normalize_nfc` is applied only where a file is *named* by the app: downloads (`streamrip_api`, `queue_controller`) and files received by the sync server. | The scanner stores `os.scandir` paths unnormalised, and so does the port (golden-tested), so phase 3 needs no normaliser. Phase 4 (sync) and 6 (downloads) do: `unorm_dart` (MIT, Unicode 17, §12) is the candidate. Search already strips NFD combining marks (`fold`). | 4, 6 | 🔨 |
| 12 | **Artwork** | Kotlin `resolveArtwork`: embedded art via `MediaMetadataRetriever`, then the sidecar file, cached by sha1 | Keep the Kotlin. The handler resolves it for the current and next rows; sidecar lookup is lazy (per folder, not per queue row). | 1 | 🧪 |
| 13 | **Audio analysis** | Kotlin `decodePcm` (MediaCodec, to mono PCM) + `dsp.py` (numpy, 906 lines). Usually offloaded to the Mac (sync). | Keep the decoder. The feature maths becomes Dart, or never runs on the phone (§6, decision B). | 6 | ⚠️ |
| 14 | **EQ and loudness boost** | `set_eq_band_gain` and `set_loudness_boost` in the extension (just_audio Android effects). `reapply_dsp` runs on every track change: config.toml `[dsp]` preset + Dynamism (boost and EQ contour from the track's play_counts features). | In the handler (`setEqBandGain`, `setLoudnessBoost`). `player/dsp.dart` ports `reapply_dsp` with Python reference values; the controller applies it on every row change, including background auto-advance. Now Playing shows the Dynamism badge. Gains set before the device's EQ bands load are applied when they arrive. No settings UI yet (the Flet-written config is honoured). | 1 | 🧪 |
| 15 | **Voice** | `flutter_tts`, `speech_to_text`, `RECORD_AUDIO`, `<queries>` for `RecognitionService` | Same plugins | 5 | ♻️ |
| 16 | **Plain HTTP to the Mac** | `usesCleartextTraffic="true"` app-wide | `network_security_config` permitting cleartext only for local or private addresses (the sync server speaks plain HTTP) | 4 | 🔨 |
| 17 | **LAN discovery** | UDP beacon on port 54545, `CHANGE_WIFI_MULTICAST_STATE`, listens for 10 s while the sync page is open | `RawDatagramSocket`, plus a small Kotlin call to hold a `WifiManager.MulticastLock`. ⚠️ Recheck local-network permission rules before raising targetSdk past 36. | 4 | 🔨⚠️ |
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
| `.config/streamrip/config.toml` (Android; on the Mac it is under `Library/Application Support/streamrip/`). Holds `general.library_sort`, `appearance.accent_color`, `[dsp]`, `[haptics]`, `[downloads] folder` and credentials. | `config.py` | phase 1 ✅ (sort, accent, `[dsp]`, `[haptics]`: `core/app_config.dart`); 3 (`[downloads] folder`, for the scan folder) | the phase that owns each setting |
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
| 5 | `track_neighbors` (the assistant's "more by this artist"; stations don't read it) |
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
| `queue_controller.py` (the DOWNLOAD queue, despite the name), `history_manager.py` (download history) | ~530 | Port with downloads | 6 |
| `ui/player/now_playing`, `mini_player`, `queue_sheet` | 1,087 | Rewrite ✅ (`player/ui/`). The auto-play chain is shown disabled until phase 2. | 1 |
| `ui/player/quality_selector` (download quality) | 168 | Rewrite | 6 |
| `db_manager.py`, read half | ~2,000 | Port, one method at a time on demand | 0–3 |
| `db_manager.py`, write half and migrations | ~2,000 | Port. Migrations ✅ (`data/library_schema.dart`: `get_connection` + `initialize`, golden-tested on fresh, legacy and real databases); `record_listens` ✅; the scan's writes ✅; playlists ✅ (`data/playlists.dart`, golden-tested); the rest with its features | 3 |
| `ui/views/library.py` | 4,915 | Rewrite, one tab at a time (tracks ✅ rows, sort, search; view bar, genres, artists, albums ✅ (`library/browse_*`, `genre_colors.dart`); playlists, the long-press menu, selection mode ✅ (`library/track_menu.dart`, `library_dialogs.dart`, `selection.dart`); network graph to come) | 1–3 |
| `track_graph.py` (stations: coordinate graph, artist-graph cache, `Station`, `open_station`, `walk`), `artist_graph.py`, `autoplay.py`, `genre_taxonomy.py`, `credit_keys` (in `metadata_enrich`) | ~2,100 | Port ✅ (`stations/`, `player/autoplay.dart`, `core/py_text.dart`; golden-tested) | 2 |
| `track_graph.py` builders (`build_acoustic_edges`, `build_genre_affinity`, `build_metadata_edges`, `bulk_analyze_library`), `genre_similarity.py` (builds the NPMI model) | ~500 | Port where Flet calls them (§10 4f): metadata edges with the Network view (3), the acoustic build with offload/import (4), analysis with decision B | 3, 4, 6 |
| `harmonic.py` | 132 | Not needed by stations (key was removed from the geometry); port only if a feature needs it | n/a |
| `ui/player/autoplay_sheet.py` | 156 | Rewrite ✅ (`player/ui/autoplay_sheet.dart`) | 2 |
| `library_scanner.py`, `storage_paths.py` + `tinytag` 2.3.2 (the tag reader the APK bundles, ~2,500 lines, MIT) | ~2,900 | Port ✅ (`scan/library_scanner.dart`, `scan/tinytag.dart`, `scan/storage_paths.dart`, `data/scan_writes.dart`; golden-tested on a synthetic corpus and the Mac's music folder) | 3 |
| `tagger.py`, `metadata_editor.py` (mutagen) | ~240 | No Flet feature edits tags. `tagger.tag_file` runs only after a download (`streamrip_api`): port with downloads (decision A), tag writer candidates in §12. `extract_artwork` runs only off Android (the Kotlin resolver serves the phone): Mac | 6, 8 |
| `sync/client`, `discovery`, `send`, parts of `manager` | ~1,900 | Port (Android side) | 4 |
| `sync/server.py`, `transcoder.py` (ffmpeg) | ~1,340 | Keep on Mac until phase 8 | 8 |
| `ui/views/sync_hub.py` | 1,012 | Rewrite | 4 |
| `state_export.py` | 294 | Port (bundle import/export) | 4 |
| `llm_engine.py` (Gemini/Ollama over an OpenAI-compatible API) | 231 | Port: plain HTTP + JSON | 5 |
| `llm_tools.py`, `assistant_runner.py`, `assistant_intent.py`, `chat_memory.py`, `web_research.py` | ~4,500 | Port, with golden intent and tool-call tests | 5 |
| `ui/views/assistant.py` | 861 | Rewrite | 5 |
| `qobuz.py`, `deezer.py`, `streamrip_api.py`, `streamrip_search.py`, `downloadable.py`, `search_results.py`, `source_metadata.py`, `new_releases.py` + deezer-py, streamrip, pycryptodomex | ~3,000 + libraries | ⚠️ Port or keep on Mac (§6, decision A) | 6 |
| `ui/views/search.py`, `ui/player/download_dock.py` | 3,806 | Rewrite. The landing's local cards ✅ (`search/`, `data/landing_queries.dart`, golden-tested); the rest with the sources | 3, 6 |
| `dsp.py`, `pca_engine.py` | 1,044 | ⚠️ Port to Dart, or keep on Mac (§6, decision B) | 6 |
| `metadata_enrich`, `metadata_ai`, `metadata_audit`, `metadata_qobuz`, `ui/player/metadata_workbench.py` | ~5,000 | Port last (desktop-heavy) | 7 |
| `ui/views/settings.py` | 3,932 | Rewrite, spread across phases (rule 4) | all |
| `ui/player/onboarding_modal.py` | 895 | Rewrite, smaller | 7 |
| `ui/widgets.py`, `ui/tokens.py` | 769 | Rewrite (tokens ✅) | as needed |
| `audio_engine_macos.py` (pyobjc / AVFoundation) | 1,311 | Rewrite for the Mac | 8 |
| main.py, the app shell (everything but playback): splash, tab bar and startup tab ✅ (`app_shell.dart`: Search and Library), back navigation and confirm-pop, Settings as a pushed page, startup jobs (state-zip auto-import, state auto-export, enrichment pass, new-releases refresh, graph readiness build, cache pruning, companion start), maintenance actions (wipe, fresh-install reset, clear index/DSP/caches), delete track/playlist ✅, notification routing, success sound | ~2,500 | Rewrite, each piece with the feature behind it (§11) | 3–7 |
| `config.py` (streamrip config: load, defaults, `repair_config`) | 552 | Read half ported for the keys in use (`core/app_config.dart`). Writes and `repair_config` with the settings that need them. | 1, then all |
| `progress_notify.py` (job notifications: progress, summary text, retry action, task-service start/stop) | 239 | Port: a Dart wrapper over the Kotlin `showProgressNotification` (not wrapped yet), with the summary and retry rules. A scan the user starts posts nothing (in-app banner only); the scan's alert ("n tracks not analysed yet") follows only the automatic scan after a download or transfer. First users: sync (4), then downloads (6), analysis (B), metadata (7). | 4 |
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
| Notification taps | Routed to the target tab | Received and dropped (none of their targets is ported) | When the target screens are. |
| DSP after background auto-advance | `reapply_dsp` runs only while the Python session hears the player, so tracks reached with the session torn down keep the previous track's settings | Applied on every row change | Intentional. |
| Track row | Format badge, Flet subtitle rules, tint on the playing row | Same | Fixed in the phase 0–1 audit. |
| Sort in Artists and Genres | One `sort_mode` for every view, coerced on each switch (Tracks → Artists → Tracks loses the Tracks sort; Genres keeps an unrelated mode and shows no tick) | Tracks and Albums share one sort, as in Flet; Artists and Genres each keep their own, starting at A–Z | Intentional. |
| Album long-press → Play Album | Passes `("album", album, artist)`, which `_play_track_core` unpacks as `(artist, album)`: outside shuffle the query finds nothing and it says "No playable media found." | Plays the album from its first track | Port is right. The Flet fix is one line (`library.py` `_open_album_context_menu`, swap the tuple); not made on master yet. |
| Search in Albums, Artists, Genres | `LIKE` on the album title / artist / genre name, plus albums and artists whose tracks match FTS; then closest match | The Tracks view's semantics (every word, accent-blind, substring) on the same fields and through the tracks; Genres by name only, as in Flet; the same closest-match scoring | Intentional, as for Tracks. |
| Network tab | Shown when enabled | Hidden until ported (§10 4i) | Until then. |
| Fix artist info, Curate in Workbench | Buttons on the artist and genre rows | Not shown | Phase 7, with the workbench. |
| Opening the database | `get_connection` + `initialize` commit after each migration step | The same steps in one transaction | Intentional: a failure leaves the file as it was. Same result otherwise (golden-tested). |
| Ledger line that is JSON but not an object (`[1]`, `5`) | `raw.get` raises outside the try: the drain fails, and the `.draining` file fails again on every later drain | Dropped like a torn line | Intentional. The handler never writes one. |
| Drain triggers | Each track change (3 s), resume, native ready | Each listen logged (3 s), show, library open | Same moments: a track change is when the outgoing listen is logged. |
| Scan: `tracks.added_date` | `st_mtime` (nanoseconds, as a double) | `FileStat.modified` (dart:io has milliseconds only) | Differs by under 1 ms; the scanner rereads a file only when its mtime moved by over 1 s, so a Flet-scanned library is not reread. Exact on whole-second mtimes (the synthetic goldens). |
| Scan: a row with `added_date` NULL | `abs(None - mtime)` raises and the scan dies | Treated as changed: the file is reread | Intentional. Only very old databases have such rows. |
| Scan: ASF/AIFF content in an `.aac` file | tinytag's ASF or AIFF parser | Skipped as unreadable | Not ported (§4, the scan); never seen in a library. |
| Search tab without source credentials | "Setup Required" prompt; no landing cards (they show only once Qobuz or Deezer is set up, or demo mode is on) | The landing cards always | Intentional: the port has no source search yet (decision A), and the cards only read the library. |
| Search tab header | Title, source switch, connection signal, recent searches, settings shortcut, search box, result tabs | The title only | The rest with the sources (6) and Settings (3h). |
| Startup tab | `general.startup_page`: Jarvis, Search, Library or Settings | Search, else Library | Jarvis (5) and Settings (3h) aren't ported. |
| Empty library | "ENTER PATHS" opens Settings → Storage | No button (Settings isn't ported); the scan button works | Until Settings (3h). |
| Untagged files on every scan | Never get a row, so each scan rereads them and counts them in "Indexed n items." | The same | Kept for parity. |
| Playlist search | `LIKE` on the name (ASCII case only), then closest match | The other views' semantics: every word, accent- and case-blind for every script; same closest-match scoring | Intentional, as for Tracks. Golden-tested on ASCII queries, where both agree. |
| Playlist rows after a reload | Opened by a tap: drag handle and remove button. Re-opened by `load_library` (after an add): plain rows, no handle | Always the handle and the remove button | Port is right; Flet's generator omits `playlist_id`. |
| Playlist count after removing a track | The row's "n tracks" stays stale until the next reload | Updated at once | Port is right. |
| Add several tracks to a playlist | One transaction per track | One transaction for all | Same rows (golden-tested); a failure adds none instead of some. |
| Saving the playlist editor with a taken name | The IntegrityError is lost in the task; nothing is shown | "Could not save playlist: UNIQUE constraint failed: playlists.name" | Intentional. |
| Back in selection mode | Leaves the app (only Search's selection mode is closed by back) | Leaves selection mode | Intentional: back closes the innermost state, as Flet does for Search. |
| Writes on a read-only library (`LIBRARY_DB` override) | n/a (Flet always owns its database) | Add to Playlist and Delete Track are disabled | Dev-only case. |
| Library reload after a scan or delete | The snackbar follows `load_library` (no graph rebuild there) | Rows reload, the snackbar shows, the station graphs rebuild behind | Same order as Flet. |

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
  `tools/state_backups/` into the port. Its paths are Android paths, so tracks
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
  `~/miniconda3/bin/python` from `mai_an_flutter/`. The scan export needs
  tinytag 2.3.2 apart from the dev env:
  `~/miniconda3/bin/pip install --target tool/.pydeps tinytag==2.3.2`
  (gitignored). Generated tables: `tool/gen_nfkd_table.py`,
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
landing; then this plan. `flutter-port` is pushed to origin.

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

**To do, in order**
1. [ ] Install the current build on the phone: `tool/install_phone.sh`
       (the database is already in; `--db` resets it). The user reports
       problems found in use.
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
       h. Settings as a pushed page (the Search header's shortcut, the
          empty library's ENTER PATHS) and its rows for all of the above
          (rule 4), incl. the EQ, Dynamism and haptics UI.
       i. The Network view (library.py's Acoustic Network: map, walk,
          list panel; ~2,000 Flet lines) with `build_metadata_edges`, which
          writes the `track_neighbors` it reads (4f).
5. [ ] Decisions A, B, C (§6) before phase 6 is planned in detail.
6. [ ] Flet, on master: the album menu's Play Album tuple (§7).

**Waiting on a later phase** (not gaps): see §11, every 🔨 row.

---

## 11. Feature inventory: everything the Flet app does

Every user-visible feature and background job of the Flet app (master,
2026-11-24), with where it stands in the port. §3 is by module; this is by
feature, so nothing hides inside a large file. When a Flet feature is added,
add its row here.

Status: ✅ done · 🧪 written, not yet confirmed in use · 🔨 to build
(phase) · ⚠️ depends on a decision (§6) · ✗ dropped.

**Counts:** 29 of the 90 rows are ported (✅ or 🧪); 61 are to build or to decide. The ported rows are the player, the stations, the library views, playlists, the long-press menu and selection mode, the ledger drain, the scan, the tab bar and the Search landing's local cards.

### App shell (`main.py`)

| Feature | Flet source | Port |
|---|---|---|
| Accent colour from config | `_apply_accent` | ✅ |
| Haptic feedback, per-action intensity | `trigger_haptic` | 🧪 |
| Playback-error snackbar or notification card | `_on_playback_error_toast` | 🧪 |
| Back: close the open sheet, leave Settings, then to the background | `navigate_back`, `_on_confirm_pop` | 🧪 root screen and selection mode; grows with each screen |
| Lifecycle: stop UI work when hidden, prefill auto-play | `_on_lifecycle` | 🧪 (§1 row 18) |
| Tab bar (Jarvis optional, Search, Library), Settings as a pushed page, startup tab from `startup_page` | `_build_ui`, `_switch_tab` | 🧪 Search and Library, startup tab, fade, back (§7); Settings 🔨 3h, Jarvis 🔨 5 |
| Splash while the app loads | `_build_splash` | 🔨 3, only if startup is slow on the phone |
| Notification taps routed to a screen (metadata sync, suggestions, download retry, transfers) | `_on_notification_navigate` | 🔨 with each target screen |
| Success sound after a download | `play_success_notification` | 🔨 6 |
| Onboarding on first launch | `check_onboarding` | 🔨 7 |

### Startup and background jobs

| Job | Flet source | Port |
|---|---|---|
| Position save loop; queue saved on every mutation | `_position_save_loop`, `_on_queue_mutated` | 🧪 |
| Auto-play history from recent plays | `autoplay.load_history` | 🧪 |
| Ledger drain into `play_counts` / `playback_history` (track change, resume) | `_schedule_ledger_drain`, `play_ledger.py` | 🧪 |
| Similarity-graph readiness build (edges, coordinates) | `_ensure_graph_built_async`, `track_graph` builders | 🔨 4: it fires only when features exist without coordinates, i.e. after an offload or import; until then the port reads Python-built coordinates |
| Cache pruning | `_prune_caches_async` | 🔨 3 (album art); 6 (previews) |
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
| Download dock (progress of every download, from any tab) | ⚠️ A |

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
| Acoustic Network: 2-D map of the coordinates; density and step chips; zoom; tap to select; walk from a node, reseed; play or queue the walk; now-playing pulse; list panel with its own menu | 🔨 3. Read-only (coordinates and `walk`), so it could come before the writes |
| Which tabs show (Appearance setting) | 🧪 read from config (the setting row 🔨 3) |
| Scan the library folder: progress, summary, "n tracks not analysed yet" | 🧪 (the alert after automatic scans 🔨 4) |
| Compute DSP on the phone (cancellable, notification) | ⚠️ B |
| Empty-library state with "enter paths" | 🧪 (the button 🔨 with Settings) |

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
| Android: check for and download transfers from the Mac | 🔨 4 |
| Android: DSP offload to the Mac (progress notification, cancel from it) | 🔨 4 |
| Android: upload a state bundle | 🔨 4 |
| Mac: server toggle, LAN address, clear pending, reset pairing, open backups | Flet on the Mac until 8 |

### Settings (`ui/views/settings.py`), built with each phase (rule 4)

| Group | Contents | Port |
|---|---|---|
| Hub | Groups list, fuzzy settings search | 🔨 3 |
| Companion & Sync | Opens the sync hub | 🔨 4 |
| Storage & Paths | Library folder, downloads folder, default download quality | 🔨 3 (folders); ⚠️ A (quality) |
| Authentication | Qobuz (email/ID, password or token, app ID and secret), Deezer ARL | ⚠️ A |
| AI Assistant | Enable, provider and API key, model list, test connection, personality presets (built-in and custom) | 🔨 5 |
| Appearance | Accent colour, startup page, Jarvis tab, library tab visibility, landing sections | 🔨 3 (accent is read ✅) |
| Audio & DSP | EQ on/off, presets, five bands, Dynamism | 🔨 3 (Flet's values are honoured 🧪) |
| Haptic Feedback | On/off, per-action intensity | 🔨 3 (honoured 🧪) |
| Metadata | Opens the workbench | 🔨 7 |
| Permissions | Status and grant buttons, how-to | 🔨 3 |
| Database Management | Wipe database, clear index, clear DSP | 🔨 3 |
| Database Management | Compute / recompute DSP, recompute PCA | ⚠️ B |
| Advanced | Raw TOML editor, preview and album-art cache clear | 🔨 3 |
| Advanced | State export / import | 🔨 4 |
| Advanced | Demo search, debug play counts, fresh-install reset | ⚠️ C |
| About | Version, credits, links | 🔨 3 |

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
| streamrip-derived helpers (filename cleaning; in the Flet app's `filepath_utils.py`, credited there) | not ported yet (phase 6) | https://github.com/nathom/streamrip | GPL-3.0: check before porting any of it |

**Templates and guidance adopted**

| What | Where | Source | License |
|---|---|---|---|
| Strict analyzer modes (`strict-casts`, `strict-inference`, `strict-raw-types`) on top of `flutter_lints` | `analysis_options.yaml` | dart.dev, "Customizing static analysis" (enabling additional type checks) | BSD-3-Clause (Dart SDK docs and `lints`) |
| `ReorderableListView` with `ReorderableDragStartListener` handles for playlist reordering | `library/browse_view.dart` | Flutter SDK (material) | BSD-3-Clause |

**Dependencies (pub.dev)**: `sqlite3`, `path_provider`, `audio_service`,
`just_audio`, `audio_session`, `permission_handler`, `toml`. Their licences
ship in the app through Flutter's `LicenseRegistry` (`showLicensePage`), so
the About page (3h) only needs to open it.

**Candidates for coming steps** (found 2026-11-24, not yet evaluated)

| Need | Candidate | Notes |
|---|---|---|
| NFC for paths the app names (sync 4, downloads 6) | [`unorm_dart`](https://pub.dev/packages/unorm_dart) 0.3.2 | MIT, pure Dart, Unicode 17, no dependencies. Golden-test against `unicodedata.normalize("NFC")` as py_text was. |
| Tag writing (`tagger.py`, with downloads, 6) | [`metatagger`](https://pub.dev/packages/metatagger) (pure Dart: ID3v2, vorbis comments, iTunes ilst); [`metadata_god`](https://pub.dev/documentation/metadata_god/latest/) and [`haudiotagger`](https://pub.dev/packages/haudiotagger) (Rust lofty via flutter_rust_bridge); [`flutter_taglib`](https://pub.dev/documentation/flutter_taglib/1.3.2/) (TagLib over FFI) | Pure Dart keeps the build free of Rust (decision B's spirit). Whatever is chosen must leave tags that mutagen and tinytag read back the same. |

