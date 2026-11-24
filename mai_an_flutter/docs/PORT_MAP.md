# Mai An Lab: Flet → Flutter port map

*Status: 2026-11-24. Branch `flutter-port` (on origin up to phase 3g; the
Settings, Network and phase 4 commits are local). Phase 0 (backbone)
done. Phase 1 (phone player) is written and audited against the Flet sources
(§9). Phase 2 (stations) is written and matches the Python output exactly on
a real library (§4, phase 2). Phase 3's code is complete: the Genres, Artists and
Albums views, the migrations the port now owns, the play-ledger drain, the
library scan (with tinytag ported to Dart), playlists, the full
long-press menu, selection mode and Delete Track, the tab bar with
the Search tab's local landing cards, Settings with the config.toml
writer, and the Acoustic Network view (§4, phase 3). Phase 4's code (sync,
Android side) is complete: the Companion protocol and discovery, tested
against Flet's own server, notifications, the sync manager, the
download queue and dock that bring the Mac's transfers onto the phone,
the Companion & Sync page, its dialogs and Send to Mac in the menus,
state bundles (export, import, the boot snapshot and import, backup to the
Mac), and the DSP offload with the acoustic build (the SVD geometry and the
genre model, identical in effect to Python's: the same stations on all 20
golden seeds) (§4, phase 4). Phase 5 (the assistant) is planned in seven
stages; stages a (the intent parser and text helpers, identical to Python
on 4,961 utterances), b (the LLM client and tool declarations), c (the
runner's deterministic half and its database and player adapters, identical
to Python across 14 conversations / 124 turns) and d (the agent loop and
tool executor, identical to Python across 51 tool cases and 9 HTTP-recorded
loop scenarios) are done (§4, phase 5).
Desktop tests pass (`flutter test`: 685, including the real-library golden
tests that need the gitignored goldens; analyzer clean in Dart's strict
modes); debug and release APKs build.
It is installed on the phone (2026-11-24, with the 2026-11-02 library, an
older build: no scan, no Settings yet). The user reports problems found in use; fix
them, or record intended differences in §7. Nothing ships from this port yet. 54 of the 91 Flet features in §11
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
| 13 | **Audio analysis** | Kotlin `decodePcm` (MediaCodec, to mono PCM) + `dsp.py` (numpy, 906 lines). Usually offloaded to the Mac (sync). | The offload is ported (4g), and so is the geometry built from the features (`build_acoustic_edges`: the SVD, no numpy). Keep the decoder. The feature maths becomes Dart, or never runs on the phone (§6, decision B). | 4, 6 | 🧪⚠️ |
| 14 | **EQ and loudness boost** | `set_eq_band_gain` and `set_loudness_boost` in the extension (just_audio Android effects). `reapply_dsp` runs on every track change: config.toml `[dsp]` preset + Dynamism (boost and EQ contour from the track's play_counts features). | In the handler (`setEqBandGain`, `setLoudnessBoost`). `player/dsp.dart` ports `reapply_dsp` with Python reference values; the controller applies it on every row change, including background auto-advance. Now Playing shows the Dynamism badge. Gains set before the device's EQ bands load are applied when they arrive. No settings UI yet (the Flet-written config is honoured). | 1 | 🧪 |
| 15 | **Voice** | `flutter_tts`, `speech_to_text`, `RECORD_AUDIO`, `<queries>` for `RecognitionService` | Same plugins | 5 | ♻️ |
| 16 | **Plain HTTP to the Mac** | `usesCleartextTraffic="true"` app-wide | Nothing to declare: dart:io sockets don't go through Android's cleartext policy (this SDK's dart:io has no such check). INTERNET is declared. Confirm on the phone with the first real pairing. | 4 | 🧪 |
| 17 | **LAN discovery** | UDP query every 2 s on port 54545, answered by unicast; beacons dropped (no multicast lock: pyjnius isn't in the build) | `sync/discovery.dart` (`RawDatagramSocket`), with `MainActivity`'s `acquireMulticastLock` and `defaultRouteIpv4` (§4, phase 4a). ⚠️ Recheck local-network permission rules before raising targetSdk past 36. | 4 | 🧪⚠️ |
| 18 | **Lifecycle** | `on_app_lifecycle_state_change`: suspend UI updates in the background, prefill the autoplay buffer on hide, run assistant and companion background hooks, pick up listens finished in the background | `AppLifecycleListener`. Flutter stops drawing frames by itself. Done: the position ticker stops, the position is saved and the auto-play buffer is topped up to 24 on hide. Listens reach auto-play in-process (§7), so there is no catch-up to do for it. The ledger drain runs on show, at start and 3 s after each listen (phase 3, 🧪). The Companion manager's background and resume hooks (phase 4e, 🧪). Still to come: the assistant's (5). | 1–5 | 🔨 |
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
| `library.db` (main database; schema below) | `db_manager.py` | phase 0 ✅ | phase 3: migrations and the ledger drain ✅ (desktop and phone); 4: offloaded features, coordinates and the genre model ✅ |
| `.config/streamrip/config.toml` (Android; on the Mac it is under `Library/Application Support/streamrip/`). Holds `general.library_sort`, `appearance.accent_color`, `[dsp]`, `[haptics]`, `[downloads] folder` and credentials. | `config.py`, `streamrip_api.update_config_params` | phase 1 ✅ (sort, accent, `[dsp]`, `[haptics]`: `core/app_config.dart`); 3 ✅ (`[downloads] folder`, `[landing]`, `appearance.show_*`) | 3 ✅: created from the template when missing, and Settings' writes, byte for byte as tomlkit 0.15.2 leaves them (`core/config_file.dart`, golden-tested); the rest of the keys with the settings that own them |
| `queue_state.json`, `queue_pos.json`, `native_resume.json` | main.py, engine | 1 ✅ | 1 ✅ (`autoplay_held`, the queue auto-play set aside: 2 ✅) |
| `.cache/queue_regular.json`, `.cache/queue_shuffle.json` (the queue set aside while shuffling the library) | main.py | 1 ✅ | 1 ✅ |
| `flet_prefs.json` (`_save_pref`; shuffle, repeat, similar-play prefs) | main.py | 1 ✅ | 1–2 ✅ (`is_shuffle`, `repeat_mode`, `play_similar_mode`; merge-write as in Flet) |
| `play_ledger.jsonl` (one line per finished listen, written by the audio handler) | handler; drained by `play_ledger.py` | 3 (drain) | 1 ✅ (same format and ids). Drained into `play_counts` / `playback_history` by `data/play_ledger.dart` (3 ✅, on the phone too). |
| `companion_transfers.json` | sync | 4 | 4 |
| `.onboarded` | onboarding | 7 | 4 ✅ (written by the boot import, as Flet does); 7 (the wizard) |
| `recent_searches.json` | search | 4 | 4 |
| `chat_history.json` (the Jarvis conversation, 15-minute expiry), `graph_state.json` (the assistant's last graph check) | `chat_memory.py` | 5 ✅ (`assistant/chat_memory.dart`) | 5 ✅ (byte for byte as `json.dump(indent=4)`; used from stage f) |
| `downloads.db`, `failed_downloads.db` | streamrip layer | 6 | 6 |
| `temp/` (artwork cache, `.nomedia`) | `filepath_utils` | not needed ✅ | not needed ✅ (on Android Flet already used the Kotlin cache, `cacheDir/np_art`, which the port shares) |
| `mai_an_lab_state_*.zip` (export bundle, written to `/sdcard/Download`) | `state_export.py` | 4 ✅ (`data/state_export.dart`; Flet's bundles import and the other way round, tested against Python) | 4 ✅ (the boot snapshot and boot import under the port's own names until cutover, §5) |

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
| 4 | `play_counts` features (the offload's batches), `play_counts.pca_coords` and `genre_affinity` (the acoustic build) ✅ (`data/graph_build.dart`) |
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
| `track_graph.py` builders (`build_acoustic_edges`, `build_genre_affinity`, `build_metadata_edges`, `bulk_analyze_library`), `genre_similarity.py` (builds the NPMI model), `pca_engine.redundant_raw_features` | ~640 | Port where Flet calls them (§10 4f): `build_metadata_edges` ✅ (`data/network_queries.dart`), `build_acoustic_edges` and `build_genre_affinity` ✅ (`stations/acoustic_geometry.dart`, `stations/genre_npmi.dart`, `data/graph_build.dart`; golden-tested, §4 phase 4g), called after a boot import, an offload, at the startup readiness check and by Recompute PCA; `bulk_analyze_library` with decision B | 3, 4, 6 |
| `harmonic.py` | 132 | Not needed by stations (key was removed from the geometry); port only if a feature needs it | n/a |
| `ui/player/autoplay_sheet.py` | 156 | Rewrite ✅ (`player/ui/autoplay_sheet.dart`) | 2 |
| `library_scanner.py`, `storage_paths.py` + `tinytag` 2.3.2 (the tag reader the APK bundles, ~2,500 lines, MIT) | ~2,900 | Port ✅ (`scan/library_scanner.dart`, `scan/tinytag.dart`, `scan/storage_paths.dart`, `data/scan_writes.dart`; golden-tested on a synthetic corpus and the Mac's music folder) | 3 |
| `tagger.py`, `metadata_editor.py` (mutagen) | ~240 | No Flet feature edits tags. `tagger.tag_file` runs only after a download (`streamrip_api`): port with downloads (decision A), tag writer candidates in §12. `extract_artwork` runs only off Android (the Kotlin resolver serves the phone): Mac | 6, 8 |
| `sync/client`, `discovery`, `send`, parts of `manager` | ~1,900 | Port (Android side) ✅. Client and listener (`sync/companion_client.dart`, `sync/discovery.dart`; tested against Flet's own server); the manager (`sync/sync_manager.dart`); `send.py` (`sync/send.dart`) | 4 |
| `sync/server.py`, `transcoder.py` (ffmpeg) | ~1,340 | Keep on Mac until phase 8 | 8 |
| `ui/views/sync_hub.py` | 1,012 | Rewrite ✅ (`settings/sync_hub.dart`: the Android view's four cards; the Mac's server view with phase 8) | 4 |
| `state_export.py` | 294 | Port ✅ (`data/state_export.dart`, `core/zip_archive.dart`, `sync/state_bundles.dart`) | 4 |
| `llm_engine.py` (Gemini/Ollama over an OpenAI-compatible API) | 231 | Port ✅ (`assistant/llm_engine.dart`, golden-tested) | 5 |
| `llm_tools.py`, `assistant_runner.py`, `assistant_intent.py`, `chat_memory.py`, `web_research.py` | ~4,500 | Port, with golden intent and tool-call tests (§4, phase 5). `assistant_intent` ✅ (`assistant/intent.dart`), `chat_memory` ✅, `llm_tools`' text helpers and `names_mentioned` ✅ (`assistant/agent_text.dart`, `assistant/web_text.dart`), over `core/py_re.dart` (Python's `re` semantics); `assistant_runner` (deterministic half + `_dispatch_llm` agent loop and bridges) ✅ (`assistant/runner.dart`, `assistant/runner_phrases.dart`, `assistant/adapters.dart`, `data/assistant_queries.dart`, `core/py_difflib.dart`, `core/py_values.dart`); `llm_tools.execute_tool` for the library, playback, status and read-only metadata tools ✅ (`assistant/tool_executor.dart`) | 5 |
| `ui/views/assistant.py` | 861 | Rewrite | 5 |
| `qobuz.py`, `deezer.py`, `streamrip_api.py`, `streamrip_search.py`, `downloadable.py`, `search_results.py`, `source_metadata.py`, `new_releases.py` + deezer-py, streamrip, pycryptodomex | ~3,000 + libraries | ⚠️ Port or keep on Mac (§6, decision A) | 6 |
| `ui/views/search.py`, `ui/player/download_dock.py` | 3,806 | Rewrite. The landing's local cards ✅ (`search/`, `data/landing_queries.dart`, golden-tested); the dock ✅ (`downloads/download_dock.dart`, 4d); the rest with the sources | 3, 4, 6 |
| `dsp.py`, `pca_engine.py` | 1,044 | `pca_engine.redundant_raw_features` ✅ (with the acoustic build, 4g); the rest of `pca_engine` is genre-taxonomy re-exports (ported in phase 2). `dsp.py`: ⚠️ port to Dart, or keep on Mac (§6, decision B) | 4, 6 |
| `metadata_enrich`, `metadata_ai`, `metadata_audit`, `metadata_qobuz`, `ui/player/metadata_workbench.py` | ~5,000 | Port last (desktop-heavy) | 7 |
| `ui/views/settings.py` | 3,932 | Rewrite, spread across phases (rule 4). Phase 3 ✅ (`settings/`): the hub and its search, Storage, Appearance, Audio & DSP (curve, presets), Haptics, Permissions, Database Management (Recompute PCA with phase 4g), Advanced (caches, raw TOML; state export and import with phase 4f), About; Companion & Sync with phase 4e. The other groups with their phases (§11). | all |
| `ui/player/onboarding_modal.py` | 895 | Rewrite, smaller | 7 |
| `ui/widgets.py`, `ui/tokens.py` | 769 | Rewrite (tokens ✅) | as needed |
| `audio_engine_macos.py` (pyobjc / AVFoundation) | 1,311 | Rewrite for the Mac | 8 |
| main.py, the app shell (everything but playback): splash, tab bar and startup tab ✅ (`app_shell.dart`: Search and Library), back navigation and confirm-pop, Settings as a pushed page ✅ (`settings/settings_page.dart`), startup jobs (state-zip auto-import ✅, state auto-export ✅, enrichment pass, new-releases refresh, graph readiness build ✅, cache pruning, companion start ✅), maintenance actions (wipe, clear index/DSP, caches ✅ `data/maintenance.dart`; fresh-install reset: decision C), delete track/playlist ✅, notification routing, success sound | ~2,500 | Rewrite, each piece with the feature behind it (§11) | 3–7 |
| `config.py` (streamrip config: load, defaults, `repair_config`) + `streamrip_api.py`'s `load_config`, `update_config_params`, `ensure_config_exists` | 552 | Read half for the keys in use (`core/app_config.dart`). Writes ✅ (`core/config_file.dart`: `update_config_params` as tomlkit edits the text, the template copy of `ensure_config_exists`). Its repair half (`fill_missing` from a newer template) when the template next changes (§7). | 1, 3, then all |
| `progress_notify.py` (job notifications: progress, summary text, retry action, task-service start/stop) | 239 | Port ✅ (`core/progress_notify.dart`, phase 4b): a Dart wrapper over the Kotlin `showProgressNotification`, with the throttle, summary and retry rules; the metadata and analysis helpers with 7 and B. A scan the user starts posts nothing (in-app banner only); the scan's alert ("n tracks not analysed yet") follows only the automatic scan after a download or transfer. First users: sync (4), then downloads (6), analysis (B), metadata (7). | 4 |
| `ui/player/dialogs.py` (confirm and text-input dialogs) | 100 | Rewrite ✅ (`library/library_dialogs.dart`: playlist editor, name dialog, delete confirmations) | 3 |
| `search_history.py` (`recent_searches.json`), `text_match.py` | 81 | Port with Search | 6 |
| streamrip-derived helpers: `client.py`, `metadata_objects.py`, `ssl_utils.py`, `exceptions.py`, `filepath_utils.py` | ~330 | Go with the source clients (decision A) | 6 |
| `mock_search.py`, `genre_eval.py`, `error_boundary.py` | ~400 | Drop or fold in (Flutter has `ErrorWidget` and zones). Demo search mode depends on `mock_search` (decision C). | n/a |
| Dev scripts: `check_spoofer.py`, `configure_paths.py`, `scratch_test_search.py`, `tools/*.py` (enrich_artists, dev_fake_features, eval_ai_metadata, eval_metadata_fusion, simulate_notifications_live) | ~1,000 | Keep on Mac: they read `library.db` or call Python modules directly; none ships in the APK | n/a |
| Build and device scripts: `build_android.sh`, `fresh_build_android.sh` (+ `.ps1`), `_adb_common.sh`, `usb_probe.sh` | n/a | Replaced by `flutter build` + `tool/install_phone.sh`; `push_db.sh` stands in for the state reinject. A bundle-based reinstall works by hand: put a bundle at `/sdcard/Download/mai_an_lab_port_state_import.zip` and start the port (§4, phase 4f). | 1 ✅, 4 ✅ |
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

### Phase 5: the assistant

The Jarvis tab: `assistant_intent.py` (417 lines), `assistant_runner.py`
(2,620), `llm_engine.py` (231), `llm_tools.py` (1,144), `chat_memory.py`
(120), `web_research.py` (233), `ui/views/assistant.py` (861), Settings →
AI Assistant, and voice through the extension's `flutter_tts` and
`speech_to_text`. It needs none of decisions A–C. Stages, one commit each:

| | Stage | Flet source | |
|---|---|---|---|
| a | The intent parser and text helpers: `parse` (Python `re` → Dart `RegExp`), `strip_markdown`, `_slim_track`, `ChatMemoryManager` (`chat_history.json`, `graph_state.json`), `names_mentioned` | `assistant_intent.py`, `llm_tools.py` (helpers), `chat_memory.py`, `web_research.py` (`_fold`, `names_mentioned`) | ✅ |
| b | The LLM client and the tool declarations: one OpenAI-compatible request (Gemini and Ollama URLs, headers, timeouts, the retry after a connection failure), `_parse_tool_calls`, the raw message kept for the echo, `list_gemini_models`; `get_agent_tools()` | `llm_engine.py`, `llm_tools.get_agent_tools` | ✅ |
| c | The runner's deterministic half: dispatch, the pending confirmation and choice, anaphora, every `_handle_*`, `_resolve_queries` (with difflib's `SequenceMatcher` ported), the avoid set, and its database and player adapters | `assistant_runner.py`, `db_manager.py` (assistant queries) | ✅ |
| d | The agent: `_dispatch_llm`'s loop and prompt, `execute_tool` for the library, playback, status and read-only metadata tools | `assistant_runner._dispatch_llm`, `llm_tools.execute_tool` | ✅ |
| e | The web tools: `ask` (DuckDuckGo Lite, Wikipedia, Google grounding), `learn_about`, `find_related_artists` | `web_research.py`, `metadata_ai`'s search half | 🔨 |
| f | The Jarvis tab and Settings → AI Assistant | `ui/views/assistant.py`, `settings.py` (AI Assistant), main.py's tab and lifecycle hooks | 🔨 |
| g | Voice: replies spoken, push-to-talk, the microphone permission | the extension's `tts_*`/`stt_*`, `assistant.py` | 🔨 |

**Stage a.** Pure functions, no UI. `assistant/intent.dart` is
`assistant_intent.py`: Flet's 41 patterns character for character, the
filler loops, `_clean_query` and the greeting pre-check. They run through
`core/py_re.dart`, which rewrites a Python pattern so Dart's RegExp
(JavaScript's) matches what Python's `re` matches: `\s` as `str.isspace`
(JavaScript adds U+FEFF and leaves out U+001C–U+001F and U+0085), `\w` and
`\b` Unicode-aware from Python's own tables (`core/py_re_tables.dart`,
`tool/gen_py_re_tables.py`), `re.IGNORECASE` as sre applies it ('i' also
takes İ and ı, 's' takes ſ, 'k' the Kelvin sign), `.` stopping only at
`\n`, `$` also before a final `\n`, and `re.MULTILINE` seeing only `\n`;
syntax it doesn't convert throws. `assistant/agent_text.dart` holds
`strip_markdown` and `_slim_track`, `assistant/web_text.dart`
`names_mentioned` and `_fold` (`nfkdCasefoldStrip`, with
`core/nfkd_casefold_table.dart` for U+0345 and the planes beyond the
BMP), and `assistant/chat_memory.dart` is `ChatMemoryManager`
(`chat_history.json` and `graph_state.json` as `json.dump(indent=4)`
writes them: `pyJsonDumps` gained `indent` and `ensureAscii`). On the way,
`pyLower` and `pyCasefold` moved to tables generated from Python
(`tool/gen_casefold_table.py`): Dart's toLowerCase is from older Unicode
data and missed ~300 letters (Georgian Mtavruli, Cyrillic and others).

**How parity is proved (stage a).** `tool/export_assistant_goldens.py`
writes `test/goldens/assistant_text.json`; `test/assistant_text_test.dart`
checks: `parse` on 4,961 utterances (every short string in Flet's
assistant tests, a grid of verbs, fillers, queries and tails, and edge
cases: Greek, accents, Turkish i, ſ, the Kelvin sign, NBSP, U+FEFF, U+001C,
U+0085, `\r`, a final `\n`, emoji), name, query, raw and extras all
equal; `_clean_query` on 32 strings; Python's own match, search, findall
and sub for 66 subject × pattern cases of the constructs `py_re` rewrites;
`strip_markdown` on 16 texts; `_slim_track`; `names_mentioned` on 9
pages; `_fold`, `str.lower` and `str.casefold` on every code point (hashed
per 4,096); `save_session` and `save_graph_state` byte for byte with a
fixed clock, and `load_session` at 900 s (kept, 50 messages) and 900.5 s
(expired, file deleted).

**Stage b.** `assistant/llm_engine.dart` is `llm_engine.py`: one
OpenAI-compatible POST over dart:io (Gemini's endpoint with the key as a
Bearer token and 30 s, or `<endpoint>/chat/completions` locally and 45 s;
10 s to connect), the payload as aiohttp's `json=` writes it
(`pyJsonDumps`), one retry 1.5 s after a refused connection, Python's
error texts (`Provider returned HTTP n: <200 characters>`, `Empty
response from provider.`, `Network error: ` and an empty reason on a
timeout), `_parse_tool_calls` (arguments as a JSON string or an object,
`<name>_<i>` for a call without an id, `{}` for anything malformed), the
provider's message kept for the echo, and `list_gemini_models` (pages of
1,000, the page token quoted as yarl quotes it, the non-chat families left
out, newest id first). `assistant/agent_tools.dart` is
`get_agent_tools()`, generated once from Python's list and pinned by the
golden: 30 tools, the later phases' included (§7).

**How parity is proved (stage b).** `tool/export_llm_goldens.py` runs
Flet's `LLMEngine` against a local server that records each request and
answers from a script; `test/llm_engine_test.dart` replays the same
scripts against the port from its own server. Equal: every request
(method, path and query, the Authorization, Content-Type and
x-goog-api-key headers, the body byte for byte) and every parsed result
(content, tool calls, success, error, raw message) in 12 chat scenarios
(Ollama, Gemini with all 30 tools, the default model, an empty tool list,
HTTP 400 with a long Greek body, HTTP 500, empty or missing choices, no
message, unconfigured, an unknown provider, a reply that isn't JSON;
eleven tool-call shapes) and 4 model-list scenarios (two pages with
filtering, duplicates and a token with `+/=`, HTTP 403, no key, an empty
reply); `get_agent_tools()` byte for byte. Also: a refused connection is
retried once, a stalled server ends in `Network error: `.

**Stage c.** `assistant/runner.dart` is `assistant_runner.py`'s
deterministic half (`AssistantRunner`, `AssistantResponse`,
`PendingConfirmation`, `ChoiceOption`, `PendingChoice`, `toEngineTrack`):
dispatch, pending choice and confirmation (including relaxed yes/no),
anaphora (recency + intent weights, ambiguity runner-up swap on "no",
far-back confirmation, legacy bold fallback, implicit `play_similar` /
`play_more_by`, `_announce_anaphora`), every `_handle_*`, `_resolve_queries`
over `core/py_difflib.dart` (`SequenceMatcher.ratio`, autojunk included),
the avoid set and `assistant/runner_phrases.dart` (`kJarvisPhrases`).
Python value/string semantics live in `core/py_values.dart` (`pyTruthy`,
`pyOr`, `pyStr`, `pyFormat`, `pyGet`, `PyValueError`) and `core/py_text.dart`
(`pyIsDigit`, `pyCapitalize`, plus `kPyTitle` and `kDigitNotDecimal` from
`tool/gen_casefold_table.py`). `assistant/adapters.dart` wires the two
interfaces onto the port: `RepoAssistantLibrary` over `LibraryRepository`
and `StationSource` (with `data/assistant_queries.dart` porting
`search_tracks_simple`, `get_all_tracks`, `get_track_full`,
`get_most_played`, `recent_played_paths`, `record_playback`,
`get_all_artists(search_query)` and the NOCASE artist seed SQL; `neighborsJob`
gained `edgeKind`), and `ControllerAssistantPlayer` over `PlayerController`
(`setShuffleOrder(bool)` beside `disableShuffleOrder`, plus `play`, `pause`,
entry queue setters and `removeAt`). `llm` is the stage d hook; deferred
play stays the view's job (f).

**How parity is proved (stage c).** `tool/export_runner_goldens.py` runs
Flet's `AssistantRunner` with a scripted 64-bit LCG random, fixed UTC clock
and identical fakes (`test/goldens/assistant_runner.json`,
`test/assistant_runner_test.dart`): `kJarvisPhrases` whole, 40 `difflib`
ratios (including the autojunk boundary at 199/200), `isdigit` hashed over
every Unicode code point, `capitalize` (including `ß` and Greek final sigma),
and 14 scenarios (124 turns covering easter eggs, transport, play now,
disambiguation, fuzzy fallback, queue add/next/remove/move/save with name
collision and `²`, random, usual, greet confirmations, moods, track info
fallbacks with timbre stripped, similar, more by, and all anaphora paths),
comparing response, intent, call log, pending/choice state, recent and queue
as sorted JSON. `tool/export_assistant_db_goldens.py` runs Python's
`DatabaseManager` and `track_graph` on a synthetic SQLite database
(`test/goldens/assistant_db.json`, `test/assistant_adapters_test.dart`),
verifying every `RepoAssistantLibrary` query and `walk`, plus
`ControllerAssistantPlayer` and an end-to-end `AssistantRunner` run over
both real adapters. All identical.

**Stage d.** `assistant/runner.dart` now carries the full agent loop
(`dispatchLlm`, `agentReset`, `agentRunIntent`, `stageTracks`,
`agentPlayTracks`, `agentRequestConfirmation`, `_agentContextLine`,
`_finalizeAgent`, `_finalizeAgentInterrupt`), reading `AssistantConfig`
from `core/app_config.dart` (`[assistant]` section): at most 6 tool
rounds, exact persona + tool directives + live context line, history
sliced to the last 10 messages with current-turn dedup, the provider's
raw assistant message echoed verbatim (preserving Gemini
`thought_signature`, with rebuilt-message fallback), a tool's
confirmation or disambiguation choice ending the turn with provenance,
and one final tool-free request when the 6 steps run out. Tool results
serialise with `pyJsonDumps(…, ensureAscii: false, defaultEncoder: …)`
(`core/py_json.dart`). `assistant/tool_executor.dart` ports
`llm_tools.execute_tool` across all 24 library, playback, status and
read-only metadata tools (`get_metadata_status`, `get_artist_metadata`),
backed by the added `db_manager` queries in `data/assistant_queries.dart`
and `RepoAssistantLibrary` (`get_total_tracks`, `get_all_tracks(search)`,
`get_all_artists`, `get_all_albums`, `get_albums_by_artist`,
`get_tracks_by_album`, `get_all_playlists`, `get_tracks_in_playlist`,
`get_recent_tracks`, `get_artist_enrichment`, `get_ai_research`,
`get_pending_ai_research`, `get_artist_source_genres` with
`_clean_source_tags`, `get_metadata_gap_artists`,
`get_metadata_coverage`). Deferred tools (`web_search`, `learn_about`,
`find_related_artists` in stage e; `search_online` in phase 6 / decision
A; `research_artist_metadata`, `apply_artist_metadata` in phase 7) stay
declared and delegate to runner hooks, returning a clear error dict when
unset (§7).

**How parity is proved (stage d).** `tool/export_agent_goldens.py` runs
Flet's `AssistantRunner._dispatch_llm` and `llm_tools.execute_tool`
against a real synthetic SQLite database (`DatabaseManager` +
`track_graph`) and a local HTTP recording server
(`test/goldens/assistant_agent.json`, `test/assistant_agent_test.dart`):
51 direct `execute_tool` cases across all 24 tools and edge cases, plus
9 multi-turn loop scenarios (22 HTTP requests: multi-step read+act with
`thought_signature` echo and >10-message history dedup, custom
personality over Ollama without `role` in the raw message, busy-queue
confirmation interrupt + `"yes"` follow-up, disambiguation choice
interrupt + `"1"` follow-up, 6-step exhaustion + final tool-free answer,
6-step exhaustion with failed final call, `llm_enabled=false`,
unconfigured fallbacks, HTTP 503 fallbacks, and fast-intent bypass).
Every HTTP request (`method`, `path`, `headers`, `body` byte for byte)
and every turn's response, pending/choice state and player calls match
Python identically.

**Stage e.** Replayed responses (DuckDuckGo Lite HTML, Wikipedia JSON,
Gemini grounding JSON) give the same results from Python and the port,
including the 30-minute cache and Google switching itself off after a
quota or capability error.

**Stage f.** The tab returns to the bar (`appearance.show_jarvis`); the
chat view keeps Flet's bubbles (user text; assistant Markdown, GitHub
flavoured, links opened outside), the option buttons, the thinking
bubble, the empty state's four prompts, the 50-message cap, Clear, the
init flow (graph status, the edge builds, `graph_state.json`, the
empty-library line), the entities stored with each reply for anaphora,
deferred play, and the background and resume hooks (session touch, the
15-minute expiry). Settings → AI Assistant: enable, provider, key, the
model list, research mode, the local server, personality presets, Test
Connection. A Markdown renderer from pub.dev is chosen here (§12).

**Stage g.** Phone checks only: the voice choice (Google engine, en-GB,
the extension's selection rules, rate and pitch 0.75), hold-to-talk (60 s
cap, 4 s after release), RECORD_AUDIO asked on the first press (§1 row
9), `<queries>` for RecognitionService, music paused while Jarvis speaks
and resumed after, speech stopped when the tab closes or voice is turned
off.

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
6. Give the boot bundles Flet's names again (`kSnapshotName`,
   `kAutoImportName` in `sync/state_bundles.dart`): the Mac offload and
   `fresh_build_android.sh` look for `mai_an_lab_state_latest.zip` and
   `mai_an_lab_state_import.zip`.
7. Branches swap (rule 1): `flutter-port` becomes master, the Flet code
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
| Settings hub | Twelve groups | The nine whose features exist (§4, Settings; Companion & Sync since 4e); Authentication, AI Assistant and Metadata with A, 5 and 7 | Until then. |
| Settings rows waiting on features | Default Download Quality, the Jarvis tab switch, Commence/Continue DSP Compute and Recompute All DSP Features, demo search, debug play counts, fresh-install reset | Not shown; values the hidden rows would save are written back as read | With decisions A, B, C and phase 5. (Export/import came with 4f, Recompute PCA with 4g.) |
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
| Companion on desktop builds | The Mac shows the server view | Desktop dev runs act as the phone (client view) | Until phase 8 ports the Mac's server; nothing ships to the Mac before then. |
| State bundle import from Settings | Closes the database and asks to force-close and relaunch | Reopens the library (migrated) and re-reads config.toml; "Imported …. Library reloaded." | Intentional: no relaunch needed. |
| Boot snapshot and boot import file names | `mai_an_lab_state_latest.zip`, `mai_an_lab_state_import.zip` | `mai_an_lab_port_state_latest.zip`, `mai_an_lab_port_state_import.zip` | Until cutover (§5 step 6): with both apps installed, the port would overwrite Flet's snapshot and take Flet's import. |
| Boot snapshot on desktop | Written to ~/Downloads on every start | Not written | Desktop runs are development until phase 8. |
| Export State on desktop | A native folder picker (~/Downloads when cancelled) | ~/Downloads | No native picker in the port yet; desktop isn't shipped. |
| A `sync` notification tap with Settings open | Switches the Settings view to Companion & Sync | Pushes Settings at Companion & Sync over what is open | Back returns to where the user was; nothing to fix unless it bothers in use. |
| Network errors from the LLM client | aiohttp's text after `Network error: ` | dart:io's | Different words, same bubble; a timeout reads `Network error: ` in both. |
| Discovery on Android | Hears only the Mac's unicast replies to its query (no multicast lock without pyjnius) | Holds a MulticastLock while listening, so the Mac's 2 s broadcast beacons arrive as well | Intentional (§1 row 17): finds the Mac sooner on networks that pass broadcasts. |
| The acoustic SVD | numpy's float32 LAPACK SVD of Z | The eigenvectors of ZᵀZ in float64 (cyclic Jacobi) for the identical float32 Z | Within float32 precision: on the real library coordinates differ by at most 2.4e-4 (largest 11.6) and cosines by 1.3e-6, and stations match on all 20 golden seeds. The port's eigenvalues equal a float64 SVD's to 1e-9. |
| The sign of each coordinate axis | Whatever LAPACK returns (it differs between numpy builds, so the Mac's and the phone's Flet can disagree) | scikit-learn's `svd_flip`: each component's largest loading positive | Cosine and distances, which the stations use, are unchanged; the Network view's map can come out mirrored next to a Python-built one. |
| Covariance cleaving: a two-scalar cluster | Both scalars carry the same \|r\|, but np.corrcoef divides it by the two deviations in opposite orders; the last bit (it varies between BLAS builds) picks which one stays | The matrix is made exactly symmetric first, (C + Cᵀ) / 2: a tie, and the lower index stays (rolloff over spectral_flatness) | Python's choice there is noise: on the real library it kept rolloff, in the `constant` golden spectral_flatness (0.9661723231667043 against …044). The goldens pin Python's cleaver to the symmetric matrix and record numpy's raw pick beside it. |
| A constant scalar column | float32 rounding can leave its standard deviation just above 1e-8, so the column survives z-scoring as a constant | The same (numpy's float32 arithmetic is reproduced) | Kept for parity (golden scenario `constant`). |
| Graph readiness build | Waits up to 20 s for a pause in scrolling before building | Builds 5 s after start, without waiting | The build runs on the database isolate (48 ms on the Mac for 1,216 tracks): nothing to wait out. |
| Offload button after "All tracks analyzed" | Re-enabled by the next manager change; a tap then says "All tracks already analysed!" | Stays off while the count is 0 | Small fix. |
| Assistant tools from later phases (`search_online`, `research_artist_metadata`, `apply_artist_metadata`; web tools until 5e) | Execute against Qobuz / `metadata_ai` / `web_research` | Declared in `agentTools()` (all 30) and hookable on `AssistantRunner`; return `{"success": false, "error": "… not available yet."}` until their phase | Read-only metadata tools (`get_metadata_status`, `get_artist_metadata`) work in 5d (`research_available: false`); web tools arrive in 5e, `search_online` with decision A, metadata writes with phase 7. |

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
  `~/miniconda3/bin/python` from `mai_an_flutter/`. The real-library parts
  of `export_network_goldens.py` and `export_acoustic_goldens.py` read the
  `library.db` that `export_station_goldens.py` copies beside its output,
  so run that one first. The scan and config
  exports need the APK's tinytag and tomlkit apart from the dev env (which
  has streamrip's tomlkit 0.7.2):
  `~/miniconda3/bin/pip install --target tool/.pydeps tinytag==2.3.2 tomlkit==0.15.2`
  (gitignored). Generated tables: `tool/gen_nfkd_table.py`, `tool/gen_nfc_table.py`,
  `tool/gen_casefold_table.py` (`str.lower`, `str.casefold`, `str.isdecimal`),
  `tool/gen_py_re_tables.py` (`re`'s `\w` and IGNORECASE letters),
  `tool/gen_fold_casefold_table.py` (NFKD + casefold exceptions; run it
  after `gen_casefold_table.py`).

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
sync manager), 4d (the download queue and dock), 4e (the Companion UI),
4f (state bundles) and 4g (the DSP offload and the acoustic build), local.
Then the phase 5 plan, stages a (the intent parser), b (the LLM
client), c (the assistant runner and adapters) and d (the agent loop and
tool executor), local.

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
- [x] Phase 4e: Settings → Companion & Sync, the PIN and transfer
      dialogs, Send to Mac in the track, album and selection menus, the
      manager wired into the app, its lifecycle and the `sync`
      notification target (§4, phase 4). Release APK: 26.4 MB.
- [x] Phase 4f: state bundles: export and import in Settings → Advanced,
      the boot snapshot and boot import, Send Backup to Mac; tested both
      ways against Flet's state_export.py (§4, phase 4). Release APK:
      26.5 MB.
- [x] Phase 4g: the acoustic build (`build_acoustic_edges`: covariance
      cleaving, numpy's float32 Z bit for bit, the SVD by Jacobi on ZᵀZ;
      `build_genre_affinity` with the NPMI model, byte for byte), called
      after the boot import, an offload, by the startup readiness check and
      by Settings' Recompute PCA; the hub's DSP Acoustic Offload card. On
      the real library the port's coordinates give Python's stations on
      all 20 golden seeds (§4, phase 4g). Phase 4's code is complete.
      Release APK: 26.6 MB.
- [x] Phase 5 planned in seven stages (§4, phase 5). Stage a: the intent
      parser over `core/py_re.dart` (Python's `re` semantics on Dart's
      RegExp), `strip_markdown`, `_slim_track`, `names_mentioned`, the chat
      memory files; identical to Python on 4,961 utterances. `pyLower` and
      `pyCasefold` now table-driven, equal to Python on every code point.
- [x] Phase 5b: the LLM client (`llm_engine.py`) and the tool
      declarations; requests and parsed replies identical to Python's in
      16 recorded scenarios.
- [x] Phase 5c: `AssistantRunner`'s deterministic half (`runner.dart`,
      `runner_phrases.dart`, `py_difflib.dart`, `py_values.dart`) and its
      adapters (`adapters.dart`, `assistant_queries.dart`,
      `PlayerController.setShuffleOrder`); identical to Python across 14
      scenarios (124 turns) and on every synthetic-DB query and walk.
- [x] Phase 5d: the agent loop (`runner.dart` `dispatchLlm` and `agent_*`
      bridges, `AssistantConfig` in `app_config.dart`, `defaultEncoder` in
      `py_json.dart`) and `executeTool` (`tool_executor.dart`, 24 library,
      playback, status and read-only metadata tools + `assistant_queries.dart`
      / `RepoAssistantLibrary`); identical to Python across 51 tool cases
      and 9 HTTP-recorded loop scenarios (22 requests byte for byte).

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
       j. Phase 4 (sync, Android side), 2026-11-24, before decisions
          A–C (none of its stages depends on them): stages a–g done (§4,
          phase 4). What remains is on the phone: a real pairing (§1 row
          16: cleartext HTTP), a transfer end to end, a bundle round trip
          (export, then import it as `mai_an_lab_port_state_import.zip`),
          and an offload end to end (the `sync` notification and its
          Cancel with the screen off; the graph rebuilt after; Settings →
          Database Management → Recompute PCA).
       k. Phase 5 (the assistant), planned 2026-11-24 in seven stages
          (§4, phase 5). Stages a (the intent parser and text helpers),
          b (the LLM client and the tool declarations), c (the runner's
          deterministic half and its database and player adapters) and d
          (the agent loop, bridges and `execute_tool` for the library,
          playback, status and read-only metadata tools) done.
          Next: stage e (the web tools: `web_research.py` `ask`,
          `learn_about`, `find_related_artists`).
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

**Counts:** 54 of the 91 rows are ported (✅ or 🧪); 37 are to build or to decide. The ported rows are the player, the stations, the library views (the Acoustic Network included), playlists, the long-press menu and selection mode, the ledger drain, the scan, the tab bar, the Search landing's local cards, the download dock, Companion pairing, transfers, Send to Mac and the DSP offload, state bundles, the similarity-graph builds, and Settings for all of these.

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
| Notification taps routed to a screen (metadata sync, suggestions, download retry, transfers) | `_on_notification_navigate` | 🧪 the router, the Library, downloads and their Retry, sync and its transfers; workbench 🔨 7 |
| Success sound after a download | `play_success_notification` | 🧪 the haptic (Flet's Android sound never played, §7); Mac 🔨 8 |
| Onboarding on first launch | `check_onboarding` | 🔨 7 |

### Startup and background jobs

| Job | Flet source | Port |
|---|---|---|
| Position save loop; queue saved on every mutation | `_position_save_loop`, `_on_queue_mutated` | 🧪 |
| Auto-play history from recent plays | `autoplay.load_history` | 🧪 |
| Ledger drain into `play_counts` / `playback_history` (track change, resume) | `_schedule_ledger_drain`, `play_ledger.py` | 🧪 |
| Similarity-graph readiness build (edges, coordinates) | `_ensure_graph_built_async`, `track_graph` builders | 🧪 (5 s after start, when analysed tracks have no coordinates; §7) |
| Cache pruning | `_prune_caches_async` | 🔨 3 (album art); 6 (previews). The manual clears are in Settings → Advanced 🧪 |
| State bundle auto-export on every boot (Downloads and library folder) | `_auto_export_state_snapshot` | 🧪 under the port's own name until cutover (§7). The Mac offload and `fresh_build_android.sh` depend on Flet's, which Flet keeps writing while installed |
| State bundle auto-import (`*_import.zip`) on boot, then graph rebuild | `_heavy_init` | 🧪 (the port's own name until cutover; the artist tier, coordinates and genre model rebuilt) |
| Companion auto-discovery | `companion_manager.start` | 🧪 |
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
| Long-press: Send to the Mac | 🧪 |
| Long-press: Redownload | ⚠️ A |
| Selection mode: play, queue, add to playlist, send | 🧪 |
| Albums: expandable rows; album menu (play, queue, send) | 🧪 |
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
| Android: find the Mac (UDP beacon), pair, unpair, manual connect, diagnosis | 🧪 |
| Android: check for and download transfers from the Mac | 🧪 |
| Android: DSP offload to the Mac (progress notification, cancel from it), then the graph rebuilt | 🧪 |
| Android: upload a state bundle | 🧪 |
| Mac: server toggle, LAN address, clear pending, reset pairing, open backups | Flet on the Mac until 8 |

### Settings (`ui/views/settings.py`), built with each phase (rule 4)

| Group | Contents | Port |
|---|---|---|
| Hub | Groups list, fuzzy settings search | 🧪 (search golden-tested) |
| Companion & Sync | Opens the sync hub | 🧪 (all four cards) |
| Storage & Paths | Library folder, downloads folder, default download quality | 🧪 folders and the Android navigator; ⚠️ A (quality) |
| Authentication | Qobuz (email/ID, password or token, app ID and secret), Deezer ARL | ⚠️ A |
| AI Assistant | Enable, provider and API key, model list, test connection, personality presets (built-in and custom) | 🔨 5 |
| Appearance | Accent colour, startup page, Jarvis tab, library tab visibility, landing sections | 🧪 (the Jarvis switch 🔨 5) |
| Audio & DSP | EQ on/off, presets, five bands, Dynamism | 🧪 |
| Haptic Feedback | On/off, per-action intensity | 🧪 |
| Metadata | Opens the workbench | 🔨 7 |
| Permissions | Status and grant buttons, how-to | 🧪 (statuses, the system-settings button) |
| Database Management | Wipe database, clear index, clear DSP | 🧪 |
| Database Management | Recompute PCA (the acoustic build over stored features) | 🧪 |
| Database Management | Commence/Continue DSP Compute, Recompute All DSP Features | ⚠️ B |
| Advanced | Raw TOML editor, preview and album-art cache clear | 🧪 |
| Advanced | State export / import | 🧪 |
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
| Python's `re` semantics for `\s`, `\w`, `\b`, `re.IGNORECASE`, `.`, `$` and `re.MULTILINE`, reproduced from its behaviour (the tables generated by asking Python), not translated | `lib/core/py_re.dart`, `lib/core/py_re_tables.dart` | CPython `re` / sre | PSF License v2; no code copied |
| Unicode lower-case, title-case, case-folding and non-decimal digit mappings, and NFKD + casefold for the characters py_text's pieces don't cover, generated from Python's `str.lower`, `str.title`, `str.casefold`, `str.isdigit` and `unicodedata` | `lib/core/py_unicode_tables.dart`, `lib/core/nfkd_casefold_table.dart` | Unicode Character Database 15.1, via CPython | Unicode License v3 (data) |
| Unicode canonical decompositions, combining classes and composition exclusions, generated from Python's `unicodedata` | `lib/core/nfc_table.dart` (with `lib/core/nfc.dart`) | Unicode Character Database 15.1, via CPython | Unicode License v3 (data) |
| numpy's float32 reduction order (`mean`/`std` down axis 0 as one sequential float32 sum per column) and `np.corrcoef`'s arithmetic, reproduced from their behaviour (checked bit for bit), not translated | `lib/stations/acoustic_geometry.dart` | https://github.com/numpy/numpy (2.4) | BSD-3-Clause; no numpy code copied |
| Python's `json.dumps` defaults (separators, `ensure_ascii`, float `repr`), written from its documented behaviour | `lib/core/py_json.dart` | CPython `json` | PSF License v2; no code copied |
| CPython's `difflib.SequenceMatcher.ratio` (b2j index with autojunk rule, `find_longest_match`, matching blocks), translated | `lib/core/py_difflib.dart` | https://github.com/python/cpython (3.13, `Lib/difflib.py`) | PSF License v2 |

**Templates and guidance adopted**

| What | Where | Source | License |
|---|---|---|---|
| Strict analyzer modes (`strict-casts`, `strict-inference`, `strict-raw-types`) on top of `flutter_lints` | `analysis_options.yaml` | dart.dev, "Customizing static analysis" (enabling additional type checks) | BSD-3-Clause (Dart SDK docs and `lints`) |
| `ReorderableListView` with `ReorderableDragStartListener` handles for playlist reordering | `library/browse_view.dart` | Flutter SDK (material) | BSD-3-Clause |
| The cyclic Jacobi eigenvalue method (Jacobi 1846; the rotation formulas and the skip rule for negligible elements as in Press et al., *Numerical Recipes*, §11.1), written from the algorithm, not from the book's code | `stations/acoustic_geometry.dart` `_symmetricEigen` | Textbook algorithm | Algorithm only; no code copied |
| SVD sign convention: each component's largest-magnitude loading positive (scikit-learn's `svd_flip`, `u_based_decision=False`) | `stations/acoustic_geometry.dart` | scikit-learn documentation | BSD-3-Clause; convention only, no code copied |

**Dependencies (pub.dev)**: `sqlite3`, `path_provider`, `audio_service`,
`just_audio`, `audio_session`, `permission_handler`, `toml`. Their licences
ship in the app through Flutter's `LicenseRegistry` (`showLicensePage`), so
Settings → About opens it (Licenses).

**Candidates for coming steps** (found 2026-11-24, not yet evaluated)

| Need | Candidate | Notes |
|---|---|---|
| Tag writing (`tagger.py`, with downloads, 6) | [`metatagger`](https://pub.dev/packages/metatagger) (pure Dart: ID3v2, vorbis comments, iTunes ilst); [`metadata_god`](https://pub.dev/documentation/metadata_god/latest/) and [`haudiotagger`](https://pub.dev/packages/haudiotagger) (Rust lofty via flutter_rust_bridge); [`flutter_taglib`](https://pub.dev/documentation/flutter_taglib/1.3.2/) (TagLib over FFI) | Pure Dart keeps the build free of Rust (decision B's spirit). Whatever is chosen must leave tags that mutagen and tinytag read back the same. |

