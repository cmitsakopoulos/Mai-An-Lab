// The acoustic build on a real library against Python's (PORT_MAP §0 rule
// 3). Needs the gitignored goldens:
//
//   ~/miniconda3/bin/python tool/export_station_goldens.py   (the library copy)
//   ~/miniconda3/bin/python tool/export_acoustic_goldens.py
//   flutter test test/acoustic_real_golden_test.dart
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:sqlite3/sqlite3.dart';

import 'acoustic_build_test.dart' show checkScenario;

File? _golden() {
  final env = Platform.environment['ACOUSTIC_GOLDEN'];
  if (env != null) return File(env);
  final dir = Directory('../tools/state_backups/goldens');
  if (!dir.existsSync()) return null;
  final found = [
    for (final d in dir.listSync().whereType<Directory>())
      if (File('${d.path}/acoustic.json').existsSync())
        File('${d.path}/acoustic.json'),
  ]..sort((a, b) => b.path.compareTo(a.path));
  return found.firstOrNull;
}

void main() {
  final file = _golden();
  if (file == null) {
    test('acoustic goldens', () {}, skip: 'no exported goldens');
    return;
  }
  final want = jsonDecode(file.readAsStringSync()) as Map<String, dynamic>;

  test('build_acoustic_edges on the real library', () async {
    final dir = Directory.systemTemp.createTempSync('acoustic_real');
    addTearDown(() => dir.deleteSync(recursive: true));
    final copy = '${dir.path}/library.db';
    final src = sqlite3.open('${file.parent.path}/library.db');
    src.execute('VACUUM INTO ?', [copy]);
    src.close();
    // Migrated as the app opens it (Python's build ran after initialize).
    await (await LibraryRepository.open(copy, writable: true)).close();
    final db = sqlite3.open(copy);
    addTearDown(db.close);
    final clock = Stopwatch()..start();
    await checkScenario(want, db);
    // ignore: avoid_print
    print('checked in ${clock.elapsedMilliseconds} ms');
  });

  test('the repository builds it and stations read it', () async {
    final dir = Directory.systemTemp.createTempSync('acoustic_real');
    addTearDown(() => dir.deleteSync(recursive: true));
    final copy = '${dir.path}/library.db';
    final src = sqlite3.open('${file.parent.path}/library.db');
    src.execute('VACUUM INTO ?', [copy]);
    src.close();
    final repo = await LibraryRepository.open(copy, writable: true);
    addTearDown(repo.close);
    final clock = Stopwatch()..start();
    final res = await repo.buildAcousticEdges();
    // ignore: avoid_print
    print('repository build: ${clock.elapsedMilliseconds} ms');
    expect(res.tracks, want['returned']);
    expect(res.redundant.toList()..sort(), want['redundant']);
    final ready = await repo.graphReadiness();
    expect(ready.analysed, want['returned']);
    expect(ready.withCoords, greaterThanOrEqualTo(res.tracks));
    final stations = await repo.stations();
    expect(stations, isNotNull);
  });
}
