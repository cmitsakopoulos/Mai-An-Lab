// The port's migrations on a real library, against db_manager's: both open
// a copy of the same library.db and every table must come out the same.
// Personal data, so the Python copy sits beside the gitignored bundles:
//
//   ~/miniconda3/bin/python tool/export_station_goldens.py   # copies library.db
//   ~/miniconda3/bin/python tool/export_db_goldens.py        # library_migrated.db
//   flutter test test/db_schema_real_test.dart
//
// Skipped when there is none.
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/data/library_schema.dart';
import 'package:sqlite3/sqlite3.dart';

Directory? _bundle() {
  final dir = Directory('../tools/state_backups/goldens');
  if (!dir.existsSync()) return null;
  final found = [
    for (final d in dir.listSync().whereType<Directory>())
      if (File('${d.path}/library_migrated.db').existsSync()) d,
  ]..sort((a, b) => b.path.compareTo(a.path));
  return found.firstOrNull;
}

String _collapse(String? s) =>
    s == null ? '' : s.replaceAll(RegExp(r'\s+'), ' ').trim();

void main() {
  final bundle = _bundle();

  test(
    'migrating a real library matches db_manager',
    () {
      final tmp = Directory.systemTemp.createTempSync('real_schema');
      addTearDown(() => tmp.deleteSync(recursive: true));
      // A consistent copy, as the export takes it (folds in any WAL).
      final src = sqlite3.open(
        '${bundle!.path}/library.db',
        mode: OpenMode.readOnly,
      );
      final path = '${tmp.path}/library.db';
      src.execute("VACUUM INTO '$path'");
      src.close();

      final db = sqlite3.open(path);
      addTearDown(db.close);
      migrateLibrary(db);
      db.execute("ATTACH DATABASE '${bundle.path}/library_migrated.db' AS py");

      List<List<Object?>> master(String schema) => [
        for (final r in db.select(
          'SELECT type, name, tbl_name, sql FROM $schema.sqlite_master '
          'ORDER BY type, name',
        ))
          [r['type'], r['name'], r['tbl_name'], _collapse(r['sql'] as String?)],
      ];
      expect(master('main'), master('py'));
      expect(
        db.select('PRAGMA main.user_version').first.values,
        db.select('PRAGMA py.user_version').first.values,
      );

      final tables = [
        for (final r in db.select(
          "SELECT name FROM main.sqlite_master WHERE type = 'table' "
          "AND name NOT LIKE 'fts_search_%' ORDER BY name",
        ))
          r['name'] as String,
      ];
      expect(tables, contains('tracks'));
      for (final t in tables) {
        int count(String sql) => db.select(sql).first.values.first as int;
        expect(
          count('SELECT count(*) FROM main."$t"'),
          count('SELECT count(*) FROM py."$t"'),
          reason: '$t row count',
        );
        for (final (a, b) in [('main', 'py'), ('py', 'main')]) {
          expect(
            count(
              'SELECT count(*) FROM (SELECT * FROM $a."$t" '
              'EXCEPT SELECT * FROM $b."$t")',
            ),
            0,
            reason: '$t: rows in $a, not in $b',
          );
        }
      }
    },
    skip: bundle == null ? 'no library_migrated.db (see header)' : false,
  );
}
