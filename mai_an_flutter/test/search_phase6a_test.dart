// Phase 6a: SearchHistoryStore, CachePruner, ownership badges, local-library
// hits, URL Artwork, and the SearchScreen New From Your Artists shelf &
// recent searches sheet.
import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/cache_pruner.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/data/release_queries.dart';
import 'package:mai_an_lab/library/browse_controller.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/player/ui/artwork.dart';
import 'package:mai_an_lab/search/landing_controller.dart';
import 'package:mai_an_lab/search/local_search.dart';
import 'package:mai_an_lab/search/new_releases.dart';
import 'package:mai_an_lab/search/search_history.dart';
import 'package:mai_an_lab/search/search_screen.dart';
import 'package:mai_an_lab/ui/tokens.dart';

const _setup = [
  "INSERT INTO artists (id, name) VALUES (1, 'Burial'), (2, 'Boards of Canada')",
  "INSERT INTO albums (id, artist_id, title, year) VALUES "
      "(1, 1, 'Untrue', '2007'), (2, 2, 'Geogaddi', '2002')",
  "INSERT INTO tracks (album_id, title, track_num, path, added_date, duration) VALUES "
      "(1, 'Archangel', 1, '/music/1.flac', 1, 200), "
      "(1, 'Near Dark', 2, '/music/2.flac', 2, 200), "
      "(1, 'Ghost Hardware', 3, '/music/3.flac', 3, 200), "
      "(2, 'Music Is Math', 1, '/music/4.flac', 4, 200)",
];

void main() {
  group('SearchHistoryStore', () {
    late Directory dir;
    late SearchHistoryStore store;

    setUp(() {
      dir = Directory.systemTemp.createTempSync('mai_an_hist');
      store = SearchHistoryStore(dir.path);
    });
    tearDown(() => dir.deleteSync(recursive: true));

    test(
      'deduplicates, moves to front, caps at 10, and handles corrupt JSON',
      () async {
        expect(await store.load(), isEmpty);
        for (var i = 1; i <= 12; i++) {
          await store.add('query $i');
        }
        final list = await store.load();
        expect(list.length, 10);
        expect(list.first, 'query 12');
        expect(list.last, 'query 3');

        await store.add('query 5');
        final moved = await store.load();
        expect(moved.length, 10);
        expect(moved.first, 'query 5');
        expect(moved.where((q) => q == 'query 5').length, 1);

        // 4-space indented JSON matching Python json.dump(..., indent=4).
        final raw = await store.file.readAsString();
        expect(raw, contains('    "query 5"'));

        // Corrupt JSON returns empty rather than throwing.
        await store.file.writeAsString('{broken');
        expect(await store.load(), isEmpty);
      },
    );
  });

  group('CachePruner', () {
    late Directory dir;

    setUp(() {
      dir = Directory.systemTemp.createTempSync('mai_an_prune');
    });
    tearDown(() => dir.deleteSync(recursive: true));

    test(
      'prunes artwork > 100 keeping newest + .nomedia, prunes previews > 24h, respects 6h cooldown',
      () async {
        final now = DateTime.now().add(const Duration(hours: 30));
        final temp = Directory('${dir.path}/temp')..createSync();
        final nomedia = File('${temp.path}/.nomedia')..writeAsStringSync('');
        nomedia.setLastModifiedSync(now.subtract(const Duration(days: 30)));

        for (var i = 0; i < 105; i++) {
          final f = File('${temp.path}/art_$i.jpg')..writeAsStringSync('x');
          f.setLastModifiedSync(now.subtract(Duration(minutes: 200 - i)));
        }

        final previews = Directory('${dir.path}/previews')..createSync();
        final oldPreview = File('${previews.path}/old.mp3')
          ..writeAsStringSync('a');
        oldPreview.setLastModifiedSync(now.subtract(const Duration(hours: 25)));
        final newPreview = File('${previews.path}/new.mp3')
          ..writeAsStringSync('b');
        newPreview.setLastModifiedSync(now.subtract(const Duration(hours: 2)));
        // Created at wall-clock `DateTime.now()`, which is 30h before `now`.
        final oldDir = Directory('${previews.path}/old_dir')..createSync();
        File('${oldDir.path}/x.mp3').writeAsStringSync('c');

        var clock = now;
        final pruner = CachePruner(dir.path, clock: () => clock);

        expect(await pruner.pruneIfDue(), isTrue);
        // Cooldown prevents immediate rerun.
        clock = now.add(const Duration(hours: 5));
        expect(await pruner.pruneIfDue(), isFalse);
        // After 6h cooldown it runs again.
        clock = now.add(const Duration(hours: 6));
        expect(await pruner.pruneIfDue(), isTrue);

        final remainingArt = temp
            .listSync()
            .map((e) => e.uri.pathSegments.last)
            .toSet();
        expect(remainingArt.contains('.nomedia'), isTrue);
        expect(remainingArt.length, 101); // 100 art + .nomedia
        expect(remainingArt.contains('art_0.jpg'), isFalse);
        expect(remainingArt.contains('art_4.jpg'), isFalse);
        expect(remainingArt.contains('art_5.jpg'), isTrue);
        expect(remainingArt.contains('art_104.jpg'), isTrue);

        expect(oldPreview.existsSync(), isFalse);
        expect(oldDir.existsSync(), isFalse);
        expect(newPreview.existsSync(), isTrue);
      },
    );
  });

  group('Ownership annotation & local matches', () {
    late Directory dir;
    late LibraryRepository repo;

    setUp(() async {
      dir = Directory.systemTemp.createTempSync('mai_an_own');
      repo = await LibraryRepository.open(
        '${dir.path}/library.db',
        writable: true,
      );
      await repo.debugExecute([for (final s in _setup) (s, const [])]);
    });

    tearDown(() async {
      await repo.close();
      dir.deleteSync(recursive: true);
    });

    test('album, artist, and track ownership badges match Flet', () async {
      final partial = await annotateOwnership(
        repo,
        mediaType: 'album',
        title: '[b]Untrue[/b]',
        artist: 'Burial',
        numTracks: 13,
      );
      final complete = await annotateOwnership(
        repo,
        mediaType: 'album',
        title: 'Untrue',
        artist: 'burial',
        numTracks: 3,
      );
      final absent = await annotateOwnership(
        repo,
        mediaType: 'album',
        title: 'Kindred',
        artist: 'Burial',
        numTracks: 3,
      );
      final artist = await annotateOwnership(
        repo,
        mediaType: 'artist',
        title: 'Burial',
        artist: '12 Albums',
      );
      final singleArtist = await annotateOwnership(
        repo,
        mediaType: 'artist',
        title: 'Boards of Canada',
        artist: '',
      );
      final track = await annotateOwnership(
        repo,
        mediaType: 'track',
        title: 'Archangel',
        artist: 'Burial',
      );

      expect((partial.ownedTracks, partial.isInLibrary), (3, false));
      expect(partial.badgeLabel, 'YOU HAVE 3/13');
      expect((complete.ownedTracks, complete.isInLibrary), (3, true));
      expect(complete.badgeLabel, 'IN LIBRARY');
      expect((absent.ownedTracks, absent.isInLibrary), (0, false));
      expect(absent.badgeLabel, isNull);
      expect(artist.badgeLabel, '3 TRACKS OWNED');
      expect(singleArtist.badgeLabel, '1 TRACK OWNED');
      expect(track.isInLibrary, isTrue);
      expect(track.badgeLabel, 'IN LIBRARY');
    });

    test(
      'loadLocalMatches skips closest-match fallback and ranks exact/prefix first',
      () async {
        final hit = await loadLocalMatches(repo, 'burial');
        expect(hit.tracks.length, 3);
        expect(hit.albums.map((a) => a.title), ['Untrue']);
        expect(hit.artists.map((a) => a.name), ['Burial']);

        // Unrelated query returns nothing (no fuzzy fallback).
        final miss = await loadLocalMatches(repo, 'zzzz unrelated');
        expect(miss.isEmpty, isTrue);
      },
    );
  });

  group('Artwork URL detection', () {
    test('recognizes http and https URLs', () {
      expect(Artwork.isNetworkUrl('https://cdn.example/cover.jpg'), isTrue);
      expect(Artwork.isNetworkUrl('http://cdn.example/cover.jpg'), isTrue);
      expect(Artwork.isNetworkUrl('/storage/emulated/0/art.jpg'), isFalse);
    });
  });

  group('SearchScreen Phase 6a UI', () {
    late Directory dir;
    late String dbPath;

    setUp(() async {
      dir = Directory.systemTemp.createTempSync('mai_an_search_ui');
      dbPath = '${dir.path}/library.db';
      final repo = await LibraryRepository.open(dbPath, writable: true);
      await repo.debugExecute([for (final s in _setup) (s, const [])]);
      final today = DateTime.now();
      await repo.saveArtistReleases('Burial', 'deezer', '1', [
        for (var i = 1; i <= 2; i++)
          ReleaseItem(
            title: 'New EP $i',
            date: formatIsoDate(today.subtract(Duration(days: i))),
            type: 'ep',
            cover: '',
            source: 'deezer',
          ),
      ]);
      await repo.saveArtistReleases('Boards of Canada', 'deezer', '2', [
        ReleaseItem(
          title: 'Fresh Single',
          date: formatIsoDate(today.subtract(const Duration(days: 2))),
          type: 'single',
          cover: '',
          source: 'deezer',
        ),
        ReleaseItem(
          title: 'New LP',
          date: formatIsoDate(today.subtract(const Duration(days: 3))),
          type: 'album',
          cover: '',
          source: 'deezer',
        ),
      ]);
      await repo.close();
    });

    tearDown(() => dir.deleteSync(recursive: true));

    testWidgets(
      'shows New From Your Artists shelves, manual refresh, recent searches, and local hits',
      (tester) async {
        tester.view.physicalSize = const Size(420 * 2, 900 * 2);
        tester.view.devicePixelRatio = 2;
        addTearDown(tester.view.reset);

        final lib = LibraryController(
          () => LibraryRepository.open(dbPath, writable: true),
        );
        await tester.runAsync(lib.load);

        final refreshGate = Completer<int>();
        var manualTriggered = 0;
        final scheduler = NewReleasesScheduler(
          repository: () => lib.repository,
          isEnabled: () => true,
          runner: (_, {required refreshAfterS, required triggerType}) {
            manualTriggered++;
            return refreshGate.future;
          },
        );
        final landing = LandingController(lib, releasesScheduler: scheduler);
        final history = SearchHistoryStore(dir.path);
        await tester.runAsync(() => history.add('burial'));

        final played = <String>[];
        final opened = <(LibraryView, String)>[];

        await tester.pumpWidget(
          MaterialApp(
            theme: Tokens.theme(),
            home: Scaffold(
              body: SearchScreen(
                landing: landing,
                searchHistory: history,
                onPlayTrack: played.add,
                onOpenInLibrary: (v, q) => opened.add((v, q)),
              ),
            ),
          ),
        );

        landing.onShow();
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 50)),
        );
        await tester.pumpAndSettle();

        // New From Your Artists shelf rendered with both Albums & EPs and Singles.
        expect(find.text('New From Your Artists'), findsOneWidget);
        expect(find.text('Albums & EPs'), findsOneWidget);
        expect(find.text('Singles'), findsOneWidget);
        expect(find.text('New EP 1'), findsOneWidget);
        expect(find.text('Fresh Single'), findsOneWidget);

        // Manual refresh button triggers scheduler and swaps to spinner while running.
        await tester.tap(find.byIcon(Icons.refresh_rounded));
        await tester.pump();
        expect(manualTriggered, 1);
        expect(find.byType(CircularProgressIndicator), findsOneWidget);
        refreshGate.complete(1);
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 50)),
        );
        await tester.pumpAndSettle();
        expect(find.byIcon(Icons.refresh_rounded), findsOneWidget);

        // Recent Searches sheet opens and runs local search on selection.
        await tester.tap(find.byIcon(Icons.history_rounded));
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 50)),
        );
        await tester.pumpAndSettle();
        expect(find.text('Recent Searches'), findsOneWidget);
        await tester.tap(find.text('burial'));
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 50)),
        );
        await tester.pumpAndSettle();

        // Local matches card ("In your library") shown with results.
        expect(find.text('In your library'), findsOneWidget);
        expect(find.text('Archangel'), findsOneWidget);
        expect(find.text('See all 5 in Library'), findsOneWidget);

        // Play button invokes callback.
        await tester.tap(find.byIcon(Icons.play_circle_outline_rounded).first);
        expect(played, ['/music/1.flac']);

        // Clear search returns to landing.
        await tester.tap(find.byIcon(Icons.close_rounded));
        await tester.pumpAndSettle();
        expect(find.text('New From Your Artists'), findsOneWidget);

        landing.dispose();
        scheduler.dispose();
        lib.dispose();
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 50)),
        );
      },
    );
  });
}
