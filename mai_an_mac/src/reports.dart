import 'dart:convert';
import 'dart:io';

import 'home.dart';

/// Problem reports from the phone: a bug the user describes, or an error the
/// app caught on its own, each with the phone's recent log. Kept as
/// `reports/<id>.json` plus a readable `<id>.txt` (to paste into an issue or
/// an AI chat). The newest [keep] stay.
class ProblemReports {
  ProblemReports(this.home, this.settings, {this.keep = 100}) {
    dir.createSync(recursive: true);
    _load();
  }

  final MaiAnHome home;
  final Settings settings;
  final int keep;

  Directory get dir => Directory('${home.root.path}/reports');

  /// Newest first: the summaries the page lists.
  final items = <Map<String, Object?>>[];

  /// Reports newer than this were not opened on the page yet.
  int get seenAt => (settings.get('reports_seen_at') as num?)?.toInt() ?? 0;
  int get unseen => items.where((r) => (r['at'] as int) > seenAt).length;

  void markSeen() {
    if (items.isEmpty) return;
    settings.set('reports_seen_at', items.first['at']);
  }

  void _load() {
    final files = dir
        .listSync()
        .whereType<File>()
        .where((f) => f.path.endsWith('.json'))
        .toList();
    for (final f in files) {
      try {
        final v = jsonDecode(f.readAsStringSync());
        if (v is Map<String, Object?>) items.add(_summary(v));
      } catch (_) {}
    }
    items.sort((a, b) => (b['at'] as int).compareTo(a['at'] as int));
  }

  /// Store what the phone sent; returns its summary. Throws
  /// [FormatException] for a body that isn't a report.
  Map<String, Object?> add(
    Map<String, Object?> body, {
    required String device,
  }) {
    final kind = body['kind'] == 'error' ? 'error' : 'bug';
    final text = _str(body['text'], 4000).trim();
    final error = _str(body['error'], 4000).trim();
    if (text.isEmpty && error.isEmpty) {
      throw const FormatException('An empty report');
    }
    final now = DateTime.now().millisecondsSinceEpoch;
    var at = now;
    while (items.any((r) => r['at'] == at)) {
      at++;
    }
    final logs = body['logs'];
    final r = <String, Object?>{
      'id': '$at-$kind',
      'at': at,
      'happened_at': (body['at'] as num?)?.toInt() ?? now,
      'kind': kind,
      'device': device,
      'app_version': _str(body['app_version'], 40),
      'text': text,
      'error': error,
      'stack': _str(body['stack'], 20000),
      'count': (body['count'] as num?)?.toInt() ?? 1,
      'logs': [
        if (logs is List)
          for (final l in logs.take(1000)) _str(l, 2000),
      ],
    };
    final id = r['id'] as String;
    atomicWrite(File('${dir.path}/$id.json'), jsonEncode(r));
    atomicWrite(File('${dir.path}/$id.txt'), describe(r));
    items.insert(0, _summary(r));
    while (items.length > keep) {
      delete(items.last['id'] as String);
    }
    return items.first;
  }

  Map<String, Object?>? read(String id) {
    if (!_safe(id)) return null;
    try {
      final v = jsonDecode(File('${dir.path}/$id.json').readAsStringSync());
      return v is Map<String, Object?> ? v : null;
    } catch (_) {
      return null;
    }
  }

  String? textOf(String id) {
    final r = read(id);
    return r == null ? null : describe(r);
  }

  String? pathOf(String id) {
    if (!_safe(id)) return null;
    final f = File('${dir.path}/$id.txt');
    return f.existsSync() ? f.path : null;
  }

  void delete(String id) {
    if (!_safe(id)) return;
    for (final ext in ['json', 'txt']) {
      try {
        File('${dir.path}/$id.$ext').deleteSync();
      } catch (_) {}
    }
    items.removeWhere((r) => r['id'] == id);
  }

  void clear() {
    for (final id in [for (final r in items) r['id'] as String]) {
      delete(id);
    }
  }

  static bool _safe(String id) => RegExp(r'^\d+-(bug|error)$').hasMatch(id);

  static String _str(Object? v, int max) {
    if (v == null) return '';
    final s = '$v';
    return s.length > max ? '${s.substring(0, max)}…' : s;
  }

  static Map<String, Object?> _summary(Map<String, Object?> r) {
    final text = '${r['text'] ?? ''}'.trim();
    final error = '${r['error'] ?? ''}'.trim();
    final title = (text.isNotEmpty ? text : error).split('\n').first;
    return {
      'id': r['id'],
      'at': r['at'],
      'kind': r['kind'],
      'device': r['device'],
      'app_version': r['app_version'],
      'title': title.length > 140 ? '${title.substring(0, 140)}…' : title,
      'has_error': error.isNotEmpty,
      'count': r['count'],
      'log_lines': (r['logs'] as List?)?.length ?? 0,
    };
  }

  /// The whole report as text: what to paste into an issue.
  static String describe(Map<String, Object?> r) {
    final at = DateTime.fromMillisecondsSinceEpoch(
      (r['happened_at'] as num?)?.toInt() ?? (r['at'] as num).toInt(),
    );
    final b = StringBuffer()
      ..writeln(
        r['kind'] == 'error' ? 'Mai An error report' : 'Mai An problem report',
      )
      ..writeln('Phone: ${r['device']} · app ${r['app_version'] ?? '?'}')
      ..writeln('When: ${at.toIso8601String().substring(0, 19)}');
    if ((r['count'] as num? ?? 1) > 1) {
      b.writeln('Happened ${r['count']} times');
    }
    final text = '${r['text'] ?? ''}'.trim();
    if (text.isNotEmpty) b.writeln('\n$text');
    final error = '${r['error'] ?? ''}'.trim();
    if (error.isNotEmpty) b.writeln('\nError:\n$error');
    final stack = '${r['stack'] ?? ''}'.trim();
    if (stack.isNotEmpty) b.writeln('\nStack:\n$stack');
    final logs = r['logs'] as List? ?? const [];
    if (logs.isNotEmpty) {
      b.writeln('\nRecent log (${logs.length} lines):');
      for (final l in logs) {
        b.writeln(l);
      }
    }
    return b.toString();
  }
}
