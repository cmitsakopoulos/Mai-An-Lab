import 'dart:async';
import 'dart:io';
import 'dart:isolate';

import 'package:flutter/foundation.dart';

import '../core/app_config.dart';
import '../data/state_export.dart';
import '../library/library_controller.dart';
import '../settings/config_controller.dart';

/// The boot snapshot's name. Flet's is `mai_an_lab_state_latest.zip`; until
/// cutover (§5) both apps sit on the phone and would overwrite each other's
/// snapshot, which the Mac offload and fresh_build_android.sh read, so the
/// port writes its own.
const kSnapshotName = 'mai_an_lab_port_state_latest.zip';

/// The bundle ingested at boot. Flet's is `mai_an_lab_state_import.zip`
/// (fresh_build_android.sh pushes it before launching Flet); the port takes
/// its own until cutover, so it never swallows one meant for Flet.
const kAutoImportName = 'mai_an_lab_port_state_import.zip';

/// Where main.py looks for the boot import, in order.
const kAutoImportDirs = ['/sdcard/Download', '/storage/emulated/0/Download'];

/// The app's state bundles (state_export.py's callers): Settings →
/// Advanced's export and import, the boot snapshot and import, and the
/// Companion hub's backup upload. File work runs on a background isolate.
class StateBundles {
  StateBundles({
    required this.dataDir,
    required this.dbPath,
    required this.library,
    required this.config,
    this.libraryFolder,
    String Function()? bundleDir,
  }) : _bundleDir = bundleDir ?? defaultBundleDir;

  final String dataDir;
  final String dbPath;
  final LibraryController library;
  final ConfigController config;

  /// main.py `library_folder`, for the boot snapshot's second copy.
  final Future<String> Function()? libraryFolder;
  final String Function() _bundleDir;

  String get configPath => AppConfig.pathFor(dataDir);

  /// search_history.py `get_search_history_path` on Android: the data dir.
  String get searchHistoryPath =>
      '$dataDir${Platform.pathSeparator}recent_searches.json';

  /// `_default_bundle_dir`.
  String get bundleDir => _bundleDir();

  /// One boot snapshot at a time (state_export.py `_SNAPSHOT_LOCK`).
  Future<void> _snapshots = Future.value();

  /// `export_state`: a timestamped bundle in [outDir] (the default bundle
  /// folder when null). Returns its path.
  Future<String> export({String? outDir}) {
    final db = dbPath, cfg = configPath, hist = searchHistoryPath;
    final dir = outDir ?? bundleDir;
    return Isolate.run(
      () => exportState(
        dbPath: db,
        configPath: cfg,
        searchHistoryPath: hist,
        outDir: dir,
      ),
    );
  }

  /// settings.py `_do_import_state`: the library closed, the files
  /// replaced, then the library reopened (migrated) and config.toml read
  /// again, so the app carries on with the imported state (Flet asks for a
  /// relaunch instead, §7).
  Future<ImportResult> import(String zipPath) async {
    final db = dbPath, cfg = configPath, hist = searchHistoryPath;
    final result = await library.replaceDatabase(
      () => Isolate.run(
        () => importState(
          zipPath: zipPath,
          dbPath: db,
          configPath: cfg,
          searchHistoryPath: hist,
        ),
      ),
    );
    await config.reload();
    return result;
  }

  /// main.py `_auto_export_state_snapshot`: the snapshot in the default
  /// bundle folder (the data dir when that is missing), then a copy in the
  /// library folder. Never throws; returns the first path, or null.
  Future<String?> autoExport() {
    final run = _snapshots.then((_) => _autoExport());
    _snapshots = run.then((_) {}, onError: (Object _) {});
    return run;
  }

  Future<String?> _autoExport() async {
    try {
      var target = bundleDir;
      if (!Directory(target).existsSync()) target = dataDir;
      final db = dbPath, cfg = configPath, hist = searchHistoryPath;
      final dir = target;
      final out = await Isolate.run(
        () => exportStateSnapshot(
          dbPath: db,
          configPath: cfg,
          outDir: dir,
          name: kSnapshotName,
          searchHistoryPath: hist,
        ),
      );
      debugPrint('Auto-export state snapshot: $out');
      final lib = await libraryFolder?.call() ?? '';
      if (lib.isNotEmpty &&
          Directory(lib).existsSync() &&
          _abs(lib) != _abs(target)) {
        final copy = '$lib/$kSnapshotName';
        try {
          await File(out).copy(copy);
          debugPrint('Auto-export state snapshot copied to library: $copy');
        } catch (e) {
          debugPrint('Failed to copy state snapshot to library: $e');
        }
      }
      return out;
    } catch (e) {
      debugPrint('Auto-export state snapshot failed (non-fatal): $e');
      return null;
    }
  }

  /// `os.path.abspath`, enough to compare two folders.
  static String _abs(String p) {
    var a = Directory(p).absolute.path;
    while (a.length > 1 && a.endsWith('/')) {
      a = a.substring(0, a.length - 1);
    }
    return a;
  }

  /// main.py `_heavy_init`'s auto-import, before the library opens: the
  /// first non-empty [kAutoImportName] in [dirs] replaces the state, is
  /// deleted, and `.onboarded` is written (a restored install is not a
  /// first run). True when a bundle was ingested; a failure is logged and
  /// leaves the bundle for the next start, as in Flet.
  static Future<bool> bootImport({
    required String dataDir,
    required String dbPath,
    List<String> dirs = kAutoImportDirs,
  }) async {
    String? zip;
    for (final d in dirs) {
      final f = File('$d/$kAutoImportName');
      if (f.existsSync()) {
        zip = f.path;
        break;
      }
    }
    // main.py checks the second path only when the first is missing, and
    // only then whether it is empty.
    if (zip == null || File(zip).lengthSync() == 0) return false;
    debugPrint('Auto-import state zip found at $zip. Ingesting...');
    final path = zip;
    final cfg = AppConfig.pathFor(dataDir);
    final hist = '$dataDir${Platform.pathSeparator}recent_searches.json';
    try {
      await Isolate.run(
        () => importState(
          zipPath: path,
          dbPath: dbPath,
          configPath: cfg,
          searchHistoryPath: hist,
        ),
      );
      File(path).deleteSync();
      debugPrint('Auto-import state ZIP processed and deleted.');
    } catch (e) {
      debugPrint('Auto-import startup hook failed: $e');
      return false;
    }
    try {
      File(
        '$dataDir${Platform.pathSeparator}.onboarded',
      ).writeAsStringSync('1');
    } on FileSystemException catch (e) {
      debugPrint("Auto-import: couldn't write the onboarding marker: $e");
    }
    return true;
  }
}
