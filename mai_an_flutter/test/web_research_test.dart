import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/assistant/adapters.dart';
import 'package:mai_an_lab/assistant/runner.dart';
import 'package:mai_an_lab/assistant/tool_executor.dart';
import 'package:mai_an_lab/assistant/web_research.dart';
import 'package:mai_an_lab/core/app_config.dart';
import 'package:mai_an_lab/core/py_json.dart';
import 'package:mai_an_lab/data/library_repository.dart';

/// Phase 5 stage e against Python's own `web_research`, `metadata_ai`
/// (`_parse_ddg_lite`, `_generate`, `_http_reason`, `_response_text`,
/// `_grounding`) and `llm_tools._web_tool` (`web_search`, `learn_about`,
/// `find_related_artists`), recorded by `tool/export_web_research_goldens.py`.
void main() {
  final golden =
      jsonDecode(File('test/goldens/web_research.json').readAsStringSync())
          as Map<String, Object?>;

  group('parseDdgLite parity against Python metadata_ai._parse_ddg_lite', () {
    for (final c
        in (golden['ddg_parse']! as List<Object?>)
            .cast<Map<String, Object?>>()) {
      test(c['name']! as String, () {
        final got = parseDdgLite(c['html']! as String);
        expect(
          pyJsonDumps(_sortKeys(got), ensureAscii: false),
          pyJsonDumps(_sortKeys(c['result']), ensureAscii: false),
        );
      });
    }
  });

  group('geminiHttpReason parity against Python metadata_ai._http_reason', () {
    for (final c
        in (golden['http_reasons']! as List<Object?>)
            .cast<Map<String, Object?>>()) {
      test('${c['status']} ${c['text']}', () {
        expect(
          geminiHttpReason(
            c['status']! as int,
            c['text']! as String,
            c['model']! as String,
          ),
          c['result'],
        );
      });
    }
  });

  group('WebResearcher.ask and web tools parity against Python', () {
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
            for (final k in const [
              'user-agent',
              'content-type',
              'x-goog-api-key',
            ])
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

    WebResearcher makeResearcher(
      Map<String, Object?> cfg, {
      required double Function() clock,
      List<double>? sleeps,
    }) {
      return WebResearcher(
        loadConfig: () => AssistantConfig(
          researchMode: cfg['mode']! as String,
          geminiApiKey: cfg['key']! as String,
          geminiModel: cfg['model']! as String,
        ),
        monotonicSeconds: clock,
        sleep: (d) async {
          sleeps?.add(d.inMicroseconds / 1e6);
        },
        ddgLiteUrl: '$base/ddg/lite/',
        wikiApiUrl: '$base/wiki/w/api.php',
        geminiGenerateUrlTemplate:
            '$base/v1beta/models/{model}:generateContent',
      );
    }

    for (final sc
        in (golden['ask_scenarios']! as List<Object?>)
            .cast<Map<String, Object?>>()) {
      test('ask: ${sc['name']}', () async {
        var nowS = 1000.0;
        final sleeps = <double>[];
        final researcher = makeResearcher(
          sc['config']! as Map<String, Object?>,
          clock: () => nowS,
          sleeps: sleeps,
        );

        final steps = (sc['steps']! as List<Object?>)
            .cast<Map<String, Object?>>();
        for (var i = 0; i < steps.length; i++) {
          final st = steps[i];
          nowS = (st['time']! as num).toDouble();
          replies
            ..clear()
            ..addAll(
              (st['replies']! as List<Object?>).cast<Map<String, Object?>>(),
            );
          requests.clear();
          sleeps.clear();

          final args = st['args']! as Map<String, Object?>;
          final res = await researcher.ask(
            args['question']! as String,
            context: args['context'] as String? ?? '',
            searchQuery: args['search_query'] as String? ?? '',
          );

          _expectRequestsEqual(
            requests,
            (st['requests']! as List<Object?>).cast<Map<String, Object?>>(),
            '${sc['name']} step $i',
          );
          expect(sleeps, [
            for (final s in st['sleeps']! as List<Object?>)
              (s! as num).toDouble(),
          ], reason: '${sc['name']} step $i sleeps');
          expect(
            researcher.googleOffReason,
            st['google_off_reason'],
            reason: '${sc['name']} step $i google_off_reason',
          );
          expect(
            pyJsonDumps(_sortKeys(res), ensureAscii: false),
            pyJsonDumps(_sortKeys(st['result']), ensureAscii: false),
            reason: '${sc['name']} step $i result',
          );
        }
      });
    }

    Future<LibraryRepository> buildFixtureRepo(Directory dir) async {
      final path = '${dir.path}/library.db';
      var repo = await LibraryRepository.open(path, writable: true);
      await repo.close();
      repo = await LibraryRepository.open(path, writable: true);
      final stmts = <(String, List<Object?>)>[
        (
          'INSERT INTO artists (id, name, album_count, track_count) VALUES (?, ?, ?, ?)',
          [1, 'Slowdive', 1, 0],
        ),
        (
          'INSERT INTO artists (id, name, album_count, track_count) VALUES (?, ?, ?, ?)',
          [2, 'Ride', 1, 0],
        ),
        (
          'INSERT INTO artists (id, name, album_count, track_count) VALUES (?, ?, ?, ?)',
          [3, 'Lush', 1, 0],
        ),
        (
          'INSERT INTO artists (id, name, album_count, track_count) VALUES (?, ?, ?, ?)',
          [4, 'Beyoncé', 1, 0],
        ),
        (
          'INSERT INTO artists (id, name, album_count, track_count) VALUES (?, ?, ?, ?)',
          [5, 'Air', 1, 0],
        ),
        (
          'INSERT INTO albums (id, artist_id, title, year, genre, track_count) VALUES (?, ?, ?, ?, ?, ?)',
          [1, 1, 'Souvlaki', 1993, 'Shoegaze', 0],
        ),
        (
          'INSERT INTO albums (id, artist_id, title, year, genre, track_count) VALUES (?, ?, ?, ?, ?, ?)',
          [2, 2, 'Nowhere', 1990, 'Shoegaze', 0],
        ),
        (
          'INSERT INTO albums (id, artist_id, title, year, genre, track_count) VALUES (?, ?, ?, ?, ?, ?)',
          [3, 3, 'Split', 1994, 'Dream Pop', 0],
        ),
        (
          'INSERT INTO albums (id, artist_id, title, year, genre, track_count) VALUES (?, ?, ?, ?, ?, ?)',
          [4, 4, 'Lemonade', 2016, 'Pop', 0],
        ),
        (
          'INSERT INTO albums (id, artist_id, title, year, genre, track_count) VALUES (?, ?, ?, ?, ?, ?)',
          [5, 5, 'Moon Safari', 1998, 'Electronic', 0],
        ),
        (
          'INSERT INTO tracks (id, album_id, title, path, duration, format, track_num, added_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
          [1, 1, 'Alison', '/m/0.flac', 231.0, 'FLAC', 1, 1.0],
        ),
        (
          'INSERT INTO tracks (id, album_id, title, path, duration, format, track_num, added_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
          [2, 1, 'When the Sun Hits', '/m/1.flac', 287.0, 'FLAC', 2, 1.0],
        ),
        (
          'INSERT INTO tracks (id, album_id, title, path, duration, format, track_num, added_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
          [3, 2, 'Vapour Trail', '/m/2.flac', 258.0, 'FLAC', 1, 1.0],
        ),
        (
          'INSERT INTO tracks (id, album_id, title, path, duration, format, track_num, added_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
          [4, 3, 'Desire Lines', '/m/3.flac', 457.0, 'FLAC', 1, 1.0],
        ),
        (
          'INSERT INTO tracks (id, album_id, title, path, duration, format, track_num, added_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
          [5, 4, 'Hold Up', '/m/4.flac', 221.0, 'FLAC', 1, 1.0],
        ),
        (
          'INSERT INTO tracks (id, album_id, title, path, duration, format, track_num, added_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
          [6, 5, 'Sexy Boy', '/m/5.flac', 298.0, 'FLAC', 1, 1.0],
        ),
        (
          'INSERT INTO artist_enrichment (artist_name, country, genres, source, status, score) VALUES (?, ?, ?, ?, ?, ?)',
          [
            'Slowdive',
            'GB',
            '[{"name": "shoegaze", "count": 10}, {"name": "dream pop", "count": 5}]',
            'musicbrainz',
            'ok',
            100,
          ],
        ),
      ];
      await repo.debugExecute(stmts);
      return repo;
    }

    for (final sc
        in (golden['tool_scenarios']! as List<Object?>)
            .cast<Map<String, Object?>>()) {
      test('tool: ${sc['name']}', () async {
        final dir = Directory.systemTemp.createTempSync('mai_an_web_tool_');
        final repo = await buildFixtureRepo(dir);
        addTearDown(() async {
          await repo.close();
          dir.deleteSync(recursive: true);
        });

        replies
          ..clear()
          ..addAll(
            (sc['replies']! as List<Object?>).cast<Map<String, Object?>>(),
          );
        requests.clear();

        final researcher = makeResearcher(
          sc['config']! as Map<String, Object?>,
          clock: () => 1000.0,
        );
        final runner = AssistantRunner(
          library: RepoAssistantLibrary(repo),
          player: _MinimalPlayer(sc['current_path']! as String),
          webResearcher: researcher,
        );
        runner.agentReset();

        final res = await executeTool(
          sc['tool']! as String,
          Map<String, Object?>.of(sc['args']! as Map<String, Object?>),
          runner,
        );

        _expectRequestsEqual(
          requests,
          (sc['requests']! as List<Object?>).cast<Map<String, Object?>>(),
          sc['name']! as String,
        );
        expect(
          pyJsonDumps(_sortKeys(res), ensureAscii: false),
          pyJsonDumps(_sortKeys(sc['result']), ensureAscii: false),
          reason: '${sc['name']} result',
        );
      });
    }

    test('generateContent retries once on refused connection', () async {
      // Bind and close immediately to guarantee a refused loopback port.
      final dead = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      final deadPort = dead.port;
      await dead.close(force: true);

      final sleeps = <Duration>[];
      final researcher = WebResearcher(
        loadConfig: () => const AssistantConfig(
          researchMode: 'google_grounding',
          geminiApiKey: 'k',
        ),
        sleep: (d) async => sleeps.add(d),
        geminiGenerateUrlTemplate:
            'http://127.0.0.1:$deadPort/v1beta/models/{model}:generateContent',
        networkRetryDelay: const Duration(milliseconds: 5),
      );
      await expectLater(
        () => researcher.generateContent('k', 'm', 'hi', useGrounding: true),
        throwsA(isA<ResearchUnavailable>()),
      );
      expect(sleeps, [const Duration(milliseconds: 5)]);
    });
  });
}

void _expectRequestsEqual(
  List<Map<String, Object?>> got,
  List<Map<String, Object?>> want,
  String label,
) {
  expect(got.length, want.length, reason: '$label request count');
  for (var i = 0; i < want.length; i++) {
    expect(got[i]['method'], want[i]['method'], reason: '$label req $i method');
    expect(got[i]['path'], want[i]['path'], reason: '$label req $i path');
    expect(got[i]['body'], want[i]['body'], reason: '$label req $i body');
    final gotH = got[i]['headers']! as Map<String, Object?>;
    final wantH = want[i]['headers']! as Map<String, Object?>;
    final wantUa = wantH['user-agent'] as String?;
    if (wantUa == kBrowserUa || wantUa == kWikiUa) {
      expect(gotH['user-agent'], wantUa, reason: '$label req $i UA');
    }
    expect(
      gotH['x-goog-api-key'],
      wantH['x-goog-api-key'],
      reason: '$label req $i key',
    );
    if (wantH['content-type'] != null) {
      // aiohttp may omit charset on application/x-www-form-urlencoded; compare media type prefix.
      expect(
        (gotH['content-type'] as String? ?? '').split(';').first.trim(),
        (wantH['content-type'] as String).split(';').first.trim(),
        reason: '$label req $i content-type',
      );
    }
  }
}

Object? _sortKeys(Object? v) => switch (v) {
  Map<Object?, Object?> m => {
    for (final k in m.keys.map((e) => e.toString()).toList()..sort())
      k: _sortKeys(m[k]),
  },
  List<Object?> l => [for (final x in l) _sortKeys(x)],
  _ => v,
};

class _MinimalPlayer implements AssistantPlayer {
  _MinimalPlayer(this.currentPath);
  @override
  final String currentPath;
  @override
  List<Row> get queue => const [];
  @override
  int get currentIndex => 0;
  @override
  String get currentTrack => '';
  @override
  String get currentArtist => '';
  @override
  String get currentAlbum => '';
  @override
  double get duration => 0.0;
  @override
  bool get isPlaying => false;
  @override
  bool isShuffle = false;
  @override
  void setQueue(List<Row> tracks, {int startIndex = 0}) {}
  @override
  void queueLast(Row track) {}
  @override
  void queueNext(Row track) {}
  @override
  void queueLastMany(List<Row> tracks) {}
  @override
  void removeFromQueue(int idx) {}
  @override
  void moveQueueItem(int oldIdx, int newIdx) {}
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
