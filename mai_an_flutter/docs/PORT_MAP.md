# Mai An Lab: Flet → Flutter port map

*Status: 2026-11-24. Branch `flutter-port` (not pushed). Phase 0 (backbone)
done. Phase 1 (phone player) is written and audited against the Flet sources
(§9). Phase 2 (stations) is written and matches the Python output exactly on
a real library (§4, phase 2). Desktop tests pass (`flutter test`: 159,
including 41 real-library golden tests that need the gitignored goldens;
analyzer clean); debug and release APKs
build. Neither phase has run on the phone yet. Nothing ships from this port
yet. 16 of the 90 Flet features in §11 are ported.*

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
   `library.db`. Python owns the schema, migrations and every write until
   phase 3. Until then the port opens the database read-only.
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
| 10 | **Music folder rules** | `storage_paths.py`: never return an app-private folder, downloads follow the library folder, fallbacks are never written back | Port the rules and their tests together (about 200 lines). | 3 | 🔨 |
| 11 | **Path identity** | Absolute, platform-specific paths: phone `/storage/emulated/0/Music/…`, Mac `/Users/…/Desktop/Music/…`. NFC-normalised everywhere (`normalize_nfc`). `play_counts.track_path` is the join key. | Dart has no Unicode normaliser. A package such as `unorm_dart` is needed before the port writes any path (scanner, sync). Search already strips NFD combining marks (`fold`). | 3 | 🔨⚠️ |
| 12 | **Artwork** | Kotlin `resolveArtwork`: embedded art via `MediaMetadataRetriever`, then the sidecar file, cached by sha1 | Keep the Kotlin. The handler resolves it for the current and next rows; sidecar lookup is lazy (per folder, not per queue row). | 1 | 🧪 |
| 13 | **Audio analysis** | Kotlin `decodePcm` (MediaCodec, to mono PCM) + `dsp.py` (numpy, 906 lines). Usually offloaded to the Mac (sync). | Keep the decoder. The feature maths becomes Dart, or never runs on the phone (§6, decision B). | 6 | ⚠️ |
| 14 | **EQ and loudness boost** | `set_eq_band_gain` and `set_loudness_boost` in the extension (just_audio Android effects). `reapply_dsp` runs on every track change: config.toml `[dsp]` preset + Dynamism (boost and EQ contour from the track's play_counts features). | In the handler (`setEqBandGain`, `setLoudnessBoost`). `player/dsp.dart` ports `reapply_dsp` with Python reference values; the controller applies it on every row change, including background auto-advance. Now Playing shows the Dynamism badge. Gains set before the device's EQ bands load are applied when they arrive. No settings UI yet (the Flet-written config is honoured). | 1 | 🧪 |
| 15 | **Voice** | `flutter_tts`, `speech_to_text`, `RECORD_AUDIO`, `<queries>` for `RecognitionService` | Same plugins | 5 | ♻️ |
| 16 | **Plain HTTP to the Mac** | `usesCleartextTraffic="true"` app-wide | `network_security_config` permitting cleartext only for local or private addresses (the sync server speaks plain HTTP) | 4 | 🔨 |
| 17 | **LAN discovery** | UDP beacon on port 54545, `CHANGE_WIFI_MULTICAST_STATE`, listens for 10 s while the sync page is open | `RawDatagramSocket`, plus a small Kotlin call to hold a `WifiManager.MulticastLock`. ⚠️ Recheck local-network permission rules before raising targetSdk past 36. | 4 | 🔨⚠️ |
| 18 | **Lifecycle** | `on_app_lifecycle_state_change`: suspend UI updates in the background, prefill the autoplay buffer on hide, run assistant and companion background hooks, pick up listens finished in the background | `AppLifecycleListener`. Flutter stops drawing frames by itself. Done: the position ticker stops, the position is saved and the auto-play buffer is topped up to 24 on hide. Listens reach auto-play in-process (§7), so there is no catch-up to do for it. Still to come: the ledger drain into the database (3), assistant and companion hooks (4–5). | 1–4 | 🔨 |
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
| `library.db` (main database; schema below) | `db_manager.py` | phase 0 ✅ | phase 3 |
| `.config/streamrip/config.toml` (Android; on the Mac it is under `Library/Application Support/streamrip/`). Holds `general.library_sort`, `appearance.accent_color`, `[dsp]`, `[haptics]` and credentials. | `config.py` | phase 1 ✅ (sort, accent, `[dsp]`, `[haptics]`: `core/app_config.dart`) | the phase that owns each setting |
| `queue_state.json`, `queue_pos.json`, `native_resume.json` | main.py, engine | 1 ✅ | 1 ✅ (`autoplay_held`, the queue auto-play set aside: 2 ✅) |
| `.cache/queue_regular.json`, `.cache/queue_shuffle.json` (the queue set aside while shuffling the library) | main.py | 1 ✅ | 1 ✅ |
| `flet_prefs.json` (`_save_pref`; shuffle, repeat, similar-play prefs) | main.py | 1 ✅ | 1–2 ✅ (`is_shuffle`, `repeat_mode`, `play_similar_mode`; merge-write as in Flet) |
| `play_ledger.jsonl` (one line per finished listen, written by the audio handler) | handler; drained by `play_ledger.py` | 3 (drain) | 1 ✅ (same format and ids). Lines accumulate until the port drains them into `play_counts` / `playback_history` in phase 3. |
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
| 3 | `playlists`, `playlist_tracks`, `fts_search`. FTS5 is kept in step by 11 triggers stored in the database, so port writes stay consistent for free. |
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
| `play_ledger.py` (drain into the database) | 126 | Port: needs database writes | 3 |
| `queue_controller.py` (the DOWNLOAD queue, despite the name), `history_manager.py` (download history) | ~530 | Port with downloads | 6 |
| `ui/player/now_playing`, `mini_player`, `queue_sheet` | 1,087 | Rewrite ✅ (`player/ui/`). The auto-play chain is shown disabled until phase 2. | 1 |
| `ui/player/quality_selector` (download quality) | 168 | Rewrite | 6 |
| `db_manager.py`, read half | ~2,000 | Port, one method at a time on demand | 0–3 |
| `db_manager.py`, write half and migrations | ~2,000 | Port | 3 |
| `ui/views/library.py` | 4,915 | Rewrite, one tab at a time (tracks ✅ rows, sort, search, long-press menu with Start radio; selection mode with phase 3; albums, artists, genres, playlists, network graph to come) | 1–3 |
| `track_graph.py` (stations: coordinate graph, artist-graph cache, `Station`, `open_station`, `walk`), `artist_graph.py`, `autoplay.py`, `genre_taxonomy.py`, `credit_keys` (in `metadata_enrich`) | ~2,100 | Port ✅ (`stations/`, `player/autoplay.dart`, `core/py_text.dart`; golden-tested) | 2 |
| `track_graph.py` builders (`build_acoustic_edges`, `build_genre_affinity`, `build_metadata_edges`, `bulk_analyze_library`), `genre_similarity.py` (builds the NPMI model) | ~500 | Port with the database writes and analysis | 3, 6 |
| `harmonic.py` | 132 | Not needed by stations (key was removed from the geometry); port only if a feature needs it | n/a |
| `ui/player/autoplay_sheet.py` | 156 | Rewrite ✅ (`player/ui/autoplay_sheet.dart`) | 2 |
| `library_scanner.py`, `tagger.py`, `metadata_editor.py`, `storage_paths.py` | ~800 | Port. Tag writing needs a Dart or Rust tag library (mutagen equivalent). | 3 |
| `sync/client`, `discovery`, `send`, parts of `manager` | ~1,900 | Port (Android side) | 4 |
| `sync/server.py`, `transcoder.py` (ffmpeg) | ~1,340 | Keep on Mac until phase 8 | 8 |
| `ui/views/sync_hub.py` | 1,012 | Rewrite | 4 |
| `state_export.py` | 294 | Port (bundle import/export) | 4 |
| `llm_engine.py` (Gemini/Ollama over an OpenAI-compatible API) | 231 | Port: plain HTTP + JSON | 5 |
| `llm_tools.py`, `assistant_runner.py`, `assistant_intent.py`, `chat_memory.py`, `web_research.py` | ~4,500 | Port, with golden intent and tool-call tests | 5 |
| `ui/views/assistant.py` | 861 | Rewrite | 5 |
| `qobuz.py`, `deezer.py`, `streamrip_api.py`, `streamrip_search.py`, `downloadable.py`, `search_results.py`, `source_metadata.py`, `new_releases.py` + deezer-py, streamrip, pycryptodomex | ~3,000 + libraries | ⚠️ Port or keep on Mac (§6, decision A) | 6 |
| `ui/views/search.py`, `ui/player/download_dock.py` | 3,806 | Rewrite | 6 |
| `dsp.py`, `pca_engine.py` | 1,044 | ⚠️ Port to Dart, or keep on Mac (§6, decision B) | 6 |
| `metadata_enrich`, `metadata_ai`, `metadata_audit`, `metadata_qobuz`, `ui/player/metadata_workbench.py` | ~5,000 | Port last (desktop-heavy) | 7 |
| `ui/views/settings.py` | 3,932 | Rewrite, spread across phases (rule 4) | all |
| `ui/player/onboarding_modal.py` | 895 | Rewrite, smaller | 7 |
| `ui/widgets.py`, `ui/tokens.py` | 769 | Rewrite (tokens ✅) | as needed |
| `audio_engine_macos.py` (pyobjc / AVFoundation) | 1,311 | Rewrite for the Mac | 8 |
| main.py, the app shell (everything but playback): splash, tab bar and startup tab, back navigation and confirm-pop, Settings as a pushed page, startup jobs (state-zip auto-import, state auto-export, enrichment pass, new-releases refresh, graph readiness build, cache pruning, companion start), maintenance actions (wipe, fresh-install reset, clear index/DSP/caches), delete track/playlist, notification routing, success sound | ~2,500 | Rewrite, each piece with the feature behind it (§11) | 3–7 |
| `config.py` (streamrip config: load, defaults, `repair_config`) | 552 | Read half ported for the keys in use (`core/app_config.dart`). Writes and `repair_config` with the settings that need them. | 1, then all |
| `progress_notify.py` (job notifications: progress, summary text, retry action, task-service start/stop) | 239 | Port: a Dart wrapper over the Kotlin `showProgressNotification` (not wrapped yet), with the summary and retry rules. First user: the scan. | 3 |
| `ui/player/dialogs.py` (confirm and text-input dialogs) | 100 | Rewrite as needed | 3 |
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
| 1 | **Phone player** 🧪 (code done, phone checks below pending) | Plays the phone's real library with the screen off. Notification, lockscreen and headset controls work. The queue survives process death in Flet's JSON format. Permissions work. | 4–5k |
| 2 | **Stations** 🧪 (code done; phone checks below pending) | "Play Similar" matches Flet's stations: golden tests on 20 fixed seeds from a `state_backups` bundle. Autoplay sheet. | 3k |
| 3 | **Library writes** | Ledger drain, scan (with its notification), tag edits, playlists, delete. The port owns migrations (respects `user_version`). Remaining library tabs, selection mode, the full long-press menu, the graph rebuild after a scan, the Search tab's local landing cards. §11 has the list. | 5k |
| 4 | **Sync (Android side)** | Phone ↔ Flet-Mac pairing, transfers and DSP offload. Bundle import and export (incl. the boot-time auto-export and auto-import). | 3k |
| 5 | **Assistant** | Golden intent and tool-call tests pass. Voice. | 4k |
| 6 | **Sources and analysis** | Search, previews, downloads, "New from your artists", on-phone analysis: as far as decisions A and B put them on the phone | 4–6k |
| 7 | **Metadata tools, onboarding** | Feature parity, incl. the automatic enrichment pass | 4k |
| 8 | **macOS** | Mac player on Flutter; sync server moves over. Needs Xcode or CI. | 3k |

### Phase 1: checks still to run on the phone

The desktop tests cover the queue model, the persistence files and the
controller against a fake player. These need the real one
(`tool/install_phone.sh --db`, §8):

1. First launch: the permission prompts, then All-files access in Settings.
2. Tap a track: it plays; the mini player and the notification appear with art.
3. Screen off for a few tracks: auto-advance continues; lockscreen and
   headset next/previous work; headphones unplugged pauses.
4. Back on the library: the app goes to the background and keeps playing;
   reopening it shows the right track.
5. `adb shell am kill` (or swipe from Recents) while paused, then reopen: the
   queue comes back on the same track and offset. Compare `queue_state.json`,
   `queue_pos.json` and `native_resume.json` with a Flet-written set.
6. Shuffle on and off mid-track: the track doesn't restart; the old queue
   comes back.
7. Queue sheet: reorder, remove, Clear; Play next and Add to queue land in the
   up-next block.
8. A deleted file in the queue: skipped with a snackbar (foreground) or a
   notification card (background).
9. Paused for 10 minutes: the notification goes away; Play brings it back.
10. `play_ledger.jsonl` lines match the Flet format (`id` is the same URI).
11. EQ and Dynamism: with a Flet-written `[dsp]` (e.g. Rock preset, Dynamism
    on), the sound changes per track and Now Playing shows "DYNAMISM ACTIVE
    +x.x dB" for analysed tracks. Check the first track after a cold start
    too (its gains are queued until the EQ bands load).
12. Haptics: long-press on a track and Play next / Add to queue vibrate at the
    `[haptics]` intensities; nothing when haptics are off.
13. Edge-to-edge: the sheets (Now Playing, queue, track menu) clear the
    status and gesture bars (§1 row 19).
14. Measure the release APK (`flutter build apk --release`) for §1 row 22.

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

**Phone checks still to run:**

1. Chain on: the rest of the queue goes aside, four recommendations appear
   under the AUTO-PLAY header, the snackbar says the queue is on hold. Off:
   the queue comes back after your picks.
2. Play through a few recommendations: the buffer stays at 3–4, all from the
   same genre; the sheet shows "now in <genre>".
3. Screen off for a long while: auto-play keeps going (24 queued on hide);
   at the end of the buffer it refills or appends and plays.
4. Skip two recommendations early (under 30 s) or remove them: the buffer is
   replaced once.
5. Long-press → Start radio on a library track: it plays now and a station
   starts from it.
6. A track with no coordinates: "isn't analysed yet" and the queue comes back.
7. Kill and reopen with auto-play on: still on, the set-aside queue survives
   (`autoplay_held` in queue_state.json), the buffer tops up.
8. Shuffle on turns auto-play off, and auto-play on turns shuffle order off.

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
| Notification taps | Routed to the target tab | Received and dropped (one screen) | When there are screens to route to. |
| DSP after background auto-advance | `reapply_dsp` runs only while the Python session hears the player, so tracks reached with the session torn down keep the previous track's settings | Applied on every row change | Intentional. |
| Track row | Format badge, Flet subtitle rules, tint on the playing row | Same | Fixed in the phase 0–1 audit. |

---

## 8. Dev workflow

- **Device:** Android phone over USB, or Wireless debugging.
  `tool/install_phone.sh` builds, installs and starts the port without a
  terminal session attached (`--help` for the flags):
  - `--db [bundle.zip|library.db]`: copy a library in first (debug builds
    only; see below). The port's database is a copy until phase 3 writes:
    rerun `--db` to pick up a newer bundle.
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

**To do, in order**
1. [ ] Phone: `tool/install_phone.sh --db`, then the phase 1 checks (§4,
       items 1–14) and the phase 2 checks (§4, phase 2, items 1–8). Then
       `--release` and repeat checks 2–3 and phase 2's 1–3 (R8 shrinking has
       not run on a device). Record any difference in §7 or fix it.
2. [ ] Back up `~/.android/debug.keystore` (§5) somewhere off this Mac.
3. [ ] Push `master` and `flutter-port`.
4. [ ] Phase 3, in this order (§11 has the rows):
       a. Decide how the two apps share writes until cutover (rule 2). The
          port has its own data dir (`.port`), so today it writes only its
          own copy; the question is which app's database is the real one.
       b. Ledger drain (`play_ledger.py`): the first write; then the
          migrations the port must own.
       c. Scan + `progress_notify` + `storage_paths` + NFC paths (§1 rows
          10–11).
       d. Playlists, delete, the rest of the long-press menu, selection mode.
       e. Albums, Artists, Genres, Playlists tabs; tab visibility.
       f. Graph rebuild after a scan (`build_metadata_edges`,
          `build_acoustic_edges`, `build_genre_affinity`).
       g. Search-tab landing cards from local data (stats, most listened).
       h. The settings rows for all of the above (rule 4), incl. the EQ,
          Dynamism and haptics UI.
5. [ ] Decisions A, B, C (§6) before phase 6 is planned in detail.

**Waiting on a later phase** (not gaps): see §11, every 🔨 row.

---

## 11. Feature inventory: everything the Flet app does

Every user-visible feature and background job of the Flet app (master,
2026-11-24), with where it stands in the port. §3 is by module; this is by
feature, so nothing hides inside a large file. When a Flet feature is added,
add its row here.

Status: ✅ done · 🧪 written, not yet checked on the phone · 🔨 to build
(phase) · ⚠️ depends on a decision (§6) · ✗ dropped.

**Counts:** 16 of the 90 rows are ported (✅ or 🧪); 74 are to build or to decide. The ported rows are the player and the stations, which carry most of the daily use.

### App shell (`main.py`)

| Feature | Flet source | Port |
|---|---|---|
| Accent colour from config | `_apply_accent` | ✅ |
| Haptic feedback, per-action intensity | `trigger_haptic` | 🧪 |
| Playback-error snackbar or notification card | `_on_playback_error_toast` | 🧪 |
| Back: close the open sheet, leave Settings, then to the background | `navigate_back`, `_on_confirm_pop` | 🧪 root screen only; grows with each screen |
| Lifecycle: stop UI work when hidden, prefill auto-play | `_on_lifecycle` | 🧪 (§1 row 18) |
| Tab bar (Jarvis optional, Search, Library), Settings as a pushed page, startup tab from `startup_page` | `_build_ui`, `_switch_tab` | 🔨 3: the port has the Library screen only |
| Splash while the app loads | `_build_splash` | 🔨 3, only if startup is slow on the phone |
| Notification taps routed to a screen (metadata sync, suggestions, download retry, transfers) | `_on_notification_navigate` | 🔨 with each target screen |
| Success sound after a download | `play_success_notification` | 🔨 6 |
| Onboarding on first launch | `check_onboarding` | 🔨 7 |

### Startup and background jobs

| Job | Flet source | Port |
|---|---|---|
| Position save loop; queue saved on every mutation | `_position_save_loop`, `_on_queue_mutated` | 🧪 |
| Auto-play history from recent plays | `autoplay.load_history` | 🧪 |
| Ledger drain into `play_counts` / `playback_history` (track change, resume) | `_schedule_ledger_drain`, `play_ledger.py` | 🔨 3 (ledger lines written 🧪) |
| Similarity-graph readiness build (edges, coordinates) | `_ensure_graph_built_async`, `track_graph` builders | 🔨 3; until then the port reads Python-built coordinates |
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
| Long-press: Add to playlist (existing or new), Delete (file + database), Select multiple | 🔨 3 |
| Long-press: Send to the Mac | 🔨 4 |
| Long-press: Redownload | ⚠️ A |
| Selection mode: play, queue, add to playlist, send | 🔨 3 (send 4) |
| Albums: expandable rows; album menu (play, queue, send) | 🔨 3 (send 4) |
| Artists: artist → albums → tracks; edit-metadata button | 🔨 3 (editor 7) |
| Genres: genre groups and colours, play a genre, curate in the workbench | 🔨 3 (curate 7) |
| Playlists: create, rename, delete, reorder (drag, up/down), remove a track | 🔨 3 |
| Acoustic Network: 2-D map of the coordinates; density and step chips; zoom; tap to select; walk from a node, reseed; play or queue the walk; now-playing pulse; list panel with its own menu | 🔨 3. Read-only (coordinates and `walk`), so it could come before the writes |
| Which tabs show (Appearance setting) | 🔨 3 |
| Scan the library folder: progress, summary, "n tracks not analysed yet" | 🔨 3 |
| Compute DSP on the phone (cancellable, notification) | ⚠️ B |
| Empty-library state with "enter paths" | 🔨 3 |

### Search tab (`ui/views/search.py`)

| Feature | Port |
|---|---|
| Landing: library stats card | 🔨 3 |
| Landing: Most Listened Tracks / Artists / Genres, opening in the Library | 🔨 3 (current counts need the ledger drain) |
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
| Tag writing to files | 🔨 3 |

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
