import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/app_shell.dart';
import 'package:mai_an_lab/assistant/adapters.dart';
import 'package:mai_an_lab/assistant/assistant_controller.dart';
import 'package:mai_an_lab/assistant/assistant_screen.dart';
import 'package:mai_an_lab/assistant/chat_memory.dart';
import 'package:mai_an_lab/assistant/llm_engine.dart';
import 'package:mai_an_lab/assistant/runner.dart' hide Row;
import 'package:mai_an_lab/core/config_file.dart';
import 'package:mai_an_lab/core/haptics.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/browse_controller.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/search/landing_controller.dart';
import 'package:mai_an_lab/settings/assistant_settings.dart';
import 'package:mai_an_lab/settings/config_controller.dart';
import 'package:mai_an_lab/settings/settings_page.dart';
import 'package:mai_an_lab/ui/tokens.dart';
import 'package:toml/toml.dart';

void main() {
  late Directory dir;
  late String dbPath;
  final template = File('assets/config_template.toml').readAsStringSync();

  setUp(() async {
    dir = Directory.systemTemp.createTempSync('mai_an_jarvis_ui_');
    dbPath = '${dir.path}/library.db';
  });

  tearDown(() async {
    await Future<void>.delayed(const Duration(milliseconds: 50));
    if (dir.existsSync()) dir.deleteSync(recursive: true);
  });

  Future<LibraryRepository> createDb({bool withTracks = true}) async {
    final repo = await LibraryRepository.open(dbPath, writable: true);
    if (withTracks) {
      await repo.debugExecute([
        (
          "INSERT INTO artists (id, name, album_count, track_count) VALUES "
              "(1, 'Slowdive', 1, 0), (2, 'Ride', 1, 0)",
          const [],
        ),
        (
          "INSERT INTO albums (id, artist_id, title, year, genre, track_count) VALUES "
              "(1, 1, 'Souvlaki', 1993, 'Shoegaze', 0), "
              "(2, 2, 'Nowhere', 1990, 'Shoegaze', 0)",
          const [],
        ),
        (
          "INSERT INTO tracks (id, album_id, title, path, duration, format, track_num, added_date) VALUES "
              "(1, 1, 'Alison', '/m/alison.flac', 231.0, 'FLAC', 1, 1.0), "
              "(2, 2, 'Alison (Cover)', '/m/alison_cover.flac', 200.0, 'FLAC', 2, 2.0), "
              "(3, 2, 'Vapour Trail', '/m/vapour.flac', 258.0, 'FLAC', 1, 3.0)",
          const [],
        ),
      ]);
    }
    return repo;
  }

  group('AssistantController', () {
    test(
      'empty library init greets once and persists chat_history.json',
      () async {
        final repo = await createDb(withTracks: false);
        await repo.close();
        final lib = LibraryController(
          () => LibraryRepository.open(dbPath, writable: true),
        );
        await lib.load();
        addTearDown(lib.dispose);

        final mem = ChatMemory(dir.path, clock: () => 1000.0);
        final c = AssistantController(
          memory: mem,
          library: lib,
          initDelay: Duration.zero,
        );
        addTearDown(c.dispose);

        await c.onShow();
        expect(c.initGreeted, isTrue);
        expect(c.messages.length, 1);
        expect(c.messages.single['text'], contains('Your library looks empty'));

        // Re-opening doesn't duplicate the greeting.
        await c.onShow();
        expect(c.messages.length, 1);

        // Persisted on disk.
        final saved = mem.loadSession();
        expect(saved.initGreeted, isTrue);
        expect(saved.messages.length, 1);
      },
    );

    test(
      'non-empty library init builds edges and writes graph_state.json',
      () async {
        final repo = await createDb(withTracks: true);
        await repo.close();
        final lib = LibraryController(
          () => LibraryRepository.open(dbPath, writable: true),
        );
        await lib.load();
        addTearDown(lib.dispose);

        final mem = ChatMemory(dir.path, clock: () => 1000.0);
        final c = AssistantController(
          memory: mem,
          library: lib,
          initDelay: Duration.zero,
        );
        addTearDown(c.dispose);

        await c.onShow();
        final gs = mem.loadGraphState();
        expect(gs['total_tracks'], 3);
        expect(c.initGreeted, isTrue);
      },
    );

    test(
      'sendUserText persists entities, respects 50-msg cap, and runs deferred play',
      () async {
        final repo = await createDb(withTracks: true);
        addTearDown(repo.close);

        final player = _FakePlayer();
        final runner = AssistantRunner(
          library: RepoAssistantLibrary(repo),
          player: player,
          now: () => DateTime.utc(2026, 11, 24, 15, 0),
        );
        final spoken = <String>[];
        var stopped = 0;
        var clock = 1000.0;
        final mem = ChatMemory(dir.path, clock: () => clock);
        final c = AssistantController(
          memory: mem,
          runner: runner,
          onSpeak: (t) async => spoken.add(t),
          onStopSpeak: () async => stopped++,
          initDelay: Duration.zero,
        );
        addTearDown(c.dispose);

        await c.sendUserText('play Vapour Trail');
        expect(c.messages.length, 2);
        expect(c.messages[0]['sender'], 'user');
        expect(c.messages[1]['sender'], 'assistant');
        expect(c.messages[1]['entities'], isA<Map<String, Object?>>());
        expect(player.playCalls, 1);
        expect(spoken, isNotEmpty);

        // Toggling TTS off calls onStopSpeak and suppresses further speech.
        c.toggleTts();
        expect(c.ttsEnabled, isFalse);
        expect(stopped, 1);
        final spokenBefore = spoken.length;
        await c.sendUserText('what time is it');
        expect(spoken.length, spokenBefore);

        // Background + resume within 15 minutes keeps history.
        c.handleAppBackground();
        clock += 600.0;
        c.handleAppResume();
        expect(c.messages, isNotEmpty);

        // Resume after >15 minutes clears history.
        clock += 1000.0;
        c.handleAppResume();
        expect(c.messages, isEmpty);

        // 50-message cap.
        for (var i = 0; i < 30; i++) {
          await c.sendUserText('help');
        }
        expect(c.messages.length, 50);
      },
    );
  });

  group('AssistantScreen and AppShell Jarvis tab', () {
    testWidgets(
      'empty state prompt chip, disambiguation options, and clear chat',
      (tester) async {
        tester.view.physicalSize = const Size(390 * 3, 844 * 3);
        tester.view.devicePixelRatio = 3;
        addTearDown(tester.view.reset);

        final repo = await tester.runAsync(() => createDb(withTracks: true));
        addTearDown(() => tester.runAsync(() => repo!.close()));

        final player = _FakePlayer();
        final runner = AssistantRunner(
          library: RepoAssistantLibrary(repo!),
          player: player,
          now: () => DateTime.utc(2026, 11, 24, 15, 0),
        );
        final mem = ChatMemory(dir.path, clock: () => 1000.0);
        final ctrl = AssistantController(
          memory: mem,
          runner: runner,
          initDelay: Duration.zero,
        );
        addTearDown(ctrl.dispose);

        await tester.pumpWidget(
          MaterialApp(
            theme: Tokens.theme(),
            home: Scaffold(body: AssistantScreen(controller: ctrl)),
          ),
        );

        // Empty state with 4 prompt chips.
        expect(find.text('JARVIS'), findsNWidgets(2));
        expect(find.text('[?] What can you do?'), findsOneWidget);

        // Tap a prompt chip -> dispatches "help" and renders Markdown reply.
        await tester.tap(find.text('[?] What can you do?'));
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 50)),
        );
        await tester.pumpAndSettle();
        expect(ctrl.messages.first['text'], 'help');
        expect(find.textContaining('Smart Curation'), findsOneWidget);

        // Ask to play ambiguous "Alis" -> renders disambiguation buttons.
        await tester.enterText(find.byType(TextField), 'play Alis');
        await tester.tap(find.byTooltip('Send'));
        for (var i = 0; i < 40; i++) {
          await tester.runAsync(
            () => Future<void>.delayed(const Duration(milliseconds: 10)),
          );
          await tester.pump(const Duration(milliseconds: 20));
          if (!ctrl.thinking && ctrl.messages.length >= 4) break;
        }
        await tester.pumpAndSettle();
        expect(
          ctrl.messages.last['text'],
          contains('Multiple matches'),
          reason: 'messages: ${ctrl.messages}',
        );
        expect(find.byType(OutlinedButton), findsNWidgets(2));
        expect(find.textContaining('Alison — Slowdive'), findsOneWidget);
        expect(find.textContaining('Alison (Cover) — Ride'), findsOneWidget);

        // Tapping the first option resolves the choice and plays.
        await tester.tap(find.byType(OutlinedButton).first);
        for (var i = 0; i < 40; i++) {
          await tester.runAsync(
            () => Future<void>.delayed(const Duration(milliseconds: 10)),
          );
          await tester.pump(const Duration(milliseconds: 20));
          if (!ctrl.thinking && ctrl.messages.length >= 6) break;
        }
        await tester.pumpAndSettle();
        expect(player.playCalls, 1);

        // Clear chat returns to empty state.
        await tester.tap(find.byTooltip('Clear conversation history'));
        await tester.pumpAndSettle();
        expect(find.text('[?] What can you do?'), findsOneWidget);
      },
    );

    testWidgets(
      'AppShell shows Jarvis tab when enabled and falls back when disabled',
      (tester) async {
        tester.view.physicalSize = const Size(390 * 3, 844 * 3);
        tester.view.devicePixelRatio = 3;
        addTearDown(tester.view.reset);

        await tester.runAsync(() async {
          final r = await createDb(withTracks: true);
          await r.close();
        });
        final lib = LibraryController(() => LibraryRepository.open(dbPath));
        await tester.runAsync(() => lib.load());
        final browse = BrowseController(lib, views: LibraryView.values);
        final landing = LandingController(lib);
        final player = ValueNotifier<PlayerController?>(null);
        final assistant = AssistantController(
          memory: ChatMemory(dir.path),
          initDelay: Duration.zero,
        );
        addTearDown(() {
          assistant.dispose();
          player.dispose();
          landing.dispose();
          browse.dispose();
          lib.dispose();
        });

        Widget buildShell(bool showJarvis) => MaterialApp(
          theme: Tokens.theme(),
          home: AppShell(
            library: lib,
            browse: browse,
            landing: landing,
            assistant: assistant,
            showJarvis: showJarvis,
            startTab: AppTab.jarvis,
            player: player,
            haptics: Haptics(const HapticsConfig()),
          ),
        );

        await tester.pumpWidget(buildShell(true));
        await tester.pumpAndSettle();
        expect(find.text('Jarvis'), findsOneWidget);
        expect(find.text('JARVIS'), findsNWidgets(2));

        // Disable Jarvis -> tab vanishes and shell falls back to Library.
        await tester.pumpWidget(buildShell(false));
        await tester.pumpAndSettle();
        expect(find.text('Jarvis'), findsNothing);
      },
    );
  });

  group('Settings → AI Assistant & Appearance Jarvis switch', () {
    late ConfigController config;
    late LibraryController library;

    Future<void> initDeps(WidgetTester tester) async {
      await tester.runAsync(() async {
        final r = await createDb(withTracks: true);
        await r.close();
        config = await ConfigController.load(
          ConfigStore(dir.path, template: () async => template),
        );
        library = LibraryController(
          () => LibraryRepository.open(dbPath, writable: true),
        );
        await library.load();
      });
      addTearDown(() async {
        config.dispose();
        library.dispose();
      });
    }

    Map<String, Object?> readToml() => TomlDocument.parse(
      File(ConfigStore(dir.path).path).readAsStringSync(),
    ).toMap();

    testWidgets(
      'hub opens AI Assistant, presets save to config.toml, models & Test Connection work',
      (tester) async {
        tester.view.physicalSize = const Size(390 * 3, 1400 * 3);
        tester.view.devicePixelRatio = 3;
        addTearDown(tester.view.reset);

        await initDeps(tester);
        final deps = SettingsDeps(
          dataDir: dir.path,
          config: config,
          library: library,
          player: ValueNotifier<PlayerController?>(null),
          haptics: Haptics(const HapticsConfig()),
        );

        var listCalls = 0;
        await tester.pumpWidget(
          MaterialApp(
            theme: Tokens.theme(),
            home: Scaffold(
              body: AssistantSettingsGroup(
                deps: deps,
                listModels: (key) async {
                  listCalls++;
                  return const [
                    (id: 'gemini-3.5-pro', name: 'Gemini 3.5 Pro'),
                    (id: 'gemini-2.5-flash', name: 'Gemini 2.5 Flash'),
                  ];
                },
                engineFactory:
                    ({
                      required provider,
                      required apiKey,
                      required model,
                      required ollamaEndpoint,
                      required ollamaModel,
                    }) => _OkLlmEngine(provider: provider, apiKey: apiKey),
              ),
            ),
          ),
        );

        // Enter API key + pick Casual Mate preset -> floating save button appears.
        await tester.enterText(
          find.widgetWithText(TextField, 'Google Gemini API Key'),
          'AIzaTestKey',
        );
        await tester.tap(find.text('Casual Mate'));
        await tester.pumpAndSettle();
        expect(find.text('SAVE ASSISTANT SETTINGS'), findsOneWidget);

        // Test Connection -> succeeds and refreshes Gemini models.
        await tester.tap(find.text('Test Connection'));
        await tester.pumpAndSettle();
        expect(listCalls, 1);
        expect(find.text('✓ gemini connection OK.'), findsOneWidget);

        // Dismiss SnackBar so it doesn't obscure the floating save button.
        ScaffoldMessenger.of(
          tester.element(find.byType(AssistantSettingsGroup)),
        ).hideCurrentSnackBar();
        await tester.pumpAndSettle();

        // Save writes [assistant] into config.toml.
        await tester.tap(find.text('SAVE ASSISTANT SETTINGS'));
        for (var i = 0; i < 40; i++) {
          await tester.runAsync(
            () => Future<void>.delayed(const Duration(milliseconds: 10)),
          );
          await tester.pump(const Duration(milliseconds: 20));
          if (config.config.assistant.geminiApiKey == 'AIzaTestKey') break;
        }
        final saved = readToml()['assistant']! as Map<String, Object?>;
        expect(saved['gemini_api_key'], 'AIzaTestKey');
        expect(saved['llm_provider'], 'gemini');
        expect(saved['personality_prompt'], contains('mate'));
        expect(saved['research_mode'], 'hybrid');
      },
    );

    testWidgets('Appearance Enable Jarvis Tab writes appearance.show_jarvis', (
      tester,
    ) async {
      tester.view.physicalSize = const Size(390 * 3, 1800 * 3);
      tester.view.devicePixelRatio = 3;
      addTearDown(tester.view.reset);

      await initDeps(tester);
      final deps = SettingsDeps(
        dataDir: dir.path,
        config: config,
        library: library,
        player: ValueNotifier<PlayerController?>(null),
        haptics: Haptics(const HapticsConfig()),
      );

      await tester.pumpWidget(
        MaterialApp(
          theme: Tokens.theme(),
          home: SettingsScreen(deps: deps, initial: SettingsPage.appearance),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.text('Enable Jarvis Tab'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('SAVE APPEARANCE SETTINGS'));
      for (var i = 0; i < 40; i++) {
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 10)),
        );
        await tester.pump(const Duration(milliseconds: 20));
        if (!config.config.showJarvis) break;
      }

      final app = readToml()['appearance']! as Map<String, Object?>;
      expect(app['show_jarvis'], isFalse);
      expect(config.config.showJarvis, isFalse);
    });
  });
}

class _OkLlmEngine extends LlmEngine {
  _OkLlmEngine({required super.provider, required super.apiKey});
  @override
  Future<LlmResponse> chatCompletion(
    List<Map<String, Object?>> messages, {
    List<Map<String, Object?>>? tools,
  }) async => LlmResponse(content: 'OK');
}

class _FakePlayer implements AssistantPlayer {
  final List<Map<String, Object?>> _queue = [];
  int _idx = 0;
  bool _playing = false;
  int playCalls = 0;

  @override
  List<Map<String, Object?>> get queue => _queue;
  @override
  int get currentIndex => _idx;
  @override
  String get currentPath =>
      _idx < _queue.length ? '${_queue[_idx]['path'] ?? ''}' : '';
  @override
  String get currentTrack =>
      _idx < _queue.length ? '${_queue[_idx]['track_title'] ?? ''}' : '';
  @override
  String get currentArtist =>
      _idx < _queue.length ? '${_queue[_idx]['artist_name'] ?? ''}' : '';
  @override
  String get currentAlbum =>
      _idx < _queue.length ? '${_queue[_idx]['album_title'] ?? ''}' : '';
  @override
  double get duration => 0;
  @override
  bool get isPlaying => _playing;
  @override
  bool isShuffle = false;

  @override
  void setQueue(List<Map<String, Object?>> tracks, {int startIndex = 0}) {
    _queue
      ..clear()
      ..addAll(tracks);
    _idx = startIndex;
  }

  @override
  void queueLast(Map<String, Object?> track) => _queue.add(track);
  @override
  void queueNext(Map<String, Object?> track) => _queue.insert(_idx + 1, track);
  @override
  void queueLastMany(List<Map<String, Object?>> tracks) =>
      _queue.addAll(tracks);
  @override
  void removeFromQueue(int idx) => _queue.removeAt(idx);
  @override
  void moveQueueItem(int oldIdx, int newIdx) {
    final x = _queue.removeAt(oldIdx);
    _queue.insert(newIdx, x);
  }

  @override
  void next() {}
  @override
  void previous() {}
  @override
  void pause() => _playing = false;
  @override
  void play() {
    _playing = true;
    playCalls++;
  }

  @override
  void stop() => _playing = false;
  @override
  void clearQueue() => _queue.clear();
}
