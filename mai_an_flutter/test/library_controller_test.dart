import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/library/track_query.dart';
import 'package:sqlite3/sqlite3.dart';

import 'fixtures.dart';

void main() {
  late Directory dir;
  late String path;

  setUp(() {
    dir = Directory.systemTemp.createTempSync('mai_an_ctrl');
    path = '${dir.path}/library.db';
    final db = sqlite3.open(path);
    createLibrarySchema(db);
    db.execute('''
      INSERT INTO artists (id, name) VALUES (1, 'Björk'), (2, 'Radiohead');
      INSERT INTO albums (id, artist_id, title) VALUES (1, 1, 'Post'), (2, 2, 'OK Computer');
      INSERT INTO tracks (id, album_id, title, path, added_date) VALUES
        (1, 1, 'Army of Me', '/m/1', 100),
        (2, 2, 'Airbag', '/m/2', 300),
        (3, 2, 'Let Down', '/m/3', 200);
    ''');
    db.close();
  });

  tearDown(() => dir.deleteSync(recursive: true));

  Future<LibraryController> loaded() async {
    final c = LibraryController(() => LibraryRepository.open(path));
    await c.load();
    return c;
  }

  test('defaults to newest-first, like the Flet tracks tab', () async {
    final c = await loaded();
    expect(c.visible.map((t) => t.id), [2, 3, 1]);
    c.setSort(LibrarySort.track);
    expect(c.visible.map((t) => t.id), [2, 1, 3]);
    c.dispose();
  });

  test(
    'search is accent-insensitive and falls back to closest match',
    () async {
      final c = await loaded();
      c.setQuery('bjork');
      expect(c.visible.map((t) => t.id), [1]);
      expect(c.closestMatch, isFalse);
      c.setQuery('airbg');
      expect(c.visible.map((t) => t.id), [2]);
      expect(c.closestMatch, isTrue);
      c.setQuery('');
      expect(c.visible, hasLength(3));
      c.dispose();
    },
  );

  test('a graph failure leaves the library usable', () async {
    // An older database: play_counts without the pca_coords column.
    final db = sqlite3.open(path);
    db.execute('''
      DROP TABLE play_counts;
      CREATE TABLE play_counts (track_path TEXT PRIMARY KEY, count INTEGER);
    ''');
    db.close();
    final c = await loaded();
    expect(c.state, LoadState.ready);
    expect(c.visible, hasLength(3));
    expect(c.graph, isNull);
    expect(c.graphError, contains('pca_coords'));
    expect(c.similarTo(c.visible.first), isNull);
    c.dispose();
  });
}
