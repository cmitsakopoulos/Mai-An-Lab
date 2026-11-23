import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/db_worker.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:sqlite3/sqlite3.dart';

import 'fixtures.dart';

void main() {
  late Directory dir;
  late String path;

  setUp(() {
    dir = Directory.systemTemp.createTempSync('mai_an_test');
    path = '${dir.path}/library.db';
    final db = sqlite3.open(path);
    createLibrarySchema(db);
    db.execute('''
      INSERT INTO artists (id, name) VALUES (1, 'Zeta'), (2, 'alpha');
      INSERT INTO albums (id, artist_id, title) VALUES (1, 1, 'Z One'), (2, 2, 'A One');
      INSERT INTO tracks (album_id, title, track_num, duration, path, format, added_date) VALUES
        (1, 'z-song', 1, 61.4, '/m/z1.flac', 'FLAC', 1700000000.5),
        (2, 'a-second', 2, 125, '/m/a2.flac', 'FLAC', NULL),
        (2, NULL, 1, NULL, '/m/a1.mp3', 'MP3', NULL),
        (99, 'no album row', 1, 1, '/m/lost.flac', 'FLAC', NULL);
    ''');
    final stmt = db.prepare(
      'INSERT INTO play_counts (track_path, pca_coords) VALUES (?, ?)',
    );
    stmt.execute([
      '/m/z1.flac',
      f32Blob([1, 0, 0]),
    ]);
    stmt.execute([
      '/m/a2.flac',
      f32Blob([0.9, 0.1, 0]),
    ]);
    stmt.execute([
      '/m/orphan.flac',
      f32Blob([0, 1, 0]),
    ]); // no tracks row
    stmt.close();
    db.close();
  });

  tearDown(() => dir.deleteSync(recursive: true));

  test('reads the same rows as get_all_tracks (inner joins)', () async {
    final repo = await LibraryRepository.open(path);
    final byPath = {for (final t in await repo.allTracks()) t.path: t};
    // '/m/lost.flac' has no album row, so Flet doesn't list it either.
    expect(byPath.keys.toSet(), {'/m/a1.mp3', '/m/a2.flac', '/m/z1.flac'});
    final a1 = byPath['/m/a1.mp3']!;
    expect(a1.title, 'a1.mp3'); // NULL title falls back to filename
    expect(a1.duration, 0);
    expect(a1.trackNum, 1);
    expect(a1.addedDate, isNull);
    final z1 = byPath['/m/z1.flac']!;
    expect(z1.durationLabel, '1:01');
    expect(z1.addedDate, 1700000000.5);
    expect(z1.artist, 'Zeta');
    await repo.close();
  });

  test('builds the coordinate graph, inner-joined to tracks', () async {
    final repo = await LibraryRepository.open(path);
    final g = await repo.coordGraph();
    expect(g.paths.toSet(), {'/m/z1.flac', '/m/a2.flac'});
    expect(g.dim, 3);
    await repo.close();
  });

  test('refuses coordinates from an older GEOMETRY_VERSION', () async {
    final db = sqlite3.open(path);
    db.execute('PRAGMA user_version = 1');
    db.close();
    final repo = await LibraryRepository.open(path);
    await expectLater(
      repo.coordGraph(),
      throwsA(
        isA<DbWorkerException>().having(
          (e) => e.message,
          'message',
          contains('geometry v1'),
        ),
      ),
    );
    expect(await repo.allTracks(), hasLength(3)); // tracks still load
    await repo.close();
  });

  test('worker is read-only and surfaces SQL errors', () async {
    final w = await DbWorker.open(path);
    await expectLater(
      w.run((db) => db.execute("INSERT INTO artists (name) VALUES ('x')")),
      throwsA(isA<DbWorkerException>()),
    );
    // Still usable after a failed job.
    expect(await w.run((db) => db.select('SELECT 1 AS v').first['v']), 1);
    await w.close();
    await expectLater(w.run((db) => 1), throwsA(isA<DbWorkerException>()));
  });

  test('close() lets queued jobs finish instead of failing them', () async {
    final w = await DbWorker.open(path);
    final queued = [
      for (var i = 0; i < 5; i++)
        w.run((db) => db.select('SELECT count(*) AS n FROM tracks').first['n']),
    ];
    await w.close();
    expect(await Future.wait(queued), everyElement(4));
  });

  test('opening a missing file fails cleanly', () async {
    await expectLater(
      DbWorker.open('${dir.path}/nope.db'),
      throwsA(isA<DbWorkerException>()),
    );
  });
}
