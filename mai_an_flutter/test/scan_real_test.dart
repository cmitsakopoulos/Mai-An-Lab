// The tag reader and the scan on a real music folder against Python
// (PORT_MAP §0 rule 3). Personal data, so the goldens are gitignored:
//
//   ~/miniconda3/bin/python tool/export_scan_goldens.py [--music DIR]
//   flutter test test/scan_real_test.dart
//
// Skipped when there is no export or the folder is gone.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/scan/library_scanner.dart';
import 'package:sqlite3/sqlite3.dart';

import 'db_golden_support.dart';
import 'tinytag_test.dart' show expectSameTags, tagResult;

const _dir = '../tools/state_backups/goldens/mac_music';

void main() {
  final tagsFile = File('$_dir/tags.json');
  final scanFile = File('$_dir/scan.json');
  if (!tagsFile.existsSync() || !scanFile.existsSync()) {
    test('real scan goldens', () {}, skip: 'no exported goldens');
    return;
  }
  final tags = jsonDecode(tagsFile.readAsStringSync()) as Map<String, dynamic>;
  final music = tags['music'] as String;
  if (!Directory(music).existsSync()) {
    test('real scan goldens', () {}, skip: '$music is gone');
    return;
  }

  test('tinytag reads every file as Python does', () {
    final files = tags['tags'] as Map<String, dynamic>;
    for (final MapEntry(:key, :value) in files.entries) {
      expectSameTags(
        tagResult('$music/$key'),
        value as Map<String, dynamic>,
        key,
      );
    }
  });

  test('scan writes the same tables', () async {
    final golden =
        jsonDecode(scanFile.readAsStringSync()) as Map<String, dynamic>;
    final tmp = Directory.systemTemp.createTempSync('scanreal');
    try {
      final repo = await LibraryRepository.open(
        '${tmp.path}/library.db',
        writable: true,
      );
      final count = await LibraryScanner(targetFolder: music, repo: repo).run();
      await repo.close();
      expect(count, golden['count']);
      final db = sqlite3.open(
        '${tmp.path}/library.db',
        mode: OpenMode.readOnly,
      );
      final got = dumpDb(db)['tables'] as Map;
      db.close();
      final want =
          goldenDump(golden['db'] as Map<String, dynamic>)['tables'] as Map;
      for (final name in want.keys) {
        final w = want[name] as Map, g = got[name] as Map;
        expect(g['columns'], w['columns'], reason: '$name columns');
        final cols = (w['columns'] as List).cast<String>();
        final added = cols.indexOf('added_date');
        final wr = w['rows'] as List, gr = g['rows'] as List;
        expect(gr.length, wr.length, reason: '$name rows');
        for (var i = 0; i < wr.length; i++) {
          final a = [...gr[i] as List], b = [...wr[i] as List];
          if (added >= 0) {
            // Python's st_mtime has nanoseconds; dart:io's FileStat only
            // milliseconds (PORT_MAP §7). Far inside the 1 s the scanner
            // allows before it rereads a file.
            final x = (a[added] as Map)['real'] as double;
            final y = (b[added] as Map)['real'] as double;
            expect((x - y).abs(), lessThan(1e-3), reason: '$name row $i mtime');
            a[added] = b[added] = null;
          }
          expect(a, b, reason: '$name row $i');
        }
      }
    } finally {
      tmp.deleteSync(recursive: true);
    }
  });
}
