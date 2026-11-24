# Mai An Lab: Flet → Flutter port map

*Updated: 2026-11-24. Branch `flutter-port`, tagged **`v0.1.0`** (`0.1.0+2`). **Phases 0–5 + Phase 6a–6b code is complete** (65/91 Flet features fully ported + 6b source clients & Auth/Quality settings done; desktop tests pass including real-library goldens; strict Dart analyzer clean; release APK builds at 27.5 MB). Decisions A, B and C are resolved (§6).*

*Primary blocker right now: **on-device verification** — the phone still has an older pre-scan/pre-Settings build, so Phases 1–6b are `🧪` (written & tested on desktop, not yet verified in daily phone use). Next code milestones: **Phase 6c–6d** (full Search UI/previews/download pipeline/tagger & On-Phone DSP) and **Phase 7** (Metadata Workbench, Auto-Enrichment & Lean Onboarding), followed by a **pre-cutover UI Polish pass**, Android cutover (§5), and **Phase 8** (macOS).*

**New agent? Start at §10 (Open items) and §11 (Remaining features).**
Archived history (Phase 0–5 build logs, golden test inventories, §3 full module table, §9 Phase 0–1 audit, completed §10/§11 items) lives in [PORT_HISTORY.md](PORT_HISTORY.md). Third-party attributions & package candidates (§12) live in [THIRD_PARTY.md](THIRD_PARTY.md).

---

## 0. Ground rules

1. **The Flet app (`StreamripApp/`) ships until cutover (§5).** The port lives in `mai_an_flutter/` on `flutter-port`; `master` stays Python-only. Flet changes commit on `master`, then `master` merges into `flutter-port` (never the reverse until cutover). At cutover, `flutter-port` becomes `master` and Flet continues on a `flet` branch (Mac runs Flet until Phase 8).
2. **The data contract is the interface (§2).** Both apps read/write the exact same `library.db` schema and sidecar files. The port migrates and writes only its own `.port` sandbox copy until cutover; any external DB (`LIBRARY_DB` override, test goldens) opens read-only.
3. **Parity is proved with Python's own outputs.** Pin reference values emitted by Python functions via `tool/export_*_goldens.py` scripts and Dart golden tests rather than porting from memory.
4. **Settings and per-screen polish ship with their phase; major visual polish is staged cleanly.** Each phase builds its own settings rows and basic transitions. Dedicated visual/motion polish runs after phone verification and before cutover; macOS gets a responsive desktop layout in Phase 8.
5. **Versioning (SemVer 2.0.0, chained to a `0.x.y` zero release).** Git tags use `vMAJOR.MINOR.PATCH` (no dot after `v`, cleanly separated from legacy Flet's `v.1.x` / `v.2.0.0` tags). `lib/core/app_version.dart` (`kAppVersion`, `kAppBuildNumber`) and `pubspec.yaml` stay in lockstep (enforced by `test/app_version_test.dart`), with Android `versionCode` starting at `+2` (strictly above Flet's `1`) and incrementing monotonically:
   - `v0.1.0` (`0.1.0+2`): Phases 0–5 baseline (`v0.1.x` for device-verification fixes)
   - `v0.2.0`: Phase 6 (Sources, Downloads, New Releases & On-Phone DSP)
   - `v0.3.0`: Phase 7 (Metadata Workbench, Auto-Enrichment & Lean Onboarding)
   - `v0.4.0`: Pre-cutover UI Polish pass
   - `v1.0.0`: Android cutover (`flutter-port` → `master`, `.port` suffix removed)
   - `v1.1.0`: Phase 8 (macOS responsive desktop app + sync server)

---

## 1. Android & OS contracts (key invariants & open risks)

Phases 0–5 already cover package identity (`.port` suffix), private data dir (`<appSupport>/data`), single-isolate `AudioServiceActivity`, background playback/audio focus, process-death resume (`native_resume.json`, `queue_state.json`, `queue_pos.json`), Kotlin artwork & notifications (`packages/maian_native`), storage permissions (`MANAGE_EXTERNAL_STORAGE` + `READ_MEDIA_AUDIO`, sideload-only), per-track EQ/Dynamism, voice (`RECORD_AUDIO` on first mic press + `<queries>`), 16 KB memory page alignment, and lifecycle hooks.

Active constraints & items needing verification or future work:

| Row | Concern | Current state / Risk | Phase |
|---|---|---|---|
| 1 | **Package ID** | `com.mitsakopoulos.maianlab.mai_an_lab.port` (`.port` suffix keeps Flet untouched until cutover). | Cutover |
| 8 | **Long screen-off tasks** | Reuses Kotlin `LibraryTaskService` (`dataSync`). ⚠️ Android 15+ caps `dataSync` foreground services at ~6 h/24 h; long offloads, downloads, or on-phone DSP batches must handle the timeout callback cleanly. Verify on device. | 4, 6 |
| 11 | **Path identity & NFC** | Scanner stores raw `os.scandir` paths; app-named files (transfers, downloads) apply `core/nfc.dart` (Python `unicodedata` NFC). Reuse `core/nfc.dart` in Phase 6 downloads. | 4, 6 |
| 13 | **Audio analysis (DSP)** | Kotlin `decodePcm` (MediaCodec → mono PCM) is already in `maian_native`; offload + acoustic SVD/NPMI graph builds are done (4g). Decision B = **YES**: port `dsp.py` feature math to run on-phone too. | 6 |
| 16 | **Cleartext HTTP to Mac** | `dart:io` sockets bypass Android's cleartext policy (`INTERNET` declared). Confirm on device with first real Mac pairing. | 4 🧪 |
| 17 | **LAN discovery** | `sync/discovery.dart` (`RawDatagramSocket` UDP 54545) + `MainActivity` `acquireMulticastLock` & `defaultRouteIpv4`. ⚠️ Recheck local-network permission rules before raising `targetSdk` past 36. | 4 🧪 |
| 19 | **Edge-to-edge & predictive back** | `targetSdk 36` enforces both. Root back calls `moveTaskToBack`; predictive back enabled. **Still to audit:** bottom-sheet & modal edge-to-edge insets on device. | Polish |
| 22 | **Release build & lint** | Release arm64 APK builds (27.5 MB), R8 not yet verified on device. `checkReleaseBuilds = false` works around lint crashing on the symlinked Kotlin in `packages/maian_native`; restore lint once Kotlin moves in at cutover. | Cutover |
| 23 | **macOS target** | Mac stays on Flet until Phase 8. On macOS 15.7.3, install Xcode 16.4 (not App Store) for local `flutter run -d macos` (§8). | 8 |

---

## 2. Data contract

All files live under `<appSupport>/data` (Android: `/data/user/0/<pkg>/files/data`) in Flet's exact format so cutover is zero-migration.

- **`library.db`**: `PRAGMA user_version` = `GEOMETRY_VERSION` (currently `2`; keep `kGeometryVersion` synced with Python). FTS5 is maintained by 11 SQLite triggers.
  - **Already ported (Phases 0–6a):** `artists`, `albums`, `tracks`, `play_counts`, `playback_history`, `artist_enrichment`, `genre_affinity`, `track_neighbors`, `playlists`, `playlist_tracks`, `fts_search`, `artist_ai_research` (read), `artist_releases`, `release_sync_log`.
  - **Phase 6d writes:** `play_counts` local DSP feature writes.
  - **Phase 7 tables:** `artist_ai_research` (writes), `metadata_audit_dismissed`, `artist_aliases`, `artist_alias_rejections`.
- **Sidecar files already ported:** `.config/streamrip/config.toml` (read/write byte-identical to tomlkit 0.15.2), `queue_state.json`, `queue_pos.json`, `native_resume.json`, `.cache/queue_{regular,shuffle}.json`, `flet_prefs.json`, `play_ledger.jsonl`, `companion_transfers.json`, `chat_history.json`, `graph_state.json`, `recent_searches.json`, `mai_an_lab_port_state_{latest,import}.zip` (renamed back at cutover, §5).
- **Sidecar files remaining:**
  - `downloads.db`, `failed_downloads.db` → **Phase 6c**
  - `.onboarded` (boot-import already writes it; wizard reads/writes it) → **Phase 7**

---

## 4. Phase roadmap & remaining work

| # | Phase | Scope | Status |
|---|---|---|---|
| 0–5 | Backbone, Player, Stations, Library writes/Settings/Network, Sync (Android), Jarvis Assistant + Voice | 59/91 features, 732 tests passing. See [PORT_HISTORY.md](PORT_HISTORY.md) for stage details. | 🧪 Code done; phone verify pending |
| **6** | **Sources, Downloads, New Releases & On-Phone DSP** | Full Flet parity (Decisions A & B): **6a done** (New Releases, cache pruning, recent searches, local-library hits & ownership badges, URL artwork); **6b done** (Qobuz/Deezer clients, Blowfish-CBC & AES-128-ECB crypto, downloadables, search/metadata parsers, `StreamripSearcher`, `downloadFromSource`, Settings → Authentication & Storage Default Download Quality); 6c–6d remaining (full Search UI/previews/downloads/tagger, quality selector, Jarvis `search_online`, on-phone `dsp.py` + bulk analysis & DSP Compute settings). | 🔨 6a–6b 🧪; 6c–6d next |
| **7** | **Metadata Tools & Lean Onboarding** | Independent of Phase 6: Metadata workbench (one confidence-ordered artist list), auto-enrichment pass (MusicBrainz/Qobuz/AI), artist editor, genre curation, audit/dismissals, aliases, Jarvis metadata write tools, leaner onboarding wizard. | 🔨 Ready to plan & build |
| **P** | **Pre-cutover Visual & UX Polish** | Device-driven polish pass after hardware testing: edge-to-edge sheet insets, Hero artwork transitions (mini-player ↔ Now Playing), motion/haptic tuning, tabular numerals & type hierarchy, Network canvas touch feel. | 🔨 After phone testing |
| **8** | **macOS Desktop** | Responsive sidebar/split-pane desktop shell, AVFoundation/macOS audio engine (`audio_engine_macos.py`), Companion sync server + ffmpeg transcoder (`sync/server.py`, `transcoder.py`), native folder pickers, Mac download success sound. | 🔨 After Android cutover |

### Phase 6 suggested staging (Decisions A = Full, B = Yes)
1. **6a — Public New Releases & Search history (🧪 Done):** `data/release_queries.dart`, `search/new_releases.dart` (`ReleaseFetcher` public Deezer + MusicBrainz, `refreshNewReleases`, `getNewReleases`, `NewReleasesScheduler` daily gate), `core/cache_pruner.dart` (`_prune_caches_async`), `search/search_history.dart` (`recent_searches.json`), `core/text_match.dart` (`levenshteinDistance`), `search/local_search.dart` (local-library hits & ownership badges), `player/ui/artwork.dart` (URL support), Search tab *New From Your Artists* shelf + Recent Searches sheet + local search bar. Goldens in `test/goldens/new_releases.json` (`tool/export_new_releases_goldens.py`).
2. **6b — Source clients (Qobuz & Deezer) (🧪 Done):** `sources/source_exceptions.dart`, `sources/source_config.dart`, `sources/source_client.dart`, `sources/deezer_crypto.dart` (clean-room Blowfish-CBC & AES-128-ECB bit-verified against `pycryptodomex`), `sources/downloadable.dart`, `sources/qobuz_client.dart` (`QobuzSpoofer`, `QobuzClient`), `sources/deezer_client.dart`, `sources/metadata_objects.dart`, `sources/source_metadata.dart`, `sources/search_results.dart`, `sources/streamrip_api.dart`, `sources/streamrip_search.dart`, Settings → Authentication (`settings/auth_settings.dart`), Settings → Storage Default Download Quality (`settings/groups.dart`). Goldens in `test/goldens/sources_phase6b.json` (`tool/export_sources_goldens.py`) and updated `test/goldens/settings_search.json`.
3. **6c — Search UI, previews & download pipeline:** Full Search tab (hero/album/artist cards, pagination, source switch, missing-credentials banner), stream previews (progress card, cancel, preview cache pruning), Quality selector sheet, Download queue source workflows + `history_manager.py` (`downloads.db`, `failed_downloads.db`), `tagger.tag_file` (evaluate pure-Dart `metatagger` first; see [THIRD_PARTY.md](THIRD_PARTY.md)), Long-press → Redownload, Jarvis `search_online` hook.
4. **6d — On-phone DSP analysis:** Port `dsp.py` (906 lines) over existing Kotlin `decodePcm` + `bulk_analyze_library` (cancellable job with `LibraryTaskService` + `progress_notify`, handling Android 15+ `dataSync` timeout). Wire Settings → Database Management (`Commence/Continue DSP Compute`, `Recompute All DSP Features`), Network setup card *Compute DSP Features* button, and quiet-scan *Analyse* button. Verify feature parity against Python on real audio fixtures.

### Phase 7 suggested staging (independent of Phase 6)
1. **7a — Enrichment & audit backend:** `metadata_enrich.py`, `metadata_qobuz.py`, `metadata_ai.py` (write/research half), `metadata_audit.py`; DB tables (`artist_ai_research`, `metadata_audit_dismissed`, `artist_aliases`, `artist_alias_rejections`); background enrichment pass (launch + post-scan) + `build_genre_affinity` trigger.
2. **7b — Jarvis metadata tools & Workbench UI:** Wire `research_artist_metadata` and `apply_artist_metadata` in `AssistantRunner`; port `metadata_workbench.py` (confidence-ordered artist list, search, suggestions, sync, artist editor, genre curation, audit/dismissals, aliases); wire Settings → Metadata, Library Artists edit button, Genres curate button, and `workbench` notification target.
3. **7c — Lean onboarding wizard:** Simplified first-run setup (folders, credentials, appearance, startup tab) + `.onboarded` marker (skipped when restored from state bundle).

---

## 5. Cutover (Android)

Replaces the Flet APK in place with zero data loss:
1. Remove `applicationIdSuffix ".port"` in `android/app/build.gradle.kts`.
2. Sign with the **same key as Flet** (`~/.android/debug.keystore`). ⚠️ **Back this keystore up off-Mac now**; losing it forces uninstall.
3. Bump `versionCode` above Flet (`> 1`).
4. Move `flet_audio_service/.../kotlin` into `packages/maian_native` (remove symlink) and re-enable Android release lint (`checkReleaseBuilds = true`).
5. Restore Flet boot bundle names (`mai_an_lab_state_latest.zip`, `mai_an_lab_state_import.zip`) in `sync/state_bundles.dart`.
6. Resolve `assets/config_template.toml` license (§6).
7. Swap branches: `flutter-port` → `master`, Flet → `flet`.

*(Moving to a release keystore later requires one uninstall/reinstall; state bundle export/import preserves all user data.)*

---

## 6. Decisions

- **A — Downloads & streaming sources on phone: YES (full parity).** Port Qobuz, Deezer, streamrip search/previews/downloads, quality selector, tag writing, Authentication settings, and Jarvis `search_online` in Phase 6.
- **B — On-phone DSP analysis: YES.** Port `dsp.py` feature extraction and `bulk_analyze_library` in Phase 6 so phones can analyze tracks locally in addition to Mac offload.
- **C — Simplify / drop:**
  - **Drop:** `mock_search.py` and Demo search mode; Flet-only plumbing (`error_boundary.py`); dev diagnostic scripts (`genre_eval.py`, `tools/*.py` stay Python-only on Mac).
  - **Simplify:** Leaner onboarding wizard (Phase 7c); omit debug play counts and fresh-install reset from Settings → Advanced unless specifically requested.
  - **Keep:** `progress_notify` (already ported in 4b).
- **Still open (pre-release):** `assets/config_template.toml` is streamrip's GPL-3.0 template + app sections ([THIRD_PARTY.md](THIRD_PARTY.md)). Keep as-is (same as Flet APK) or replace with a clean custom template before shipping.

---

## 7. Intentional divergences & Flet bugs found

Permanent deliberate differences from Flet (do **not** "fix" these back to Flet's buggy/legacy behavior). Historical items fixed in Phases 1–5 are in [PORT_HISTORY.md](PORT_HISTORY.md).

- **Playback & queue:** Auto-play listens arrive directly in-process from the audio handler (no ledger round-trip, ledger still written for DB drain). Queue edits while unloaded defer until first play. Shuffle toggle preserves track position & play/pause state (`keepListen`) and doesn't log a fake skip. Queue rows carry `duration`. Per-track DSP (`reapply_dsp`) runs on background auto-advance too. Restored tracks missing on disk load on first Play.
- **Search & sorting:** Instant in-memory word-substring matching (accent/case-blind across all scripts, including playlists) + identical closest-match fallback; tie-break by `id`. Tracks & Albums share sort; Artists & Genres keep their own sort (default A–Z).
- **Library, playlists & writes:** DB migrations run in a single transaction; multi-track playlist adds run in one transaction; torn/non-object ledger lines are safely dropped; `tracks.added_date` uses ms precision (well inside 1 s mtime tolerance); `NULL added_date` re-scans cleanly; untagged files stay unindexed (for parity); playlist drag handles & track counts update immediately; duplicate playlist name shows error; Back exits selection mode first.
- **Network & acoustic SVD:** Pinch-zoom counter-scales every frame; step pills centered; rebuild cancels stale re-walks; SVD uses float64 Jacobi on identical float32 `Z` with `svd_flip` sign convention and symmetric `(C + Cᵀ)/2` tie-break (matching all 20 golden stations); graph readiness builds 5 s after start on DB isolate without scroll-pause wait.
- **Settings & config:** Changes apply live without UI restart; EQ curve/slider writes on drag end (haptics every 0.1 dB); tapping switch labels toggles; Album Cache clears both `temp/` and Android `np_art`; `config.toml` repair (`fill_missing`) deferred until template keys change; About copies GitHub link + offers `showLicensePage`.
- **Sync & bundles:** Android uses `MulticastLock` + `ConnectivityManager` IPv4; saved transfers use `MediaScannerConnection.scanFile` (no `chmod`); `Send to Mac` uses RFC 5987 `filename*=utf-8''` so non-ASCII filenames don't corrupt; state import reloads live without force-close; boot snapshots use `mai_an_lab_port_state_*.zip` until cutover (§5); Offload button stays disabled at 0 tracks; desktop dev builds run client view until Phase 8.
- **Assistant & permissions:** `RECORD_AUDIO` requested on first mic press (not first launch). All 30 agent tools declared; unported hooks (`search_online`, metadata writes) return a clean error dict until Phases 6–7. Disambiguation `response.extras["options"]` is properly forwarded to option buttons.
- **4 Flet bugs found (not yet backported to `master`):**
  1. `library.py` `_open_album_context_menu`: Play Album swaps `(album, artist)` tuple.
  2. `settings.py` `_save_appearance_settings`: writes `default_sort` instead of `library_sort`.
  3. `library.py` `_build_interactive_network_canvas`: missing `walk_seed` node on rebuild.
  4. `sync/send.py` `push_track`: non-ASCII filenames percent-encoded and saved raw by server.

---

## 8. Dev & testing workflow

- **Connected Android phone (fastest, real audio/sync):**
  ```bash
  cd mai_an_flutter
  ./tool/install_phone.sh --db          # debug build + latest phone state bundle (library.db + config.toml)
  ./tool/install_phone.sh --release     # smooth arm64 release build (keeps data from debug install)
  flutter run                           # hot reload (r) / hot restart (R)
  ```
  *Tip:* `brew install scrcpy && scrcpy` mirrors and controls the phone directly on macOS.
  `tool/push_db.sh` pushes the newest bundle from `tools/state_backups/` (Android paths).
- **Android Emulator on macOS (no Xcode needed):** `~/Library/Android/sdk/emulator/emulator` and system image `android-36.1` are already installed — create an AVD with `avdmanager` and run `flutter run`.
- **Native macOS UI preview on macOS 15.7.3 (Sequoia):** App Store blocks newer Xcode, but **Xcode 16.4** works on macOS 15.7.3 (`brew install xcodesorg/made/xcodes && xcodes install 16.4`, then `sudo xcode-select -s /Applications/Xcode.app/Contents/Developer`; CocoaPods is already installed). Run read-only against the Mac Flet DB:
  ```bash
  flutter run -d macos --dart-define=LIBRARY_DB=/Users/chrismitsacopoulos/Desktop/Mai-An-Lab/StreamripApp/.flet/storage/data/library.db
  ```
  *(UI/Library/Network/Jarvis/Companion client work; Mac audio playback & sync server arrive in Phase 8.)*
- **Headless tests & analyzer (zero setup):**
  ```bash
  flutter analyze && flutter test
  LIBRARY_DB=/path/to/library.db flutter test test/real_library_test.dart
  ```
- **Goldens:** Run `~/miniconda3/bin/python tool/export_*_goldens.py` from `mai_an_flutter/` (run `export_station_goldens.py` before network/acoustic real-library exports; `tinytag==2.3.2` & `tomlkit==0.15.2` live in `tool/.pydeps`).
- **Native Kotlin:** `packages/maian_native/android/src/main/kotlin` is symlinked to `flet_audio_service/.../kotlin` so fixes apply to both apps until cutover (§5).

---

## 10. Open items (handoff)

1. [ ] **Verify Phases 1–5 on the Pixel 8:** Run `./tool/install_phone.sh --db` (and later `--release` to test R8). Check:
   - Playback, background/lockscreen controls, EQ curve & sliders, Dynamism, folder browser, Permissions flow, Clear Index.
   - Acoustic Network (enable in Appearance): pinch/pan, small-node taps, pulse, Now Playing across track changes.
   - Companion & Sync: real Mac pairing (cleartext HTTP), transfer end-to-end, bundle export + `mai_an_lab_port_state_import.zip` round-trip, DSP offload end-to-end (notification Cancel screen-off, graph rebuild, Recompute PCA).
   - Jarvis: chat + tool calls, Settings → AI Assistant, TTS voice choice, hold-to-talk STT (`RECORD_AUDIO` prompt), music pause/resume while speaking.
   - Edge-to-edge bottom-sheet insets (§1 row 19).
2. [ ] **Ops:** Back up `~/.android/debug.keystore` off this Mac (§5). (`master` and `flutter-port` + `v0.1.0` are synced with `origin`.)
3. [ ] **Phase 6** (Sources, Downloads, New Releases & On-Phone DSP — §4, Decisions A & B).
4. [ ] **Phase 7** (Metadata Workbench, Auto-Enrichment & Lean Onboarding — §4, independent of Phase 6).
5. [ ] **Pre-cutover UI Polish pass** (insets, Hero/sheet motion, tabular typography, Network touch feel).
6. [ ] **Decide `assets/config_template.toml`** (keep GPL-3.0 template vs clean rewrite) and complete **Android Cutover (§5)**.
7. [ ] **Phase 8** (macOS responsive desktop layout, AVFoundation player, sync server + ffmpeg transcoder).
8. [ ] *(Optional)* Backport the 4 Flet bugs (§7) to `master`.

---

## 11. Remaining features (to build)

65 of 91 Flet features are fully ported (✅/🧪, Phases 0–5 archived in [PORT_HISTORY.md](PORT_HISTORY.md), Phase 6a–6b in §4 above — with 6b source clients & Auth/Quality settings in place); 3 dropped per Decision C (`Demo search mode`, `debug play counts`, `fresh-install reset` — revisit if ever needed). The **23 remaining feature rows** below define completion:

### Phase 6c–6d — Search UI, Downloads & On-Phone DSP (9)
| Area | Feature | Notes |
|---|---|---|
| Player | Quality selector sheet (download quality) | `ui/player/quality_selector.py` |
| Player | Download dock — source downloads | Mac transfers done; wire streamrip download tasks |
| Library | Long-press → Redownload | `library/track_menu.dart` |
| Library | On-phone DSP compute (cancellable, notification) | `dsp.py` + `bulk_analyze_library` over `decodePcm` (Decision B); wire Network & quiet-scan Analyse buttons |
| Search | Qobuz / Deezer search UI (hero, cards, pagination, source switch, creds banner) | Backend done in 6b (`lib/sources/`); wire Search tab UI |
| Search | Stream previews (progress card, cancel) | Preview playback + cache |
| Search | Download result / selection (quality sheet, dock, haptic/sound, auto-scan) | `history_manager.py` (`downloads.db`, `failed_downloads.db`), `tagger.tag_file` |
| Jarvis | `search_online` tool hook | Wire into `AssistantRunner` |
| Settings | DB Management → DSP Compute / Recompute All | Authentication & Default Download Quality done in 6b |

### Phase 7 — Metadata Tools & Lean Onboarding (10)
| Area | Feature | Notes |
|---|---|---|
| App shell | Onboarding on first launch + `.onboarded` marker | Leaner wizard (Decision C); skipped on bundle restore |
| App shell | Notification tap → Metadata workbench (`workbench` target) | Register in `NotificationRouter` |
| Startup job | Artist-metadata enrichment pass (MusicBrainz / Qobuz; launch + post-scan) | `metadata_enrich.enrich_pending` + `build_genre_affinity` |
| Library | Artists edit-metadata button & Genres curate button | Wire to editor / workbench |
| Settings | Metadata group → opens workbench | `settings/` |
| Metadata | Metadata workbench (confidence-ordered list, search, suggestions, sync) | `metadata_workbench.py` |
| Metadata | Artist metadata editor (country, genres, hand-set flag) | `metadata_enrich` |
| Metadata | Genre curation | `metadata_workbench.py` |
| Metadata | Metadata audit & dismissals; artist aliases & rejections | `metadata_audit.py` |
| Metadata / Jarvis | AI artist research + `research_artist_metadata` / `apply_artist_metadata` tools | `metadata_ai.py` write half |

### Minor / Optional & Phase 8 — macOS (4)
| Area | Feature | Notes |
|---|---|---|
| App shell | Splash screen while app loads | Only if cold start is noticeably slow on Pixel 8 |
| App shell | `config.toml` `fill_missing` repair | Only when `config_template.toml` gains new keys |
| macOS (8) | Mac player (AVFoundation engine), native folder pickers, download sound, responsive desktop layout | `audio_engine_macos.py` + desktop shell |
| macOS (8) | Companion sync server + ffmpeg transcoder + Mac Sync Hub view | `sync/server.py`, `transcoder.py`, `ui/views/sync_hub.py` |

---

## 12. Third-party code and sources

Full attribution table, ported algorithm/table credits, pub.dev dependencies, and Phase 6 tag-writer candidates live in [THIRD_PARTY.md](THIRD_PARTY.md). Add any new external source or template there before committing.
