import 'dart:async';

import 'package:flutter/foundation.dart';

import '../core/db_worker.dart';
import '../core/progress_notify.dart';
import '../data/library_repository.dart';
import 'audio_features.dart' show AudioFeatures;
import 'dsp_analyser.dart';

/// Where an on-phone analysis was started from. Flet has three copies of
/// the flow that differ only in their messages: library.py
/// `_do_compute_dsp` (the Network setup card and the "not analysed yet"
/// notification's Analyse) and settings.py `_do_compute_dsp` /
/// `_do_recompute_all_dsp` (Database Management).
enum DspEntry { library, settings, recomputeAll }

enum DspNoticeKind { info, success, error }

/// A snackbar the flow asks for.
typedef DspNotice = ({String text, DspNoticeKind kind});

/// What the analysis needs from the library (LibraryController).
abstract interface class AnalysisHost {
  LibraryRepository? get repository;
  Future<AcousticBuildResult> rebuildSimilarityGraph({bool metadata = true});
}

/// The on-phone DSP compute (decision B): every track lacking current
/// features analysed, then the similarity graph rebuilt. One run at a
/// time; the `dsp` notification carries progress and Cancel while the app
/// is in the background, and the Network view shows [status] and
/// [progress] while it runs (library.py's "Computing Acoustic Features"
/// card).
class DspAnalysis extends ChangeNotifier {
  DspAnalysis(
    this._host, {
    Future<PcmDecoder?> Function()? decoder,
    NotificationBridge? bridge,
    @visibleForTesting this.analyse,
  }) : _decoder = decoder ?? PcmDecoder.platform,
       _bridge = bridge;

  final AnalysisHost _host;
  final Future<PcmDecoder?> Function() _decoder;
  final NotificationBridge? _bridge;

  /// Replaces [analyzeTrack] (tests).
  final Future<AudioFeatures> Function(PcmDecoder, String)? analyse;

  /// `_analyzing_dsp`.
  bool running = false;

  /// `_analyzer_progress`: 0..1, or null for the indeterminate stage.
  double? progress;

  /// `_analyzer_status`.
  String status = '';

  /// Structured progress fields for [ActivityPill] clean 2-line rendering.
  int doneCount = 0;
  int totalCount = 0;
  int failedCount = 0;
  String? etaText;
  String stageTitle = '';

  /// Brief completion/error summary shown in [ActivityPill] after a run ends.
  ({String title, String detail, bool isError})? lastOutcome;
  Timer? _outcomeTimer;

  bool _cancelled = false;
  bool _disposed = false;

  /// The in-app Cancel (the notification has its own).
  void cancel() => _cancelled = true;

  /// Dismisses the post-run completion banner immediately.
  void clearOutcome() {
    _outcomeTimer?.cancel();
    _outcomeTimer = null;
    if (lastOutcome != null) {
      lastOutcome = null;
      if (!_disposed) notifyListeners();
    }
  }

  void _setOutcome({
    required String title,
    required String detail,
    bool isError = false,
  }) {
    _outcomeTimer?.cancel();
    lastOutcome = (title: title, detail: detail, isError: isError);
    _outcomeTimer = Timer(const Duration(seconds: 4), clearOutcome);
    if (!_disposed) notifyListeners();
  }

  void _set({
    double? progress,
    required String status,
    String? stageTitle,
    int? doneCount,
    int? totalCount,
    int? failedCount,
    String? etaText,
  }) {
    this.progress = progress;
    this.status = status;
    if (stageTitle != null) this.stageTitle = stageTitle;
    if (doneCount != null) this.doneCount = doneCount;
    if (totalCount != null) this.totalCount = totalCount;
    if (failedCount != null) this.failedCount = failedCount;
    this.etaText = etaText;
    if (!_disposed) notifyListeners();
  }

  /// Runs the flow for [entry]; [notice] shows its snackbars.
  Future<AnalysisResult?> run(
    DspEntry entry, {
    void Function(DspNotice)? notice,
  }) async {
    void say(String text, [DspNoticeKind kind = DspNoticeKind.info]) =>
        notice?.call((text: text, kind: kind));
    String err(Object e) => e is DbWorkerException ? e.message : '$e';

    if (running) {
      say('Analysis is already running.');
      return null;
    }
    final repo = _host.repository;
    if (repo == null || !repo.writable) {
      say('The library is read-only.', DspNoticeKind.error);
      return null;
    }
    // The "not analysed yet" card is being acted on.
    if (entry == DspEntry.library) cancelAlert('scan', bridge: _bridge);
    clearOutcome();
    running = true;
    doneCount = 0;
    totalCount = 0;
    failedCount = 0;
    etaText = null;
    stageTitle = 'Analysing library';
    _cancelled = false;
    var timedOut = false;
    try {
      final decoder = await _decoder();
      if (decoder == null) {
        say(
          'Audio service not ready — native analyser unavailable.',
          DspNoticeKind.error,
        );
        return null;
      }
      if (entry == DspEntry.recomputeAll) {
        say('Purging acoustic features and starting full DSP re-analysis...');
        try {
          await repo.clearDspFeatures();
        } catch (e) {
          debugPrint('Advanced: clear_dsp_features failed: $e');
          say('DSP clear failed: ${err(e)}', DspNoticeKind.error);
          return null;
        }
      }
      final List<String> missing;
      try {
        missing = await repo.tracksMissingFeatures();
      } catch (e) {
        debugPrint('DSP compute: missing-feature query failed: $e');
        say('DSP query failed: ${err(e)}', DspNoticeKind.error);
        return null;
      }

      // Flet's Library flow posts no notification when there is nothing
      // to analyse; Settings' does (its graph stage).
      final notifier = entry == DspEntry.library && missing.isEmpty
          ? null
          : ProgressNotifier(
              'dsp',
              'Analysing library',
              onCancel: () {
                _cancelled = true;
                debugPrint('DSP analysis cancelled via system notification');
              },
              onTimeout: () {
                _cancelled = true;
                timedOut = true;
              },
              bridge: _bridge,
            );

      var result = (analysed: 0, failed: 0, total: 0);
      if (missing.isEmpty) {
        switch (entry) {
          case DspEntry.library:
            say('All tracks already analysed.', DspNoticeKind.success);
          case DspEntry.settings:
            say('All tracks already analysed — rebuilding graph and PCA.');
          case DspEntry.recomputeAll:
            break;
        }
      } else {
        final total = missing.length;
        switch (entry) {
          case DspEntry.library:
            break;
          case DspEntry.settings:
            say('Analysing $total tracks — this can take a while.');
          case DspEntry.recomputeAll:
            say(
              'Recomputing features for $total tracks — this can take a '
              'while.',
            );
        }
        _set(
          progress: 0,
          status: 'Analysing 1 / $total tracks...',
          stageTitle: 'Analysing library',
          doneCount: 0,
          totalCount: total,
          failedCount: 0,
        );
        final clock = Stopwatch()..start();
        try {
          result = await bulkAnalyzeLibrary(
            missing: missing,
            decoder: decoder,
            persist: repo.updateTrackFeatures,
            cancelled: () => _cancelled,
            analyse: analyse,
            progress: (done, total, current, failures) {
              final osText = dspProgressText(
                done,
                total,
                failures,
                clock.elapsed,
              );
              notifier?.update(
                done,
                total,
                osText,
                title: 'Analysing library ($done / $total)',
              );
              String? eta;
              if (done > 0 && total > done) {
                final perTrack = clock.elapsed.inMicroseconds / 1e6 / done;
                eta = formatEta(perTrack * (total - done));
              }
              final suffix = failures > 0 ? ' ($failures failed)' : '';
              final etaSuffix = eta != null ? ' ($eta)' : '';
              _set(
                progress: total > 0 ? done / total : 0.0,
                status: 'Analysing $done / $total$suffix$etaSuffix...',
                stageTitle: 'Analysing library',
                doneCount: done,
                totalCount: total,
                failedCount: failures,
                etaText: eta,
              );
            },
          );
        } catch (e) {
          debugPrint('DSP compute: bulk_analyze_library failed: $e');
          say('DSP analysis failed: ${err(e)}', DspNoticeKind.error);
          notifier?.finish('Analysis failed: ${err(e)}');
          _setOutcome(title: 'Analysis failed', detail: err(e), isError: true);
          return null;
        }
        if (_cancelled) {
          notifier?.finish(
            timedOut
                ? "Analysis paused: Android's background time limit was "
                      'reached. Start it again later to continue.'
                : 'Analysis cancelled.',
          );
          if (timedOut) {
            _setOutcome(
              title: 'Analysis paused',
              detail: 'Background limit reached — resume anytime.',
              isError: true,
            );
          }
          return result;
        }
      }

      _set(
        status: 'Linking similar tracks...',
        stageTitle: 'Linking similar tracks…',
        doneCount: result.total,
        totalCount: result.total,
        failedCount: result.failed,
      );
      notifier?.stage(
        'Linking similar tracks…',
        title: 'Building acoustic map',
      );
      var graphOk = true;
      try {
        await _host.rebuildSimilarityGraph();
      } catch (e) {
        graphOk = false;
        debugPrint('DSP compute: graph rebuild failed: $e');
        if (entry != DspEntry.library || missing.isNotEmpty) {
          say('Graph rebuild failed: ${err(e)}', DspNoticeKind.error);
        }
        if (entry != DspEntry.library) {
          notifier?.finish('Graph rebuild failed: ${err(e)}');
          _setOutcome(
            title: 'Graph rebuild failed',
            detail: err(e),
            isError: true,
          );
          return result;
        }
      }
      if (entry == DspEntry.library) {
        final suffix = result.failed > 0 ? ', ${result.failed} failed' : '';
        notifier?.finish(
          graphOk
              ? 'Analysed ${result.total} tracks$suffix. Similarity walks '
                    'are ready.'
              : 'Analysed ${result.total} tracks$suffix; graph rebuild '
                    'failed.',
        );
        if (result.total > 0) {
          _setOutcome(
            title: graphOk ? 'Acoustic index ready' : 'Analysed with issues',
            detail: graphOk
                ? '${result.total} tracks analysed$suffix · Auto-Play ready'
                : '${result.total} tracks analysed$suffix · Graph rebuild failed',
            isError: !graphOk || result.failed > 0,
          );
        }
      } else {
        notifier?.finish('Analysis complete. Similarity walks are ready.');
        _setOutcome(
          title: 'Acoustic index ready',
          detail: result.total > 0
              ? '${result.total} tracks analysed · Auto-Play ready'
              : 'Similarity graph rebuilt.',
          isError: result.failed > 0,
        );
        say(
          entry == DspEntry.recomputeAll
              ? 'DSP features, edges, and PCA space recomputed.'
              : 'DSP features, edges, and PCA space rebuilt.',
          DspNoticeKind.success,
        );
      }
      return result;
    } finally {
      running = false;
      progress = null;
      status = '';
      doneCount = 0;
      totalCount = 0;
      failedCount = 0;
      etaText = null;
      stageTitle = '';
      if (!_disposed) notifyListeners();
    }
  }

  @override
  void dispose() {
    _outcomeTimer?.cancel();
    _disposed = true;
    _cancelled = true;
    super.dispose();
  }
}
