import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:sqlite3/sqlite3.dart';

import '../core/zip_archive.dart';

/// utils/state_export.py: the state bundle, a ZIP of library.db (an online
/// backup, consistent while the app writes), config.toml and
/// recent_searches.json, with a manifest. Bundles cross between the Flet
/// app and the port in both directions. Synchronous: callers run these on
/// a background isolate.

/// `BUNDLE_VERSION`: import refuses any other.
const kBundleVersion = 3;

/// export_state's timestamped names, and the prefix its pruning keeps five
/// of (`mai_an_lab_state_*.zip`).
const kBundlePrefix = 'mai_an_lab_state_';

/// `_default_bundle_dir`: the phone's Downloads, else ~/Downloads.
String defaultBundleDir({String? home}) {
  for (final p in const ['/sdcard/Download', '/storage/emulated/0/Download']) {
    if (Directory(p).existsSync()) return p;
  }
  return '${home ?? Platform.environment['HOME'] ?? ''}/Downloads';
}

/// `time.strftime("%Y%m%d_%H%M%S")`, local time.
String bundleTimestamp(DateTime t) {
  String two(int v) => v.toString().padLeft(2, '0');
  return '${t.year.toString().padLeft(4, '0')}${two(t.month)}${two(t.day)}'
      '_${two(t.hour)}${two(t.minute)}${two(t.second)}';
}

/// `_snapshot_sqlite`: SQLite's online backup of [src] into a fresh [dst],
/// so a live connection (WAL or mid-transaction) needs no closing; a raw
/// copy when the source won't open as a database. Nothing for a missing or
/// empty source.
Future<void> snapshotSqlite(String src, String dst) async {
  final f = File(src);
  if (!f.existsSync() || f.lengthSync() == 0) return;
  try {
    // Read-only, so the source side never grows a WAL file.
    final from = sqlite3.open(src, mode: OpenMode.readOnly);
    try {
      final to = sqlite3.open(dst);
      try {
        // Python's backup(): every page in one step, copied as they are
        // (VACUUM INTO would compact the file).
        await from.backup(to, nPage: -1).drain<void>();
      } finally {
        to.close();
      }
    } finally {
      from.close();
    }
  } on SqliteException catch (e) {
    stderr.writeln('sqlite backup failed ($e); falling back to copy');
    f.copySync(dst);
  }
}

/// export_state's and export_state_snapshot's shared body: the ZIP at
/// [out], from the snapshot at [tmpDb] and the other files, with
/// [extra] in the manifest. Returns nothing; [out] is complete or absent.
void _writeBundle({
  required String out,
  required String tmpDb,
  required String? configPath,
  required String? searchHistoryPath,
  required Map<String, Object?> manifestHead,
}) {
  final contents = <String, Object?>{};
  final zip = ZipWriter(out);
  try {
    void add(String member, String? path) {
      if (path == null || path.isEmpty) return;
      final f = File(path);
      if (!f.existsSync()) return;
      zip.addFile(member, path);
      contents[member] = {'bytes': f.lengthSync()};
    }

    add('library.db', tmpDb);
    add('config.toml', configPath);
    add('recent_searches.json', searchHistoryPath);
    zip.addString(
      'manifest.json',
      const JsonEncoder.withIndent(
        '  ',
      ).convert({...manifestHead, 'contents': contents}),
    );
  } finally {
    zip.close();
  }
}

void _remove(String path) {
  try {
    File(path).deleteSync();
  } on FileSystemException {
    // Already gone.
  }
}

/// `export_state`: `<outDir>/mai_an_lab_state_<timestamp>.zip`, then the
/// folder's bundles pruned to the newest five. Returns the path.
Future<String> exportState({
  required String dbPath,
  required String? configPath,
  String? searchHistoryPath,
  String? outDir,
  DateTime? now,
}) async {
  final dir = outDir ?? defaultBundleDir();
  Directory(dir).createSync(recursive: true);
  final ts = bundleTimestamp(now ?? DateTime.now());
  final out = '$dir/$kBundlePrefix$ts.zip';
  final tmpDb = '$out.db.tmp';
  await snapshotSqlite(dbPath, tmpDb);
  try {
    _writeBundle(
      out: out,
      tmpDb: tmpDb,
      configPath: configPath,
      searchHistoryPath: searchHistoryPath,
      manifestHead: {'bundle_version': kBundleVersion, 'exported_at': ts},
    );
  } finally {
    _remove(tmpDb);
  }
  pruneBundles(dir);
  return out;
}

/// export_state's auto-prune: the folder's `mai_an_lab_state_*.zip` files
/// beyond the newest [keep] (by mtime) are deleted.
void pruneBundles(String dir, {int keep = 5}) {
  try {
    final found = <(DateTime, String)>[];
    for (final e in Directory(dir).listSync(followLinks: false)) {
      final name = e.uri.pathSegments.last;
      if (e is File &&
          name.startsWith(kBundlePrefix) &&
          name.endsWith('.zip')) {
        found.add((e.lastModifiedSync(), e.path));
      }
    }
    // Python sorts (mtime, path) tuples, newest first.
    found.sort((a, b) {
      final c = b.$1.compareTo(a.$1);
      return c != 0 ? c : b.$2.compareTo(a.$2);
    });
    for (final (_, path) in found.skip(keep)) {
      try {
        File(path).deleteSync();
      } on FileSystemException catch (e) {
        stderr.writeln('Failed to auto-prune $path: $e');
      }
    }
  } catch (e) {
    stderr.writeln('Error running state bundle auto-pruning: $e');
  }
}

/// `export_state_snapshot`: [name] (Flet's is `mai_an_lab_state_latest.zip`)
/// in [outDir], replaced atomically, so a crash never leaves half a bundle
/// and a reader never sees one. Unique temp names, so overlapping exports
/// never delete each other's database copy. Throws rather than publish a
/// bundle without the library when the snapshot fails.
Future<String> exportStateSnapshot({
  required String dbPath,
  required String? configPath,
  required String outDir,
  required String name,
  String? searchHistoryPath,
  DateTime? now,
}) async {
  Directory(outDir).createSync(recursive: true);
  final out = '$outDir/$name';
  final tag = Random().nextInt(1 << 32).toRadixString(16).padLeft(8, '0');
  final tmpDb = '$out.$tag.db.tmp';
  final tmpZip = '$out.$tag.writing.tmp';
  try {
    await snapshotSqlite(dbPath, tmpDb);
    final db = File(dbPath);
    if (!File(tmpDb).existsSync() && db.existsSync() && db.lengthSync() > 0) {
      throw StateError(
        'library DB snapshot failed; not publishing a bundle without it '
        '($dbPath)',
      );
    }
    _writeBundle(
      out: tmpZip,
      tmpDb: tmpDb,
      configPath: configPath,
      searchHistoryPath: searchHistoryPath,
      manifestHead: {
        'bundle_version': kBundleVersion,
        'exported_at': bundleTimestamp(now ?? DateTime.now()),
        'auto_snapshot': true,
      },
    );
    File(tmpZip).renameSync(out);
  } finally {
    _remove(tmpDb);
    _remove(tmpZip);
  }
  return out;
}

/// `inspect_bundle`: the manifest, or `{"error": …}` when it is missing or
/// unreadable. Throws [BadZipFile] for a file that is not a ZIP.
Map<String, Object?> inspectBundle(String zipPath) {
  final zip = ZipReader(zipPath);
  try {
    final raw = zip.read('manifest.json');
    if (raw == null) return {'error': 'missing manifest.json'};
    try {
      final m = jsonDecode(utf8.decode(raw));
      if (m is Map<String, Object?>) return m;
      // Python returns any JSON value; only a mapping has .get().
      return {'error': 'manifest parse failed: not an object'};
    } on FormatException catch (e) {
      return {'error': 'manifest parse failed: ${e.message}'};
    }
  } finally {
    zip.close();
  }
}

/// Python's `str()` of a JSON value, for the version mismatch text.
String _pyStr(Object? v) => switch (v) {
  null => 'None',
  true => 'True',
  false => 'False',
  final double d when d == d.truncateToDouble() && d.isFinite => '$d',
  _ => '$v',
};

/// `import_state`'s result: the members replaced (in that order) and where
/// they went, and the manifest.
typedef ImportResult = ({
  Map<String, String> replaced,
  Map<String, Object?> manifest,
});

/// `import_state`: replaces the live files with the bundle's (each written
/// beside its target, then renamed over it), and deletes library.db's
/// `-wal` and `-shm` so SQLite can't replay the old journal onto the new
/// file. The caller closes the database first and reopens it after.
/// Throws [BundleError] with Python's texts (`ValueError`) for a bad
/// manifest or another bundle version, [BadZipFile] for a non-ZIP.
ImportResult importState({
  required String zipPath,
  required String dbPath,
  required String? configPath,
  String? searchHistoryPath,
}) {
  final manifest = inspectBundle(zipPath);
  if (manifest['error'] case final e?) throw BundleError('$e');
  if (manifest['bundle_version'] != kBundleVersion) {
    throw BundleError(
      'bundle_version ${_pyStr(manifest['bundle_version'])} '
      '!= expected $kBundleVersion',
    );
  }
  final replaced = <String, String>{};
  final zip = ZipReader(zipPath);
  try {
    final names = zip.names.toSet();
    void replace(String member, String? target) {
      if (target == null || target.isEmpty || !names.contains(member)) return;
      File(target).parent.createSync(recursive: true);
      final tmp = '$target.import.tmp';
      zip.extractTo(member, tmp);
      File(tmp).renameSync(target);
      replaced[member] = target;
    }

    replace('library.db', dbPath);
    for (final suffix in const ['-wal', '-shm']) {
      final stale = File('$dbPath$suffix');
      if (stale.existsSync()) {
        try {
          stale.deleteSync();
        } on FileSystemException catch (e) {
          stderr.writeln('Failed to delete stale SQLite journal $stale: $e');
        }
      }
    }
    replace('config.toml', configPath);
    replace('recent_searches.json', searchHistoryPath);
  } finally {
    zip.close();
  }
  return (replaced: replaced, manifest: manifest);
}

/// Python's `ValueError` from import_state: the text is shown as is
/// ("Import failed: …").
class BundleError implements Exception {
  BundleError(this.message);
  final String message;
  @override
  String toString() => message;
}
