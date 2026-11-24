// The play-ledger drain against Python's (PORT_MAP §0 rule 3): parsing,
// the scrobble rule, rounding, crash replay, what play_counts and
// playback_history hold after each drain.
//
//   ~/miniconda3/bin/python tool/export_db_goldens.py
//   flutter test test/play_ledger_test.dart
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/py_num.dart';
import 'package:mai_an_lab/data/library_schema.dart';
import 'package:mai_an_lab/data/play_ledger.dart';
import 'package:sqlite3/sqlite3.dart';

import 'db_golden_support.dart';

void main() {
  final golden = loadDbGolden()['ledger'] as Map<String, dynamic>;
  late Directory dir;

  setUp(() => dir = Directory.systemTemp.createTempSync('ledger'));
  tearDown(() => dir.deleteSync(recursive: true));

  test('round(x, 1) as Python', () {
    for (final [ms as int, want]
        in (golden['rounding'] as List).cast<List<Object?>>()) {
      expect(
        dbValue(pyRound1(ms / 1000.0)),
        goldenValue(want),
        reason: '$ms ms',
      );
    }
    expect(pyRound1(-61.25), -61.2);
    expect(pyRound1(0.05), 0.1); // 0.05 is stored just above .05
  });

  test('parse_lines as Python', () {
    final lines = ((golden['steps'] as List).first['lines'] as List)
        .cast<String>();
    expect(
      [
        for (final e in parseLedgerLines(lines))
          [e.path, e.listenedS, e.durationS, e.endedAt],
      ],
      [
        for (final e in (golden['parsed'] as List).cast<Map<String, Object?>>())
          [
            e['path'],
            double.parse((e['listened_s'] as Map)['real'] as String),
            double.parse((e['duration_s'] as Map)['real'] as String),
            e['ended_at'],
          ],
      ],
    );
  });

  test('a line that is JSON but not an object is dropped, not fatal', () {
    expect(parseLedgerLines(['[1, 2]', '5', '"x"', 'null']), isEmpty);
  });

  test('drains write what record_listens writes', () async {
    final db = sqlite3.open('${dir.path}/library.db');
    addTearDown(db.close);
    for (final sql in (golden['setup_sql'] as List).cast<String>()) {
      db.execute(sql);
    }
    migrateLibrary(db);
    db.execute(golden['setup_update'] as String);

    final drain = LedgerDrain(
      dir.path,
      (e) async => recordListens(db, e),
      settle: Duration.zero,
    );
    var i = 0;
    for (final step in (golden['steps'] as List).cast<Map<String, dynamic>>()) {
      final reason = 'drain ${i++}';
      if (step['draining'] case final List<Object?> lines) {
        File(
          drain.drainingPath,
        ).writeAsStringSync(lines.map((l) => '$l\n').join());
      }
      final lines = (step['lines'] as List).cast<String>();
      if (lines.isNotEmpty) {
        File(drain.livePath).writeAsStringSync(lines.map((l) => '$l\n').join());
      }
      expect(await drain.drain(), step['counted'], reason: reason);
      expect(
        dir
            .listSync()
            .map((f) => f.uri.pathSegments.last)
            .where((n) => n.startsWith(kLedgerName)),
        isEmpty,
        reason: reason,
      );
      expect(
        dbRows(
          db.select(
            'SELECT track_path, played_at, event, listened_s, seed_path '
            'FROM playback_history ORDER BY rowid',
          ),
        ),
        goldenRows(step['playback_history'] as List),
        reason: '$reason: playback_history',
      );
      expect(
        dbRows(
          db.select(
            'SELECT track_path, count, last_played, bpm, energy, brightness '
            'FROM play_counts ORDER BY track_path',
          ),
        ),
        goldenRows(step['play_counts'] as List),
        reason: '$reason: play_counts',
      );
    }
  });

  test('a line without an end time is stamped now', () {
    final db = sqlite3.open(':memory:');
    addTearDown(db.close);
    migrateLibrary(db);
    final entries = parseLedgerLines([
      '{"id": "file:///m/a.flac", "ms": 40000, "dur": 0, "end": 0}',
    ]);
    expect(entries.single.endedAt, isNull);
    expect(recordListens(db, entries, now: 1234), 1);
    expect(
      db.select('SELECT played_at, event FROM playback_history').single.values,
      [1234.0, 'completed'],
    );
  });

  test('a failed write keeps the file for the next drain', () async {
    final drain = LedgerDrain(
      dir.path,
      (_) async => throw StateError('disk full'),
      settle: Duration.zero,
    );
    File(
      drain.livePath,
    ).writeAsStringSync('{"id": "file:///m/a.flac", "ms": 40000, "end": 5}\n');
    await expectLater(drain.drain(), throwsStateError);
    expect(File(drain.drainingPath).existsSync(), isTrue);

    var got = 0;
    final retry = LedgerDrain(
      dir.path,
      (e) async => got = e.length,
      settle: Duration.zero,
    );
    expect(await retry.drain(), 1);
    expect(got, 1);
    expect(File(drain.drainingPath).existsSync(), isFalse);
  });

  test('schedule coalesces while a drain is pending', () async {
    var drains = 0;
    final drain = LedgerDrain(dir.path, (e) async {
      drains++;
      return e.length;
    }, settle: Duration.zero);
    File(
      drain.livePath,
    ).writeAsStringSync('{"id": "file:///m/a.flac", "ms": 40000, "end": 5}\n');
    drain.schedule(const Duration(milliseconds: 10));
    drain.schedule(const Duration(milliseconds: 10));
    await Future<void>.delayed(const Duration(milliseconds: 100));
    expect(drains, 1);
    expect(File(drain.livePath).existsSync(), isFalse);
  });
}
