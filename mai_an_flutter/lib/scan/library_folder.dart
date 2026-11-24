import 'dart:io';

import 'package:path_provider/path_provider.dart';

import '../core/app_config.dart';
import '../player/queue_store.dart' show Prefs;
import 'storage_paths.dart';

/// main.py `sync_config_to_ui` + library.py `start_scan`: the folder a scan
/// indexes. Read fresh each time, as Flet does, from flet_prefs.json's
/// `library_path` and config.toml's `[downloads] folder`; a stored path
/// that is empty or app-private falls back (never written back).
Future<String> resolveLibraryFolder(String dataDir) async {
  final config = await AppConfig.load(dataDir);
  final prefs = Prefs(dataDir);
  await prefs.load();
  final support = await getApplicationSupportDirectory();
  final sp = StoragePaths(
    StoragePlatform.current(
      dataDir: dataDir,
      privateRoots: [
        support.path,
        File(AppConfig.pathFor(dataDir)).parent.path,
      ],
    ),
  );
  final dl = config.downloadsFolder;
  final library = sp.resolveLibraryDir(prefs.getString('library_path', ''), dl);
  final target = sp.resolveDownloadDir(dl, library);
  return sp.resolveLibraryDir(library, target);
}
