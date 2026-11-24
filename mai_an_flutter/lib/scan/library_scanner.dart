import 'dart:io';
import 'dart:isolate';

import 'package:flutter/foundation.dart';

import '../data/library_repository.dart';
import 'storage_paths.dart';
import 'tinytag.dart';

/// `LibraryScanner.supported_extensions`.
const kScanExtensions = [
  '.mp3',
  '.flac',
  '.m4a',
  '.wav',
  '.ogg',
  '.opus',
  '.aac',
  '.mp4',
];

/// `_get_disk_files`: every supported file under [root] with its mtime, in
/// `os.scandir` order (depth first, a folder's entries in directory order).
/// Symlinked files count; symlinked folders are not followed. Unreadable
/// entries and folders are skipped.
Map<String, double> diskFiles(String root, {bool Function()? stop}) {
  final out = <String, double>{};
  void scan(String dir) {
    if (stop?.call() ?? false) return;
    final List<FileSystemEntity> entries;
    try {
      entries = Directory(dir).listSync(followLinks: false);
    } on FileSystemException {
      return;
    }
    for (final e in entries) {
      if (stop?.call() ?? false) break;
      final isFile =
          e is File || (e is Link && FileSystemEntity.isFileSync(e.path));
      if (isFile) {
        if (_supported(e.path)) {
          try {
            final st = FileStat.statSync(e.path);
            if (st.type == FileSystemEntityType.notFound) continue;
            // Milliseconds: dart:io has no finer mtime (Python: ns). The
            // scanner only rereads a file whose mtime moved by over 1 s.
            out[e.path] = st.modified.microsecondsSinceEpoch / 1e6;
          } on FileSystemException {
            // skipped, as in Python
          }
        }
      } else if (e is Directory) {
        scan(e.path);
      }
    }
  }

  scan(root);
  return out;
}

bool _supported(String path) {
  final name = path.toLowerCase();
  return kScanExtensions.any(name.endsWith);
}

String _basename(String path) => path.substring(path.lastIndexOf('/') + 1);

/// `os.path.splitext(path)[1][1:].upper()`: leading dots of the name don't
/// start an extension.
String formatOf(String path) {
  final name = _basename(path);
  var i = name.lastIndexOf('.');
  if (i <= 0) return '';
  var j = 0;
  while (j < i && name[j] == '.') {
    j++;
  }
  if (j == i) return '';
  return name.substring(i + 1).toUpperCase();
}

bool _truthy(Object? v) => v != null && v != '' && v != 0;

/// `_read_tags_batch`: the files tinytag can read and that have a title or
/// an artist; the rest are skipped.
List<ScannedTrack> readTagsBatch(List<(String, double)> batch) {
  final out = <ScannedTrack>[];
  for (final (path, mtime) in batch) {
    try {
      final tag = TinyTag.get(path);
      if (!_truthy(tag.title) && !_truthy(tag.artist)) continue;
      out.add(
        ScannedTrack(
          title: _truthy(tag.title) ? tag.title! : _basename(path),
          artist: [
            tag.albumartist,
            tag.artist,
          ].firstWhere(_truthy, orElse: () => 'Unknown Artist')!,
          album: _truthy(tag.album) ? tag.album! : 'Unknown Album',
          year: tag.year,
          genre: tag.genre,
          duration: _truthy(tag.duration) ? tag.duration! : 0.0,
          path: path,
          format: formatOf(path),
          addedDate: mtime,
          bitrate: _truthy(tag.bitrate) ? tag.bitrate! : 0,
          trackNum: tag.track,
        ),
      );
    } catch (e) {
      debugPrint('Failed to parse $path: $e');
    }
  }
  return out;
}

/// Progress: [percent] is -1 while the folder is being walked, else 0–100.
typedef ScanProgress = void Function(double percent, String message);

/// `utils/library_scanner.py`: indexes the library folder into the
/// database. Only new and changed files (mtime moved by more than 1 s) are
/// read; files gone from disk are removed. A large scan (200 files or more)
/// drops the per-row insert trigger and rebuilds counts and search in one
/// pass at the end.
///
/// The folder walk and tag reading run on background isolates (Python:
/// `asyncio.to_thread`), the writes on the database's own isolate.
class LibraryScanner {
  LibraryScanner({
    required String targetFolder,
    required this.repo,
    this.onProgress,
    String? home,
  }) : targetFolder = targetFolder.isEmpty
           ? ''
           : absPath(targetFolder, home: home);

  final String targetFolder;
  final LibraryRepository repo;
  final ScanProgress? onProgress;
  bool _stop = false;

  static const batchSize = 500; // database insert granularity
  static const readChunkSize = 50; // tag-read granularity, for progress
  static const bulkThreshold = 200;

  void stop() => _stop = true;

  /// Runs the scan. Returns how many files were read (Python's completion
  /// count: files skipped for lacking tags count too), or null when stopped
  /// before the walk finished (Python returns without completing).
  Future<int?> run() async {
    if (targetFolder.isEmpty || !Directory(targetFolder).existsSync()) {
      debugPrint('Target folder does not exist or is empty.');
      return 0;
    }
    final dbState = await repo.pathMtimeMap();
    onProgress?.call(-1, 'Calculating library size...');
    final root = targetFolder;
    final disk = await Isolate.run(() => diskFiles(root));
    if (_stop) return null;

    final deleted = [
      for (final p in dbState.keys)
        if (!disk.containsKey(p)) p,
    ];
    if (deleted.isNotEmpty) await repo.deleteTracksByPaths(deleted);

    final toProcess = <(String, double)>[];
    disk.forEach((path, mtime) {
      final known = dbState[path];
      if (!dbState.containsKey(path) ||
          known == null ||
          (known - mtime).abs() > 1.0) {
        toProcess.add((path, mtime));
      }
    });

    final total = toProcess.length;
    if (total == 0) {
      await repo.pruneOrphans();
      return 0;
    }

    final aliases = await repo.artistAliases();
    final bulk = total >= bulkThreshold;
    if (bulk) await repo.beginBulkImport();
    var processed = 0;
    try {
      for (var i = 0; i < total && !_stop; i += batchSize) {
        final chunk = toProcess.sublist(i, _min(i + batchSize, total));
        final results = <ScannedTrack>[];
        for (var j = 0; j < chunk.length && !_stop; j += readChunkSize) {
          final sub = chunk.sublist(j, _min(j + readChunkSize, chunk.length));
          results.addAll(await Isolate.run(() => readTagsBatch(sub)));
          processed += sub.length;
          final current = sub.isEmpty ? '' : _basename(sub.last.$1);
          var msg = '$processed/$total';
          if (current.isNotEmpty) msg += ' — $current';
          onProgress?.call(processed / total * 100, msg);
        }
        if (results.isNotEmpty) {
          await repo.insertTracksBatch(results, aliases);
        }
      }
    } finally {
      if (bulk) await repo.endBulkImport();
    }
    await repo.checkpoint();
    await repo.pruneOrphans();
    return processed;
  }
}

int _min(int a, int b) => a < b ? a : b;
