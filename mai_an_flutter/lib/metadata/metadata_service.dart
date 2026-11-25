import 'dart:async';

import 'package:flutter/foundation.dart';

import '../core/progress_notify.dart';
import '../data/library_repository.dart';
import '../sources/source_client.dart';
import 'metadata_enrich.dart';

/// main.py's metadata plumbing: the automatic artist-metadata pass
/// (`schedule_metadata_enrichment`, `_enrich_metadata_async`,
/// `_run_enrich_pass`) — once per launch 30 s after start, and after any
/// scan that indexed something — plus `refresh_walk_models`, the coalesced
/// rebuild every enrichment write goes through before auto-play sees it.
class MetadataService {
  MetadataService({
    required this.repository,
    required this.reloadLibrary,
    required this.autoEnrichEnabled,
    required this.preferredProvider,
    this.qobuzClient,
    ProgressNotifier Function()? notifier,
    this.refreshDebounce = const Duration(milliseconds: 1500),
  }) : _notifier =
           notifier ??
           (() => ProgressNotifier('metadata', 'Fetching artist metadata'));

  /// `_BOOT_ENRICH_DELAY_S`.
  static const bootDelay = Duration(seconds: 30);

  final LibraryRepository? Function() repository;

  /// The library's rows and station graphs reloaded (album genres and the
  /// artist graph follow the enrichment).
  final Future<void> Function() reloadLibrary;
  final bool Function() autoEnrichEnabled;

  /// `_prefs["sync_provider"]`: 'musicbrainz' or 'qobuz'.
  final String Function() preferredProvider;
  final Future<SourceClient> Function()? qobuzClient;
  final ProgressNotifier Function() _notifier;
  final Duration refreshDebounce;

  bool _bootDone = false;
  bool _running = false;
  bool _rerun = false;
  Timer? _timer;

  /// A pass is running (the workbench's Sync waits for it, as Flet's
  /// single-flight state does).
  bool get running => _running;
  Completer<void>? _idle;

  /// Completes when no pass is running (at once when none is).
  Future<void> whenIdle() => _idle?.future ?? Future.value();

  /// The dependencies an enrichment pass runs with; [refresh] runs the walk
  /// models now (a pass ends with one rebuild, not a debounced one).
  EnrichDeps? deps() {
    final repo = repository();
    if (repo == null || !repo.writable) return null;
    return EnrichDeps(
      repo: repo,
      refresh: refreshNow,
      qobuzClient: qobuzClient,
    );
  }

  /// The boot catch-up, once per process.
  void scheduleBoot() {
    if (_bootDone) return;
    _bootDone = true;
    schedule(delay: bootDelay);
  }

  /// `schedule_metadata_enrichment`: the pass in [delay]. Coalesced: a pass
  /// already waiting sees what was just indexed; one already running goes
  /// round once more when done.
  void schedule({Duration delay = const Duration(seconds: 5)}) {
    if (_running) {
      _rerun = true;
      return;
    }
    if (_timer?.isActive ?? false) return;
    _timer = Timer(delay, () => unawaited(runPending()));
  }

  /// `_enrich_metadata_async`: single-flight, honouring
  /// `general.auto_enrich_metadata`.
  Future<void> runPending() async {
    if (_running) {
      _rerun = true;
      return;
    }
    if (!autoEnrichEnabled()) return;
    _running = true;
    final idle = _idle = Completer<void>();
    try {
      do {
        _rerun = false;
        await _runPass();
      } while (_rerun);
    } finally {
      _running = false;
      _idle = null;
      idle.complete();
    }
  }

  Future<void> _runPass() async {
    final d = deps();
    if (d == null) return;
    ProgressNotifier? notifier;
    var summary = <String, Object?>{};
    try {
      final provider = preferredProvider();
      notifier = _notifier();
      final n = notifier;
      // Failures don't raise the notification: an offline launch would
      // otherwise post "connection lost" for a pass nobody asked for.
      summary = await enrichPending(
        d,
        preferred: provider,
        progress: (i, total, name, res) {
          if (res['status'] != 'error') {
            n.update(i, total, metadataProgressText(i, total, name));
          }
        },
      );
    } catch (e) {
      debugPrint('Metadata enrichment failed: $e');
      summary = {'status': 'error: $e'};
    } finally {
      notifier?.finish(
        metadataSummaryText(summary),
        metadataRetryAction(summary),
      );
    }
  }

  Future<Map<String, Object?>>? _refreshing;
  Timer? _refreshTimer;
  final _refreshWaiters = <Completer<Map<String, Object?>>>[];

  /// `_do_refresh_walk_models` and the station rebuild, now.
  Future<Map<String, Object?>> refreshNow() async {
    final repo = repository();
    if (repo == null) return const {};
    final prior = _refreshing;
    if (prior != null) await prior;
    final run = () async {
      final out = await repo.refreshWalkModels();
      try {
        await reloadLibrary();
      } catch (e) {
        debugPrint('library reload after metadata refresh failed: $e');
      }
      return out;
    }();
    _refreshing = run;
    try {
      return await run;
    } finally {
      if (identical(_refreshing, run)) _refreshing = null;
    }
  }

  /// `refresh_walk_models`: a coalesced rebuild after ANY enrichment write.
  /// Calls inside the debounce window collapse into one; the future
  /// completes when the rebuild has landed ("Auto-Play updated" is true).
  Future<Map<String, Object?>> refreshWalkModels() {
    final c = Completer<Map<String, Object?>>();
    _refreshWaiters.add(c);
    _refreshTimer?.cancel();
    _refreshTimer = Timer(refreshDebounce, () async {
      final waiters = List.of(_refreshWaiters);
      _refreshWaiters.clear();
      Map<String, Object?> out;
      try {
        out = await refreshNow();
      } catch (e) {
        debugPrint('refresh_walk_models failed: $e');
        out = const {};
      }
      for (final w in waiters) {
        w.complete(out);
      }
    });
    return c.future;
  }

  void dispose() {
    _timer?.cancel();
    _refreshTimer?.cancel();
  }
}
