import 'dart:async';
import 'dart:collection';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'companion_client.dart';

/// Problem reports for the Mac Companion: the user describes a bug, or the
/// app catches an error on its own; either goes to the Mac with the app's
/// recent log, where the Companion page lists it.
///
/// The log is whatever went through [debugPrint] (the app's own logging),
/// kept in memory. Caught errors wait in [file] until the Mac is reachable,
/// so an error on the train arrives once you're home.
class ProblemReporter extends ChangeNotifier {
  ProblemReporter({this.file, this.maxLog = 400}) {
    _load();
  }

  final File? file;
  final int maxLog;

  /// The Mac, when connected (set by the app).
  CompanionClient? Function()? connected;

  final _log = ListQueue<String>();
  final pending = <Map<String, Object?>>[];
  int _errorsThisRun = 0;
  bool _flushing = false;

  int get pendingErrors => pending.where((r) => r['kind'] == 'error').length;
  bool get hasPending => pending.isNotEmpty;
  List<String> get recentLog => List.unmodifiable(_log);

  // ── capture ───────────────────────────────────────────────────────────────

  /// Hook debugPrint and the error handlers (once, early in main).
  void install() {
    final previous = debugPrint;
    debugPrint = (String? m, {int? wrapWidth}) {
      if (m != null) log(m);
      previous(m, wrapWidth: wrapWidth);
    };
    final flutter = FlutterError.onError;
    FlutterError.onError = (d) {
      // Image loads failing offline are routine, not bugs.
      if (!d.silent && d.library != 'image resource service') {
        recordError(d.exception, d.stack, context: d.context?.toString());
      }
      flutter?.call(d);
    };
    final platform = PlatformDispatcher.instance.onError;
    PlatformDispatcher.instance.onError = (e, s) {
      recordError(e, s);
      return platform?.call(e, s) ?? false;
    };
  }

  void log(String line) {
    final t = DateTime.now();
    String two(int n) => n.toString().padLeft(2, '0');
    _log.add('${two(t.hour)}:${two(t.minute)}:${two(t.second)} $line');
    while (_log.length > maxLog) {
      _log.removeFirst();
    }
  }

  /// An uncaught error: queued for the Mac. The same error again only
  /// counts up.
  void recordError(Object error, StackTrace? stack, {String? context}) {
    if (_errorsThisRun++ > 50) return; // an error loop
    final text = '$error';
    final stackText = stack == null ? '' : '$stack';
    final key = '${text.split('\n').first}|${_firstFrame(stackText)}';
    final now = DateTime.now().millisecondsSinceEpoch;
    for (final r in pending) {
      if (r['key'] == key) {
        r['count'] = ((r['count'] as int?) ?? 1) + 1;
        r['at'] = now;
        _changed();
        return;
      }
    }
    pending.add({
      'kind': 'error',
      'key': key,
      'text': context ?? '',
      'error': text,
      'stack': stackText.length > 8000
          ? stackText.substring(0, 8000)
          : stackText,
      'count': 1,
      'at': now,
      'app_version': _appVersion,
      'logs': _log.toList(),
    });
    while (pending.length > 20) {
      pending.removeAt(0);
    }
    _changed();
    if (connected?.call() case final c?) unawaited(flush(c));
  }

  static String _firstFrame(String stack) {
    for (final l in stack.split('\n')) {
      if (l.trim().isNotEmpty) return l.trim();
    }
    return '';
  }

  static String get _appVersion =>
      CompanionClient.appHeaders['X-App-Version'] ?? '';

  // ── sending ───────────────────────────────────────────────────────────────

  /// The user's own report, with the log. True: the Mac has it; false:
  /// saved, and it goes when the Mac is next reachable.
  Future<bool> report(String text) async {
    pending.add({
      'kind': 'bug',
      'text': text.trim(),
      'at': DateTime.now().millisecondsSinceEpoch,
      'app_version': _appVersion,
      'logs': _log.toList(),
    });
    _changed();
    final c = connected?.call();
    if (c == null) return false;
    await flush(c);
    return !pending.any((r) => r['kind'] == 'bug' && r['text'] == text.trim());
  }

  /// Send what's waiting, oldest first; stops at the first failure.
  Future<void> flush(CompanionClient c) async {
    if (_flushing || pending.isEmpty) return;
    _flushing = true;
    try {
      while (pending.isNotEmpty) {
        final r = pending.first;
        try {
          await c.postReport({...r}..remove('key'));
        } on CompanionHttpError catch (e) {
          // The Mac refused this one (an older Companion: 404): drop it
          // rather than retry forever.
          if (e.status != 404 && e.status != 400) rethrow;
        }
        pending.remove(r);
        _changed();
      }
    } catch (e) {
      // Kept for the next connection. Not debugPrint: that would log a
      // line into the next report about failing to send it.
      if (kDebugMode) print('problem report not sent: $e');
    } finally {
      _flushing = false;
    }
  }

  // ── keeping what's unsent ─────────────────────────────────────────────────

  void _changed() {
    notifyListeners();
    final f = file;
    if (f == null) return;
    unawaited(
      f
          .writeAsString(jsonEncode(pending))
          .then<void>((_) {}, onError: (Object _) {}),
    );
  }

  void _load() {
    final f = file;
    if (f == null || !f.existsSync()) return;
    try {
      final v = jsonDecode(f.readAsStringSync());
      if (v is List) {
        for (final r in v) {
          if (r is Map) pending.add(Map<String, Object?>.from(r));
        }
      }
    } catch (_) {}
  }
}
