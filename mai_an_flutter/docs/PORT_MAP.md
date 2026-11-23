# Mai An Lab: Flet → Flutter port map

*Status: 2026-11-23. Branch `flutter-port` (not pushed). Phase 0 (backbone)
done. Phase 1 (phone player) is written, audited against the Flet sources
(§9) and passes its desktop tests (`flutter test`: 87, analyzer clean); the
debug APK builds. It has not run on the phone yet, so phase 1 is not done.
Nothing ships from this port yet.*

**Next session: start at §10 (open items).**

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
   `mai_an_flutter/` on its own branch, and master stays Python-only.
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
| 18 | **Lifecycle** | `on_app_lifecycle_state_change`: suspend UI updates in the background, prefill the autoplay buffer on hide, run assistant and companion background hooks, pick up listens finished in the background | `AppLifecycleListener`. Flutter stops drawing frames by itself. Done: the position ticker stops and the position is saved on hide. Still to come: the auto-play prefill (2), ledger catch-up (3), assistant and companion hooks (4–5). | 1–4 | 🔨 |
| 19 | **Edge-to-edge, predictive back** | Flet/Flutter defaults | targetSdk 36 forces both. Back on the root screen calls `moveTaskToBack` (MainActivity channel `mai_an/app`); predictive back is enabled. Still to audit: edge-to-edge insets in the sheets. | 1 | 🧪 |
| 20 | **16 KB memory pages** (Android 15+ devices) | Flet build | ✅ Checked: every `.so` in the port's APK has 16 KB LOAD alignment, and the zip is 16 KB-aligned | 0 | ✅ |
| 21 | **Haptics, links, clipboard, file picker** | `HapticFeedback` (about 15 call sites, configurable intensity), `UrlLauncher`, `FilePicker` (1) | `core/haptics.dart` reads `[haptics]` (on/off, per-action intensity, main.py's defaults); used by the track menu (long_press, swipe_queue): 🧪. `url_launcher`, `file_selector` when needed: 🔨. | as needed | 🧪🔨 |
| 22 | **APK size** | CPython + site-packages (about 72 MB of site-packages before trimming) | Debug build 146 MB (tooling included); release still to be measured | 1 | 🔨 |
| 23 | **macOS** | Same Python codebase | No Xcode on this Mac (15.7.3). Build in CI or after an Xcode install. The Mac keeps the Flet app until phase 8. | 8 | ⚠️ |

---

## 2. Data contract

Everything sits in the private data dir (`<appSupport>/data`) unless stated.
The port reuses each file's exact format, so a cutover install inherits it.

| File | Owner today | Port reads | Port writes |
|---|---|---|---|
| `library.db` (main database; schema below) | `db_manager.py` | phase 0 ✅ | phase 3 |
| `.config/streamrip/config.toml` (Android; on the Mac it is under `Library/Application Support/streamrip/`). Holds `general.library_sort`, `appearance.accent_color`, `[dsp]`, `[haptics]` and credentials. | `config.py` | phase 1 ✅ (sort, accent, `[dsp]`, `[haptics]`: `core/app_config.dart`) | the phase that owns each setting |
| `queue_state.json`, `queue_pos.json`, `native_resume.json` | main.py, engine | 1 ✅ | 1 ✅ |
| `.cache/queue_regular.json`, `.cache/queue_shuffle.json` (the queue set aside while shuffling the library) | main.py | 1 ✅ | 1 ✅ |
| `flet_prefs.json` (`_save_pref`; shuffle, repeat, similar-play prefs) | main.py | 1 ✅ | 1 ✅ (`is_shuffle`, `repeat_mode`; merge-write as in Flet) |
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
| 3 | `playback_history`, `play_counts` (counts), written by the ledger drain |
| 2 | `track_neighbors`, `track_partitions`, `genre_affinity`, `autoplay_hidden`, `artist_aliases` |
| 3 | `playlists`, `playlist_tracks`, `fts_search`. FTS5 is kept in step by 11 triggers stored in the database, so port writes stay consistent for free. |
| 5 | `artist_enrichment`, `artist_ai_research` |
| 6 | `artist_releases`, `release_sync_log` |
| 7 | `metadata_audit_dismissed`, `artist_alias_rejections` |

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
| `ui/views/library.py` | 4,915 | Rewrite, one tab at a time (tracks ✅ rows, sort, search, long-press menu for phase 1; selection mode with phase 3; albums, artists, genres, playlists, network graph to come) | 1–3 |
| `track_graph.py`, `artist_graph.py`, `autoplay.py`, `genre_taxonomy.py`, `genre_similarity.py`, `harmonic.py`, `credit_keys` (in `metadata_enrich`) | ~2,500 | Port, with golden station tests | 2 |
| `ui/player/autoplay_sheet.py` | 156 | Rewrite | 2 |
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
| `mock_search.py`, `genre_eval.py`, `progress_notify` bridge, `error_boundary.py` | ~800 | Drop or fold in (Flutter has `ErrorWidget` and zones) | n/a |

---

## 4. Phases

Each phase ends when its parity tests pass and it is in daily use on the
phone. Rough sizes are in Dart lines, excluding tests.

| # | Phase | Done when | Size |
|---|---|---|---|
| 0 | **Backbone** ✅ | Reads `library.db` read-only on a background isolate. Tracks tab with Flet's sort modes and search semantics. Coordinate graph. Android build. | 1k |
| 1 | **Phone player** 🧪 (code done, phone checks below pending) | Plays the phone's real library with the screen off. Notification, lockscreen and headset controls work. The queue survives process death in Flet's JSON format. Permissions work. | 4–5k |
| 2 | **Stations** | "Play Similar" matches Flet's stations: golden tests on 20 fixed seeds from a `state_backups` bundle. Autoplay sheet. | 3k |
| 3 | **Library writes** | Scan, tag edits and playlists. The port owns migrations (respects `user_version`). Remaining library tabs. | 5k |
| 4 | **Sync (Android side)** | Phone ↔ Flet-Mac pairing, transfers and DSP offload. Bundle import and export. | 3k |
| 5 | **Assistant** | Golden intent and tool-call tests pass. Voice. | 4k |
| 6 | **Sources and analysis** | Only if decisions A and B say "on the phone" | 4–6k |
| 7 | **Metadata tools, onboarding** | Feature parity | 4k |
| 8 | **macOS** | Mac player on Flutter; sync server moves over. Needs Xcode or CI. | 3k |

### Phase 1: checks still to run on the phone

The desktop tests cover the queue model, the persistence files and the
controller against a fake player. These need the real one (`flutter run`,
then `tool/push_db.sh` and `R`):

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
  and `genre_eval` diagnostics, `mock_search`, Flet-specific plumbing
  (`progress_notify` bridge, `error_boundary`).

---

## 7. Parity log: where the port differs from Flet today

| Area | Flet | Port | Resolution |
|---|---|---|---|
| Similar tracks | `track_graph.open_station` + `artist_graph` Drift + `credit_keys` | Cosine walk with per-artist and per-album caps | Phase 2. The sheet is titled "Nearest by sound" so it doesn't claim to be Play Similar. |
| Search matching | FTS5 prefix match (unicode61, accent-blind), then `LIKE`, then bigram closest match | Every query word as a substring, accent-blind, same punctuation stripping, then the same closest-match fallback | Intentionally broader: it finds everything FTS finds, plus mid-word hits. Closest-match scoring is identical and tested. |
| Search timing | 300 ms debounce, SQL per query | In-memory, per keystroke | Faster. Nothing to fix. |
| Tie order within a sort | Unspecified (SQLite) | By id | Deterministic. Nothing to fix. |
| Initial sort | `config.toml` `general.library_sort` | Reads `general.library_sort` (was "always date") | Fixed in phase 1. |
| Shuffle on/off | `set_queue` reloads the playing track from 0:00 and starts playback even when paused; the ledger logs it as an early skip | The track keeps its position and play/pause state (`keepListen`) | Intentional: the Flet behaviour is a side effect, not a feature. |
| Restored track missing on disk | Not preloaded, and Play then does nothing | Not preloaded; the first Play loads it at the saved offset | Intentional. |
| Queue rows | `_play_track_core` rows carry no `duration` | They carry `duration` | Harmless to the Flet reader; the slider has the right length before the decoder reports. |
| Microphone permission | Asked at first launch with the rest | Not asked until voice exists (phase 5) | Intentional. |
| Long-press on a track | Flet's full context menu | Play next, Add to queue (haptic tick as in Flet), Nearest by sound | The rest arrives with the features behind it (playlists, radio, metadata). |
| Notification taps | Routed to the target tab | Received and dropped (one screen) | When there are screens to route to. |
| DSP after background auto-advance | `reapply_dsp` runs only while the Python session hears the player, so tracks reached with the session torn down keep the previous track's settings | Applied on every row change | Intentional. |
| Track row | Format badge, Flet subtitle rules, tint on the playing row | Same | Fixed in the phase 0–1 audit. |

---

## 8. Dev workflow

- **Device:** Android phone over USB. Run `flutter run` in `mai_an_flutter/`
  (hot reload with `r`, restart with `R`).
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

Last session: 2026-11-23. Commits on `flutter-port`: `4a116b9` (phases 0–1),
`c6ff28d` (audit fixes), then this plan update.

**Done**
- [x] Phase 0: backbone (read-only DB isolate, tracks tab, sort, search,
      coordinate graph, Android build, 16 KB pages).
- [x] Phase 1 code: audio handler, player controller, queue model, Flet
      persistence files, prefs, player UI, permissions, lifecycle hide/show,
      back-to-background.
- [x] Phase 0–1 audit against the Flet sources, and its six fixes (§9).
- [x] Port moved to its own branch, `flutter-port`.

**To do, in order**
1. [ ] Run the phase 1 phone checks (§4, items 1–14). Phase 1 is done only
       when they pass and the port is in daily use. Record any difference in
       §7 or fix it.
2. [ ] Back up `~/.android/debug.keystore` (§5).
3. [ ] Repo housekeeping: the working tree holds uncommitted Flet changes
       (`StreamripApp/main.py`, `tests/test_autoplay_ui.py`,
       `tests/test_genre_view.py`, `ui/player/autoplay_sheet.py`,
       `ui/player/metadata_workbench.py`, `ui/player/now_playing.py`). They
       are not part of the port: commit them on master (rule 1), not here.
       Push `flutter-port` when ready.
4. [ ] Phase 2 (stations), first step: a small Python script that exports
       golden JSON for 20 fixed seeds from a `tools/state_backups/` bundle
       (rule 3), then port `track_graph`, `artist_graph`, `autoplay`,
       `genre_*`, `harmonic`, `credit_keys`, the autoplay sheet, and the
       Now Playing chain button (enable it, artwork border, availability
       check). Replace "Nearest by sound" with Play Similar (§7) and add
       Start radio to the long-press menu. Lifecycle: auto-play prefill on
       hide (§1 row 18).

**Waiting on a later phase** (not gaps)
- Notification taps routed to screens (once there is more than one).
- Ledger drain into `play_counts` / `playback_history`, and catching up on
  resume (phase 3).
- Tracks tab selection mode, playlist rows, Add to playlist and Delete in the
  long-press menu, the other library tabs (phase 3).
- EQ / Dynamism / haptics settings UI (with the settings work, rule 4);
  `get_equalizer_bands` is needed only by that UI.
- `url_launcher`, `file_selector` (when a feature needs them).
- Decisions A, B, C (§6).

