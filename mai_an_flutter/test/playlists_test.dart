// Playlist writes and reads against Python's (PORT_MAP §0 rule 3): every
// db_manager playlist method and delete_tracks_by_paths, run in sequence on
// one database, with the tables, both listings and each playlist's tracks
// compared after every step; then the playlist search.
//
//   ~/miniconda3/bin/python tool/export_playlist_goldens.py
//   flutter test test/playlists_test.dart
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/db_worker.dart';
import 'package:mai_an_lab/core/text_fold.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/track_query.dart';
import 'package:sqlite3/sqlite3.dart';

import 'db_golden_support.dart';

void main() {
  final golden =
      jsonDecode(
            File('test/goldens/playlists_synthetic.json').readAsStringSync(),
          )
          as Map<String, dynamic>;
  late Directory dir;

  setUp(() => dir = Directory.systemTemp.createTempSync('playlists'));
  tearDown(() => dir.deleteSync(recursive: true));

  test('every playlist step leaves what Python leaves', () async {
    final path = '${dir.path}/library.db';
    final repo = await LibraryRepository.open(path, writable: true);
    await repo.debugExecute([
      for (final s in (golden['setup'] as List).cast<String>()) (s, const []),
    ]);
    final peek = sqlite3.open(path, mode: OpenMode.readOnly);
    var i = 0;
    for (final step in (golden['steps'] as List).cast<Map<String, dynamic>>()) {
      final args = step['args'] as List;
      final reason = 'step ${i++}: ${step['method']} $args';
      Object? result;
      String? error;
      try {
        final Future<Object?> call = switch (step['method'] as String) {
          'create_playlist' => repo.createPlaylist(args[0] as String),
          'add_track_to_playlist' => repo.addTracksToPlaylist(args[0] as int, [
            args[1] as String,
          ]),
          'move_playlist_track' => repo.movePlaylistTrack(
            args[0] as int,
            args[1] as int,
            args[2] as int,
          ),
          'remove_track_from_playlist' => repo.removeTrackFromPlaylist(
            args[0] as int,
            args[1] as String,
          ),
          'delete_tracks_by_paths' => repo.deleteTracksByPaths(
            (args[0] as List).cast<String>(),
          ),
          'update_playlist' => repo.updatePlaylist(
            args[0] as int,
            name: args[1] as String?,
            color: args[2] as String?,
          ),
          'delete_playlist' => repo.deletePlaylist(args[0] as int),
          'sql' => repo.debugExecute([
            (args[0] as String, (args[1] as List).cast<Object?>()),
          ]),
          final m => throw StateError('no step $m'),
        };
        result = await call;
      } on DbWorkerException catch (e) {
        error = e.message;
      }
      if (step['created'] case final int created when result is int) {
        await repo.debugExecute([
          ('UPDATE playlists SET created = ? WHERE id = ?', [created, result]),
        ]);
      }
      expect(result, step['result'], reason: reason);
      expect(error, step['error'], reason: reason);
      expect(
        dbRows(
          peek.select(
            'SELECT id, name, created, color FROM playlists ORDER BY id',
          ),
        ),
        goldenRows(step['playlists'] as List),
        reason: reason,
      );
      expect(
        dbRows(
          peek.select(
            'SELECT id, playlist_id, track_path, order_index '
            'FROM playlist_tracks ORDER BY id',
          ),
        ),
        goldenRows(step['playlist_tracks'] as List),
        reason: reason,
      );
      final listing = step['listing'] as Map<String, dynamic>;
      for (final sort in ['name', 'date']) {
        expect(
          [
            for (final p in await repo.allPlaylists(sort))
              {
                'id': p.id,
                'name': p.name,
                'created': p.created,
                'color': p.color,
                'track_count': p.trackCount,
              },
          ],
          listing[sort],
          reason: '$reason, sorted by $sort',
        );
      }
      final tracks = step['tracks'] as Map<String, dynamic>;
      for (final MapEntry(:key, :value) in tracks.entries) {
        expect(
          [for (final t in await repo.tracksInPlaylist(int.parse(key))) t.path],
          value,
          reason: '$reason, tracks of $key',
        );
      }
    }
    peek.close();
    await repo.close();
  });

  test('playlist search finds what get_all_playlists finds', () {
    final names = ['Chill Out', 'Road Trip', 'Ελληνικά'];
    final rows = [for (final n in names) (n, fold(n))];
    final searches = golden['searches'] as Map<String, dynamic>;
    for (final MapEntry(:key, :value) in searches.entries) {
      final want = value as Map<String, dynamic>;
      final (hits, closest) = searchRows(rows, key, fields: (n) => [n]);
      expect(hits, want['names'], reason: 'query "$key"');
      expect(closest, want['closest'], reason: 'query "$key"');
    }
  });

  test('a read-only library refuses playlist writes', () async {
    final path = '${dir.path}/ro.db';
    await (await LibraryRepository.open(path, writable: true)).close();
    final repo = await LibraryRepository.open(path);
    expect(() => repo.createPlaylist('x'), throwsStateError);
    expect(() => repo.deletePlaylist(1), throwsStateError);
    expect(await repo.allPlaylists('name'), isEmpty);
    await repo.close();
  });
}
