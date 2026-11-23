import 'dart:async';
import 'dart:isolate';

import 'package:sqlite3/sqlite3.dart';

/// A unit of database work. It runs on the worker isolate, so it must be a
/// top-level/static function or a closure that captures only sendable values
/// (strings, numbers, lists, typed data) — never `this` of a widget or a
/// controller.
typedef DbJob<T> = T Function(Database db);

class DbWorkerException implements Exception {
  DbWorkerException(this.message, [this.remoteStack]);
  final String message;

  /// Stack trace from the worker isolate, for logs; not for display.
  final String? remoteStack;
  @override
  String toString() => 'DbWorkerException: $message';
}

/// Owns one SQLite connection on a long-lived background isolate.
///
/// This replaces `aiosqlite` + the asyncio loop: SQLite calls are synchronous
/// FFI, so they never run on the UI isolate. Jobs execute one at a time, in
/// submission order, which gives the same serialisation the Python
/// `_write_lock` provided — for reads and writes alike.
class DbWorker {
  DbWorker._(this._commands, this._responses) {
    _responses.listen(_onMessage);
  }

  final SendPort _commands;
  final ReceivePort _responses;
  final Map<int, Completer<Object?>> _pending = {};
  final Completer<void> _exited = Completer();
  int _nextId = 0;
  bool _closed = false;

  static Future<DbWorker> open(String path, {bool readOnly = true}) async {
    final init = ReceivePort();
    final responses = ReceivePort();
    final isolate = await Isolate.spawn(
      _workerMain,
      (init.sendPort, responses.sendPort, path, readOnly),
      debugName: 'db-worker',
      // A null on `responses` means the isolate died; pending jobs fail.
      onExit: responses.sendPort,
    );
    final first = await init.first;
    if (first is SendPort) return DbWorker._(first, responses);
    responses.close();
    isolate.kill();
    throw DbWorkerException('Could not open $path: $first');
  }

  Future<T> run<T>(DbJob<T> job) {
    if (_closed) return Future.error(DbWorkerException('worker is closed'));
    final id = _nextId++;
    final done = Completer<Object?>();
    _pending[id] = done;
    _commands.send((id, job));
    return done.future.then((value) => value as T);
  }

  /// Stops accepting jobs, lets the ones already queued finish (a write
  /// submitted before close() still commits and still reports its result),
  /// then closes the connection. Completes when the isolate has exited.
  Future<void> close() {
    if (!_closed) {
      _closed = true;
      _commands.send(null);
    }
    return _exited.future;
  }

  void _onMessage(Object? message) {
    if (message == null) {
      // The isolate exited: after close(), or because it crashed.
      _closed = true;
      _failAll('worker isolate exited');
      _responses.close();
      if (!_exited.isCompleted) _exited.complete();
      return;
    }
    final (id, ok, payload) = message as (int, bool, Object?);
    final done = _pending.remove(id);
    if (done == null) return;
    ok ? done.complete(payload) : done.completeError(_remoteError(payload));
  }

  void _failAll(String reason) {
    for (final done in _pending.values) {
      done.completeError(DbWorkerException(reason));
    }
    _pending.clear();
  }

  static void _workerMain((SendPort, SendPort, String, bool) args) {
    final (init, out, path, readOnly) = args;
    final Database db;
    try {
      db = sqlite3.open(
        path,
        mode: readOnly ? OpenMode.readOnly : OpenMode.readWriteCreate,
      );
    } catch (e) {
      init.send('$e');
      return;
    }
    final commands = ReceivePort();
    init.send(commands.sendPort);
    commands.listen((message) {
      if (message == null) {
        db.close();
        commands.close();
        return;
      }
      final (id, job) = message as (int, Object? Function(Database));
      try {
        out.send((id, true, job(db)));
      } catch (e, st) {
        // Covers both a failing query and a result that can't cross isolates.
        out.send((id, false, ('$e', '$st')));
      }
    });
  }
}

DbWorkerException _remoteError(Object? payload) {
  final (message, stack) = payload as (String, String);
  return DbWorkerException(message, stack);
}
