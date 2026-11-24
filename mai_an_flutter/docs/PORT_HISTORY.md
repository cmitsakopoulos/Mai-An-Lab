# Mai An Lab: Port History & Completed Reference Archive

*Archived from `PORT_MAP.md` on 2026-11-24 after Phases 0–5 reached code completion (59/91 features, 732 tests passing). Read on demand for historical context, golden-test inventories, completed module mappings, and the Phase 0–1 audit. For active handoff, see [PORT_MAP.md](PORT_MAP.md).*

---

## Flet codebase size (baseline reference)

| Area | Python (lines) |
|---|---|
| `main.py` | 3,287 |
| `utils/` | 23,922 |
| `utils/sync/` | 3,265 |
| `ui/` (views, player, widgets, tokens) | 19,970 |
| **App total** | **~50,400** |
| Tests (`StreamripApp/tests/`) | 16,879 |
| `flet_audio_service` extension, Dart (reused) | 1,878 |
| `flet_audio_service` extension, Kotlin (reused) | 1,283 |

---

## Completed phases (0–5) summary & commit log

Commits on `flutter-port` through 2026-11-24:
- `4a116b9` (Phases 0–1), `c6ff28d` (Phase 0–1 audit fixes), `73ca39e` (plan update), Phase 2 (stations + `tool/install_phone.sh`), `2ec2865` (merge of `master`: plain-toggle chain, one-list workbench).
- Phase 3 (pushed through 3g): strict analyzer & format, Python primitives, migrations & play-ledger drain, scan engine (`tinytag` 2.3.2), views & playlists queries, library UI (views, scan, playlists, long-press menu, selection, delete), tab bar & Search landing.
- Local commits after 3g: Settings (`e946579`), Acoustic Network view (`build_metadata_edges`), Phase 4a–4g (Companion protocol, notifications, sync manager, download queue & dock, Companion UI, state bundles, DSP offload & acoustic SVD/NPMI build), Phase 5a–5g (Jarvis assistant & voice).

### Phase 0 — Backbone ✅
Read-only DB isolate, Tracks tab with Flet sort modes & search semantics, coordinate graph, Android build, 16 KB ELF/zip alignment verified.

### Phase 1 — Phone player 🧪
Lifted `flet_audio_service` handler into `lib/player/audio_handler.dart` and `packages/maian_native`; thin `PlayerController` with in-process op ordering; `PlayQueue` + Flet's 5 persistence files (`queue_state.json`, `queue_pos.json`, `native_resume.json`, `.cache/queue_{regular,shuffle}.json`) & `flet_prefs.json`; `reapply_dsp` (EQ + Dynamism); Mini Player, Now Playing, Queue Sheet; permissions; lifecycle hide/show; root back → `moveTaskToBack`.

### Phase 2 — Stations 🧪
Golden export script (`tool/export_station_goldens.py`), `genre_taxonomy.dart`, `credit_keys.dart`, `artist_graph.dart`, `station.dart` (Drift + coordinate walk), `autoplay.dart`, chain toggle button, Queue Sheet AUTO-PLAY header, Auto-play sheet, Start radio, background prefill to 24. All 20 real-library golden seeds match Python exactly.

### Phase 3 — Library writes, Views, Settings, Network 🧪
- **3a (Views):** View bar, Genres, Artists, Albums, `genre_colors.dart`, `pyTitle` with Greek Final_Sigma; goldens match Python.
- **3b (Schema & Ledger):** Port owns `data/library_schema.dart` (`get_connection` + `initialize`) and `data/play_ledger.dart` (`record_listens`); verified on synthetic, legacy, real DBs, and live on phone.
- **3c (Scan):** `scan/library_scanner.dart`, `scan/tinytag.dart` (2.3.2 ported line-by-line), `scan/storage_paths.dart`, `data/scan_writes.dart`; identical to Python on synthetic corpus and 454-track Mac library.
- **3d (Playlists & Menus):** `data/playlists.dart`, playlists view/editor/reorder/delete, full long-press menu, selection mode, Delete Track; goldens match Python.
- **3g (Shell & Search landing):** `app_shell.dart` tab bar (Search, Library) and Search landing local cards (`data/landing_queries.dart`).
- **3h (Settings):** Hub + fuzzy search, Storage (Android folder navigator), Appearance, Audio & DSP (EQ curve & presets), Haptics, Permissions, Database Management, Advanced, About; `core/config_file.dart` byte-identical to `tomlkit 0.15.2` on 192 writes.
- **3i (Acoustic Network):** `library/network_view.dart`, `network_layout.dart`, `data/network_queries.dart` (`build_metadata_edges`); identical on synthetic scenarios, 24 real seed × density re-walk cases, and 13,788 artist edges.

### Phase 4 — Sync (Android side) 🧪
- **4a:** `sync/companion_client.dart`, `sync/multipart.dart`, `sync/discovery.dart` tested hermetically against Flet's `CompanionServer` (`tool/companion_test_server.py`); Kotlin `acquireMulticastLock` and `defaultRouteIpv4`.
- **4b:** `core/progress_notify.dart`, `core/notification_router.dart`.
- **4c:** `sync/sync_manager.dart` tested on virtual clock (`fake_async`) and against Flet's server.
- **4d:** `downloads/download_queue.dart`, `download_dock.dart`, `companion_download.dart`, `core/nfc.dart` (Python `unicodedata` tables, 6,142 strings), `pick_download_dir`, quiet scan & "not analysed" card.
- **4e:** `settings/sync_hub.dart` (4 Android cards), PIN/transfer dialogs, Send to Mac in track/album/selection menus, lifecycle hooks, `sync` notification target.
- **4f:** `data/state_export.dart`, `core/zip_archive.dart`, `sync/state_bundles.dart` (export, import, boot snapshot/import, Send Backup to Mac; round-trip tested against Python `state_export.py`).
- **4g:** `stations/acoustic_geometry.dart` (covariance cleaving, bit-exact float32 Z, float64 cyclic Jacobi SVD on ZᵀZ), `stations/genre_npmi.dart` (byte-identical NPMI model), `data/graph_build.dart`, `sync/offload_library.dart`, hub DSP Offload card, Recompute PCA, startup readiness check. Real-library coordinates reproduce Python's stations on all 20 golden seeds.

### Phase 5 — Jarvis Assistant & Voice 🧪
- **5a (Intent & text):** `assistant/intent.dart` (41 patterns over `core/py_re.dart` + `py_re_tables.dart` matching CPython `re`), `agent_text.dart`, `web_text.dart` (`names_mentioned`, `nfkdCasefoldStrip`), `chat_memory.dart` (`chat_history.json`, `graph_state.json` byte-for-byte), table-driven `pyLower`/`pyCasefold`. Golden-tested (`tool/export_assistant_goldens.py`, `test/assistant_text_test.dart`) on 4,961 utterances + all Unicode code points.
- **5b (LLM client & tools):** `assistant/llm_engine.dart` (Gemini/Ollama OpenAI-compatible POST, retry, timeouts, `_parse_tool_calls`, `list_gemini_models`), `assistant/agent_tools.dart` (30 tools). Golden-tested (`tool/export_llm_goldens.py`, `test/llm_engine_test.dart`) across 16 recorded HTTP scenarios.
- **5c (Deterministic runner & adapters):** `assistant/runner.dart`, `runner_phrases.dart`, `core/py_difflib.dart` (`SequenceMatcher.ratio` + autojunk), `core/py_values.dart`, `assistant/adapters.dart`, `data/assistant_queries.dart`, `PlayerController.setShuffleOrder`. Golden-tested (`tool/export_runner_goldens.py`, `tool/export_assistant_db_goldens.py`) across 14 scenarios (124 turns) and synthetic DB queries.
- **5d (Agent loop & tool executor):** `dispatchLlm` (≤6 rounds, raw message echo preserving `thought_signature`, confirmation/choice interrupts, final tool-free pass) + `tool_executor.dart` (24 library, playback, status, read-only metadata tools). Golden-tested (`tool/export_agent_goldens.py`, `test/assistant_agent_test.dart`) across 51 tool cases + 9 HTTP loop scenarios (22 requests byte-for-byte).
- **5e (Web research tools):** `assistant/web_research.dart` (DuckDuckGo Lite HTML parser, Wikipedia search, Gemini `generateContent` with `googleSearch` grounding, 30-min/200-entry cache, Google-off latch) wired to `web_search`, `learn_about`, `find_related_artists`. Golden-tested (`tool/export_web_research_goldens.py`, `test/web_research_test.dart`) across 29 cases.
- **5f (Jarvis UI & Settings):** `assistant/assistant_controller.dart`, `assistant_screen.dart` (`flutter_markdown_plus`, `url_launcher`, disambiguation buttons, 50-msg cap, graph init, lifecycle hooks), `AppShell` `AppTab.jarvis` + `show_jarvis`, `settings/assistant_settings.dart`, Appearance Jarvis switch (`test/assistant_ui_test.dart`).
- **5g (Voice):** `assistant/voice_service.dart` (`flutter_tts`, `speech_to_text`, `pickJarvisVoice` British male preference, rate/pitch 0.75), hold-to-talk state machine (60 s cap, 4 s release timeout, `RECORD_AUDIO` on first press, music pause/resume while speaking), Android `<queries>` (`test/voice_service_test.dart`).

---

## Phase 0–1 audit (2026-11-23, original §9)

Every Flet source in Phases 0–1 was audited line-for-line against the port (`AudioPlayerHandler`, `audio_engine.py`, `queue_layout.py`, `main.py` playback/persistence/lifecycle, `now_playing.py`, `mini_player.py`, `queue_sheet.py`, tracks part of `library.py` & `db_manager.get_all_tracks`).

Gaps found and fixed in `c6ff28d`:
1. Per-track DSP (`reapply_dsp`) & Dynamism badge added.
2. Haptics wired to `[haptics]` config; Play next / Add to queue use haptic tick instead of snackbar.
3. Track rows gained format badge, Flet subtitle rules, playing-row tint, and truncated durations.
4. Missing title/artist/album fallbacks matched Flet (`basename` / `"Unknown"`).
5. Playback-failure snackbar wording matched.
6. Queue sheet shows kept queue after stop.

---

## Completed parity fixes (archived from original §7)

Items that initially differed in early phases and were brought to parity:
- **Similar tracks:** Interim "Nearest by sound" sheet replaced with full `track_graph.open_station` + `artist_graph` Drift + `credit_keys` in Phase 2.
- **Initial sort:** Reads `general.library_sort` (fixed in Phase 1).
- **Track row:** Format badge, Flet subtitle rules, playing-row tint (fixed in Phase 0–1 audit).
- **Empty library:** `"ENTER PATHS"` opens Settings → Storage (fixed in Phase 3 Settings).

---

## Completed features (59 of 91, original §11)

All 59 rows below are implemented (`✅`/`🧪`):
- **App shell (7):** Accent colour from config, per-action haptics, playback-error snackbar/card, back navigation (sheets/selection/Settings/root background), lifecycle hooks, tab bar (Jarvis/Search/Library + startup tab + pushed Settings), notification tap routing (Library/downloads/sync; workbench in 7), Android download success haptic.
- **Startup & background jobs (7):** Position save loop & queue saves, auto-play history load, play-ledger drain, similarity-graph readiness build, state bundle auto-export & auto-import on boot (`.port` names until cutover), Companion auto-discovery.
- **Player (8):** Mini player / Now Playing / Queue sheet, background playback/notification/lockscreen/headset/audio focus, resume after process death, shuffle (set-aside queue) & repeat, per-track EQ & Dynamism + badge, embedded/sidecar artwork, Auto-play (chain/header/sheet/refills/feedback/prefill), Download dock (Mac transfers).
- **Library (13):** Tracks (rows/badges/sort/search/tint), Long-press (Play next, Add to queue, Start radio, Add to playlist, Delete, Select multiple, Send to Mac), Selection mode, Albums + album menu, Artists drill-down, Genres + colours + play, Playlists (CRUD/reorder/remove), Acoustic Network canvas + list panel, tab visibility, library folder scan + progress/summary, empty-library state.
- **Search (2):** Landing library stats card, Landing Most Listened Tracks/Artists/Genres cards.
- **Jarvis (4):** Chat + LLM agent/tools/intents, Voice (push-to-talk STT + TTS toggle), chat memory/clear/lifecycle, web research.
- **Companion & Sync (4):** Android discovery/pairing/diagnosis, receive Mac transfers, DSP offload to Mac + graph rebuild, state bundle upload.
- **Settings (14):** Hub + fuzzy search, Companion & Sync, Storage folders + Android navigator, AI Assistant, Appearance, Audio & DSP, Haptics, Permissions, DB Management (Wipe, Clear Index, Clear DSP, Recompute PCA), Advanced (raw TOML, cache clears, state export/import), About (version, credits, licenses).

---

## Completed module mappings (archived from original §3)

| Flet module | Ported location(s) in `mai_an_flutter/lib/` |
|---|---|
| `flet_audio_service` (Dart + Kotlin) | `player/audio_handler.dart`, `packages/maian_native` |
| `audio_engine.py` (Android), `queue_layout.py`, `main.py` playback | `player/player_controller.dart`, `dsp.dart`, `play_queue.dart`, `queue_entry.dart`, `queue_store.dart` |
| `ui/player/{now_playing,mini_player,queue_sheet,autoplay_sheet}.py` | `player/ui/*` |
| `play_ledger.py` | `data/play_ledger.dart` |
| `db_manager.py` (migrations, read/write for Phases 0–5) | `data/{library_schema,library_repository,browse_queries,playlists,scan_writes,landing_queries,network_queries,assistant_queries,feature_store,graph_build,maintenance}.dart` |
| `track_graph.py`, `artist_graph.py`, `autoplay.py`, `genre_taxonomy.py`, `genre_similarity.py`, `pca_engine.redundant_raw_features` | `stations/{station,artist_graph,genre_taxonomy,credit_keys,acoustic_geometry,genre_npmi}.dart`, `player/autoplay.dart` |
| `library_scanner.py`, `storage_paths.py`, `tinytag` 2.3.2 | `scan/{library_scanner,tinytag,storage_paths,library_folder}.dart` |
| `ui/views/library.py`, `ui/player/dialogs.py` | `library/{library_screen,library_controller,browse_view,browse_controller,network_view,network_layout,network_controller,track_menu,library_dialogs,selection,track_query,track_row,genre_colors}.dart` |
| `sync/{client,discovery,send,manager}.py`, `state_export.py`, `ui/views/sync_hub.py`, `queue_controller.py` (queue), `download_dock.py`, `progress_notify.py` | `sync/*`, `downloads/*`, `data/state_export.dart`, `core/{zip_archive,nfc,progress_notify,notification_router}.dart`, `settings/sync_hub.dart` |
| `llm_engine.py`, `llm_tools.py`, `assistant_runner.py`, `assistant_intent.py`, `chat_memory.py`, `web_research.py`, `ui/views/assistant.py` | `assistant/*`, `core/{py_re,py_re_tables,py_difflib,py_json,py_num,py_text,py_unicode_tables,py_values,nfkd_casefold_table,nfkd_fold_table}.dart` |
| `ui/views/settings.py` (Phases 0–5 groups), `config.py` | `settings/*`, `core/{app_config,config_file,haptics}.dart` |
| `ui/tokens.py`, `ui/widgets.py` (core) | `ui/tokens.dart`, `app_shell.dart` |
