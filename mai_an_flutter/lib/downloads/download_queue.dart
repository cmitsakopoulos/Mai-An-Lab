import 'dart:async';

import 'package:flutter/foundation.dart';

import '../core/progress_notify.dart';
import '../sync/sync_manager.dart' show TransferQueue;
import 'companion_download.dart';
import 'file_names.dart';

/// queue_controller.py: the download queue (despite the Flet name, not the
/// play queue). Owns the jobs and their lifecycle and knows nothing about
/// any screen: the dock is one listener among any.
///
/// Until the source clients exist (decision A) the only jobs are the Mac's
/// transfers (`companion`); anything else fails with Python's "Could not
/// parse" message.

enum JobState {
  queued,
  downloading,
  done,
  failed,
  cancelled;

  bool get terminal => this == done || this == failed || this == cancelled;
}

/// How many finished jobs the dock keeps showing. Bounded: this list lives
/// for the whole session.
const historyLimit = 30;

/// One download, as the dock renders it. Mutable, like Python's job dict.
class DownloadJob {
  DownloadJob._({
    required this.id,
    required this.url,
    required this.metadata,
    required this.source,
    required this.mediaType,
    required this.title,
    required this.artist,
    required this.image,
    required this.qualityLabel,
  });

  final String id;
  final String url;
  final Map<String, Object?> metadata;
  final String source;
  final String mediaType;
  final String title;
  final String artist;
  final String image;
  final String qualityLabel;
  JobState state = JobState.queued;
  double percent = 0;
  String status = 'Queued';
  String detail = '';

  /// The Mac transfer this job downloads, if any.
  String? get transferId {
    final t = metadata['companion_transfer_id'];
    return (t == null || t == '') ? null : '$t';
  }

  DownloadJob _copy(String id) => DownloadJob._(
    id: id,
    url: url,
    metadata: metadata,
    source: source,
    mediaType: mediaType,
    title: title,
    artist: artist,
    image: image,
    qualityLabel: qualityLabel,
  );
}

/// What the queue needs from the app.
abstract class DownloadHost {
  void snack(String text);

  /// `pick_download_dir(target_folder or default, library_folder)`.
  Future<String> downloadDir();

  /// Download [job] into [dir]; Python's `streamrip_api.download`.
  Future<List<String>> download(
    DownloadJob job,
    String dir, {
    required DownloadProgress progress,
    required bool Function() isCancelled,
  });

  /// Store features that came with the job; null when there is no writable
  /// library.
  Future<void> Function(String path, Map<String, Object?> dsp)?
  get persistFeatures;

  /// `play_success_notification`.
  void downloadSucceeded();

  /// A quiet library scan (library.py `start_scan(quiet=True)`).
  void quietScan();

  /// The sync manager's `on_transfer_finished`.
  void transferFinished(String transferId, {required bool ok});
}

String _pyStr(Object? v) =>
    (v == null || v == '' || v == false || v == 0) ? '' : '$v';

class DownloadQueue implements Listenable, TransferQueue {
  DownloadQueue(
    this.host, {
    NotificationBridge? bridge,
    Stopwatch? clock,
    this.retryDelay = _defaultRetryDelay,
  }) : _bridge = bridge,
       _clock = clock ?? (Stopwatch()..start());

  final DownloadHost host;
  final NotificationBridge? _bridge;
  final Stopwatch _clock;

  /// `5 * 2 ** attempt` seconds before retry [attempt] + 1.
  final Duration Function(int attempt) retryDelay;
  static Duration _defaultRetryDelay(int attempt) =>
      Duration(seconds: 5 * (1 << attempt));

  final _pending = <DownloadJob>[];
  final _history = <DownloadJob>[];
  DownloadJob? currentJob;
  bool isProcessing = false;
  bool _cancel = false;
  int _ids = 0;
  final _listeners = <VoidCallback>[];
  bool _scanNeeded = false;

  /// The running batch's notification (which also keeps the native task
  /// service, and the download, alive with the screen off).
  ProgressNotifier? _notifier;
  final _batchCounts = {
    JobState.done: 0,
    JobState.failed: 0,
    JobState.cancelled: 0,
  };

  /// The failed jobs of the current or last batch: the summary card's Retry
  /// covers exactly the failures it reports.
  final _batchFailedIds = <String>[];

  /// Tests hold the worker to inspect queued jobs (Python replaces
  /// `_ensure_worker`).
  @visibleForTesting
  bool holdWorker = false;

  Future<void>? _worker;

  /// The running worker (tests await it).
  @visibleForTesting
  Future<void>? get worker => _worker;

  // ── observation ───────────────────────────────────────────────────────────

  @override
  void addListener(VoidCallback listener) {
    if (!_listeners.contains(listener)) _listeners.add(listener);
  }

  @override
  void removeListener(VoidCallback listener) => _listeners.remove(listener);

  void _notify() {
    for (final fn in List.of(_listeners)) {
      try {
        fn();
      } catch (e) {
        debugPrint('queue listener failed: $e');
      }
    }
  }

  // ── introspection ─────────────────────────────────────────────────────────

  /// Jobs still waiting.
  List<DownloadJob> get pending => List.unmodifiable(_pending);

  @visibleForTesting
  List<DownloadJob> get history => List.unmodifiable(_history);

  /// Everything the dock renders: active first, then waiting, then the
  /// newest finished.
  List<DownloadJob> get jobs => [
    ?currentJob,
    ..._pending,
    ..._history.reversed,
  ];

  /// Jobs that have not reached a terminal state.
  int get activeCount => _pending.length + (currentJob != null ? 1 : 0);

  bool get isIdle => activeCount == 0;

  @visibleForTesting
  bool get cancelRequested => _cancel;

  DownloadJob? find(String id) {
    for (final j in jobs) {
      if (j.id == id) return j;
    }
    return null;
  }

  // ── job construction ──────────────────────────────────────────────────────

  /// `_quality_int`: streamrip's quality number for a source and tier.
  static int? qualityInt(String source, String tier) {
    final src = source.toLowerCase();
    if (tier == 'mp3') return 1;
    if (tier == 'cd') return 2;
    if (tier == 'hires') {
      if (src == 'qobuz') return 4;
      if (src == 'tidal') return 3;
      if (src == 'deezer') return 2;
    }
    return null;
  }

  DownloadJob _makeJob(Map<String, Object?> item, String tier) {
    final source = (item['source'] ?? 'qobuz').toString();
    final mediaType = (item['media_type'] ?? 'track').toString();
    final id = (item['id'] ?? '').toString();
    final url = _pyStr(item['url']).isNotEmpty
        ? '${item['url']}'
        : 'https://www.$source.com/$mediaType/$id';
    final meta = Map<String, Object?>.of(item);
    meta['quality'] = qualityInt(source, tier);
    // Mac transfers carry the Mac's actual encode ("AAC 320"); the tier only
    // says the phone takes the bytes as they are.
    final label = _pyStr(item['quality_label']).isNotEmpty
        ? '${item['quality_label']}'
        : tier.toUpperCase();
    meta['quality_label'] = label;
    String first(List<String> keys, String fallback) {
      for (final k in keys) {
        final v = _pyStr(meta[k]);
        if (v.isNotEmpty) return v;
      }
      return fallback;
    }

    return DownloadJob._(
      id: 'dl${++_ids}',
      url: url,
      metadata: meta,
      source: source,
      mediaType: mediaType,
      title: first(['ui_title', 'name'], 'Unknown'),
      artist: first(['ui_subtitle', 'artist'], ''),
      image: first(['image', 'image_url'], ''),
      qualityLabel: label,
    );
  }

  // ── public API ────────────────────────────────────────────────────────────

  DownloadJob enqueue(Map<String, Object?> item, {String qualityTier = 'mp3'}) {
    final job = _makeJob(item, qualityTier);
    _pending.add(job);
    _ensureWorker();
    _notify();
    return job;
  }

  /// One notification and one worker check for the whole batch, so 40
  /// selected tracks don't schedule 40 UI passes.
  @override
  List<DownloadJob> enqueueMany(
    List<Map<String, Object?>> items, {
    String qualityTier = 'mp3',
  }) {
    final jobs = [for (final i in items) _makeJob(i, qualityTier)];
    _pending.addAll(jobs);
    if (jobs.isNotEmpty) {
      _ensureWorker();
      _notify();
    }
    return jobs;
  }

  void _ensureWorker() {
    if (holdWorker || _worker != null) return;
    // Started on the next turn, as asyncio.create_task is.
    _worker = Future(_workerLoop);
  }

  /// Drop one job; cancels it if it is the one in flight.
  bool remove(String id) {
    if (currentJob?.id == id) {
      cancelCurrent();
      return true;
    }
    final i = _pending.indexWhere((j) => j.id == id);
    if (i >= 0) {
      final job = _pending.removeAt(i)
        ..state = JobState.cancelled
        ..status = 'Removed';
      _pushHistory(job);
      _notify();
      return true;
    }
    // Already finished: drop it from the history outright.
    final h = _history.indexWhere((j) => j.id == id);
    if (h >= 0) {
      _history.removeAt(h);
      _notify();
      return true;
    }
    return false;
  }

  /// Re-queue a finished job.
  DownloadJob? retry(String id) {
    final job = find(id);
    if (job == null || !job.state.terminal) return null;
    _history.removeWhere((j) => j.id == id);
    final fresh = job._copy('dl${++_ids}');
    _pending.add(fresh);
    _ensureWorker();
    _notify();
    return fresh;
  }

  /// The last batch's failed jobs a Retry can re-queue. Mac transfers are
  /// left out: the sync manager offers those again on its next poll, so
  /// retrying them here as well would download them twice.
  List<DownloadJob> retryableFailures() => [
    for (final j in _history)
      if (_batchFailedIds.contains(j.id) &&
          j.state == JobState.failed &&
          j.transferId == null)
        j,
  ];

  /// The summary notification's Retry.
  int retryFailed() {
    var n = 0;
    for (final j in retryableFailures()) {
      if (retry(j.id) != null) n++;
    }
    return n;
  }

  int clearFinished() {
    final n = _history.length;
    _history.clear();
    _notify();
    return n;
  }

  /// Cancel everything, the job in flight included.
  int clear() {
    final n = activeCount;
    for (final job in _pending) {
      job
        ..state = JobState.cancelled
        ..status = 'Cancelled';
      _pushHistory(job);
    }
    _pending.clear();
    _cancel = true;
    _notify();
    host.snack('Queue cleared ($n item${n != 1 ? 's' : ''}).');
    return n;
  }

  void cancelCurrent() {
    _cancel = true;
    host.snack('Cancellation requested…');
  }

  // ── worker ────────────────────────────────────────────────────────────────

  /// Every terminal job passes through here exactly once.
  @visibleForTesting
  void pushHistory(DownloadJob job) => _pushHistory(job);

  void _pushHistory(DownloadJob job) {
    _reportCompanionResult(job);
    _history.add(job);
    if (_history.length > historyLimit) {
      _history.removeRange(0, _history.length - historyLimit);
    }
  }

  /// Tests replace the per-job workflow (Python patches `_workflow`).
  @visibleForTesting
  Future<void> Function(DownloadJob job)? workflowOverride;

  Future<void> _workerLoop() async {
    isProcessing = true;
    final notifier = _notifier = ProgressNotifier(
      'download',
      'Downloads',
      bridge: _bridge,
    )..setCancelHandler(clear);
    _batchCounts.updateAll((_, _) => 0);
    _batchFailedIds.clear();
    _notify();
    try {
      while (_pending.isNotEmpty) {
        final job = _pending.removeAt(0);
        currentJob = job;
        job
          ..state = JobState.downloading
          ..status = 'Starting…';
        _cancel = false;
        _notify();
        _postProgress(job);

        await (workflowOverride ?? _workflow)(job);

        if (_batchCounts.containsKey(job.state)) {
          _batchCounts[job.state] = _batchCounts[job.state]! + 1;
        }
        if (job.state == JobState.failed) _batchFailedIds.add(job.id);
        _pushHistory(job);
        currentJob = null;
        _notify();

        if (_pending.isNotEmpty) {
          // A brief beat between jobs, so a burst doesn't look like one
          // continuous blur of progress.
          await Future<void>.delayed(const Duration(milliseconds: 400));
        }
      }
    } finally {
      isProcessing = false;
      _worker = null;
      _cancel = false;
      // Also on a crash: an unfinished notifier would keep the native task
      // service (and its wakelock) running.
      notifier.finish(
        _batchSummary(),
        retryableFailures().isNotEmpty ? ('Retry', 'downloads:retry') : null,
      );
      _notifier = null;
      _notify();
      // Re-index once, after the whole queue drains.
      if (_scanNeeded) {
        _scanNeeded = false;
        Timer(const Duration(seconds: 1), host.quietScan);
      }
    }
  }

  /// Change a job and tell the observers. Status prose never names the
  /// backend: the source is a badge.
  @visibleForTesting
  void set(
    DownloadJob job, {
    String? status,
    double? percent,
    String? detail,
    JobState? state,
  }) {
    if (status != null) job.status = status;
    if (percent != null) job.percent = percent;
    if (detail != null) job.detail = detail;
    if (state != null) job.state = state;
    _notify();
    _postProgress(job);
  }

  /// The batch bar: finished jobs count 100 each, the running one its
  /// percent. The total grows when more is queued mid-batch.
  void _postProgress(DownloadJob job) {
    final notifier = _notifier;
    if (notifier == null || !identical(job, currentJob)) return;
    final finished = _batchCounts.values.fold(0, (a, b) => a + b);
    final total = finished + activeCount;
    final pct = job.percent.clamp(0.0, 100.0);
    var head = job.title.isEmpty ? 'Unknown' : job.title;
    if (job.artist.isNotEmpty) head += ' — ${job.artist}';
    final count = total > 1 ? '${finished + 1} of $total' : '';
    final text = [
      head,
      count,
      job.status,
    ].where((p) => p.isNotEmpty).join(' · ');
    notifier.update(finished * 100 + pct.toInt(), total * 100, text);
  }

  /// The done notification's text; empty (the notification is removed)
  /// when everything was cancelled.
  String _batchSummary() {
    final done = _batchCounts[JobState.done]!;
    final failed = _batchCounts[JobState.failed]!;
    if (done == 0 && failed == 0) return '';
    final parts = [
      if (done > 0) '$done download${done != 1 ? 's' : ''} finished',
      if (failed > 0) '$failed failed',
    ];
    return '${parts.join(' · ')}.';
  }

  Future<void> _sleep(Duration d) => Future<void>.delayed(d);

  Future<void> _workflow(DownloadJob job) async {
    var lastUpdate = const Duration(days: -1);
    void progressHook(String status, int? pct, String message) {
      final now = _clock.elapsed;
      if (pct == null ||
          pct >= 100 ||
          now - lastUpdate > const Duration(milliseconds: 250)) {
        lastUpdate = now;
        final s = status.isEmpty
            ? ''
            : status[0].toUpperCase() + status.substring(1).toLowerCase();
        set(
          job,
          status: s.isEmpty ? 'Downloading' : s,
          percent: pct?.toDouble(),
          detail: message,
        );
      }
    }

    try {
      // Falls back to the library folder, then the public Music folder,
      // when the download folder can't be written; never to app storage.
      final target = await host.downloadDir();
      for (var attempt = 0; attempt < 3; attempt++) {
        try {
          if (_cancel) throw const JobCancelled();
          set(
            job,
            status: 'Initializing…',
            percent: 5,
            detail: attempt == 0
                ? 'Contacting API…'
                : 'Contacting API… (attempt ${attempt + 1} of 3)',
          );
          final paths = await host.download(
            job,
            target,
            progress: progressHook,
            isCancelled: () => _cancel,
          );
          final dsp = job.metadata['dsp_features'];
          final persist = host.persistFeatures;
          if (dsp is Map &&
              dsp.isNotEmpty &&
              paths.isNotEmpty &&
              persist != null) {
            try {
              await persist(
                normalizeNfc(paths.first),
                dsp.cast<String, Object?>(),
              );
            } catch (e) {
              debugPrint('Failed to persist pre-computed DSP features: $e');
            }
          }
          break;
        } on JobCancelled {
          rethrow;
        } catch (e) {
          if (attempt < 2) {
            final wait = retryDelay(attempt);
            set(
              job,
              status: 'Retrying',
              percent: 0,
              detail: '$e. Retrying in ${wait.inSeconds}s…',
            );
            final halves = wait.inMilliseconds ~/ 500;
            for (var i = 0; i < halves; i++) {
              if (_cancel) throw const JobCancelled();
              await _sleep(const Duration(milliseconds: 500));
            }
          } else {
            throw _Failed('Failed after 3 attempts: $e');
          }
        }
      }
      set(
        job,
        state: JobState.done,
        status: 'Finished',
        percent: 100,
        detail: 'Download complete',
      );
      try {
        host.downloadSucceeded();
      } catch (e) {
        debugPrint('success notification failed: $e');
      }
      _scanNeeded = true;
    } on JobCancelled {
      set(
        job,
        state: JobState.cancelled,
        status: 'Cancelled',
        percent: 0,
        detail: 'Aborted by user',
      );
    } catch (e) {
      set(
        job,
        state: JobState.failed,
        status: 'Failed',
        percent: 0,
        detail: '$e',
      );
    }
  }

  /// Tell the sync manager how a Mac transfer ended: it acks the Mac only
  /// for files that arrived, so a failure stays queued there.
  void _reportCompanionResult(DownloadJob job) {
    final tid = job.transferId;
    if (tid == null) return;
    try {
      host.transferFinished(tid, ok: job.state == JobState.done);
    } catch (e) {
      debugPrint('companion transfer report failed: $e');
    }
  }
}

class _Failed implements Exception {
  const _Failed(this.message);
  final String message;
  @override
  String toString() => message;
}
