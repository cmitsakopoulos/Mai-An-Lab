// Builds the scan goldens' folder layouts (tool/export_scan_goldens.py
// `apply`), so the port scans the same tree Python scanned.
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

List<int> _synchsafe(int n) => [
  (n >> 21) & 0x7F,
  (n >> 14) & 0x7F,
  (n >> 7) & 0x7F,
  n & 0x7F,
];

/// The export's `id3(3, [f23(fid, txt(0, value)), ...])`.
Uint8List id3v23Latin1(List<(String, String)> frames) {
  final body = BytesBuilder();
  for (final (fid, value) in frames) {
    final data = [0, ...latin1.encode(value)];
    body
      ..add(latin1.encode(fid))
      ..add([
        (data.length >> 24) & 0xFF,
        (data.length >> 16) & 0xFF,
        (data.length >> 8) & 0xFF,
        data.length & 0xFF,
        0,
        0,
      ])
      ..add(data);
  }
  final b = body.takeBytes();
  return Uint8List.fromList([
    ...ascii.encode('ID3'),
    3,
    0,
    0,
    ..._synchsafe(b.length),
    ...b,
  ]);
}

/// The export's `extra_sources`: files made per case, tags only.
Map<String, Uint8List> extraSources(Map<String, dynamic> extra) {
  final base = File('test/goldens/tags/plain_cbr.mp3').readAsBytesSync();
  return {
    for (final MapEntry(:key, :value) in extra.entries)
      key: Uint8List.fromList([
        ...id3v23Latin1([
          for (final f in value as List)
            ((f as List)[0] as String, f[1] as String),
        ]),
        ...base,
      ]),
  };
}

DateTime _mtime(num s) =>
    DateTime.fromMicrosecondsSinceEpoch((s * 1e6).round());

/// The export's `apply`: copy, symlink, delete, touch.
void applyLayout(
  String root,
  List<dynamic> entries,
  Map<String, Uint8List> extra,
) {
  for (final raw in entries.cast<Map<String, dynamic>>()) {
    if (raw['delete'] case final String p) {
      File('$root/$p').deleteSync();
      continue;
    }
    if (raw['touch'] case final Map<String, Object?> t) {
      File('$root/${t['path']}').setLastModifiedSync(_mtime(t['mtime'] as num));
      continue;
    }
    final e = (raw['copy'] as Map<String, dynamic>?) ?? raw;
    final dst = '$root/${e['path']}';
    Directory(File(dst).parent.path).createSync(recursive: true);
    if (e['symlink'] case final String target) {
      Link(dst).createSync(target);
      continue;
    }
    final src = e['src'] as String;
    final bytes =
        extra[src] ?? File('test/goldens/tags/$src').readAsBytesSync();
    File(dst)
      ..writeAsBytesSync(bytes)
      ..setLastModifiedSync(_mtime(e['mtime'] as num));
  }
}

/// The export's `relocate`: paths under [root] as `$ROOT/...`.
Object? relocate(Object? v, String root) => switch (v) {
  String s => s.replaceAll(root, r'$ROOT'),
  List<Object?> l => [for (final x in l) relocate(x, root)],
  Map<Object?, Object?> m => <String, Object?>{
    for (final e in m.entries) e.key as String: relocate(e.value, root),
  },
  _ => v,
};
