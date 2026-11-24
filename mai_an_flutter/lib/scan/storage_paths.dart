import 'dart:io';

/// `utils/storage_paths.py`: where music lives. One resolver for the library
/// and download folders.
///
/// Every user-facing folder must be one the user can reach from a file
/// manager. On Android 11+ the app's sandbox (`/data/user/0/<pkg>/…`, and
/// the app-specific `/storage/emulated/0/Android/data/<pkg>/…`) is sealed:
/// no files app can open it without root, so a track saved there is lost.
///
/// The rules:
///  * An app-private path is never returned, whatever is stored.
///  * Downloads follow the library: no download folder, or an unusable one,
///    means "download into the library folder".
///  * Fallbacks are resolved in memory and never written back. The user's
///    settings change only when they change them.
///
/// Python reads the platform and the sandbox roots from globals; here they
/// are a [StoragePlatform], so tests can stand in for Android.
class StoragePlatform {
  const StoragePlatform({
    required this.isAndroid,
    required this.realHome,
    this.appHome,
    this.privateRoots = const [],
  });

  /// This device. [privateRoots] are the app's own directories (Python:
  /// the Flet sandbox env vars, the temp dir, the config dir, the data dir).
  factory StoragePlatform.current({
    required String dataDir,
    List<String> privateRoots = const [],
  }) {
    final android = Platform.isAndroid;
    return StoragePlatform(
      isAndroid: android,
      realHome: android
          ? '/storage/emulated/0'
          : (Platform.environment['HOME'] ?? '/'),
      appHome: dataDir,
      privateRoots: [dataDir, ...privateRoots, Directory.systemTemp.path],
    );
  }

  final bool isAndroid;

  /// `real_home()`: the user's own home, never the app's data dir.
  final String realHome;

  /// What `~` expands to. The Flet app points HOME at its data dir
  /// (main.py), so a stored `~/Music` means a folder inside the sandbox,
  /// which is never usable.
  final String? appHome;
  final List<String> privateRoots;

  static const androidMusicDirs = [
    '/storage/emulated/0/Music',
    '/sdcard/Music',
  ];
}

/// `os.path.normpath` (POSIX).
String normpath(String path) {
  if (path.isEmpty) return '.';
  final initial = path.startsWith('/')
      // POSIX allows exactly two leading slashes to mean something else.
      ? (path.startsWith('//') && !path.startsWith('///') ? '//' : '/')
      : '';
  final out = <String>[];
  for (final c in path.split('/')) {
    if (c.isEmpty || c == '.') continue;
    if (c != '..' ||
        (initial.isEmpty && out.isEmpty) ||
        (out.isNotEmpty && out.last == '..')) {
      out.add(c);
    } else if (out.isNotEmpty) {
      out.removeLast();
    }
  }
  final s = initial + out.join('/');
  return s.isEmpty ? '.' : s;
}

/// `os.path.abspath(os.path.expanduser(path))`, with `~` as [home].
String absPath(String path, {String? home}) {
  var p = path;
  if (home != null && (p == '~' || p.startsWith('~/'))) {
    p = home + p.substring(1);
  }
  if (!p.startsWith('/')) p = '${Directory.current.path}/$p';
  return normpath(p);
}

class StoragePaths {
  const StoragePaths(this.platform);
  final StoragePlatform platform;

  /// `os.path.expanduser`.
  String _expand(String p) => (p == '~' || p.startsWith('~/'))
      ? (platform.appHome ?? platform.realHome) + p.substring(1)
      : p;

  /// `_norm`: absolute, normalised, symlinks resolved where the path exists.
  String _norm(String path) {
    final p = absPath(_expand(path));
    if (!p.startsWith('/')) return p;
    try {
      return normpath(Directory(p).resolveSymbolicLinksSync());
    } on FileSystemException {
      return p;
    }
  }

  static bool _under(String path, String root) {
    final r = root.endsWith('/') ? root.substring(0, root.length - 1) : root;
    return path == root || path.startsWith('$r/');
  }

  List<String> _privateRoots() {
    final home = _norm(platform.realHome);
    // The user's home is never a sandbox, even when a root sits above it.
    return {
      for (final r in platform.privateRoots)
        if (r.isNotEmpty) _norm(r),
    }.where((r) => !_under(home, r)).toList();
  }

  /// `is_app_private`: inside storage only this app can see.
  bool isAppPrivate(String? path) {
    if (path == null || path.isEmpty) return false;
    final p = _norm(path);
    if (platform.isAndroid) {
      final raw = absPath(path);
      if (_under(raw, '/data') || _under(p, '/data')) return true;
      // App-specific external storage: other apps (file managers included)
      // are locked out of these since Android 11.
      final parts = p.split('/');
      final i = parts.indexOf('Android');
      if (i >= 0 &&
          i + 1 < parts.length &&
          (parts[i + 1] == 'data' || parts[i + 1] == 'obb')) {
        return true;
      }
    }
    return _privateRoots().any((r) => _under(p, r));
  }

  /// `usable`: set, absolute, and not app-private. A missing folder is fine
  /// (it is created at download time).
  bool usable(String? path) {
    if (path == null || path.trim().isEmpty) return false;
    final p = _expand(path.trim());
    return p.startsWith('/') && !isAppPrivate(p);
  }

  /// `default_music_dir`: the platform's public music folder.
  String defaultMusicDir() {
    if (platform.isAndroid) {
      for (final d in StoragePlatform.androidMusicDirs) {
        if (Directory(d).existsSync()) return d;
      }
      return StoragePlatform.androidMusicDirs.first;
    }
    return '${platform.realHome}/Music';
  }

  /// `resolve_library_dir`: the user's choice, else their download folder,
  /// else the public Music folder.
  String resolveLibraryDir(String? libraryPref, [String? downloadCfg]) {
    for (final cand in [libraryPref, downloadCfg]) {
      if (usable(cand)) return _expand(cand!.trim());
    }
    return defaultMusicDir();
  }

  /// `resolve_download_dir`: the user's choice, else the library folder.
  String resolveDownloadDir(String? downloadCfg, String libraryDir) {
    if (usable(downloadCfg)) return _expand(downloadCfg!.trim());
    return usable(libraryDir) ? libraryDir : defaultMusicDir();
  }
}
