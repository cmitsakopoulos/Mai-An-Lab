// The library scan against Python's LibraryScanner (PORT_MAP §0 rule 3):
// the same folder trees, scanned into fresh databases, then changed and
// scanned again; every table must come out the same.
//
//   ~/miniconda3/bin/python tool/export_scan_goldens.py
//   flutter test test/scan_test.dart
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/scan/library_scanner.dart';
import 'package:sqlite3/sqlite3.dart';

import 'db_golden_support.dart';
import 'scan_support.dart';

void main() {
  final golden =
      jsonDecode(File('test/goldens/scan_synthetic.json').readAsStringSync())
          as Map<String, dynamic>;
  late Directory dir;

  setUp(() => dir = Directory.systemTemp.createTempSync('scan'));
  tearDown(() => dir.deleteSync(recursive: true));

  for (final c in (golden['scan'] as List).cast<Map<String, dynamic>>()) {
    test('${c['name']}: same counts and tables as LibraryScanner', () async {
      final root = '${dir.path}/root';
      final db = '${dir.path}/library.db';
      final extra = extraSources(c['extra'] as Map<String, dynamic>);
      final steps = [
        c["layout"] as List<Object?>,
        ...(c["steps"] as List).cast<List<Object?>>(),
      ];
      final results = (c['results'] as List).cast<Map<String, dynamic>>();
      for (var i = 0; i < steps.length; i++) {
        applyLayout(root, steps[i], extra);
        final repo = await LibraryRepository.open(db, writable: true);
        if (i == 0) {
          await repo.debugExecute([
            for (final a in (c["aliases"] as List).cast<List<Object?>>())
              (
                'INSERT OR IGNORE INTO artist_aliases (alias, canonical, created_at) VALUES (?, ?, 0)',
                [a[0], a[1]],
              ),
          ]);
        }
        final count = await LibraryScanner(
          targetFolder: '$root/${c['target']}',
          repo: repo,
        ).run();
        await repo.close();
        expect(count, results[i]['count'], reason: 'step $i count');
        final conn = sqlite3.open(db, mode: OpenMode.readOnly);
        try {
          expectSameDump(
            relocate(dumpDb(conn), root) as Map<String, Object?>,
            goldenDump(results[i]['db'] as Map<String, dynamic>),
          );
        } finally {
          conn.close();
        }
      }
    });
  }

  test('a folder that does not exist scans nothing', () async {
    final repo = await LibraryRepository.open(
      '${dir.path}/library.db',
      writable: true,
    );
    expect(
      await LibraryScanner(targetFolder: '${dir.path}/nope', repo: repo).run(),
      0,
    );
    await repo.close();
  });

  test('format: splitext rules', () {
    expect(formatOf('/a/b.flac'), 'FLAC');
    expect(formatOf('/a/.hidden.mp3'), 'MP3');
    expect(formatOf('/a/.mp3'), '');
    expect(formatOf('/a/..mp3'), '');
    expect(formatOf('/a.b/c'), '');
  });
}
