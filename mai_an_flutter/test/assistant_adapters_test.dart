import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/assistant/adapters.dart';
import 'package:mai_an_lab/assistant/runner.dart';
import 'package:mai_an_lab/core/py_json.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/player/queue_store.dart';

import 'fake_backend.dart';

/// Phase 5 stage c2: [RepoAssistantLibrary] golden parity against Python's
/// `DatabaseManager` + `track_graph` (`tool/export_assistant_db_goldens.py`),
/// [ControllerAssistantPlayer] behaviour over [PlayerController], and an
/// end-to-end [AssistantRunner] run over both real adapters.
void main() {
  final golden =
      jsonDecode(File('test/goldens/assistant_db.json').readAsStringSync())
          as Map<String, Object?>;

  Future<LibraryRepository> buildFixtureRepo(Directory dir) async {
    final path = '${dir.path}/library.db';
    var repo = await LibraryRepository.open(path, writable: true);
    await repo.close();
    // Second open adds play_counts.cluster_id (as in Python).
    repo = await LibraryRepository.open(path, writable: true);
    final fx = golden['fixture']! as Map<String, Object?>;
    final stmts = <(String, List<Object?>)>[];
    for (final a in (fx['artists']! as List<Object?>).cast<List<Object?>>()) {
      stmts.add(('INSERT INTO artists (id, name) VALUES (?, ?)', [a[0], a[1]]));
    }
    for (final al in (fx['albums']! as List<Object?>).cast<List<Object?>>()) {
      stmts.add((
        'INSERT INTO albums (id, artist_id, title, year, genre) '
            'VALUES (?, ?, ?, ?, ?)',
        [al[0], al[1], al[2], al[3], al[4]],
      ));
    }
    for (final t in (fx['tracks']! as List<Object?>).cast<List<Object?>>()) {
      stmts.add((
        'INSERT INTO tracks (id, album_id, title, path, duration, format, '
            'added_date, bpm, energy, brightness) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        t,
      ));
    }
    for (final pc
        in (fx['play_counts']! as List<Object?>).cast<List<Object?>>()) {
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
    for (final n in (fx['neighbors']! as List<Object?>).cast<List<Object?>>()) {
      stmts.add((
        'INSERT INTO track_neighbors (track_path, neighbor_path, weight, '
            'edge_kind) VALUES (?, ?, ?, ?)',
        n,
      ));
    }
    final nowS = DateTime.now().millisecondsSinceEpoch ~/ 1000;
    for (final h in (fx['history']! as List<Object?>).cast<List<Object?>>()) {
      stmts.add((
        'INSERT INTO playback_history (track_path, played_at, event, '
            'seed_path) VALUES (?, ?, ?, ?)',
        [h[0], nowS - (h[1]! as int), h[2], h[3]],
      ));
    }
    await repo.debugExecute(stmts);
    return repo;
  }

  group('RepoAssistantLibrary parity against Python DatabaseManager', () {
    late Directory dir;
    late LibraryRepository repo;
    late RepoAssistantLibrary lib;

    setUp(() async {
      dir = Directory.systemTemp.createTempSync('mai_an_asst_db');
      repo = await buildFixtureRepo(dir);
      lib = RepoAssistantLibrary(repo);
    });

    tearDown(() async {
      await repo.close();
      dir.deleteSync(recursive: true);
    });

    test('searchTracksSimple matches Python on every query shape', () async {
      for (final c
          in (golden['search_tracks_simple']! as List<Object?>)
              .cast<Map<String, Object?>>()) {
        final q = c['query']! as String;
        final limit = c['limit']! as int;
        final got = await lib.searchTracksSimple(q, limit: limit);
        expect(
          _canon(got),
          _canon(c['result']),
          reason: 'query=${jsonEncode(q)} limit=$limit',
        );
      }
    });

    test('getAllTracks matches Python row for row', () async {
      final got = await lib.getAllTracks();
      expect(_canon(got), _canon(golden['get_all_tracks']));
    });

    test('getTrackFull matches Python including timbre bytes', () async {
      final want = golden['get_track_full']! as Map<String, Object?>;
      for (final e in want.entries) {
        final got = await lib.getTrackFull(e.key);
        expect(_canon(got), _canon(e.value), reason: e.key);
      }
    });

    test('hasCurrentFeatures matches Python', () async {
      final want = golden['has_current_features']! as Map<String, Object?>;
      for (final e in want.entries) {
        expect(await lib.hasCurrentFeatures(e.key), e.value, reason: e.key);
      }
    });

    test('getMostPlayed matches Python with full columns', () async {
      final want = golden['get_most_played']! as Map<String, Object?>;
      for (final e in want.entries) {
        final got = await lib.getMostPlayed(limit: int.parse(e.key));
        expect(_canon(got), _canon(e.value), reason: 'limit=${e.key}');
      }
    });

    test('recentPlayedPaths and recordPlayback', () async {
      final want = golden['recent_played_paths']! as Map<String, Object?>;
      for (final e in want.entries) {
        final got = (await lib.recentPlayedPaths(
          windowSeconds: int.parse(e.key),
        )).toList()..sort();
        expect(got, e.value, reason: 'window=${e.key}');
      }

      // recordPlayback writes a 'played' event and surfaces immediately; empty
      // path is a no-op.
      await lib.recordPlayback('', 'played');
      await lib.recordPlayback(
        '/m/svefn.flac',
        'played',
        seedPath: '/m/echoes_pf.flac',
      );
      final after = await lib.recentPlayedPaths(windowSeconds: 600);
      expect(after, contains('/m/svefn.flac'));
      expect(after, isNot(contains('')));
    });

    test('searchArtistNames matches get_all_artists(search_query)', () async {
      for (final c
          in (golden['search_artist_names']! as List<Object?>)
              .cast<Map<String, Object?>>()) {
        final q = c['query']! as String;
        expect(
          await lib.searchArtistNames(q),
          c['result'],
          reason: 'query=${jsonEncode(q)}',
        );
      }
    });

    test('firstTrackPathByArtist matches NOCASE lookup', () async {
      final want =
          golden['first_track_path_by_artist']! as Map<String, Object?>;
      for (final e in want.entries) {
        expect(await lib.firstTrackPathByArtist(e.key), e.value, reason: e.key);
      }
    });

    test('artistNeighbors restricts to artist tier and respects k', () async {
      final want = golden['artist_neighbors']! as Map<String, Object?>;
      for (final e in want.entries) {
        expect(
          await lib.artistNeighbors('/m/echoes_pf.flac', k: int.parse(e.key)),
          e.value,
          reason: 'k=${e.key}',
        );
      }
    });

    test('walk matches Python track_graph.walk', () async {
      final want = golden['walk']! as Map<String, Object?>;
      expect(
        await lib.walk(
          '/m/echoes_pf.flac',
          length: 5,
          avoid: {'/m/echoes_pf.flac'},
        ),
        want['plain'],
      );
      expect(
        await lib.walk(
          '/m/echoes_pf.flac',
          length: 5,
          avoid: {'/m/echoes_pf.flac', '/m/paranoid.flac'},
        ),
        want['avoid'],
      );
    });

    test('createPlaylist and addTrackToPlaylist persist to library', () async {
      final pid = await lib.createPlaylist('Jarvis Walk');
      await lib.addTrackToPlaylist(pid, '/m/echoes_pf.flac');
      await lib.addTrackToPlaylist(pid, '/m/time_pf.flac');
      final rows = await repo.tracksInPlaylist(pid);
      expect(
        [for (final r in rows) r.path],
        ['/m/echoes_pf.flac', '/m/time_pf.flac'],
      );
    });
  });

  group('ControllerAssistantPlayer and end-to-end AssistantRunner', () {
    late Directory dir;
    late FakeBackend backend;
    late LibraryRepository repo;
    late PlayerController controller;
    late ControllerAssistantPlayer player;
    late RepoAssistantLibrary lib;
    late AssistantRunner runner;

    setUp(() async {
      dir = Directory.systemTemp.createTempSync('mai_an_asst_e2e');
      repo = await buildFixtureRepo(dir);
      backend = FakeBackend();
      controller = PlayerController(
        backend: backend,
        store: QueueStore(dir.path),
        prefs: Prefs(dir.path),
        libraryTracks: () => repo.allTracks(),
      );
      await controller.init();
      player = ControllerAssistantPlayer(controller);
      lib = RepoAssistantLibrary(repo);
      runner = AssistantRunner(
        library: lib,
        player: player,
        now: () => DateTime.utc(2026, 11, 24, 12, 0, 0),
      );
    });

    tearDown(() async {
      await controller.flush();
      controller.dispose();
      await repo.close();
      dir.deleteSync(recursive: true);
    });

    test('isShuffle switches order in place without swapping queue', () async {
      final resp = await runner.dispatchText('play Paranoid Android');
      expect(resp.success, isTrue);
      await controller.settled;
      expect(player.queue.length, 1);
      expect(player.currentPath, '/m/paranoid.flac');
      expect(player.isPlaying, isTrue);

      // Toggling shuffle via the runner flips shuffle order in place (keeps
      // the 1-track queue rather than swapping in the whole library).
      final shufOn = await runner.dispatchText('shuffle');
      expect(shufOn.displayed, 'Shuffle on.');
      await controller.settled;
      expect(player.isShuffle, isTrue);
      expect(backend.shuffleOn, isTrue);
      expect(player.queue.length, 1);

      final shufOff = await runner.dispatchText('shuffle');
      expect(shufOff.displayed, 'Shuffle off.');
      await controller.settled;
      expect(player.isShuffle, isFalse);
      expect(backend.shuffleOn, isFalse);
    });

    test(
      'end-to-end play, queue, move, remove, save, similar, stop, track info',
      () async {
        // Play single track via "by" split
        var r = await runner.dispatchText('play echoes by pink floyd');
        expect(r.success, isTrue);
        await controller.settled;
        expect(player.currentPath, '/m/echoes_pf.flac');
        expect(player.currentTrack, 'Echoes');
        expect(player.currentArtist, 'Pink Floyd');

        // Queue next and queue last
        r = await runner.dispatchText('add Time to queue');
        expect(r.success, isTrue);
        r = await runner.dispatchText('play Money next');
        expect(r.success, isTrue);
        await controller.settled;
        expect(
          [for (final q in player.queue) q['path']],
          ['/m/echoes_pf.flac', '/m/money_pf.flac', '/m/time_pf.flac'],
        );

        // Move Time (track 3) to play next (#2)
        r = await runner.dispatchText('move Time to top');
        expect(r.success, isTrue);
        await controller.settled;
        expect(
          [for (final q in player.queue) q['path']],
          ['/m/echoes_pf.flac', '/m/time_pf.flac', '/m/money_pf.flac'],
        );

        // Remove Money (last)
        r = await runner.dispatchText('remove the last song');
        expect(r.success, isTrue);
        await controller.settled;
        expect(
          [for (final q in player.queue) q['path']],
          ['/m/echoes_pf.flac', '/m/time_pf.flac'],
        );

        // Save queue as playlist
        r = await runner.dispatchText('save queue as playlist Evening');
        expect(r.success, isTrue);
        final playlists = await repo.allPlaylists('name');
        expect(playlists.map((p) => p.name), contains('Evening'));
        final saved = await repo.tracksInPlaylist(playlists.first.id);
        expect(
          [for (final t in saved) t.path],
          ['/m/echoes_pf.flac', '/m/time_pf.flac'],
        );

        // Track info strips timbre and reports metadata
        r = await runner.dispatchText('tell me about this track');
        expect(r.success, isTrue);
        expect(r.displayed, contains('Echoes'));
        expect(
          (r.extras['track']! as Map<String, Object?>).containsKey('timbre'),
          isFalse,
        );

        // Play similar walks StationSource
        r = await runner.dispatchText('play similar');
        expect(r.success, isTrue);
        await controller.settled;
        expect(player.queue, isNotEmpty);

        // Pause, resume, stop
        r = await runner.dispatchText('pause');
        expect(r.displayed, 'Paused.');
        await controller.settled;
        expect(player.isPlaying, isFalse);

        r = await runner.dispatchText('resume');
        expect(r.displayed, 'Resuming playback.');
        await controller.settled;
        expect(player.isPlaying, isTrue);

        r = await runner.dispatchText('stop playing');
        expect(r.displayed, 'Stopped.');
        await controller.settled;
        expect(player.isPlaying, isFalse);
        expect(player.currentPath, '');
        expect(player.currentTrack, '');
      },
    );
  });
}

Object? _normalize(Object? v) => switch (v) {
  Uint8List b => {'__bytes__': b.toList()},
  Map<Object?, Object?> m => {
    for (final k in m.keys.map((e) => e.toString()).toList()..sort())
      k: _normalize(m[k]),
  },
  List<Object?> l => [for (final e in l) _normalize(e)],
  _ => v,
};

String _canon(Object? v) => pyJsonDumps(_normalize(v), ensureAscii: false);
