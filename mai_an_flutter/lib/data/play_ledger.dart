import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:sqlite3/sqlite3.dart';

import '../core/db_worker.dart';
import '../core/py_num.dart';
import '../player/queue_entry.dart' show srcToPath;

/// `play_ledger.py`: moves the audio handler's play ledger into the
/// database. The handler appends one JSON line per finished listen to
/// `play_ledger.jsonl` in the data dir:
///
///     {"id": "<src uri>", "ms": <audible ms>, "dur": <ms or 0>,
///      "start": <unix s>, "end": <unix s>}
///
/// Crash safety, as in Python: the file is renamed to `.draining` before it
/// is read and deleted only after the database commit. A leftover
/// `.draining` from an interrupted drain is ingested first next time; the
/// database ignores rows it already has, so a replay never double-counts.
const kLedgerName = 'play_ledger.jsonl';

/// `db_manager.LISTEN_COUNT_MIN_S`.
const kListenCountMinS = 30.0;

/// `play_ledger.is_listen`: a listen counts after [minS] seconds, or half the
/// track for short tracks. Anything less is an early skip.
bool isListen(double listenedS, double durationS, {double minS = 30.0}) =>
    listenedS >= minS || (durationS > 0 && listenedS >= 0.5 * durationS);

typedef LedgerEntry = ({
  String path,
  double listenedS,
  double durationS,
  int? endedAt,
});

/// `play_ledger.parse_lines`. Lines that aren't JSON (a torn final line from
/// a killed process), streams and values Python's `float()`/`int()` reject
/// are dropped. Unlike Python, a line that is JSON but not an object is
/// dropped too: there `raw.get` raises outside the try, the drain fails
/// and the same file fails again on every later drain.
List<LedgerEntry> parseLedgerLines(Iterable<String> lines) {
  final out = <LedgerEntry>[];
  for (var line in lines) {
    line = line.trim();
    if (line.isEmpty) continue;
    final Object? raw;
    try {
      raw = jsonDecode(line);
    } on FormatException {
      continue;
    }
    if (raw is! Map) continue;
    final path = srcToPath(_pyStr(_or(raw['id'], '')));
    if (path.isEmpty) continue;
    final ms = _pyFloat(_or(raw['ms'], 0));
    final dur = _pyFloat(_or(raw['dur'], 0));
    final end = _pyInt(_or(raw['end'], 0));
    if (ms == null || dur == null || end == null) continue;
    out.add((
      path: path,
      listenedS: ms / 1000.0,
      durationS: dur / 1000.0,
      endedAt: end == 0 ? null : end,
    ));
  }
  return out;
}

/// Python's `x or default`.
Object? _or(Object? v, Object fallback) =>
    (v == null ||
        v == false ||
        v == 0 ||
        (v is String && v.isEmpty) ||
        (v is List && v.isEmpty) ||
        (v is Map && v.isEmpty))
    ? fallback
    : v;

String _pyStr(Object? v) => switch (v) {
  true => 'True',
  String s => s,
  _ => jsonEncode(v),
};

/// `float(v)`; null where Python raises.
double? _pyFloat(Object? v) => switch (v) {
  bool b => b ? 1.0 : 0.0,
  num n => n.toDouble(),
  String s => double.tryParse(s.trim()),
  _ => null,
};

/// `int(v)`: floats truncate, strings must be integers; null where Python
/// raises.
int? _pyInt(Object? v) => switch (v) {
  bool b => b ? 1 : 0,
  int n => n,
  double d => d.isFinite ? d.truncate() : null,
  String s => int.tryParse(s.trim()),
  _ => null,
};

/// `DatabaseManager.record_listens`: every entry in ONE transaction. A
/// listen that passes the scrobble rule is a 'completed' event and bumps
/// play_counts (with the track's features copied across, as
/// `increment_play_count` does); a shorter one is 'skipped_early'. Replays
/// hit `idx_history_listen_uniq` and are ignored. Returns how many counted.
/// [now] stands in for `time.time()` when a line has no end time.
DbJob<int> recordListensJob(List<LedgerEntry> entries, {int? now}) =>
    (db) => recordListens(db, entries, now: now);

int recordListens(Database db, List<LedgerEntry> entries, {int? now}) {
  if (entries.isEmpty) return 0;
  final fallbackEnd = now ?? DateTime.now().millisecondsSinceEpoch ~/ 1000;
  var counted = 0;
  db.execute('BEGIN IMMEDIATE');
  try {
    final history = db.prepare(
      'INSERT OR IGNORE INTO playback_history '
      '(track_path, played_at, event, listened_s) VALUES (?, ?, ?, ?)',
    );
    final counts = db.prepare('''
                        INSERT INTO play_counts (track_path, count, last_played, bpm, energy, brightness)
                        SELECT ?, 1, ?, COALESCE(t.bpm, 0), COALESCE(t.energy, 0), COALESCE(t.brightness, 0)
                        FROM (SELECT 1) LEFT JOIN tracks t ON t.path = ?
                        WHERE true
                        ON CONFLICT(track_path) DO UPDATE SET
                            count = count + 1,
                            last_played = MAX(COALESCE(last_played, 0), EXCLUDED.last_played),
                            bpm = EXCLUDED.bpm,
                            energy = EXCLUDED.energy,
                            brightness = EXCLUDED.brightness
                        ''');
    try {
      for (final e in entries) {
        if (e.path.isEmpty) continue;
        final endedAt = e.endedAt ?? fallbackEnd;
        final listen = isListen(
          e.listenedS,
          e.durationS,
          minS: kListenCountMinS,
        );
        history.execute([
          e.path,
          endedAt,
          listen ? 'completed' : 'skipped_early',
          pyRound1(e.listenedS),
        ]);
        if (!listen || db.updatedRows != 1) continue;
        counts.execute([e.path, endedAt, e.path]);
        counted++;
      }
    } finally {
      history.close();
      counts.close();
    }
    db.execute('COMMIT');
  } catch (_) {
    db.execute('ROLLBACK');
    rethrow;
  }
  return counted;
}

/// `drain_ledger` plus main.py's `_schedule_ledger_drain`.
class LedgerDrain {
  LedgerDrain(this.dataDir, this._record, {this.settle = _settle});

  final String dataDir;
  final Future<int> Function(List<LedgerEntry>) _record;

  /// After renaming the ledger aside, wait this long before reading, so an
  /// append already in flight against the old name lands in the renamed
  /// file (appends are ~150 bytes; this is generous).
  final Duration settle;
  static const _settle = Duration(milliseconds: 500);

  String get livePath => '$dataDir${Platform.pathSeparator}$kLedgerName';
  String get drainingPath => '$livePath.draining';

  Future<int> _last = Future.value(0);
  Timer? _timer;
  bool _pending = false;

  /// Ingests every pending line. Returns the number of listens that counted.
  /// Drains run one at a time.
  Future<int> drain() {
    final run = _last.then((_) => _drainOnce(), onError: (_) => _drainOnce());
    _last = run;
    return run;
  }

  Future<int> _drainOnce() async {
    final live = File(livePath);
    final draining = File(drainingPath);
    var total = 0;
    // Two passes: a leftover .draining first, then what was appended since.
    for (var pass = 0; pass < 2; pass++) {
      if (!await draining.exists()) {
        if (!await live.exists()) break;
        try {
          await live.rename(draining.path);
        } on PathNotFoundException {
          break;
        }
        await Future<void>.delayed(settle);
      }
      final text = utf8.decode(
        await draining.readAsBytes(),
        allowMalformed: true,
      );
      final entries = parseLedgerLines(text.split(RegExp(r'\r\n|\r|\n')));
      final counted = entries.isEmpty ? 0 : await _record(entries);
      try {
        await draining.delete();
      } on PathNotFoundException {
        // already gone
      }
      if (entries.isNotEmpty) {
        debugPrint(
          'play ledger: ingested ${entries.length} listens ($counted counted)',
        );
      }
      total += counted;
    }
    return total;
  }

  /// `_schedule_ledger_drain`: coalescing. While a drain is pending, a new
  /// request is dropped; the pending one picks up everything appended
  /// before it runs, and a later line is caught by the next request. A
  /// failure keeps the file for the next drain.
  void schedule([Duration delay = const Duration(seconds: 3)]) {
    if (_pending) return;
    _pending = true;
    _timer = Timer(delay, () async {
      try {
        await drain();
      } catch (e) {
        debugPrint('play ledger drain failed: $e');
      } finally {
        _pending = false;
      }
    });
  }

  void dispose() => _timer?.cancel();
}
