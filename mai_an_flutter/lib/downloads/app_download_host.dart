import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/haptics.dart';
import '../library/library_controller.dart';
import '../scan/library_folder.dart';
import '../sync/sync_manager.dart';
import 'companion_download.dart';
import 'download_queue.dart';

const _appChannel = MethodChannel('mai_an/app');

/// The app's side of the download queue (main.py's `app` as
/// queue_controller.py sees it).
class AppDownloadHost implements DownloadHost {
  AppDownloadHost({
    required this.dataDir,
    required this.library,
    required this.haptics,
    required this.onSnack,
  });

  final String dataDir;
  final LibraryController library;
  final Haptics haptics;
  final void Function(String text, {IconData? icon, Color? color}) onSnack;

  /// The Companion manager: it hears how each Mac transfer ended.
  CompanionSyncManager? sync;

  @override
  void snack(String text) => onSnack(text);

  @override
  Future<String> downloadDir() async {
    final (lib, target, paths) = await resolveFolders(dataDir);
    return paths.pickDownloadDir(target, lib);
  }

  @override
  Future<List<String>> download(
    DownloadJob job,
    String dir, {
    required DownloadProgress progress,
    required bool Function() isCancelled,
  }) async {
    final (_, _, paths) = await resolveFolders(dataDir);
    return companionDownload(
      job.url,
      dir,
      progress: progress,
      isCancelled: isCancelled,
      isAppPrivate: paths.isAppPrivate,
      onSaved: Platform.isAndroid ? _mediaScan : null,
    );
  }

  /// streamrip_api.py's Android step: make the new file visible to other
  /// apps (Flet broadcasts the deprecated MEDIA_SCANNER_SCAN_FILE; the port
  /// asks MediaScannerConnection).
  static Future<void> _mediaScan(String path) async {
    try {
      await _appChannel.invokeMethod<void>('scanMediaFile', path);
    } catch (e) {
      debugPrint('media scan of $path failed: $e');
    }
  }

  @override
  Future<void> Function(String path, Map<String, Object?> dsp)?
  get persistFeatures {
    final repo = library.repository;
    if (repo == null || !repo.writable) return null;
    return repo.persistFeatures;
  }

  @override
  void downloadSucceeded() => haptics.trigger('vibrate');

  @override
  void quietScan() => library.scan(quiet: true);

  @override
  void transferFinished(String transferId, {required bool ok}) =>
      sync?.onTransferFinished(transferId, ok: ok);
}
