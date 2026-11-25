// Phase 6a: utils/new_releases.py, utils/text_match.py, release DB queries,
// daily sync gate, and NewReleasesScheduler — verified against Python goldens
// (test/goldens/new_releases.json) and StreamripApp/tests/test_new_releases.py.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/text_match.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/data/release_queries.dart';
import 'package:mai_an_lab/search/local_search.dart';
import 'package:mai_an_lab/search/new_releases.dart';

String _daysAgo(DateTime today, int n) =>
    formatIsoDate(today.subtract(Duration(days: n)));

Future<LibraryRepository> _makeLibrary(Directory dir) async {
  final repo = await LibraryRepository.open(
    '${dir.path}/library.db',
    writable: true,
  );
  await repo.debugExecute([
    (
      "INSERT INTO artists (id, name) VALUES "
          "(1, 'Burial'), (2, 'Mad Clip'), "
          "(3, 'Billy Sio, Mad Clip'), (4, 'Various Artists')",
      const [],
    ),
    (
      "INSERT INTO albums (id, artist_id, title) VALUES "
          "(1, 1, 'Untrue'), (2, 2, 'Ksexwrista'), "
          "(3, 3, 'Duet'), (4, 4, 'Hits')",
      const [],
    ),
    (
      "INSERT INTO tracks (album_id, title, path) VALUES "
          "(1, 'Song 1', '/m/1.flac'), "
          "(1, 'Song 2', '/m/2.flac'), "
          "(2, 'Song 3', '/m/3.flac'), "
          "(3, 'Song 4', '/m/4.flac'), "
          "(4, 'Song 5', '/m/5.flac')",
      const [],
    ),
  ]);
  return repo;
}

void main() {
  final golden =
      jsonDecode(File('test/goldens/new_releases.json').readAsStringSync())
          as Map<String, dynamic>;

  group('Python golden parity', () {
    test('normReleaseText matches new_releases.norm', () {
      final cases = golden['norm'] as Map<String, dynamic>;
      cases.forEach((input, expected) {
        expect(normReleaseText(input), expected, reason: 'norm($input)');
      });
    });

    test('isLookupCandidate matches new_releases.is_lookup_candidate', () {
      final cases = golden['is_lookup_candidate'] as Map<String, dynamic>;
      cases.forEach((input, expected) {
        expect(isLookupCandidate(input), expected, reason: 'cand($input)');
      });
    });

    test('levenshteinDistance matches text_match.levenshtein_distance', () {
      final cases = (golden['levenshtein'] as List)
          .cast<Map<String, dynamic>>();
      for (final c in cases) {
        expect(
          levenshteinDistance(c['a'] as String, c['b'] as String),
          c['d'],
          reason: '${c['a']} vs ${c['b']}',
        );
      }
    });

    test('stripMarkup matches ui/widgets.py strip_markup', () {
      final cases = golden['strip_markup'] as Map<String, dynamic>;
      cases.forEach((input, expected) {
        expect(stripMarkup(input), expected, reason: 'strip($input)');
      });
    });
  });

  group('releaseDateLabel', () {
    final today = DateTime(2026, 11, 24);
    test('formats relative and calendar dates naturally', () {
      expect(releaseDateLabel('2026-11-24', today: today), 'Today');
      expect(releaseDateLabel('2026-11-25', today: today), 'Today');
      expect(releaseDateLabel('2026-11-23', today: today), 'Yesterday');
      expect(releaseDateLabel('2026-11-21', today: today), '3 days ago');
      expect(releaseDateLabel('2026-10-12', today: today), '12 Oct');
      expect(releaseDateLabel('2019-03-07', today: today), '7 Mar 2019');
      expect(releaseDateLabel('invalid', today: today), 'invalid');
    });
  });

  group('ReleaseFetcher (Deezer & MusicBrainz)', () {
    final today = DateTime(2026, 11, 24);

    ReleaseFetcher makeFetcher(
      Map<String, Object?> routes,
      List<String> calls, {
      Map<String, List<ReleaseHttpResponse?>>? scripted,
    }) {
      return ReleaseFetcher(
        deezerInterval: Duration.zero,
        deezerQuotaBackoff: Duration.zero,
        mbInterval: Duration.zero,
        mbBackoffUnit: Duration.zero,
        clock: () => today,
        httpGet: (uri, {headers = const {}, timeout = Duration.zero}) async {
          final key = uri.path.replaceFirst(RegExp(r'^/(ws/2/)?'), '');
          calls.add(key);
          if (scripted != null && scripted.containsKey(key)) {
            final q = scripted[key]!;
            return q.isEmpty ? null : q.removeAt(0);
          }
          if (!routes.containsKey(key)) return null;
          final val = routes[key];
          if (val == null) return null;
          if (val is ReleaseHttpResponse) return val;
          return ReleaseHttpResponse(200, jsonEncode(val));
        },
      );
    }

    test('picks the namesake whose discography overlaps the library', () async {
      final calls = <String>[];
      final fetcher = makeFetcher({
        'search/artist': {
          'data': [
            {'id': 1, 'name': 'Burial'},
            {'id': 2, 'name': 'BURIAL'},
          ],
        },
        'artist/1/albums': {
          'data': [
            {
              'title': 'Rot In Hell',
              'release_date': _daysAgo(today, 3),
              'record_type': 'album',
            },
          ],
        },
        'artist/2/albums': {
          'data': [
            {'title': 'Untrue', 'release_date': '2007-11-05'},
            {
              'title': 'Dreamfear',
              'release_date': _daysAgo(today, 10),
              'record_type': 'ep',
              'cover_medium': 'https://img/c.jpg',
            },
            {
              'title': 'Best Of',
              'release_date': _daysAgo(today, 5),
              'record_type': 'compile',
            },
          ],
        },
      }, calls);

      final res = await fetcher.deezerReleases('Burial', {'untrue'});
      expect(res.status, 'ok');
      expect(res.sourceId, '2');
      expect([for (final r in res.releases!) r.title], ['Dreamfear']);
      expect(res.releases!.first.cover, 'https://img/c.jpg');
    });

    test('no overlap means notfound rather than a guess', () async {
      final calls = <String>[];
      final fetcher = makeFetcher({
        'search/artist': {
          'data': [
            {'id': 1, 'name': 'LEX'},
          ],
        },
        'artist/1/albums': {
          'data': [
            {'title': 'Other', 'release_date': _daysAgo(today, 1)},
          ],
        },
      }, calls);

      final res = await fetcher.deezerReleases('LEX', {'2xxx'});
      expect(res.status, 'notfound');
      expect(res.sourceId, isNull);
      expect(res.releases, isEmpty);
    });

    test('ambiguous name with nothing owned is skipped', () async {
      final calls = <String>[];
      final fetcher = makeFetcher({
        'search/artist': {
          'data': [
            {'id': 1, 'name': 'Strat'},
            {'id': 2, 'name': 'Strat'},
          ],
        },
      }, calls);

      final res = await fetcher.deezerReleases('Strat', const {});
      expect(res.status, 'notfound');
      expect(calls, ['search/artist']);
    });

    test('known Deezer ID skips the artist search', () async {
      final calls = <String>[];
      final fetcher = makeFetcher({
        'artist/9/albums': {'data': <Object?>[]},
      }, calls);

      final res = await fetcher.deezerReleases('Burial', {
        'untrue',
      }, knownId: '9');
      expect(res.status, 'ok');
      expect(res.sourceId, '9');
      expect(calls, ['artist/9/albums']);
    });

    test('Deezer quota code 4 backs off once and retries', () async {
      final calls = <String>[];
      final fetcher = makeFetcher(
        const {},
        calls,
        scripted: {
          'artist/9/albums': [
            ReleaseHttpResponse(
              200,
              jsonEncode({
                'error': {'code': 4, 'message': 'Quota limit exceeded'},
              }),
            ),
            ReleaseHttpResponse(
              200,
              jsonEncode({
                'data': [
                  {
                    'title': 'Dreamfear',
                    'release_date': _daysAgo(today, 2),
                    'record_type': 'ep',
                  },
                ],
              }),
            ),
          ],
        },
      );

      final res = await fetcher.deezerReleases(
        'Burial',
        const {},
        knownId: '9',
      );
      expect(res.status, 'ok');
      expect(res.releases!.map((r) => r.title), ['Dreamfear']);
      expect(calls.length, 2);
    });

    test('network failure is an error, not a miss', () async {
      final fetcher = makeFetcher(const {}, []);
      final res = await fetcher.deezerReleases('Burial', {'untrue'});
      expect(res.status, 'error');
      expect(res.releases, isNull);
    });

    test(
      'MusicBrainz filters secondary types and builds CAA cover URL',
      () async {
        final calls = <String>[];
        final fetcher = makeFetcher({
          'release-group': {
            'release-groups': [
              {
                'id': 'rg-1',
                'title': 'New LP',
                'first-release-date': _daysAgo(today, 15),
                'primary-type': 'Album',
                'secondary-types': <Object?>[],
              },
              {
                'id': 'rg-2',
                'title': 'Live in Athens',
                'first-release-date': _daysAgo(today, 5),
                'primary-type': 'Album',
                'secondary-types': ['Live'],
              },
            ],
          },
        }, calls);

        final res = await fetcher.musicBrainzReleases('mb-1');
        expect(res.status, 'ok');
        expect(res.sourceId, 'mb-1');
        expect(res.releases!.length, 1);
        expect(res.releases!.first.title, 'New LP');
        expect(res.releases!.first.type, 'album');
        expect(
          res.releases!.first.cover,
          'https://coverartarchive.org/release-group/rg-1/front-250',
        );
      },
    );

    test('MusicBrainz 404 is notfound and 503 retries', () async {
      final calls = <String>[];
      final notFoundFetcher = makeFetcher({
        'release-group': const ReleaseHttpResponse(404, '{}'),
      }, calls);
      expect(
        (await notFoundFetcher.musicBrainzReleases('mb-1')).status,
        'notfound',
      );

      final retryCalls = <String>[];
      final retryFetcher = makeFetcher(
        const {},
        retryCalls,
        scripted: {
          'release-group': [
            const ReleaseHttpResponse(503, ''),
            ReleaseHttpResponse(
              200,
              jsonEncode({'release-groups': <Object?>[]}),
            ),
          ],
        },
      );
      expect((await retryFetcher.musicBrainzReleases('mb-2')).status, 'ok');
      expect(retryCalls.length, 2);
    });
  });

  group('refreshNewReleases, getNewReleases & daily sync gate', () {
    late Directory dir;
    late LibraryRepository repo;
    final today = DateTime(2026, 11, 24, 14, 30);

    setUp(() async {
      dir = Directory.systemTemp.createTempSync('mai_an_releases');
      repo = await _makeLibrary(dir);
    });

    tearDown(() async {
      await repo.close();
      dir.deleteSync(recursive: true);
    });

    test('only solo artists are looked up and then cached', () async {
      final lookedUp = <String>[];
      final fetcher = _StubFetcher(
        onDeezer: (name, owned, knownId) async {
          lookedUp.add(name);
          return const ReleaseFetchResult(
            status: 'ok',
            sourceId: '1',
            releases: [],
          );
        },
      );
      final written1 = await refreshNewReleases(
        repo,
        fetcher: fetcher,
        cooperativeDelay: Duration.zero,
        clock: () => today,
      );
      expect(lookedUp..sort(), ['Burial', 'Mad Clip']);
      expect(written1, 2);

      lookedUp.clear();
      final written2 = await refreshNewReleases(
        repo,
        fetcher: fetcher,
        cooperativeDelay: Duration.zero,
        clock: () => today.add(const Duration(hours: 2)),
      );
      expect(written2, 0);
      expect(lookedUp, isEmpty);
    });

    test(
      'network errors are not cached and abort after 3 consecutive errors',
      () async {
        await repo.debugExecute([
          (
            "INSERT INTO artists (id, name) VALUES (5, 'A1'), (6, 'A2'), (7, 'A3')",
            const [],
          ),
          (
            "INSERT INTO albums (id, artist_id, title) VALUES (5, 5, 'X'), (6, 6, 'Y'), (7, 7, 'Z')",
            const [],
          ),
          (
            "INSERT INTO tracks (album_id, title, path) VALUES "
                "(5, 'T5', '/m/5a.flac'), (6, 'T6', '/m/6a.flac'), (7, 'T7', '/m/7a.flac')",
            const [],
          ),
        ]);
        var attempts = 0;
        final fetcher = _StubFetcher(
          onDeezer: (_, _, _) async {
            attempts++;
            return const ReleaseFetchResult(status: 'error');
          },
        );
        final written = await refreshNewReleases(
          repo,
          fetcher: fetcher,
          cooperativeDelay: Duration.zero,
          clock: () => today,
        );
        expect(written, 0);
        expect(attempts, 3);
        expect(await repo.artistReleaseCache(), isEmpty);
        final sync = await repo.latestReleaseSync();
        expect(sync?.status, 'interrupted');
        expect(sync?.artistsChecked, 3);
      },
    );

    test('MusicBrainz covers artists Deezer cannot identify', () async {
      await repo.debugExecute([
        (
          "INSERT INTO artist_enrichment (artist_name, mbid, status) "
              "VALUES ('Mad Clip', 'mb-1', 'ok')",
          const [],
        ),
      ]);
      final fetcher = _StubFetcher(
        onDeezer: (_, _, _) async =>
            const ReleaseFetchResult(status: 'notfound', releases: []),
        onMb: (mbid) async => ReleaseFetchResult(
          status: 'ok',
          sourceId: mbid,
          releases: [
            ReleaseItem(
              title: 'Νέο',
              date: _daysAgo(today, 5),
              type: 'album',
              cover: '',
              source: 'musicbrainz',
            ),
          ],
        ),
      );
      await refreshNewReleases(
        repo,
        fetcher: fetcher,
        cooperativeDelay: Duration.zero,
        clock: () => today,
      );
      final cache = await repo.artistReleaseCache();
      expect(cache['mad clip']?.source, 'musicbrainz');
      expect(cache['burial']?.source, 'none');
    });

    test(
      'getNewReleases applies ownership, date, variant, and per-artist rules',
      () async {
        await repo.saveArtistReleases('Burial', 'deezer', '1', [
          ReleaseItem(
            title: 'Untrue (Deluxe)',
            date: _daysAgo(today, 2),
            type: 'album',
            cover: '',
            source: 'deezer',
          ), // owned
          ReleaseItem(
            title: 'Dreamfear',
            date: _daysAgo(today, 20),
            type: 'ep',
            cover: '',
            source: 'deezer',
          ),
          ReleaseItem(
            title: 'Antidawn',
            date: _daysAgo(today, 200),
            type: 'album',
            cover: '',
            source: 'deezer',
          ),
          ReleaseItem(
            title: 'Old Single',
            date: _daysAgo(today, 100),
            type: 'single',
            cover: '',
            source: 'deezer',
          ), // stale single
          ReleaseItem(
            title: 'Shock (Remix)',
            date: _daysAgo(today, 1),
            type: 'single',
            cover: '',
            source: 'deezer',
          ), // variant
          ReleaseItem(
            title: 'Future',
            date: formatIsoDate(today.add(const Duration(days: 9))),
            type: 'album',
            cover: '',
            source: 'deezer',
          ), // unreleased
        ]);
        await repo.saveArtistReleases('Mad Clip', 'deezer', '2', [
          ReleaseItem(
            title: 'One',
            date: _daysAgo(today, 1),
            type: 'single',
            cover: '',
            source: 'deezer',
          ),
          ReleaseItem(
            title: 'Two',
            date: _daysAgo(today, 2),
            type: 'single',
            cover: '',
            source: 'deezer',
          ),
          ReleaseItem(
            title: 'Three',
            date: _daysAgo(today, 3),
            type: 'single',
            cover: '',
            source: 'deezer',
          ),
        ]);

        final got = await getNewReleases(repo, today: today);
        expect(
          [for (final r in got) (r.artist, r.title)],
          [
            ('Mad Clip', 'One'),
            ('Mad Clip', 'Two'),
            ('Burial', 'Dreamfear'),
            ('Burial', 'Antidawn'),
          ],
        );
      },
    );

    test('album beats single of same normalized title', () async {
      await repo.saveArtistReleases('Burial', 'deezer', '1', [
        ReleaseItem(
          title: 'Rival Dealer',
          date: _daysAgo(today, 4),
          type: 'single',
          cover: 's.jpg',
          source: 'deezer',
        ),
        ReleaseItem(
          title: 'Rival Dealer (Deluxe)',
          date: _daysAgo(today, 3),
          type: 'album',
          cover: 'a.jpg',
          source: 'deezer',
        ),
      ]);
      final got = await getNewReleases(repo, today: today);
      expect(got.length, 1);
      expect(got.first.type, 'album');
      expect(got.first.cover, 'a.jpg');
    });

    test('daily release sync ledger and cooldown/skew gates', () async {
      var now = DateTime(2026, 11, 24, 10, 0);
      expect(await repo.shouldRunDailyReleaseSync(now: now), isTrue);

      final logId = await repo.logReleaseSyncStart(
        triggerType: 'daily_auto',
        now: now,
      );
      final running = await repo.latestReleaseSync();
      expect(running?.status, 'running');
      expect(running?.triggerType, 'daily_auto');

      // Running < 1h ago -> gate returns false.
      expect(
        await repo.shouldRunDailyReleaseSync(
          now: now.add(const Duration(minutes: 30)),
        ),
        isFalse,
      );
      // Stale running > 1h ago -> gate returns true.
      expect(
        await repo.shouldRunDailyReleaseSync(
          now: now.add(const Duration(hours: 2)),
        ),
        isTrue,
      );

      // Finish sync today.
      await repo.logReleaseSyncFinish(
        logId,
        artistsChecked: 10,
        newFound: 2,
        status: 'completed',
        now: now,
      );
      final done = await repo.latestReleaseSync();
      expect(done?.status, 'completed');
      expect(done?.artistsChecked, 10);
      expect(done?.newFound, 2);

      // Same day -> false.
      expect(
        await repo.shouldRunDailyReleaseSync(
          now: now.add(const Duration(hours: 13)),
        ),
        isFalse,
      );

      // Next day but < 12h elapsed (midnight crossing) -> false.
      final lateNight = DateTime(2026, 11, 24, 23, 0);
      await repo.logReleaseSyncFinish(
        logId,
        status: 'completed',
        now: lateNight,
      );
      expect(
        await repo.shouldRunDailyReleaseSync(now: DateTime(2026, 11, 25, 1, 0)),
        isFalse,
      );

      // Next day and >= 12h elapsed -> true.
      expect(
        await repo.shouldRunDailyReleaseSync(
          now: DateTime(2026, 11, 25, 12, 0),
        ),
        isTrue,
      );

      // Clock rolled backward (> 1h before finished_at) -> true.
      expect(
        await repo.shouldRunDailyReleaseSync(
          now: DateTime(2026, 11, 24, 20, 0),
        ),
        isTrue,
      );
    });
  });

  group('NewReleasesScheduler', () {
    late Directory dir;
    late LibraryRepository repo;

    setUp(() async {
      dir = Directory.systemTemp.createTempSync('mai_an_sched');
      repo = await _makeLibrary(dir);
    });

    tearDown(() async {
      await repo.close();
      dir.deleteSync(recursive: true);
    });

    test(
      'manual refresh claims running synchronously and ignores double tap',
      () async {
        String? seenTrigger;
        double? seenRefreshAfter;
        var runs = 0;
        var refreshedWith = -1;
        final scheduler = NewReleasesScheduler(
          repository: () => repo,
          isEnabled: () => true,
          runner: (r, {required refreshAfterS, required triggerType}) async {
            runs++;
            seenTrigger = triggerType;
            seenRefreshAfter = refreshAfterS;
            return 7;
          },
        )..onRefreshed = (w) => refreshedWith = w;

        scheduler.triggerManualRefresh();
        expect(scheduler.isRefreshing, isTrue);
        scheduler.triggerManualRefresh(); // double tap ignored

        await Future<void>.delayed(const Duration(milliseconds: 30));
        expect(runs, 1);
        expect(seenTrigger, 'manual');
        expect(seenRefreshAfter, 0.0);
        expect(scheduler.isRefreshing, isFalse);
        expect(scheduler.isPending, isFalse);
        expect(refreshedWith, 7);
        scheduler.dispose();
      },
    );

    test('daily gate skip clears pending and does not notify', () async {
      final now = DateTime(2026, 11, 24, 12);
      final id = await repo.logReleaseSyncStart(now: now);
      await repo.logReleaseSyncFinish(id, status: 'completed', now: now);

      var notified = false;
      var runs = 0;
      final scheduler = NewReleasesScheduler(
        repository: () => repo,
        isEnabled: () => true,
        clock: () => now.add(const Duration(minutes: 5)),
        runner: (_, {required refreshAfterS, required triggerType}) async {
          runs++;
          return 1;
        },
      )..onRefreshed = (_) => notified = true;

      scheduler.ensureRefresh();
      await Future<void>.delayed(const Duration(milliseconds: 30));
      expect(runs, 0);
      expect(scheduler.isPending, isFalse);
      expect(notified, isFalse);
      scheduler.dispose();
    });

    test('maybeRecheck waits for boot check then runs hourly', () async {
      var now = DateTime(2026, 11, 24, 12);
      final id = await repo.logReleaseSyncStart(now: now);
      await repo.logReleaseSyncFinish(id, status: 'completed', now: now);

      var runs = 0;
      final scheduler = NewReleasesScheduler(
        repository: () => repo,
        isEnabled: () => true,
        clock: () => now,
        runner: (_, {required refreshAfterS, required triggerType}) async {
          runs++;
          return 1;
        },
      );

      // Before boot check: no-op.
      scheduler.maybeRecheck();
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(runs, 0);

      // Boot check records timestamp (gate skips today).
      scheduler.ensureRefresh();
      await Future<void>.delayed(const Duration(milliseconds: 30));
      expect(runs, 0);

      // Advance to next day but only 30 min after gate check -> still no-op.
      now = now.add(const Duration(minutes: 30));
      scheduler.maybeRecheck();
      await Future<void>.delayed(const Duration(milliseconds: 30));
      expect(runs, 0);

      // Advance past 1 hour AND next day (>= 12h) -> runs!
      now = DateTime(2026, 11, 25, 13);
      scheduler.maybeRecheck();
      await Future<void>.delayed(const Duration(milliseconds: 50));
      expect(runs, 1);
      scheduler.dispose();
    });
  });
}

class _StubFetcher extends ReleaseFetcher {
  _StubFetcher({required this.onDeezer, this.onMb});

  final Future<ReleaseFetchResult> Function(
    String name,
    Set<String> ownedTitles,
    String? knownId,
  )
  onDeezer;
  final Future<ReleaseFetchResult> Function(String mbid)? onMb;

  @override
  Future<ReleaseFetchResult> deezerReleases(
    String name,
    Set<String> ownedTitles, {
    String? knownId,
  }) => onDeezer(name, ownedTitles, knownId);

  @override
  Future<ReleaseFetchResult> musicBrainzReleases(String mbid) =>
      onMb?.call(mbid) ??
      Future.value(const ReleaseFetchResult(status: 'notfound', releases: []));
}
