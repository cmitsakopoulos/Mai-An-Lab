import 'dart:io';

import 'package:flutter/foundation.dart';

/// Port of `main.py` `_prune_caches_async`: keeps the album-artwork and
/// stream-preview caches bounded without blocking the UI.
class CachePruner {
  CachePruner(
    this.dataDir, {
    Future<String?> Function()? nativeArtworkCacheDir,
    DateTime Function()? clock,
  }) : _nativeArtworkCacheDir = nativeArtworkCacheDir,
       _clock = clock ?? DateTime.now;

  final String dataDir;
  final Future<String?> Function()? _nativeArtworkCacheDir;
  final DateTime Function() _clock;

  /// `main.py`: 21600 s (6 hours).
  static const cooldown = Duration(hours: 6);

  /// `main.py`: keep the 100 most recently modified artwork files.
  static const maxArtworkFiles = 100;

  /// `main.py`: 86400 s (24 hours) for preview files and folders.
  static const previewMaxAge = Duration(hours: 24);

  DateTime? _lastRun;
  bool _running = false;

  /// Prunes caches if at least [cooldown] has passed since the last run (or
  /// unconditionally when [force] is true). Returns true if a prune ran.
  Future<bool> pruneIfDue({bool force = false}) async {
    if (_running) return false;
    final now = _clock();
    final last = _lastRun;
    if (!force && last != null && now.difference(last) < cooldown) {
      return false;
    }
    _running = true;
    _lastRun = now;
    try {
      final sep = Platform.pathSeparator;
      await _pruneArtworkDir(Directory('$dataDir${sep}temp'));
      final nativeArt = await _nativeArtworkCacheDir?.call();
      if (nativeArt != null && nativeArt.isNotEmpty) {
        await _pruneArtworkDir(Directory(nativeArt));
      }
      await _prunePreviewDir(Directory('$dataDir${sep}previews'), now);
      return true;
    } catch (e) {
      debugPrint('Cache pruning failed: $e');
      return false;
    } finally {
      _running = false;
    }
  }

  Future<void> _pruneArtworkDir(Directory dir) async {
    if (!await dir.exists()) return;
    final files = <(File, DateTime)>[];
    await for (final entity in dir.list()) {
      if (entity is! File) continue;
      final name = entity.uri.pathSegments.where((s) => s.isNotEmpty).last;
      if (name == '.nomedia') continue;
      try {
        final stat = await entity.stat();
        files.add((entity, stat.modified));
      } on FileSystemException {
        // Skip unreadable entries, as Python does.
      }
    }
    if (files.length <= maxArtworkFiles) return;
    files.sort((a, b) => a.$2.compareTo(b.$2)); // oldest first
    final toDelete = files.length - maxArtworkFiles;
    for (var i = 0; i < toDelete; i++) {
      try {
        await files[i].$1.delete();
      } on FileSystemException {
        // Ignore racing deletions.
      }
    }
  }

  Future<void> _prunePreviewDir(Directory dir, DateTime now) async {
    if (!await dir.exists()) return;
    final cutoff = now.subtract(previewMaxAge);
    await for (final entity in dir.list()) {
      try {
        final stat = await entity.stat();
        if (stat.modified.isBefore(cutoff)) {
          await entity.delete(recursive: true);
        }
      } on FileSystemException {
        // Ignore racing deletions.
      }
    }
  }
}
