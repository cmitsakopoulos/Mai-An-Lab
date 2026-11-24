import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/app_config.dart';
import '../core/haptics.dart';
import '../library/library_controller.dart';
import '../scan/library_folder.dart';
import '../settings/config_controller.dart';
import '../sources/streamrip_api.dart';
import '../sources/tagger.dart';
import '../sync/sync_manager.dart';
import 'companion_download.dart';
import 'download_history.dart';
import 'download_queue.dart';

const _appChannel = MethodChannel('mai_an/app');

/// The app's side of the download queue (main.py's `app` as
/// queue_controller.py sees it). Dispatches Companion transfers to
/// [companionDownload] and Qobuz/Deezer downloads to [downloadFromSource]
/// with pure-Dart [tagFile] tagging, history logging, and Android media scan.
class AppDownloadHost implements DownloadHost {
  AppDownloadHost({
    required this.dataDir,
    required this.library,
    required this.haptics,
    required this.onSnack,
    this.config,
    this.history,
    this.clientFactory,
    this.tagWriter = tagFile,
    this.downloadDirOverride,
    this.isAppPrivateOverride,
  });

  final String dataDir;
  final LibraryController library;
  final Haptics haptics;
  final void Function(String text, {IconData? icon, Color? color}) onSnack;
  final ConfigController? config;
  final DownloadHistoryStore? history;
  final SourceClientFactory? clientFactory;
  final TrackTagWriter tagWriter;
  final Future<String> Function()? downloadDirOverride;
  final bool Function(String path)? isAppPrivateOverride;

  /// The Companion manager: it hears how each Mac transfer ended.
  CompanionSyncManager? sync;

  @override
  void snack(String text) => onSnack(text);

  @override
  Future<String> downloadDir() async {
    if (downloadDirOverride != null) return downloadDirOverride!();
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
    final isPrivate =
        isAppPrivateOverride ?? (await resolveFolders(dataDir)).$3.isAppPrivate;
    final parsed = detectTypeAndId(job.url);
    if (parsed == null) {
      throw FormatException(
        'Could not parse Qobuz, Deezer, or Companion URL: ${job.url}',
      );
    }

    if (parsed.source == 'companion') {
      return companionDownload(
        job.url,
        dir,
        progress: progress,
        isCancelled: isCancelled,
        isAppPrivate: isPrivate,
        onSaved: Platform.isAndroid ? _mediaScan : null,
      );
    }

    final srcConfig =
        config?.config.sources ?? (await AppConfig.load(dataDir)).sources;
    final rawQuality = job.metadata['quality'];
    final quality = rawQuality is num ? rawQuality.toInt() : null;
    final saved = await downloadFromSource(
      url: job.url,
      downloadDir: dir,
      config: srcConfig,
      quality: quality,
      isAppPrivate: isPrivate,
      progressCallback: progress,
      isCancelled: isCancelled,
      clientFactory: clientFactory,
      tagWriter: tagWriter,
      onFileSaved: Platform.isAndroid ? _mediaScan : null,
    );
    if (history != null && saved.isNotEmpty) {
      await history!.add({...job.metadata, 'url': job.url});
    }
    return saved;
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
