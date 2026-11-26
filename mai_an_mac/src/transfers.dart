import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';

import 'home.dart';

/// `_transfer_id`: stable across runs, so re-sending a track replaces its
/// pending entry instead of duplicating it.
String transferId(String path, String quality) => sha1
    .convert(utf8.encode('$path\u0000$quality'))
    .toString()
    .substring(0, 16);

/// The path's hash used to name its transcode (transcoder.py).
String pathDigest(String path) =>
    sha1.convert(utf8.encode(path)).toString().substring(0, 12);

/// Tracks queued for the phone to pick up. Persisted, so quitting the Mac
/// program doesn't drop what the phone hasn't fetched yet.
class TransferQueue {
  TransferQueue(this.file);
  final File file;
  List<Map<String, Object?>> items = [];

  void load() {
    try {
      final v = jsonDecode(file.readAsStringSync());
      if (v is List) {
        // Files moved or deleted since are dropped, not offered and 404'd.
        items = [
          for (final t in v)
            if (t is Map<String, Object?> &&
                File((t['path'] as String?) ?? '').existsSync())
              t,
        ];
      }
    } catch (_) {}
  }

  void save() => atomicWrite(file, jsonEncode(items));

  bool hasPath(String path) => items.any((t) => t['path'] == path);

  void addAll(List<Map<String, Object?>> fresh) {
    final ids = {for (final f in fresh) f['id']};
    items = [
      for (final t in items)
        if (!ids.contains(t['id'])) t,
      ...fresh,
    ];
    save();
  }

  void ack(Iterable<String> ids) {
    final s = ids.toSet();
    items = [
      for (final t in items)
        if (!s.contains('${t['id']}')) t,
    ];
    save();
  }

  void clear() {
    items = [];
    save();
  }
}
