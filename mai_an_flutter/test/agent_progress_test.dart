import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/assistant/adapters.dart';
import 'package:mai_an_lab/assistant/agent_progress.dart';
import 'package:mai_an_lab/assistant/assistant_controller.dart';
import 'package:mai_an_lab/assistant/assistant_screen.dart';
import 'package:mai_an_lab/assistant/chat_memory.dart';
import 'package:mai_an_lab/assistant/runner.dart' hide Row;
import 'package:mai_an_lab/core/app_config.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/ui/tokens.dart';

/// Phase 1 of live agent progress: tool events from the runner, the
/// controller's LiveTurn and saved trail, and the chat's live card / chip.
void main() {
  group('toolLabel / toolSummary', () {
    test('labels quote arguments and drop dangling prepositions', () {
      expect(
        toolLabel('search_library', {'query': 'Slowdive'}),
        'Searching your library for “Slowdive”',
      );
      expect(toolLabel('search_library', {}), 'Searching your library');
      expect(toolLabel('web_search', {'query': ''}), 'Searching the web');
      expect(toolLabel('steer_mood', {}), 'Steering the mood');
      expect(
        toolLabel('find_related_artists', {'artist': 'Ride'}),
        'Finding artists like “Ride”',
      );
      expect(toolLabel('some_new_tool', {}), 'some new tool');
      final long = toolLabel('web_search', {'query': 'x' * 60});
      expect(long, endsWith('…”'));
    });

    test('summaries report errors and counts', () {
      expect(toolSummary({'success': true, 'count': 3}), '3 results');
      expect(toolSummary({'success': true, 'count': 1}), '1 result');
      expect(toolSummary({'success': true}), '');
      expect(toolSummary({'success': false, 'error': 'Offline'}), 'Offline');
      expect(toolSummary({'success': false}), 'Failed');
    });

    test('LiveTurn tracks running, done and failed steps', () {
      final t = LiveTurn();
      expect(t.awaitingModel, isTrue);
      t.apply(const AgentStepStarted(0));
      t.apply(const AgentToolStarted('a', 'web_search', {'query': 'q'}));
      t.apply(const AgentToolStarted('b', 'list_artists', {}));
      expect(t.awaitingModel, isFalse);
      t.apply(const AgentToolFinished('a', 'web_search', ok: true));
      t.apply(
        const AgentToolFinished('b', 'list_artists', ok: false, summary: 'x'),
      );
      expect(t.awaitingModel, isTrue);
      expect(t.modelSteps, 1);
      expect(
        [for (final s in t.steps) s.state],
        [ToolStepState.done, ToolStepState.failed],
      );
      expect(t.steps[1].toJson(), {
        'name': 'list_artists',
        'label': 'Listing artists',
        'ok': false,
        'summary': 'x',
      });
    });
  });

  test('LiveTurn drafts streamed text and keeps a tool preamble', () {
    final t = LiveTurn()
      ..apply(const AgentStepStarted(0))
      ..apply(const AgentReplyDelta('Let me '))
      ..apply(const AgentReplyDelta('check. '));
    expect(t.draft, 'Let me check. ');
    expect(t.awaitingModel, isFalse);
    t.apply(const AgentToolStarted('a', 'list_artists', {}));
    expect(t.draft, '');
    expect(t.interim, 'Let me check.');
    t
      ..apply(const AgentToolFinished('a', 'list_artists', ok: true))
      ..apply(const AgentStepStarted(1));
    expect(t.awaitingModel, isTrue);
    t.apply(const AgentReplyDelta('You have 3.'));
    expect(t.draft, 'You have 3.');
    expect(t.awaitingModel, isFalse);
  });

  group('agent turn over a local server', () {
    late Directory dir;
    late HttpServer server;
    late LibraryRepository repo;
    late AssistantRunner runner;
    final replies = <Map<String, Object?>>[];
    final bodies = <Map<String, Object?>>[];

    /// When set, a streamed request gets real SSE, and the reply text is
    /// held after its first word until the gate opens.
    var sse = false;
    Completer<void>? gate;

    HttpOverrides? savedOverrides;

    setUp(() async {
      // testWidgets' binding stubs HttpClient; this group talks to a real
      // loopback server.
      savedOverrides = HttpOverrides.current;
      HttpOverrides.global = null;
      dir = Directory.systemTemp.createTempSync('mai_an_progress_');
      repo = await LibraryRepository.open(
        '${dir.path}/library.db',
        writable: true,
      );
      await repo.debugExecute([
        (
          "INSERT INTO artists (id, name, album_count, track_count) VALUES "
              "(1, 'Slowdive', 1, 0)",
          const [],
        ),
        (
          "INSERT INTO albums (id, artist_id, title, year, genre, track_count) "
              "VALUES (1, 1, 'Souvlaki', 1993, 'Shoegaze', 0)",
          const [],
        ),
        (
          "INSERT INTO tracks (id, album_id, title, path, duration, format, "
              "track_num, added_date) VALUES "
              "(1, 1, 'Alison', '/m/alison.flac', 231.0, 'FLAC', 1, 1.0)",
          const [],
        ),
      ]);

      replies.clear();
      server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      bodies.clear();
      sse = false;
      gate = null;
      server.listen((req) async {
        final body =
            jsonDecode(await utf8.decodeStream(req)) as Map<String, Object?>;
        bodies.add(body);
        final message = replies.removeAt(0);
        if (sse && body['stream'] == true) {
          req.response
            ..headers.set('content-type', 'text/event-stream')
            ..bufferOutput = false;
          void send(Map<String, Object?> delta) => req.response.write(
            'data: ${jsonEncode({
              'choices': [
                {'index': 0, 'delta': delta},
              ],
            })}\n\n',
          );
          final text = message['content'];
          if (text is String) {
            final cut = text.indexOf(' ') + 1;
            send({'role': 'assistant', 'content': text.substring(0, cut)});
            await req.response.flush();
            await gate?.future;
            send({'content': text.substring(cut)});
          } else {
            send({
              'role': 'assistant',
              'tool_calls': [
                for (final (i, tc) in (message['tool_calls']! as List).indexed)
                  {'index': i, ...(tc as Map<String, Object?>)},
              ],
            });
          }
          req.response.write('data: [DONE]\n\n');
          await req.response.close();
          return;
        }
        req.response.headers.contentType = ContentType.json;
        req.response.write(
          jsonEncode({
            'choices': [
              {'message': message},
            ],
          }),
        );
        await req.response.close();
      });

      final cfg = AssistantConfig(
        llmProvider: 'ollama',
        ollamaEndpoint: 'http://127.0.0.1:${server.port}/v1',
      );
      runner = AssistantRunner(
        library: RepoAssistantLibrary(repo),
        player: _IdlePlayer(),
        now: () => DateTime.utc(2026, 11, 26, 12),
        loadAssistantConfig: () => cfg,
      );
    });

    tearDown(() async {
      HttpOverrides.global = savedOverrides;
      await server.close(force: true);
      await repo.close();
      dir.deleteSync(recursive: true);
    });

    void scriptToolThenAnswer() {
      replies.addAll([
        {
          'role': 'assistant',
          'content': null,
          'tool_calls': [
            {
              'id': 'call_1',
              'type': 'function',
              'function': {
                'name': 'search_library',
                'arguments': '{"query": "Alison"}',
              },
            },
            {
              'id': 'call_2',
              'type': 'function',
              'function': {
                'name': 'get_playlist_tracks',
                'arguments': '{"name": "Nope"}',
              },
            },
          ],
        },
        {'role': 'assistant', 'content': 'You have **Alison** by Slowdive.'},
      ]);
    }

    test('runner emits step and tool events in order', () async {
      scriptToolThenAnswer();
      final events = <AgentEvent>[];
      final resp = await runner.dispatchText(
        'do I have anything dreamy from the nineties',
        historyProvider: () => const [],
        onEvent: events.add,
      );
      expect(resp.displayed, 'You have **Alison** by Slowdive.');
      final shape = [
        for (final e in events)
          switch (e) {
            AgentStepStarted(:final step) => 'step$step',
            AgentToolStarted(:final name) => 'start:$name',
            AgentToolFinished(:final name, :final ok) => 'end:$name:$ok',
            AgentReplyDelta(:final text) => 'text:$text',
          },
      ];
      expect(shape, [
        'step0',
        'start:search_library',
        'end:search_library:true',
        'start:get_playlist_tracks',
        'end:get_playlist_tracks:false',
        'step1',
        // This server ignores `stream`; its whole JSON reply is one delta.
        'text:You have **Alison** by Slowdive.',
      ]);
      final first = events[2] as AgentToolFinished;
      expect(first.summary, '1 result');
    });

    test(
      'a watched turn streams; an unwatched one sends Python\'s request',
      () async {
        sse = true;
        scriptToolThenAnswer();
        final deltas = <String>[];
        final resp = await runner.dispatchText(
          'do I have anything dreamy from the nineties',
          historyProvider: () => const [],
          onEvent: (e) {
            if (e is AgentReplyDelta) deltas.add(e.text);
          },
        );
        expect(resp.displayed, 'You have **Alison** by Slowdive.');
        expect(deltas, ['You ', 'have **Alison** by Slowdive.']);
        expect([for (final b in bodies) b['stream']], [true, true]);

        bodies.clear();
        scriptToolThenAnswer();
        await runner.dispatchText(
          'do I have anything dreamy from the nineties',
          historyProvider: () => const [],
        );
        expect(
          [for (final b in bodies) b.containsKey('stream')],
          [false, false],
        );
      },
    );

    test('a throwing listener does not break the turn', () async {
      scriptToolThenAnswer();
      final resp = await runner.dispatchText(
        'do I have anything dreamy from the nineties',
        historyProvider: () => const [],
        onEvent: (_) => throw StateError('boom'),
      );
      expect(resp.displayed, contains('Alison'));
    });

    testWidgets('controller saves the trail and the chat renders the chip', (
      tester,
    ) async {
      tester.view.physicalSize = const Size(390 * 3, 844 * 3);
      tester.view.devicePixelRatio = 3;
      addTearDown(tester.view.reset);

      scriptToolThenAnswer();
      final ctrl = AssistantController(
        memory: ChatMemory(dir.path, clock: () => 1000.0),
        runner: runner,
        onSpeak: (_) async {},
        initDelay: Duration.zero,
      );
      addTearDown(ctrl.dispose);
      var sawLive = false;
      ctrl.addListener(() {
        if ((ctrl.liveTurn?.steps.length ?? 0) > 0) sawLive = true;
      });

      await tester.pumpWidget(
        MaterialApp(
          theme: Tokens.theme(),
          home: Scaffold(body: AssistantScreen(controller: ctrl)),
        ),
      );
      await tester.runAsync(
        () => ctrl.sendUserText('do I have anything dreamy from the nineties'),
      );
      await tester.pumpAndSettle();

      expect(sawLive, isTrue);
      expect(ctrl.liveTurn, isNull);
      final tools = ctrl.messages.last['tools']! as List<Object?>;
      expect(tools, hasLength(2));
      expect(
        (tools.first! as Map)['label'],
        'Searching your library for “Alison”',
      );

      // Persisted with the session.
      final reloaded = ChatMemory(dir.path, clock: () => 1000.0).loadSession();
      expect(reloaded.messages.last['tools'], tools);

      expect(find.text('Used 2 tools · 1 failed'), findsOneWidget);
      expect(find.textContaining('Searching your library'), findsNothing);
      await tester.tap(find.text('Used 2 tools · 1 failed'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Searching your library'), findsOneWidget);
    });

    testWidgets('streamed text shows as a draft, then one final bubble', (
      tester,
    ) async {
      tester.view.physicalSize = const Size(390 * 3, 844 * 3);
      tester.view.devicePixelRatio = 3;
      addTearDown(tester.view.reset);

      sse = true;
      gate = Completer<void>();
      scriptToolThenAnswer();
      final ctrl = AssistantController(
        memory: ChatMemory(dir.path, clock: () => 1000.0),
        runner: runner,
        onSpeak: (_) async {},
        initDelay: Duration.zero,
      );
      addTearDown(ctrl.dispose);
      await tester.pumpWidget(
        MaterialApp(
          theme: Tokens.theme(),
          home: Scaffold(body: AssistantScreen(controller: ctrl)),
        ),
      );

      late Future<void> turn;
      await tester.runAsync(() async {
        turn = ctrl.sendUserText('do I have anything dreamy from the nineties');
      });
      for (var i = 0; i < 100 && (ctrl.liveTurn?.draft ?? '') != 'You '; i++) {
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 10)),
        );
        await tester.pump();
      }
      expect(ctrl.liveTurn?.draft, 'You ');
      await tester.pump();
      // The checklist stays above the draft; no Copy row while it streams.
      expect(find.textContaining('Searching your library'), findsOneWidget);
      expect(find.textContaining('You'), findsOneWidget);
      expect(find.text('Copy'), findsNothing);

      gate!.complete();
      await tester.runAsync(() => turn);
      await tester.pumpAndSettle();
      expect(ctrl.liveTurn, isNull);
      expect(ctrl.messages, hasLength(2));
      expect(find.textContaining('by Slowdive'), findsOneWidget);
      expect(find.textContaining('Searching your library'), findsNothing);
      expect(find.text('Copy'), findsOneWidget);
    });
  });
}

class _IdlePlayer implements AssistantPlayer {
  @override
  final List<Map<String, Object?>> queue = [];
  @override
  int get currentIndex => 0;
  @override
  String get currentPath => '';
  @override
  String get currentTrack => '';
  @override
  String get currentArtist => '';
  @override
  String get currentAlbum => '';
  @override
  double get duration => 0;
  @override
  bool get isPlaying => false;
  @override
  bool isShuffle = false;
  @override
  void setQueue(List<Map<String, Object?>> tracks, {int startIndex = 0}) {}
  @override
  void queueLast(Map<String, Object?> track) {}
  @override
  void queueNext(Map<String, Object?> track) {}
  @override
  void queueLastMany(List<Map<String, Object?>> tracks) {}
  @override
  void removeFromQueue(int index) {}
  @override
  void moveQueueItem(int oldIndex, int newIndex) {}
  @override
  void next() {}
  @override
  void previous() {}
  @override
  void pause() {}
  @override
  void play() {}
  @override
  void stop() {}
  @override
  void clearQueue() {}
}
