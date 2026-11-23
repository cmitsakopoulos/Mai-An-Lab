import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:isolate';

import 'package:flutter/foundation.dart';

import 'queue_entry.dart';

/// Where a restore should start: (row, position s, duration s).
typedef ResumePoint = ({int index, double position, double duration});

/// The native resume record, parsed: (path, row hint, position s, written at).
typedef NativeResume = ({String path, int? hint, double position, double at});

/// The saved queue (queue_state.json).
class SavedQueue {
  SavedQueue({
    required this.queue,
    required this.index,
    required this.position,
    required this.duration,
    this.autoplayHeld,
  });
  final List<QueueEntry> queue;
  final int index;
  final double position;
  final double duration;

  /// Auto-play's set-aside queue. Phase 2 owns it; until then it is carried
  /// through unchanged so a Flet-written snapshot loses nothing.
  final Object? autoplayHeld;
}

/// The Flet app's playback files, same names and JSON, in the data dir
/// (main.py `_save_queue_state`, `_save_position_state`, `_read_native_resume`,
/// `_save_queue_to_file`, `_choose_resume`):
///
///   queue_state.json     {"queue": [...], "current_index", "position",
///                         "duration", "autoplay_held"?}
///   queue_pos.json       {"path", "current_index", "position", "duration"}
///   native_resume.json   written by the audio handler on every track change
///                        and play/pause: {"src", "index", "position_ms",
///                        "playing", "at"}
///   .cache/queue_regular.json, .cache/queue_shuffle.json
///                        the queue set aside while shuffling the library
///                        (XDG_CACHE_HOME is <data>/.cache in the Flet app)
///
/// Writes are atomic (tmp + rename) and serialised.
class QueueStore {
  QueueStore(this.dataDir);

  final String dataDir;

  static const queueStateName = 'queue_state.json';
  static const positionName = 'queue_pos.json';
  static const nativeResumeName = 'native_resume.json';
  static const ledgerName = 'play_ledger.jsonl';

  String get queueStatePath => _join(dataDir, queueStateName);
  String get positionPath => _join(dataDir, positionName);
  String get nativeResumePath => _join(dataDir, nativeResumeName);
  String get ledgerPath => _join(dataDir, ledgerName);
  String partitionPath(String name) => _join(_join(dataDir, '.cache'), name);

  /// `_QUEUE_STATE["held"]`: whether this process has held a queue (restored,
  /// or saved non-empty). Until it has, an empty queue means "not loaded
  /// yet", never "cleared", so it must not delete the saved snapshot.
  bool held = false;

  Future<void> _writes = Future.value();

  // ── reading ──────────────────────────────────────────────────────────────

  Future<SavedQueue?> readQueueState() async {
    final raw = await _readJson(queueStatePath);
    if (raw is! Map) return null;
    final rows = raw['queue'];
    if (rows is! List || rows.isEmpty) return null;
    return SavedQueue(
      queue: [
        for (final r in rows)
          if (r is Map) QueueEntry(Map<String, Object?>.from(r)),
      ],
      index: _int(raw['current_index']) ?? 0,
      position: _num(raw['position']),
      duration: _num(raw['duration']),
      autoplayHeld: raw['autoplay_held'],
    );
  }

  Future<Map<String, Object?>?> readPosition() async {
    final raw = await _readJson(positionPath);
    return raw is Map ? Map<String, Object?>.from(raw) : null;
  }

  /// `_read_native_resume`: null if absent, unreadable or not a library track.
  Future<NativeResume?> readNativeResume() async {
    final raw = await _readJson(nativeResumePath);
    if (raw is! Map) return null;
    final path = srcToPath('${raw['src'] ?? ''}');
    if (path.isEmpty) return null;
    final ms = _num(raw['position_ms']);
    return (
      path: path,
      hint: _int(raw['index']),
      position: ms > 0 ? ms / 1000.0 : 0.0,
      at: _num(raw['at']),
    );
  }

  /// Modification time in seconds (os.path.getmtime), 0 when absent.
  Future<double> mtime(String path) async {
    try {
      final t = await File(path).lastModified();
      return t.microsecondsSinceEpoch / 1e6;
    } on FileSystemException {
      return 0;
    }
  }

  /// Everything a cold start needs, resolved with [chooseResume].
  Future<(SavedQueue, ResumePoint)?> loadResume() async {
    final state = await readQueueState();
    if (state == null) return null;
    final pstate = await readPosition();
    final native = await readNativeResume();
    final posMtime = await mtime(positionPath);
    var r = chooseResume(
      state.queue,
      state.index,
      state.position,
      state.duration,
      pstate,
      native,
      posMtime,
    );
    final idx = r.index.clamp(0, state.queue.length - 1);
    r = (index: idx, position: r.position, duration: r.duration);
    return (state, r);
  }

  /// `_choose_resume`. The queue file's own index is the floor. The position
  /// file is written far more often, so it wins when its track is still
  /// queued. The native record is written by the player itself, so after
  /// background auto-advance it is the newer truth: it wins when written
  /// after the position file and its track is still queued.
  static ResumePoint chooseResume(
    List<QueueEntry> queue,
    int index,
    double pos,
    double dur,
    Map<String, Object?>? pstate,
    NativeResume? native,
    double posMtime,
  ) {
    final ppath = pstate?['path'];
    if (pstate != null && ppath is String && ppath.isNotEmpty) {
      final pi = rowOf(queue, ppath, pstate['current_index']);
      if (pi != null) {
        index = pi;
        pos = _num(pstate['position']);
        dur = _num(pstate['duration']);
      }
    }
    if (native != null) {
      final ni = rowOf(queue, native.path, native.hint);
      if (ni != null && native.at > posMtime) {
        if (ni != index) dur = queue[ni].duration;
        index = ni;
        pos = native.position;
      }
    }
    return (index: index, position: pos, duration: dur);
  }

  /// `_row_of`: [hint] when it holds [path] (the same song can be queued
  /// twice), else the first match, else null.
  static int? rowOf(List<QueueEntry> queue, String path, Object? hint) {
    if (hint is int &&
        hint >= 0 &&
        hint < queue.length &&
        queue[hint].path == path) {
      return hint;
    }
    final i = queue.indexWhere((t) => t.path == path);
    return i < 0 ? null : i;
  }

  // ── writing ──────────────────────────────────────────────────────────────

  /// `_save_queue_state`. An empty queue deletes both files, but only once
  /// this process has [held] one.
  Future<void> saveQueueState({
    required List<QueueEntry> queue,
    required int index,
    required double position,
    required double duration,
    Object? autoplayHeld,
  }) {
    if (queue.isEmpty) {
      if (!held) return _writes;
      return _enqueue(() async {
        await _delete(queueStatePath);
        await _delete(positionPath);
      });
    }
    held = true;
    final state = <String, Object?>{
      'queue': [for (final t in queue) t.data],
      'current_index': index,
      'position': position,
      'duration': duration,
      'autoplay_held': ?autoplayHeld,
    };
    final pos = _positionRecord(queue, index, position, duration);
    return _enqueue(() async {
      await _writeJsonAtomic(queueStatePath, state);
      // Keep the position file at least as new as the queue file, so a
      // restore can always trust it.
      await _writeJsonAtomic(positionPath, pos);
    });
  }

  /// `_save_position_state`: where we are, without rewriting the queue.
  Future<void> savePosition({
    required List<QueueEntry> queue,
    required int index,
    required double position,
    required double duration,
  }) {
    if (queue.isEmpty) return _enqueue(() => _delete(positionPath));
    final rec = _positionRecord(queue, index, position, duration);
    return _enqueue(() => _writeJsonAtomic(positionPath, rec));
  }

  static Map<String, Object?> _positionRecord(
    List<QueueEntry> q,
    int ci,
    double position,
    double duration,
  ) => {
    'path': (ci >= 0 && ci < q.length) ? q[ci].path : '',
    'current_index': ci,
    'position': position,
    'duration': duration,
  };

  /// `_save_queue_to_file` (partition files). Empty deletes.
  Future<void> savePartition(
    String name, {
    required List<QueueEntry> queue,
    required int index,
    required double position,
    required double duration,
  }) {
    final path = partitionPath(name);
    if (queue.isEmpty) return _enqueue(() => _delete(path));
    final state = {
      'queue': [for (final t in queue) t.data],
      'current_index': index,
      'position': position,
      'duration': duration,
    };
    return _enqueue(() async {
      await Directory(_join(dataDir, '.cache')).create(recursive: true);
      await _writeJsonAtomic(path, state);
    });
  }

  Future<SavedQueue?> loadPartition(String name) async {
    await _writes;
    final raw = await _readJson(partitionPath(name));
    if (raw is! Map) return null;
    final rows = raw['queue'];
    if (rows is! List) return null;
    return SavedQueue(
      queue: [
        for (final r in rows)
          if (r is Map) QueueEntry(Map<String, Object?>.from(r)),
      ],
      index: _int(raw['current_index']) ?? 0,
      position: _num(raw['position']),
      duration: _num(raw['duration']),
    );
  }

  /// Completes once every write queued so far has landed.
  Future<void> flush() => _writes;

  Future<void> _enqueue(Future<void> Function() job) {
    return _writes = _writes.then((_) async {
      try {
        await job();
      } catch (e) {
        debugPrint('QueueStore write failed: $e');
      }
    });
  }
}

/// flet_prefs.json: one flat JSON object. `_save_pref` merges each key into
/// what is on disk instead of dumping a cached copy, so a stale copy can never
/// erase keys someone else saved.
class Prefs {
  Prefs(String dataDir) : path = _join(dataDir, 'flet_prefs.json');

  final String path;
  Map<String, Object?> _values = {};
  Future<void> _writes = Future.value();

  Future<void> load() async {
    final raw = await _readJson(path);
    _values = raw is Map ? Map<String, Object?>.from(raw) : {};
  }

  Object? operator [](String key) => _values[key];
  bool getBool(String key, [bool fallback = false]) {
    final v = _values[key];
    return v is bool ? v : fallback;
  }

  String getString(String key, String fallback) {
    final v = _values[key];
    return v is String ? v : fallback;
  }

  Future<void> set(String key, Object? value) {
    _values[key] = value;
    return _writes = _writes.then((_) async {
      try {
        final onDisk = await _readJson(path);
        final merged = onDisk is Map
            ? Map<String, Object?>.from(onDisk)
            : <String, Object?>{};
        merged[key] = value;
        await _writeJsonAtomic(path, merged);
      } catch (e) {
        debugPrint('Prefs: failed to save $key: $e');
      }
    });
  }

  Future<void> flush() => _writes;
}

// ── helpers ─────────────────────────────────────────────────────────────────

String _join(String a, String b) => '$a${Platform.pathSeparator}$b';

Future<Object?> _readJson(String path) async {
  try {
    final text = await File(path).readAsString();
    // A big queue (5k rows ≈ 1.6 MB) decodes off the UI isolate.
    return text.length > 64 * 1024
        ? await Isolate.run(() => jsonDecode(text))
        : jsonDecode(text);
  } catch (_) {
    return null;
  }
}

/// tmp + rename: a kill mid-write leaves the previous file, never a torn one.
Future<void> _writeJsonAtomic(String path, Object? value) async {
  Future<void> write() async {
    final tmp = File('$path.tmp');
    try {
      await tmp.writeAsString(jsonEncode(value), flush: true);
      await tmp.rename(path);
    } catch (e) {
      try {
        await tmp.delete();
      } catch (_) {}
      rethrow;
    }
  }

  final big = value is Map && value['queue'] is List && (value['queue'] as List).length > 500;
  return big ? Isolate.run(write) : write();
}

Future<void> _delete(String path) async {
  try {
    await File(path).delete();
  } on FileSystemException {
    // already gone
  }
}

int? _int(Object? v) => v is int ? v : (v is num ? v.toInt() : null);

double _num(Object? v) {
  if (v is num) return v.toDouble();
  if (v is String) return double.tryParse(v) ?? 0;
  return 0;
}
