# Mai An Lab: Flet → Flutter port map

*Updated: 2026-11-25. Branch `flutter-port`, version **`0.3.0+4`** (last tag `v0.1.0`; tags `v0.2.0` and `v0.3.0` not yet created). **Phases 0–7 code is complete** (84/91 Flet features ported; 889 desktop tests pass including real-library and real-audio goldens; strict Dart analyzer clean). Decisions A, B and C are resolved (§6).*

*Primary blocker right now: **on-device verification**: the phone still has an older pre-scan/pre-Settings build, so Phases 1–7 are `🧪` (written & tested on desktop, not yet verified in daily phone use). Next: **Device hardening (H, `v0.3.x`)**: playback ↔ OS bugs seen on the phone and performance, then the **pre-cutover UI Polish pass**, Android cutover (§5), and **Phase 8** (macOS).*

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
   - `v0.3.x`: Device hardening (H): phone-verification fixes, playback ↔ OS, performance
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
| 8 | **Long screen-off tasks** | Reuses Kotlin `LibraryTaskService` (`dataSync`). Android 15+ caps `dataSync` at ~6 h/24 h; the Kotlin `onTimeout` sends `fgs_timeout`, which `ProgressNotifier` now forwards to jobs that pass `onTimeout` (on-phone DSP stops between tracks with an "Analysis paused" summary). ⚠️ Offload and downloads don't opt in yet; verify on device. | 4, 6 |
| 11 | **Path identity & NFC** | Scanner stores raw `os.scandir` paths; app-named files (transfers, downloads) apply `core/nfc.dart` (Python `unicodedata` NFC). Reuse `core/nfc.dart` in Phase 6 downloads. | 4, 6 |
| 13 | **Audio analysis (DSP)** | Kotlin `decodePcm` (MediaCodec → mono PCM, 120 s from the middle) now has a Dart wrapper (`MaianNative.decodePcm`); `dsp.py` runs in pure Dart on a worker isolate (6d, ~2 s per track on the Mac JIT). ⚠️ Measure per-track time, peak memory (~80 MB in the worker) and decode while playing on the Pixel 8. | 6 🧪 |
| 16 | **Cleartext HTTP to Mac** | `dart:io` sockets bypass Android's cleartext policy (`INTERNET` declared). Confirm on device with first real Mac pairing. | 4 🧪 |
| 17 | **LAN discovery** | `sync/discovery.dart` (`RawDatagramSocket` UDP 54545) + `MainActivity` `acquireMulticastLock` & `defaultRouteIpv4`. ⚠️ Recheck local-network permission rules before raising `targetSdk` past 36. | 4 🧪 |
| 19 | **Edge-to-edge & predictive back** | `targetSdk 36` enforces both. Root back calls `moveTaskToBack`; predictive back enabled. **Still to audit:** bottom-sheet & modal edge-to-edge insets on device. | Polish |
| 22 | **Release build & lint** | Release arm64 APK builds (27.5 MB), R8 not yet verified on device. `checkReleaseBuilds = false` works around lint crashing on the symlinked Kotlin in `packages/maian_native`; restore lint once Kotlin moves in at cutover. | Cutover |
| 23 | **macOS target** | Mac stays on Flet until Phase 8. On macOS 15.7.3, install Xcode 16.4 (not App Store) for local `flutter run -d macos` (§8). | 8 |

---

## 2. Data contract

All files live under `<appSupport>/data` (Android: `/data/user/0/<pkg>/files/data`) in Flet's exact format so cutover is zero-migration.

- **`library.db`**: `PRAGMA user_version` = `GEOMETRY_VERSION` (currently `2`; keep `kGeometryVersion` synced with Python). FTS5 is maintained by 11 SQLite triggers.
  - **Already ported (Phases 0–6):** `artists`, `albums`, `tracks`, `play_counts` (including on-phone DSP feature writes, `update_track_features`), `playback_history`, `artist_enrichment`, `genre_affinity`, `track_neighbors`, `playlists`, `playlist_tracks`, `fts_search`, `artist_ai_research` (read), `artist_releases`, `release_sync_log`.
  - **Ported in Phase 7:** `artist_ai_research` (writes), `metadata_audit_dismissed`, `artist_aliases`, `artist_alias_rejections`.
- **Sidecar files already ported:** `.onboarded` (Phase 7c), `.config/streamrip/config.toml` (read/write byte-identical to tomlkit 0.15.2), `queue_state.json`, `queue_pos.json`, `native_resume.json`, `.cache/queue_{regular,shuffle}.json`, `flet_prefs.json`, `play_ledger.jsonl`, `companion_transfers.json`, `chat_history.json`, `graph_state.json`, `recent_searches.json`, `history.json`, `mai_an_lab_port_state_{latest,import}.zip` (renamed back at cutover, §5).
- **Sidecar files remaining:** none.
  *(Note: `downloads.db` / `failed_downloads.db` exist only as legacy streamrip `config.toml` path keys; Flet itself writes `history.json` via `history_manager.py`, now ported in `downloads/download_history.dart`.)*

---

## 4. Phase roadmap & remaining work

| # | Phase | Scope | Status |
|---|---|---|---|
| **H** | **Device Hardening: Playback ↔ OS & Performance** | Driven by the phone, before visual polish (perf fixes can move what Polish tunes). **Playback ↔ OS:** bugs reported on the release build: skipping from the notification and then in the app, auto-play glitching when a song is removed from the queue (buffer rebuilt, removed song back). Fixed so far: system Next/Previous now go through `PlayerController` (`RemoteControls`); removals steer auto-play without rebuilding the buffer and are never offered again (§7). Still to check: media session/notification state after stop, swipe-away and process death; headset/car buttons; `skipToQueueItem` from system UIs (queue published in play order). **Performance:** DSP per-track time/memory and decode while playing (§1 row 13), `dataSync` 6 h cap for offload/downloads (§1 row 8), Phase 7 metadata pass cost (MusicBrainz rate limit, `refreshWalkModels`) during playback and cold start, R8 release build (§1 row 22), cold start (splash, §11), scroll jank. Measure with `tool/install_phone.sh --profile` + DevTools. | 🔨 Next |
| **P** | **Pre-cutover Visual & UX Polish** | Device-driven polish pass after hardware testing: edge-to-edge sheet insets, Hero artwork transitions (mini-player ↔ Now Playing), motion/haptic tuning, tabular numerals & type hierarchy, Network canvas touch feel. | 🔨 After phone testing |
| **8** | **macOS Desktop** | Responsive sidebar/split-pane desktop shell, AVFoundation/macOS audio engine (`audio_engine_macos.py`), Companion sync server + ffmpeg transcoder (`sync/server.py`, `transcoder.py`), native folder pickers, Mac download success sound. | 🔨 After Android cutover |

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

- **Still open (pre-release):** `assets/config_template.toml` is streamrip's GPL-3.0 template + app sections ([THIRD_PARTY.md](THIRD_PARTY.md)). Keep as-is (same as Flet APK) or replace with a clean custom template before shipping.

---

## 7. Intentional divergences & Flet bugs found

Permanent deliberate differences from Flet (do **not** "fix" these back to Flet's buggy/legacy behavior). Historical items fixed in Phases 1–5 are in [PORT_HISTORY.md](PORT_HISTORY.md).

- **Playback & queue:** The system's Next/Previous (notification, lockscreen, headset, car) run the controller's `next()`/`previous()` instead of moving just_audio directly (Flet's extension did the latter): one path for every skip, Previous restarts past 3 s, Next at the end asks auto-play. Auto-play listens arrive directly in-process from the audio handler (no ledger round-trip, ledger still written for DB drain). Queue edits while unloaded defer until first play. Shuffle toggle preserves track position & play/pause state (`keepListen`) and doesn't log a fake skip. Queue rows carry `duration`. Per-track DSP (`reapply_dsp`) runs on background auto-advance too. Restored tracks missing on disk load on first Play.
- **Search & sorting:** Instant in-memory word-substring matching (accent/case-blind across all scripts, including playlists) + identical closest-match fallback; tie-break by `id`. Tracks & Albums share sort; Artists & Genres keep their own sort (default A–Z).
- **Library, playlists & writes:** DB migrations run in a single transaction; multi-track playlist adds run in one transaction; torn/non-object ledger lines are safely dropped; `tracks.added_date` uses ms precision (well inside 1 s mtime tolerance); `NULL added_date` re-scans cleanly; untagged files stay unindexed (for parity); playlist drag handles & track counts update immediately; duplicate playlist name shows error; Back exits selection mode first.
- **Network & acoustic SVD:** Pinch-zoom counter-scales every frame; step pills centered; rebuild cancels stale re-walks; SVD uses float64 Jacobi on identical float32 `Z` with `svd_flip` sign convention and symmetric `(C + Cᵀ)/2` tie-break (matching all 20 golden stations); graph readiness builds 5 s after start on DB isolate without scroll-pause wait.
- **Settings & config:** Changes apply live without UI restart; EQ curve/slider writes on drag end (haptics every 0.1 dB); tapping switch labels toggles; Album Cache clears both `temp/` and Android `np_art`; `config.toml` repair (`fill_missing`) deferred until template keys change; About copies GitHub link + offers `showLicensePage`.
- **Sync & bundles:** Android uses `MulticastLock` + `ConnectivityManager` IPv4; saved transfers use `MediaScannerConnection.scanFile` (no `chmod`); `Send to Mac` uses RFC 5987 `filename*=utf-8''` so non-ASCII filenames don't corrupt; state import reloads live without force-close; boot snapshots use `mai_an_lab_port_state_*.zip` until cutover (§5); Offload button stays disabled at 0 tracks; desktop dev builds run client view until Phase 8.
- **Auto-play removals:** Removing a recommendation steers the station and tops up behind the rows still showing; it no longer counts toward the two-negative replan (Flet rebuilt the list the listener was editing). Removed songs are kept for the session (`AutoPlay.removed`, last 150) and never offered again, even by a new station or a genre restart (Flet's reopened station could bring them back). Early skips still replan as in Flet.
- **On-phone DSP:** One analysis at a time (Flet could start a second from Settings while the Library one ran); the Network progress card and Settings get an in-app Cancel (Flet: notification only); `fgs_timeout` stops the run between tracks with an "Analysis paused" summary (Flet ignored it); the Library flow's "nothing to analyse" path posts no notification, as in Flet. Features agree with `dsp.py` to float32 precision (numpy's single-precision FFT/BLAS), not bit for bit; BPM and key match exactly on every golden.
- **Assistant & permissions:** `RECORD_AUDIO` requested on first mic press (not first launch). All 30 agent tools declared and wired. Disambiguation `response.extras["options"]` is properly forwarded to option buttons.
- **Metadata (Phase 7):** The audit's collaborator-culture vote iterates linked artists in sorted order (Flet iterates a Python `set`, so ties could flip between runs). The workbench's manual Sync waits for a running automatic pass instead of racing it (one pass at a time). Workbench reloads that overlap (the page opening while a Library edit button asks for a focused one) share a single load, and a focused artist's editor opens even if it was found before the page listened. The Sync pill's label ellipsizes on narrow screens.
- **Onboarding (Phase 7c):** One `.onboarded` marker in the data dir (Flet wrote copies beside config.toml and in Flet storage; here those are the same folder). Finishing starts a scan of the chosen folder (Flet's text promised "Auto-indexed on startup" but nothing indexed until the user scanned); the folder hint reads "Indexed when you finish." Accent, views and startup screen apply live (no `restart_ui`).
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
  ./tool/install_phone.sh --profile     # release speed + DevTools: `flutter attach` for jank/CPU/memory
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
- **Goldens:** Run `~/miniconda3/bin/python tool/export_*_goldens.py` from `mai_an_flutter/` (run `export_station_goldens.py` before network/acoustic real-library exports; `tinytag==2.3.2` & `tomlkit==0.15.2` live in `tool/.pydeps`). `export_dsp_goldens.py --real 6` also caches real Flet-library tracks for the DSP parity test (needs ffmpeg).
- **Native Kotlin:** `packages/maian_native/android/src/main/kotlin` is symlinked to `flet_audio_service/.../kotlin` so fixes apply to both apps until cutover (§5).

---

## 10. Open items (handoff)

2. [ ] **Device hardening (H, `v0.3.x`, §4)** — after or alongside item 1:
   - Playback ↔ OS (reported): re-test notification skip → in-app skip, and removing auto-play rows, on `--release --logs`; the log names every auto-play replan/new station (`AutoPlay:`) and queue repair (`PlayerController: divergence`). Then: stop/swipe-away/idle release and Play again, process death resume, headset & car buttons.
   - Performance: `--profile` + DevTools: DSP seconds/track and memory (with and without music playing), metadata pass during playback, cold start, Library/Network scroll jank; R8 release.
3. [ ] **Ops:** Back up `~/.android/debug.keystore` off this Mac (§5). (`master` and `flutter-port` + `v0.1.0` are synced with `origin`.)
4. [x] **Phase 6** code (Sources, Downloads, New Releases & On-Phone DSP — §4, Decisions A & B). Bumped to `0.2.0+3`; tag `v0.2.0` and push when ready.
5. [x] **Phase 7** code (Metadata Workbench, Auto-Enrichment & Lean Onboarding — §4). Bumped to `0.3.0+4`; tag `v0.3.0` when ready. Phone checks: the boot pass notification (30 s after launch), a scan → pass, Settings → Metadata Sync (MusicBrainz and Qobuz), AI Research with a Gemini key, Library edit/curate buttons, notification Retry/Review buttons, Jarvis "set X's genre to Y" → yes, and a fresh install (clear data) showing the wizard.
6. [ ] **Pre-cutover UI Polish pass** (insets, Hero/sheet motion, tabular typography, Network touch feel).
7. [ ] **Decide `assets/config_template.toml`** (keep GPL-3.0 template vs clean rewrite) and complete **Android Cutover (§5)**.
8. [ ] **Phase 8** (macOS responsive desktop layout, AVFoundation player, sync server + ffmpeg transcoder).
9. [ ] *(Optional)* Backport the 4 Flet bugs (§7) to `master`.

---

## 11. Remaining features (to build)

84 of 91 Flet features are ported (✅/🧪, Phases 0–5 archived in [PORT_HISTORY.md](PORT_HISTORY.md), Phases 6–7 in §4 above); 3 dropped per Decision C (`Demo search mode`, `debug play counts`, `fresh-install reset` — revisit if ever needed). The **4 remaining feature rows** below define completion:

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
