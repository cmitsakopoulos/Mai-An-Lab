import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/assistant/adapters.dart';
import 'package:mai_an_lab/assistant/llm_engine.dart';
import 'package:mai_an_lab/assistant/runner.dart';
import 'package:mai_an_lab/assistant/tool_executor.dart';
import 'package:mai_an_lab/core/app_config.dart';
import 'package:mai_an_lab/core/py_json.dart';
import 'package:mai_an_lab/data/library_repository.dart';

/// Phase 5 stage d against Python's own `AssistantRunner._dispatch_llm` and
/// `llm_tools.execute_tool` over a real synthetic SQLite database and a local
/// HTTP recording server (`tool/export_agent_goldens.py`).
void main() {
  final golden =
      jsonDecode(File('test/goldens/assistant_agent.json').readAsStringSync())
          as Map<String, Object?>;
  final fixture = golden['fixture']! as Map<String, Object?>;

  Future<LibraryRepository> buildFixtureRepo(Directory dir) async {
    final path = '${dir.path}/library.db';
    var repo = await LibraryRepository.open(path, writable: true);
    await repo.close();
    // Second open adds play_counts.cluster_id (as in Python).
    repo = await LibraryRepository.open(path, writable: true);
    final stmts = <(String, List<Object?>)>[];
    for (final a
        in (fixture['artists']! as List<Object?>).cast<List<Object?>>()) {
      stmts.add((
        'INSERT INTO artists (id, name, album_count, track_count) '
            'VALUES (?, ?, ?, ?)',
        a,
      ));
    }
    for (final al
        in (fixture['albums']! as List<Object?>).cast<List<Object?>>()) {
      stmts.add((
        'INSERT INTO albums (id, artist_id, title, year, genre, track_count) '
            'VALUES (?, ?, ?, ?, ?, ?)',
        al,
      ));
    }
    for (final t
        in (fixture['tracks']! as List<Object?>).cast<List<Object?>>()) {
      stmts.add((
        'INSERT INTO tracks (id, album_id, title, path, duration, format, '
            'track_num, added_date, bpm, energy, brightness) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        t,
      ));
    }
    for (final pc
        in (fixture['play_counts']! as List<Object?>).cast<List<Object?>>()) {
      final rawTimbre = pc[4] as List<Object?>?;
      final timbre = rawTimbre == null
          ? null
          : Uint8List.fromList(rawTimbre.cast<int>());
      final rawCoords = pc[5] as List<Object?>?;
      Uint8List? coords;
      if (rawCoords != null) {
        final f = Float32List.fromList([
          for (final v in rawCoords) (v! as num).toDouble(),
        ]);
        coords = f.buffer.asUint8List();
      }
      stmts.add((
        'INSERT INTO play_counts (track_path, count, last_played, '
            'features_version, timbre, pca_coords) VALUES (?, ?, ?, ?, ?, ?)',
        [pc[0], pc[1], pc[2], pc[3], timbre, coords],
      ));
    }
    for (final p
        in (fixture['playlists']! as List<Object?>).cast<List<Object?>>()) {
      stmts.add((
        'INSERT INTO playlists (id, name, created, color) VALUES (?, ?, ?, ?)',
        p,
      ));
    }
    for (final pt
        in (fixture['playlist_tracks']! as List<Object?>)
            .cast<List<Object?>>()) {
      stmts.add((
        'INSERT INTO playlist_tracks (playlist_id, track_path, order_index) '
            'VALUES (?, ?, ?)',
        pt,
      ));
    }
    for (final h
        in (fixture['history']! as List<Object?>).cast<List<Object?>>()) {
      stmts.add((
        'INSERT INTO playback_history (track_path, played_at, event, '
            'seed_path) VALUES (?, ?, ?, ?)',
        h,
      ));
    }
    for (final e
        in (fixture['enrichment']! as List<Object?>).cast<List<Object?>>()) {
      stmts.add((
        'INSERT INTO artist_enrichment (artist_name, country, genres, source, '
            'provenance) VALUES (?, ?, ?, ?, ?)',
        e,
      ));
    }
    for (final r
        in (fixture['ai_research']! as List<Object?>).cast<List<Object?>>()) {
      stmts.add((
        'INSERT INTO artist_ai_research (artist_name, payload, state, '
            'created_at) VALUES (?, ?, ?, ?)',
        r,
      ));
    }
    await repo.debugExecute(stmts);
    return repo;
  }

  group('executeTool parity against Python llm_tools.execute_tool', () {
    final cases = (golden['tool_cases']! as List<Object?>)
        .cast<Map<String, Object?>>();

    for (final c in cases) {
      test(c['name']! as String, () async {
        final dir = Directory.systemTemp.createTempSync('mai_an_tool_');
        final repo = await buildFixtureRepo(dir);
        addTearDown(() async {
          await repo.close();
          dir.deleteSync(recursive: true);
        });
        final eng = _FakeEngine(c['engine']! as Map<String, Object?>);
        final runner = AssistantRunner(
          library: RepoAssistantLibrary(repo),
          player: eng,
          random: _ScriptedRandom(42),
          now: () => DateTime.utc(2026, 11, 24, 14, 30, 0),
        );
        runner.agentReset();
        final res = await executeTool(
          c['tool']! as String,
          Map<String, Object?>.of(c['args']! as Map<String, Object?>),
          runner,
        );
        final interrupt = runner.agentInterrupt;
        final got = {
          'result': res,
          'deferred_play': runner.agentDeferredPlay,
          'interrupt': interrupt == null
              ? null
              : {
                  'spoken': interrupt.spoken,
                  'displayed': interrupt.displayed,
                  'success': interrupt.success,
                  'extras': interrupt.extras,
                },
          'pending': _serializePending(runner.pending),
          'choice': _serializeChoice(runner.pendingChoice),
          'queue': [for (final t in eng.queue) t['path']],
          'engine_calls': List<Object?>.of(eng.calls),
        };
        final want = {
          'result': c['result'],
          'deferred_play': c['deferred_play'],
          'interrupt': c['interrupt'],
          'pending': c['pending'],
          'choice': c['choice'],
          'queue': c['queue'],
          'engine_calls': c['engine_calls'],
        };
        expect(
          pyJsonDumps(_sortKeys(got), ensureAscii: false),
          pyJsonDumps(_sortKeys(want), ensureAscii: false),
          reason: c['name']! as String,
        );
      });
    }
  });

  group('dispatchLlm loop parity against Python over local HTTP server', () {
    late HttpServer server;
    late String base;
    final replies = <Map<String, Object?>>[];
    final requests = <Map<String, Object?>>[];

    setUp(() async {
      replies.clear();
      requests.clear();
      server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      base = 'http://127.0.0.1:${server.port}';
      server.listen((req) async {
        final body = await utf8.decodeStream(req);
        requests.add({
          'method': req.method,
          'path': req.uri.toString(),
          'headers': {
            for (final k in ['authorization', 'content-type', 'x-goog-api-key'])
              if (req.headers.value(k) != null) k: req.headers.value(k),
          },
          'body': body,
        });
        final r = replies.removeAt(0);
        req.response
          ..statusCode = r['status']! as int
          ..headers.set('content-type', r['content_type']! as String)
          ..add(utf8.encode(r['body']! as String));
        await req.response.close();
      });
    });

    tearDown(() => server.close(force: true));

    final scenarios = (golden['loop_scenarios']! as List<Object?>)
        .cast<Map<String, Object?>>();

    for (final sc in scenarios) {
      test(sc['name']! as String, () async {
        final dir = Directory.systemTemp.createTempSync('mai_an_loop_');
        final repo = await buildFixtureRepo(dir);
        addTearDown(() async {
          await repo.close();
          dir.deleteSync(recursive: true);
        });
        final eng = _FakeEngine(sc['engine']! as Map<String, Object?>);
        final rawCfg = sc['config']! as Map<String, Object?>;
        final cfg = AssistantConfig(
          llmEnabled: rawCfg['llm_enabled'] as bool? ?? true,
          llmProvider: rawCfg['llm_provider'] as String? ?? 'gemini',
          geminiApiKey: rawCfg['gemini_api_key'] as String? ?? '',
          geminiModel: rawCfg['gemini_model'] as String? ?? defaultGeminiModel,
          ollamaEndpoint: rawCfg.containsKey('ollama_endpoint')
              ? '$base${rawCfg['ollama_endpoint']}'
              : defaultOllamaEndpoint,
          ollamaModel: rawCfg['ollama_model'] as String? ?? defaultOllamaModel,
          personalityPrompt: rawCfg['personality_prompt'] as String? ?? '',
        );

        final runner = AssistantRunner(
          library: RepoAssistantLibrary(repo),
          player: eng,
          random: _ScriptedRandom(sc['seed']! as int),
          now: () => DateTime.utc(2026, 11, 24, 14, 30, 0),
          loadAssistantConfig: () => cfg,
          engineFactory: (c) => LlmEngine(
            provider: c?.llmProvider ?? 'gemini',
            apiKey: c?.geminiApiKey ?? '',
            model: (c != null && c.geminiModel.isNotEmpty)
                ? c.geminiModel
                : defaultGeminiModel,
            ollamaEndpoint: c?.ollamaEndpoint ?? defaultOllamaEndpoint,
            ollamaModel: c?.ollamaModel ?? defaultOllamaModel,
            geminiUrl: '$base/v1beta/openai/chat/completions',
          ),
        );

        final turns = (sc['turns']! as List<Object?>)
            .cast<Map<String, Object?>>();
        final expected = (sc['expected']! as List<Object?>)
            .cast<Map<String, Object?>>();
        expect(turns.length, expected.length);

        for (var i = 0; i < turns.length; i++) {
          final turn = turns[i];
          final want = expected[i];
          replies
            ..clear()
            ..addAll(
              (turn['replies']! as List<Object?>).cast<Map<String, Object?>>(),
            );
          requests.clear();
          eng.calls.clear();
          final hist = (turn['history'] as List<Object?>?) ?? const [];

          final resp = await runner.dispatchText(
            turn['text']! as String,
            historyProvider: () => hist,
          );

          // Every HTTP request (including body!) must match Python byte for byte.
          final wantReqs = (want['requests']! as List<Object?>)
              .cast<Map<String, Object?>>();
          expect(
            requests.length,
            wantReqs.length,
            reason: '${sc['name']} turn $i request count',
          );
          for (var j = 0; j < wantReqs.length; j++) {
            expect(
              requests[j]['method'],
              wantReqs[j]['method'],
              reason: '${sc['name']} turn $i req $j method',
            );
            expect(
              requests[j]['path'],
              wantReqs[j]['path'],
              reason: '${sc['name']} turn $i req $j path',
            );
            expect(
              requests[j]['headers'],
              wantReqs[j]['headers'],
              reason: '${sc['name']} turn $i req $j headers',
            );
            expect(
              requests[j]['body'],
              wantReqs[j]['body'],
              reason: '${sc['name']} turn $i req $j body',
            );
          }

          final gotResp = {
            'spoken': resp.spoken,
            'displayed': resp.displayed,
            'success': resp.success,
            'extras': resp.extras,
            'action': resp.action,
            'deferred_play': resp.deferredPlay,
            'pending': _serializePending(runner.pending),
            'choice': _serializeChoice(runner.pendingChoice),
            'queue': [for (final t in eng.queue) t['path']],
            'current_index': eng.currentIndex,
            'is_playing': eng.isPlaying,
            'is_shuffle': eng.isShuffle,
            'engine_calls': List<Object?>.of(eng.calls),
          };
          expect(
            pyJsonDumps(_sortKeys(gotResp), ensureAscii: false),
            pyJsonDumps(_sortKeys(want['response']), ensureAscii: false),
            reason: '${sc['name']} turn $i response',
          );
        }
      });
    }
  });

  group('Deferred tools, hooks, pyJsonDumps defaultEncoder, AssistantConfig', () {
    test(
      'deferred tools return readable errors and call hooks when wired',
      () async {
        final dir = Directory.systemTemp.createTempSync('mai_an_defer_');
        final repo = await buildFixtureRepo(dir);
        addTearDown(() async {
          await repo.close();
          dir.deleteSync(recursive: true);
        });
        final lib = RepoAssistantLibrary(repo);
        final eng = _FakeEngine(const {});

        final unhooked = AssistantRunner(library: lib, player: eng);
        for (final name in [
          'search_online',
          'web_search',
          'learn_about',
          'find_related_artists',
          'research_artist_metadata',
          'apply_artist_metadata',
        ]) {
          final r = await executeTool(name, const {'query': 'test'}, unhooked);
          expect(r['success'], isFalse, reason: name);
          expect(r['error'], isA<String>(), reason: name);
        }

        final hooked = AssistantRunner(
          library: lib,
          player: eng,
          webToolHandler: (name, args, _) async => {
            'success': true,
            'hook': 'web:$name',
            'when': DateTime.utc(2026, 11, 24),
          },
          onlineSearchHandler: (name, args, _) async => {
            'success': true,
            'hook': 'online:$name',
          },
          metadataWriteHandler: (name, args, _) async => {
            'success': true,
            'hook': 'meta:$name',
          },
        );
        final w = await executeTool('web_search', const {'query': 'x'}, hooked);
        expect(w['hook'], 'web:web_search');
        // pyJsonDumps with defaultEncoder serialises non-JSON DateTime cleanly.
        expect(
          pyJsonDumps(w, ensureAscii: false, defaultEncoder: (o) => '$o'),
          contains('2026-11-24'),
        );
        expect(
          (await executeTool('search_online', const {
            'query': 'x',
          }, hooked))['hook'],
          'online:search_online',
        );
        expect(
          (await executeTool(
            'apply_artist_metadata',
            const {},
            hooked,
          ))['hook'],
          'meta:apply_artist_metadata',
        );
      },
    );

    test('AppConfig parses [assistant] section with Python defaults', () {
      final defaults = AppConfig.parse('');
      expect(defaults.assistant.llmEnabled, isTrue);
      expect(defaults.assistant.llmProvider, 'gemini');
      expect(defaults.assistant.geminiApiKey, '');
      expect(defaults.assistant.geminiModel, defaultGeminiModel);
      expect(defaults.assistant.researchMode, 'hybrid');
      expect(defaults.assistant.ollamaEndpoint, defaultOllamaEndpoint);
      expect(defaults.assistant.ollamaModel, defaultOllamaModel);
      expect(defaults.assistant.personalityPrompt, '');

      final custom = AppConfig.parse('''
[assistant]
llm_enabled = false
llm_provider = "ollama"
gemini_api_key = "AIzaTest"
gemini_model = "gemini-2.5-pro"
research_mode = "web_snippets"
ollama_endpoint = "http://10.0.0.2:11434/v1"
ollama_model = "qwen2.5"
personality_prompt = "Be brief."
''');
      expect(custom.assistant.llmEnabled, isFalse);
      expect(custom.assistant.llmProvider, 'ollama');
      expect(custom.assistant.geminiApiKey, 'AIzaTest');
      expect(custom.assistant.geminiModel, 'gemini-2.5-pro');
      expect(custom.assistant.researchMode, 'web_snippets');
      expect(custom.assistant.ollamaEndpoint, 'http://10.0.0.2:11434/v1');
      expect(custom.assistant.ollamaModel, 'qwen2.5');
      expect(custom.assistant.personalityPrompt, 'Be brief.');
    });

    test(
      'dispatchLlm falls back to rebuilt assistant message when rawMessage is null',
      () async {
        final dir = Directory.systemTemp.createTempSync('mai_an_raw_');
        final repo = await buildFixtureRepo(dir);
        addTearDown(() async {
          await repo.close();
          dir.deleteSync(recursive: true);
        });
        final sent = <List<Map<String, Object?>>>[];
        final fakeEngine = _StubLlmEngine((messages, tools) async {
          sent.add([for (final m in messages) Map<String, Object?>.of(m)]);
          if (sent.length == 1) {
            return LlmResponse(
              content: '',
              toolCalls: const [ToolCall('c1', 'get_player_status', {})],
              rawMessage: null,
            );
          }
          return LlmResponse(content: 'Idle, sir.');
        });
        final runner = AssistantRunner(
          library: RepoAssistantLibrary(repo),
          player: _FakeEngine(const {}),
          loadAssistantConfig: () => const AssistantConfig(geminiApiKey: 'k'),
          engineFactory: (_) => fakeEngine,
        );
        final resp = await runner.dispatchText('what is playing');
        expect(resp.success, isTrue);
        expect(resp.displayed, 'Idle, sir.');
        expect(sent.length, 2);
        final echoed = sent[1].firstWhere(
          (m) => m['role'] == 'assistant' && m.containsKey('tool_calls'),
        );
        expect(echoed['content'], '');
        final tcs = (echoed['tool_calls']! as List<Object?>)
            .cast<Map<String, Object?>>();
        expect(tcs.single['id'], 'c1');
        expect(
          (tcs.single['function']! as Map<String, Object?>)['arguments'],
          '{}',
        );
      },
    );
  });
}

class _StubLlmEngine extends LlmEngine {
  _StubLlmEngine(this._handler) : super(apiKey: 'k');
  final Future<LlmResponse> Function(
    List<Map<String, Object?>> messages,
    List<Map<String, Object?>>? tools,
  )
  _handler;

  @override
  Future<LlmResponse> chatCompletion(
    List<Map<String, Object?>> messages, {
    List<Map<String, Object?>>? tools,
  }) => _handler(messages, tools);
}

Object? _sortKeys(Object? v) => switch (v) {
  Map<Object?, Object?> m => {
    for (final k in m.keys.map((e) => e.toString()).toList()..sort())
      k: _sortKeys(m[k]),
  },
  List<Object?> l => [for (final e in l) _sortKeys(e)],
  _ => v,
};

Map<String, Object?>? _serializePending(PendingConfirmation? p) => p == null
    ? null
    : {
        'prompt': p.prompt,
        'on_yes_action': p.onYesAction,
        'on_yes_msg': p.onYesMsg,
        'on_no_msg': p.onNoMsg,
        'has_on_yes': p.onYes != null,
        'has_on_no': p.onNo != null,
      };

Map<String, Object?>? _serializeChoice(PendingChoice? c) => c == null
    ? null
    : {
        'prompt': c.prompt,
        'options': [
          for (final o in c.options)
            {'id': o.id, 'title': o.title, 'subtitle': o.subtitle},
        ],
      };

class _ScriptedRandom implements AssistantRandom {
  _ScriptedRandom(int seed) : _state = BigInt.from(seed) & _mask64;

  static final _mul = BigInt.parse('6364136223846793005');
  static final _inc = BigInt.parse('1442695040888963407');
  static final _mask64 = (BigInt.one << 64) - BigInt.one;
  BigInt _state;

  int _next() {
    _state = (_state * _mul + _inc) & _mask64;
    return (_state >> 32).toInt();
  }

  @override
  int randbelow(int n) => _next() % n;

  @override
  double random() => _next() / 4294967296.0;
}

class _FakeEngine implements AssistantPlayer {
  _FakeEngine(Map<String, Object?> spec)
    : _queue = [
        for (final t in (spec['queue'] as List<Object?>?) ?? const [])
          Map<String, Object?>.of(t! as Map<String, Object?>),
      ],
      _currentIndex = spec['current_index'] as int? ?? 0,
      _isPlaying = spec['is_playing'] as bool? ?? false,
      _isShuffle = spec['is_shuffle'] as bool? ?? false,
      _currentPath = spec['current_path'] as String? ?? '',
      _currentTrack = spec['current_track'] as String? ?? '',
      _currentArtist = spec['current_artist'] as String? ?? '',
      _currentAlbum = spec['current_album'] as String? ?? '',
      _duration = (spec['duration'] as num?)?.toDouble() ?? 0.0;

  final List<Row> _queue;
  int _currentIndex;
  bool _isPlaying;
  bool _isShuffle;
  String _currentPath;
  String _currentTrack;
  String _currentArtist;
  String _currentAlbum;
  double _duration;
  final List<Object?> calls = [];

  void _sync() {
    if (_currentIndex >= 0 && _currentIndex < _queue.length) {
      final t = _queue[_currentIndex];
      _currentPath = (t['path'] as String?) ?? '';
      _currentTrack =
          (t['track_title'] as String?) ?? (t['title'] as String?) ?? '';
      _currentArtist =
          (t['artist_name'] as String?) ?? (t['artist'] as String?) ?? '';
      _currentAlbum =
          (t['album_title'] as String?) ?? (t['album'] as String?) ?? '';
      _duration = (t['duration'] as num?)?.toDouble() ?? 0.0;
    } else {
      _currentPath = '';
      _currentTrack = '';
      _currentArtist = '';
      _currentAlbum = '';
      _duration = 0.0;
    }
  }

  @override
  List<Row> get queue => _queue;
  @override
  int get currentIndex => _currentIndex;
  @override
  String get currentPath => _currentPath;
  @override
  String get currentTrack => _currentTrack;
  @override
  String get currentArtist => _currentArtist;
  @override
  String get currentAlbum => _currentAlbum;
  @override
  double get duration => _duration;
  @override
  bool get isPlaying => _isPlaying;
  @override
  bool get isShuffle => _isShuffle;
  @override
  set isShuffle(bool v) => _isShuffle = v;

  @override
  void setQueue(List<Row> tracks, {int startIndex = 0}) {
    calls.add([
      'set_queue',
      [for (final t in tracks) t['path']],
      startIndex,
    ]);
    _queue
      ..clear()
      ..addAll([for (final t in tracks) Map<String, Object?>.of(t)]);
    _currentIndex = startIndex;
    _sync();
  }

  @override
  void queueLast(Row track) {
    calls.add(['queue_last', track['path']]);
    _queue.add(Map<String, Object?>.of(track));
  }

  @override
  void queueNext(Row track) {
    calls.add(['queue_next', track['path']]);
    final pos = _currentIndex + 1 < _queue.length
        ? _currentIndex + 1
        : _queue.length;
    _queue.insert(pos, Map<String, Object?>.of(track));
  }

  @override
  void queueLastMany(List<Row> tracks) {
    calls.add([
      'queue_last_many',
      [for (final t in tracks) t['path']],
    ]);
    _queue.addAll([for (final t in tracks) Map<String, Object?>.of(t)]);
  }

  @override
  void removeFromQueue(int idx) {
    calls.add(['remove_from_queue', idx]);
    _queue.removeAt(idx);
    if (idx < _currentIndex) {
      _currentIndex--;
    } else if (_currentIndex >= _queue.length) {
      _currentIndex = _queue.isEmpty ? 0 : _queue.length - 1;
    }
    _sync();
  }

  @override
  void moveQueueItem(int oldIdx, int newIdx) {
    calls.add(['move_queue_item', oldIdx, newIdx]);
    final item = _queue.removeAt(oldIdx);
    _queue.insert(newIdx, item);
    _sync();
  }

  @override
  void next() {
    calls.add(['next']);
    if (_currentIndex + 1 < _queue.length) {
      _currentIndex++;
      _sync();
    }
  }

  @override
  void previous() {
    calls.add(['previous']);
    if (_currentIndex > 0) {
      _currentIndex--;
      _sync();
    }
  }

  @override
  void pause() {
    calls.add(['pause']);
    _isPlaying = false;
  }

  @override
  void play() {
    calls.add(['play']);
    _isPlaying = true;
  }

  @override
  void stop() {
    calls.add(['stop']);
    _isPlaying = false;
    _currentPath = '';
    _currentTrack = '';
    _currentArtist = '';
    _currentAlbum = '';
  }

  @override
  void clearQueue() {
    calls.add(['clear_queue']);
    _queue.clear();
    _currentIndex = 0;
    _isPlaying = false;
    _sync();
  }
}
