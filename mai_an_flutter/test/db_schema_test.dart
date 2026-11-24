// The schema the port leaves behind when it opens a database, against what
// db_manager leaves (PORT_MAP §0 rule 3): fresh files, files opened twice,
// and the shapes older Flet builds left behind.
//
//   ~/miniconda3/bin/python tool/export_db_goldens.py
//   flutter test test/db_schema_test.dart
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/data/library_schema.dart';
import 'package:sqlite3/sqlite3.dart';

import 'db_golden_support.dart';

void main() {
  final golden = loadDbGolden();
  late Directory dir;

  setUp(() => dir = Directory.systemTemp.createTempSync('schema'));
  tearDown(() => dir.deleteSync(recursive: true));

  test('geometry version matches Python', () {
    expect(kGeometryVersion, golden['geometry_version']);
  });

  for (final c in (golden['schema'] as List).cast<Map<String, dynamic>>()) {
    test('${c['name']}: same schema and rows as db_manager', () {
      final path = '${dir.path}/${c['name']}.db';
      final setup = sqlite3.open(path);
      for (final sql in (c['sql'] as List).cast<String>()) {
        setup.execute(sql);
      }
      setup.close();
      for (var i = 0; i < (c['opens'] as int); i++) {
        final db = sqlite3.open(path);
        migrateLibrary(db);
        db.close();
      }
      final db = sqlite3.open(path, mode: OpenMode.readOnly);
      addTearDown(db.close);
      expectSameDump(
        dumpDb(db),
        goldenDump(c['result'] as Map<String, dynamic>),
      );
    });
  }

  test('a failing migration leaves the file untouched', () {
    final path = '${dir.path}/broken.db';
    final setup = sqlite3.open(path);
    // `tracks` as a view: initialize()'s indexes on it fail.
    setup.execute('CREATE TABLE t (path TEXT)');
    setup.execute('CREATE VIEW tracks AS SELECT path FROM t');
    setup.close();
    final db = sqlite3.open(path);
    addTearDown(db.close);
    expect(() => migrateLibrary(db), throwsA(isA<SqliteException>()));
    final names = [
      for (final r in db.select('SELECT name FROM sqlite_master ORDER BY name'))
        r['name'],
    ];
    expect(names, ['t', 'tracks']);
    expect(db.select('PRAGMA user_version').first.values.first, 0);
  });

  test(
    'a writable repository migrates on open; a read-only one does not',
    () async {
      final path = '${dir.path}/repo.db';
      sqlite3.open(path)
        ..execute(
          ((golden['schema'] as List).cast<Map<String, dynamic>>().firstWhere(
                    (c) => c['name'] == 'legacy_old',
                  )['sql']
                  as List)
              .cast<String>()
              .join(';\n'),
        )
        ..close();
      final ro = await LibraryRepository.open(path);
      expect(await ro.allTracks(), hasLength(2));
      await ro.close();
      final before = sqlite3.open(path, mode: OpenMode.readOnly);
      expect(before.select('PRAGMA user_version').first.values.first, 0);
      before.close();

      final rw = await LibraryRepository.open(path, writable: true);
      expect(await rw.allTracks(), hasLength(2));
      await rw.close();
      final after = sqlite3.open(path, mode: OpenMode.readOnly);
      addTearDown(after.close);
      expect(
        after.select('PRAGMA user_version').first.values.first,
        kGeometryVersion,
      );
    },
  );
}
