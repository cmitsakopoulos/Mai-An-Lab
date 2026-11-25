import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/app_config.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/metadata/metadata_ai.dart';
import 'package:mai_an_lab/metadata/metadata_audit.dart';
import 'package:mai_an_lab/metadata/metadata_enrich.dart';
import 'package:mai_an_lab/metadata/metadata_qobuz.dart';
import 'package:mai_an_lab/metadata/musicbrainz.dart';
import 'package:mai_an_lab/search/new_releases.dart';
import 'package:mai_an_lab/sources/source_client.dart';

/// Phase 7a: the artist-metadata backend against Flet's own outputs
/// (`tool/export_phase7a_goldens.py`): MusicBrainz matching and lookups,
/// Qobuz genres, the enrichment passes, the audit, AI research and the
/// `db_manager` writes, on the same synthetic library and canned responses.

final Map<String, Object?> g =
    jsonDecode(File('test/goldens/phase7a.json').readAsStringSync())
        as Map<String, Object?>;

List<Object?> _l(Object? v) => v! as List<Object?>;
Map<String, Object?> _m(Object? v) => v! as Map<String, Object?>;

/// JSON round-trip, so Dart records/sets compare like Python's lists.
Object? _j(Object? v) => jsonDecode(jsonEncode(v));

// ── canned MusicBrainz ──────────────────────────────────────────────────────

final mbRequests = <Map<String, Object?>>[];
final _keyRe = RegExp(r'artist:"((?:[^"\\]|\\.)*)"');

Future<ReleaseHttpResponse?> fakeMb(
  Uri uri, {
  Map<String, String> headers = const {},
  Duration timeout = Duration.zero,
}) async {
  final path = uri.path.split('/ws/2/').last;
  final params = uri.queryParameters;
  mbRequests.add({'path': path, 'params': Map<String, Object?>.of(params)});
  final fail = _m(g['mb_fail']);
  if (path == 'artist') {
    final m = _keyRe.firstMatch(params['query']!);
    final key = m == null ? '' : m[1]!.replaceAll(r'\', '');
    if (fail.containsKey(key)) {
      final st = fail[key]! as int;
      return st == -1 ? null : ReleaseHttpResponse(st, '');
    }
    return ReleaseHttpResponse(
      200,
      jsonEncode({'artists': _m(g['mb'])[key] ?? []}),
    );
  }
  final id = path.split('/').last;
  final d = _m(g['mb_details'])[id];
  return d == null
      ? const ReleaseHttpResponse(404, '')
      : ReleaseHttpResponse(200, jsonEncode(d));
}

MusicBrainzClient mbClient() => MusicBrainzClient(
  contact: 'x@example.com',
  httpGet: fakeMb,
  minInterval: Duration.zero,
  backoffUnit: Duration.zero,
);

/// Python's patched `_get` answers once; the real client retries a 503 or
/// an offline request, so repeats are collapsed on both sides.
/// Query values are strings on the wire (Python passes `limit` as an int).
List<Object?> _collapse(List<Object?> xs) {
  final out = <Object?>[];
  for (final x0 in xs) {
    final x = {
      'path': (x0! as Map)['path'],
      'params': {
        for (final e in ((x0 as Map)['params'] as Map).entries)
          e.key: '${e.value}',
      },
    };
    if (out.isEmpty || jsonEncode(out.last) != jsonEncode(x)) out.add(x);
  }
  return out;
}

// ── canned Qobuz ────────────────────────────────────────────────────────────

class RuntimeError implements Exception {
  @override
  String toString() => 'boom';
}

class FakeQobuz implements SourceClient {
  final requests = <List<Object?>>[];

  @override
  Future<void> login() async {}
  @override
  Future<void> close() async {}

  @override
  Future<List<Map<String, Object?>>> search(
    String mediaType,
    String query, {
    int limit = 500,
    int offset = 0,
    ProgressStageCallback? progressCallback,
  }) async {
    requests.add(['search', mediaType, query, limit]);
    if (_l(g['qobuz_error']).contains(query)) throw RuntimeError();
    final hit = _m(g['qobuz'])[query] as Map<String, Object?>?;
    return [
      {
        'artists': {
          'items': [
            {'id': 999, 'name': 'Decoy Act'},
            if (hit != null) {'id': hit['id'], 'name': hit['name']},
          ],
        },
      },
    ];
  }

  @override
  Future<Map<String, Object?>> getMetadata(
    String itemId,
    String mediaType, {
    int limit = 500,
    int offset = 0,
  }) async {
    requests.add(['get', itemId, mediaType]);
    for (final v in _m(g['qobuz']).values.cast<Map<String, Object?>>()) {
      if ('${v['id']}' == itemId) {
        return {
          'albums': {'items': v['albums']},
        };
      }
    }
    return {};
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

/// The researcher behind the AI goldens: canned snippets and a canned
/// Gemini answer per prompt (`_fake_snippets` / `_fake_generate`).
MetadataResearcher goldenResearcher(
  LibraryRepository repo, {
  List<String>? prompts,
  List<Object?>? calls,
}) {
  final snippetMap = _m(g['snippet_map']);
  final snippets = _l(g['snippets']);
  List<Map<String, String>> sn(Object? v) => [
    for (final s in _l(v)) Map<String, String>.from(s! as Map),
  ];
  return MetadataResearcher(
    repo: repo,
    loadConfig: () => const AssistantConfig(
      geminiApiKey: 'KEY',
      geminiModel: 'gemini-test',
      researchMode: 'hybrid',
    ),
    sleep: (_) async {},
    fetchSnippets: (artist, _) async => snippetMap.containsKey(artist)
        ? (snippetMap[artist] == null ? null : sn(snippetMap[artist]))
        : sn([snippets[2]]),
    generate:
        (key, model, prompt, {required useGrounding, responseSchema}) async {
          prompts?.add(prompt);
          calls?.add({
            'api_key': key,
            'model': model,
            'use_grounding': useGrounding,
            'structured': responseSchema != null,
          });
          final n = RegExp(
            r'^ARTIST \d+:',
            multiLine: true,
          ).allMatches(prompt).length;
          return {
            'candidates': [
              {
                'content': {
                  'parts': [
                    {
                      'text': jsonEncode({
                        'artists': [
                          for (var i = 1; i <= n; i++)
                            {
                              'id': i,
                              'identified': i != 2,
                              'matched_as': 'Artist $i',
                              'summary': 'Artist number $i',
                              'genres': i.isOdd ? ['Laiko', 'pop'] : ['trap'],
                              'country': i == 1 ? 'GR' : 'us',
                              'confidence': 'high',
                            },
                        ],
                      }),
                    },
                  ],
                },
              },
            ],
          };
        },
  );
}

// ── fixture ─────────────────────────────────────────────────────────────────

Future<LibraryRepository> fixtureRepo(Directory dir, String name) async {
  final path = '${dir.path}/$name.db';
  var repo = await LibraryRepository.open(path, writable: true);
  await repo.close();
  repo = await LibraryRepository.open(path, writable: true);
  final fx = _m(g['fixture']);
  final stmts = <(String, List<Object?>)>[
    for (final a in _l(fx['artists']).cast<List<Object?>>())
      ('INSERT INTO artists (id, name) VALUES (?, ?)', a),
    for (final a in _l(fx['albums']).cast<List<Object?>>())
      (
        'INSERT INTO albums (id, artist_id, title, year, genre) '
            'VALUES (?, ?, ?, ?, ?)',
        a,
      ),
    for (final t in _l(fx['tracks']).cast<List<Object?>>())
      (
        'INSERT INTO tracks (id, album_id, title, path, duration, format, '
            'added_date) VALUES (?, ?, ?, ?, ?, ?, ?)',
        [
          t[0],
          t[1],
          t[2],
          '/m/${t[0]}.flac',
          100.0,
          'flac',
          1000.0 + (t[0]! as int),
        ],
      ),
    for (final e in _l(fx['enrichment']).cast<List<Object?>>())
      (
        'INSERT INTO artist_enrichment (artist_name, mbid, country, area, '
            'genres, source, score, status, provenance, fetched_at, attempts) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        e,
      ),
    for (final r in _l(fx['ai_research']).cast<List<Object?>>())
      (
        'INSERT INTO artist_ai_research (artist_name, payload, state, '
            'created_at) VALUES (?, ?, ?, 1000)',
        r,
      ),
    for (final r in _l(fx['dismissed']).cast<List<Object?>>())
      (
        'INSERT INTO metadata_audit_dismissed (artist_name, rule, value) '
            'VALUES (?, ?, ?)',
        r,
      ),
    for (final r in _l(fx['rejections']).cast<List<Object?>>())
      ('INSERT INTO artist_alias_rejections (a, b) VALUES (?, ?)', r),
  ];
  await repo.debugExecute(stmts);
  return repo;
}

Future<Map<String, Object?>> dump(LibraryRepository repo) async {
  final out = <String, Object?>{};
  for (final (table, sql) in const [
    (
      'artists',
      'SELECT id, name, album_count, track_count FROM artists ORDER BY id',
    ),
    (
      'albums',
      'SELECT id, artist_id, title, year, genre, genre_bucket, track_count '
          'FROM albums ORDER BY id',
    ),
    ('tracks', 'SELECT id, album_id, artist_tag FROM tracks ORDER BY id'),
    (
      'enrichment',
      'SELECT artist_name, mbid, country, area, genres, source, score, status, '
          'provenance, attempts FROM artist_enrichment ORDER BY artist_name',
    ),
    (
      'aliases',
      'SELECT alias, canonical, enrichment FROM artist_aliases ORDER BY alias',
    ),
    ('rejections', 'SELECT a, b FROM artist_alias_rejections ORDER BY a, b'),
    (
      'research',
      'SELECT artist_name, payload, state FROM artist_ai_research '
          'ORDER BY artist_name',
    ),
    (
      'dismissed',
      'SELECT artist_name, rule, value FROM metadata_audit_dismissed '
          'ORDER BY artist_name, rule',
    ),
  ]) {
    out[table] = await repo.debugSelect(sql);
  }
  for (final r in out['research']! as List<List<Object?>>) {
    try {
      final p = jsonDecode(r[1]! as String) as Map<String, Object?>
        ..remove('researched_at');
      r[1] = p;
    } catch (_) {}
  }
  return out;
}

/// The stored JSON text columns parse to the same values as Python's
/// (`json.dumps` byte-for-byte is checked separately where it matters).
void expectState(Map<String, Object?> actual, Object? expected) {
  final e = _m(expected);
  for (final k in actual.keys) {
    expect(_j(actual[k]), e[k], reason: 'table $k');
  }
}

Map<String, Object?> _noTime(Map<String, Object?> m) =>
    Map.of(m)..remove('researched_at');

void main() {
  late Directory tmp;
  setUpAll(() => tmp = Directory.systemTemp.createTempSync('phase7a_'));
  tearDownAll(() => tmp.deleteSync(recursive: true));

  group('pure functions', () {
    test('name matching, Lucene, genres, country', () {
      for (final c in _l(g['name_close']).cast<List<Object?>>()) {
        expect(nameClose(c[0]! as String, c[1]! as String), c[2], reason: '$c');
      }
      for (final c in _l(g['lucene']).cast<List<Object?>>()) {
        expect(escapeLucene(c[0]! as String), c[1]);
      }
      for (final c in _l(g['extract_genres']).cast<List<Object?>>()) {
        expect(_j(extractGenres(_m(c[0]))), c[1], reason: '${c[0]}');
      }
      for (final c in _l(g['extract_country']).cast<List<Object?>>()) {
        expect(extractCountry(c[0]), c[1], reason: '${c[0]}');
      }
      for (final c in _l(g['qobuz_tokens']).cast<List<Object?>>()) {
        expect(tokensForGenre(c[0]), c[1], reason: '${c[0]}');
      }
      for (final c in _l(g['skeleton']).cast<List<Object?>>()) {
        expect(skeleton(c[0]! as String), c[1], reason: '${c[0]}');
      }
      for (final c in _l(g['summary_text']).cast<List<Object?>>()) {
        expect(metadataSummaryText(_m(c[0])), c[1]);
        final a = metadataRetryAction(_m(c[0]));
        expect(a == null ? null : [a.$1, a.$2], c[2]);
      }
    });

    test('AI corroboration, prompt, parsing, validation', () {
      for (final c in _l(g['ai_norm']).cast<List<Object?>>()) {
        expect(aiNorm(c[0]), c[1]);
      }
      for (final c in _l(g['search_queries']).cast<List<Object?>>()) {
        expect(searchQueries(c[0]! as String, _m(c[1])), c[2]);
      }
      List<Map<String, String>> sn(Object? v) => [
        for (final s in _l(v)) Map<String, String>.from(s! as Map),
      ];
      final ev = _m(g['evidence']);
      expect(_j(onTargetSnippets(sn(g['snippets']), ev)), g['on_target']);
      for (final c in _l(g['countries_named']).cast<List<Object?>>()) {
        expect(countriesNamed(c[0]! as String).toList()..sort(), c[1]);
      }
      for (final c in _l(g['country_basis']).cast<List<Object?>>()) {
        expect(
          countryBasis(c[0]! as String, sn(c[1]), _m(c[2])),
          c[3],
          reason: '$c',
        );
      }
      for (final c in _l(g['parse_answers']).cast<List<Object?>>()) {
        final got = parseAnswers(c[0] as String?, c[1]! as int);
        expect(_j({for (final e in got.entries) '${e.key}': e.value}), c[2]);
      }
      for (final c in _l(g['validate']).cast<List<Object?>>()) {
        expect(
          _j(
            validateAnswer(
              c[0] as Map<String, Object?>?,
              sn(c[1]),
              _l(c[2]).cast<String>(),
              allowMemoryCountry: c[3]! as bool,
              basis: c[4] as String?,
            ),
          ),
          c[5],
          reason: '${c[0]}',
        );
      }
      for (final c in _l(g['clean_country']).cast<List<Object?>>()) {
        expect(cleanCountry(c[0]), c[1]);
      }
      final prompts = _m(g['prompt']);
      final ev2 = {
        ...ev,
        'artist': 'Laiko Band',
        'disputed': 'Tagged laiko, but the country is FR.',
      };
      final snippets = sn(g['snippets']);
      expect(
        buildResearchPrompt(
          [(ev, snippets.sublist(0, 2)), (ev2, const [])],
          ['laiko', 'pop'],
        ),
        prompts['hybrid'],
      );
      expect(
        buildResearchPrompt([(ev, const [])], const [], mode: 'weights'),
        prompts['weights'],
      );
      expect(
        buildResearchPrompt(
          [(ev, snippets)],
          List.filled(70, 'x'),
          mode: 'google_grounding',
        ),
        prompts['google'],
      );
      final finish = _l(g['finish']);
      expect(
        _j(
          _noTime(
            finishAnswer(
              ev,
              {
                'identified': true,
                'genres': ['Laiko'],
                'country': 'GR',
                'confidence': 'medium',
              },
              snippets.sublist(0, 2),
              false,
              ['laiko'],
              'hybrid',
              'm',
            ),
          ),
        ),
        finish[0],
      );
      expect(
        _j(
          _noTime(
            finishAnswer(
              ev,
              {
                'identified': true,
                'genres': <String>[],
                'country': 'US',
                'confidence': 'high',
              },
              const [],
              true,
              const [],
              'weights',
              'm',
            ),
          ),
        ),
        finish[1],
      );
      expect(
        _j(
          _noTime(
            finishAnswer(
              ev,
              null,
              const [],
              false,
              const [],
              'google_grounding',
              'm',
              grounding: (
                [
                  {'title': 'g', 'uri': 'https://g'},
                ],
                ['q'],
              ),
            ),
          ),
        ),
        finish[2],
      );
    });
  });

  test('MusicBrainz lookups and candidates', () async {
    mbRequests.clear();
    final client = mbClient();
    final lookups = <Object?>[];
    for (final c in _l(g['mb_lookups']).cast<List<Object?>>()) {
      lookups.add([
        c[0],
        c[1],
        await client.lookupArtist(c[0]! as String, withGenres: c[1]! as bool),
      ]);
    }
    expect(_j(lookups), g['mb_lookups']);
    expect(
      _collapse(_j(mbRequests)! as List),
      _collapse(_l(g['mb_lookup_requests'])),
    );
    mbRequests.clear();
    expect(
      _j(await client.searchCandidates('Kanye West', limit: 5)),
      g['mb_candidates'],
    );
    expect(
      _collapse(_j(mbRequests)! as List),
      _collapse(_l(g['mb_candidates_requests'])),
    );
  });

  test('Qobuz genre lookups', () async {
    final q = FakeQobuz();
    final out = <Object?>[];
    for (final c in _l(g['qobuz_lookups']).cast<List<Object?>>()) {
      out.add([c[0], await lookupQobuzArtistGenres(q, c[0]! as String)]);
    }
    expect(_j(out), g['qobuz_lookups']);
    expect(_j(q.requests), g['qobuz_lookup_requests']);
  });

  test('reads, audit and duplicates on the fixture', () async {
    final repo = await fixtureRepo(tmp, 'base');
    final b = _m(g['base']);
    expect(_j(await repo.genreVocabulary()), b['vocabulary']);
    expect(_j(await repo.genreVocabulary(3)), b['vocabulary_3']);
    expect(_j(await repo.libraryCountries()), b['countries']);
    expect(_j(await repo.lowConfidenceArtists()), b['low_confidence']);
    expect(
      _j(await repo.metadataGapArtists(limit: 1000, everything: true)),
      b['gaps_everything'],
    );
    final items = _m(b['workbench_items']);
    for (final n in items.keys) {
      expect(_j(await repo.artistWorkbenchItem(n)), items[n], reason: n);
    }
    final ev = _m(b['evidence']);
    for (final n in ev.keys) {
      expect(_j(await repo.artistResearchEvidence(n)), ev[n], reason: n);
    }
    List<Object?> pairs(List<(String, int)> xs) => [
      for (final (n, a) in xs) [n, a],
    ];
    expect(pairs(await repo.artistsNeedingEnrichment()), b['needing']);
    expect(
      pairs(await repo.artistsNeedingEnrichment(includeFailed: true)),
      b['needing_failed'],
    );
    expect(
      pairs(await repo.artistsNeedingEnrichment(retryIncomplete: true)),
      b['needing_incomplete'],
    );
    expect(
      pairs(await repo.artistsNeedingEnrichment(limit: 2)),
      b['needing_limit'],
    );
    expect(
      (await repo.researchedArtistNames()).toList()..sort(),
      b['researched'],
    );
    expect(_j(await repo.auditFlags()), b['audit_flags']);
    expect(_j(await repo.duplicateArtists()), b['duplicates']);
    expect(await repo.albumTitles('Slipknot'), b['album_titles']);
    expect(await repo.aliasesOf('Haris Alexiou'), b['aliases_none']);
    await repo.close();
  });

  test('album genre normalisation', () async {
    final repo = await fixtureRepo(tmp, 'fix');
    final f = _m(g['fix']);
    final first = await repo.refreshWalkModels();
    expect(_j(first['genre_fix']), f['summary']);
    expect(_j((await dump(repo))['albums']), f['after']);
    expect(_j((await repo.refreshWalkModels())['genre_fix']), f['second']);
    await repo.close();
  });

  test('enrichment writes, bulk tagging and accepted suggestions', () async {
    final repo = await fixtureRepo(tmp, 'writes');
    final w = _m(g['writes']);
    await repo.upsertArtistEnrichment(
      'Kasabian',
      country: 'US',
      genres: [
        {'name': 'x', 'count': 1},
      ],
    );
    await repo.upsertArtistEnrichment(
      'Ghost Name',
      mbid: 'm',
      source: 'qobuz',
      score: 5,
      status: 'lowconfidence',
      provenance: const {},
    );
    expect([
      await repo.recordEnrichmentFailure('Mid Score', source: 'qobuz'),
      await repo.recordEnrichmentFailure('Mid Score', source: 'musicbrainz'),
      await repo.recordEnrichmentFailure('Kasabian', source: 'musicbrainz'),
      await repo.recordEnrichmentFailure('Slipknot', source: 'musicbrainz'),
      await repo.recordEnrichmentFailure(
        'Light',
        source: 'musicbrainz',
        maxAttempts: 1,
      ),
    ], w['failure']);
    await repo.setManualArtistEnrichment(
      'Light',
      country: 'NL',
      genres: [
        'dutch rock',
        ' ',
        {'name': 'x', 'count': 3},
      ],
    );
    await repo.setManualArtistEnrichment(
      'Nomad',
      country: '',
      genres: const [],
      origin: 'ai',
    );
    expect(
      await repo.bulkTagArtists([
        'Moose',
        'Sigur Rós',
        'Haris Alexiou',
      ], genre: 'Hip Hop, indie rock,, Trap'),
      w['bulk_tag'],
    );
    await repo.refreshWalkModels();
    expect(
      await repo.bulkTagArtists(['Moose'], genre: ' ', country: ''),
      w['bulk_tag_none'],
    );
    expect(
      await repo.setArtistsGenres(
        ['Slipknot', 'Divers Act'],
        ['Metal ', 'metal', 'NU METAL', ''],
        country: 'gb',
      ),
      w['set_genres'],
    );
    await repo.refreshWalkModels();
    expect(
      await repo.setManualArtistEnrichmentBulk(
        ['Drifter'],
        country: 'DE',
        genres: ['folk'],
      ),
      w['bulk_manual'],
    );
    await repo.refreshWalkModels();
    expect(
      await repo.acceptArtistMatches(['Toquel, Moose', 'Moose', '']),
      w['accept_matches'],
    );
    await repo.confirmArtistMatch(
      'Ghost Name',
      mbid: 'mb-g',
      country: 'ES',
      genres: const [],
    );
    await repo.rejectAliasPair('Weeknd', 'The Weeknd');
    await repo.dismissAuditFlag('Moose', 'collab', 'US');
    await repo.saveAiResearch('Moose', {
      'status': 'ok',
      'genres': ['trap'],
      'country': 'GR',
      'researched_at': 1.5,
    });
    await repo.setAiResearchState('Laiko Band', 'accepted');
    await repo.acceptAiSuggestion('Moose');
    await repo.acceptAiSuggestion(
      'Nomad',
      genres: ['Ambient', 'ambient', ''],
      country: '',
    );
    await repo.acceptAiSuggestion('Unknown Artist', genres: ['lo-fi']);
    await repo.setAiResearchState('Drifter', 'dismissed');
    expectState(await dump(repo), w['state']);
    expect(_j(await repo.genreVocabulary()), w['vocabulary']);
    // The stored JSON is json.dumps' own text.
    final stored = await repo.debugSelect(
      'SELECT genres, provenance FROM artist_enrichment ORDER BY artist_name',
    );
    final py = [
      for (final r in _l(_m(w['state'])['enrichment']).cast<List<Object?>>())
        [r[4], r[8]],
    ];
    expect(stored, py);
    await repo.close();
  });

  test('merge and separate', () async {
    final repo = await fixtureRepo(tmp, 'merge');
    final m = _m(g['merge']);
    await repo.mergeArtists('Χάρις Αλεξίου', 'Haris Alexiou');
    await repo.mergeArtists('Weeknd', 'The Weeknd');
    final merged = await dump(repo);
    expectState(merged, m['merged']);
    expect(
      await repo.aliasesOf('Haris Alexiou'),
      _m(m['merged'])['aliases_of'],
    );
    final errors = <String?>[];
    for (final (a, c) in [
      ('Haris Alexiou', 'haris alexiou'),
      ('Nobody', 'Moose'),
      ('', 'Moose'),
    ]) {
      try {
        await repo.mergeArtists(a, c);
        errors.add(null);
      } catch (e) {
        errors.add(e.toString().replaceFirst('DbWorkerException: ', ''));
      }
    }
    expect(errors, m['errors']);
    await repo.separateArtist('Χάρις Αλεξίου');
    expectState(await dump(repo), m['separated']);
    await expectLater(repo.separateArtist('Moose'), throwsA(anything));
    await repo.close();
  });

  group('enrichment passes', () {
    Future<(LibraryRepository, EnrichDeps, FakeQobuz)> setup(
      String name, {
      bool brokenQobuz = false,
    }) async {
      final repo = await fixtureRepo(tmp, name);
      final q = FakeQobuz();
      return (
        repo,
        EnrichDeps(
          repo: repo,
          refresh: repo.refreshWalkModels,
          musicBrainz: mbClient,
          qobuzClient: () async {
            if (brokenQobuz) throw StateError('bad');
            return q;
          },
        ),
        q,
      );
    }

    for (final label in ['mb_failed', 'mb_incomplete', 'qobuz']) {
      test('manual Sync: $label', () async {
        final s = _m(g['sync_$label']);
        final kw = _m(s['kwargs']);
        final (repo, deps, q) = await setup('sync_$label');
        mbRequests.clear();
        final events = <Object?>[];
        final summary = await enrichLibrary(
          deps,
          provider: s['provider']! as String,
          includeFailed: kw['include_failed'] == true,
          retryIncomplete: kw['retry_incomplete'] == true,
          limit: kw['limit'] as int?,
          progress: (i, t, n, r) => events.add([i, t, n, r]),
        );
        expect(_j(summary), s['summary']);
        expect(_j(events), s['events']);
        expectState(await dump(repo), s['state']);
        expect(
          _collapse(_j(mbRequests)! as List),
          _collapse(_l(s['mb_requests'])),
        );
        expect(_j(q.requests), s['qobuz_requests']);
        await repo.close();
      });
    }

    test('three consecutive errors abort without costing attempts', () async {
      final s = _m(g['sync_abort']);
      final saved = Map<String, Object?>.of(_m(g['mb_fail']));
      _m(g['mb_fail'])
        ..clear()
        ..addAll(_m(s['mb_fail']));
      try {
        final (repo, deps, _) = await setup('sync_abort');
        final events = <Object?>[];
        final summary = await enrichLibrary(
          deps,
          progress: (i, t, n, r) => events.add([i, t, n, r]),
        );
        expect(_j(summary), s['summary']);
        expect(_j(events), s['events']);
        expectState(await dump(repo), s['state']);
        await repo.close();
      } finally {
        _m(g['mb_fail'])
          ..clear()
          ..addAll(saved);
      }
    });

    for (final (label, pref, broken) in [
      ('pending_musicbrainz', 'musicbrainz', false),
      ('pending_qobuz', 'qobuz', false),
      ('pending_noqobuz', 'qobuz', true),
    ]) {
      test('automatic pass: $label', () async {
        final s = _m(g[label]);
        final (repo, deps, _) = await setup(label, brokenQobuz: broken);
        final events = <Object?>[];
        final summary = await enrichPending(
          deps,
          preferred: pref,
          progress: (i, t, n, r) => events.add([i, t, n, r]),
        );
        expect(_j(summary), s['summary']);
        expect(_j(events), s['events']);
        expectState(await dump(repo), s['state']);
        await repo.close();
      });
    }
  });

  test('AI research flows', () async {
    final r = _m(g['research']);
    final repo = await fixtureRepo(tmp, 'research');
    final prompts = <String>[];
    final calls = <Object?>[];
    final researcher = goldenResearcher(repo, prompts: prompts, calls: calls);
    final events = <Object?>[];
    final summary = await researcher.researchArtists(
      [
        'Haris Alexiou',
        'Laiko Band',
        'Ghost Name',
        'Nomad',
        'Moose',
        'Kasabian',
      ],
      progress: (i, t, n, res) => events.add([i, t, n, _noTime(res)]),
      batchSize: 4,
    );
    final single = await researcher.researchArtist(
      'Slipknot',
      dispute: 'Tagged x, but the country is US.',
    );
    final cached = await researcher.researchArtist('Haris Alexiou');
    final forced = await researcher.researchArtist(
      'Haris Alexiou',
      force: true,
      mode: 'weights',
    );
    expect(_j(summary), r['summary']);
    expect(_j(events), r['events']);
    expect(prompts, r['prompts']);
    expect(_j(calls), r['calls']);
    expect(_j(_noTime(single)), r['single']);
    expect(_j(_noTime(cached)), r['cached']);
    expect(_j(_noTime(forced)), r['forced']);
    expectState(await dump(repo), r['state']);
    await repo.close();
  });
}
