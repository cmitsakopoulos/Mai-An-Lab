import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../analysis/library_analysis.dart';
import '../downloads/download_dock.dart';
import '../downloads/download_queue.dart';
import 'frost.dart';
import 'tokens.dart';

enum _ActivityKind { downloads, analysis }

/// Unified floating frosted glass pill above the mini player for background
/// activities (Downloads and on-phone DSP Acoustic Analysis).
///
/// - Shows one crisp pill at a time; when both Downloads and DSP run
///   simultaneously, a tappable `+1` badge lets the user toggle between them
///   without doubling the bottom chrome height.
/// - Displays a living 3-bar equalizer pulse for DSP analysis and a smooth
///   animated progress bar along the bottom edge.
/// - Morphs briefly into a completion/error summary when a job ends so the
///   user sees the outcome in-app without a missed popup.
class ActivityPill extends StatefulWidget {
  const ActivityPill({
    super.key,
    required this.analysis,
    required this.onSnack,
    this.downloads,
    this.onOpenAudioSettings,
    this.grouped = true,
  });

  final DspAnalysis analysis;
  final DownloadQueue? downloads;
  final void Function(String text) onSnack;
  final VoidCallback? onOpenAudioSettings;
  final bool grouped;

  /// Whether [ActivityPill] currently occupies space above the mini player.
  static bool isVisible(DspAnalysis analysis, DownloadQueue? downloads) =>
      analysis.running ||
      analysis.lastOutcome != null ||
      (downloads?.activeCount ?? 0) > 0 ||
      downloads?.lastBatchOutcome != null;

  @override
  State<ActivityPill> createState() => _ActivityPillState();
}

class _ActivityPillState extends State<ActivityPill> {
  _ActivityKind _preferred = _ActivityKind.downloads;

  @override
  Widget build(BuildContext context) {
    final listenables = <Listenable>[widget.analysis, ?widget.downloads];
    return ListenableBuilder(
      listenable: Listenable.merge(listenables),
      builder: (context, _) {
        final q = widget.downloads;
        final dlActive = (q?.activeCount ?? 0) > 0;
        final dspActive = widget.analysis.running;
        final bothActive = dlActive && dspActive;

        Widget? card;
        if (dlActive || dspActive) {
          final showDownloads = bothActive
              ? _preferred == _ActivityKind.downloads
              : dlActive;
          card = showDownloads
              ? _buildDownloadCard(context, q!, extraCount: bothActive ? 1 : 0)
              : _buildAnalysisCard(
                  context,
                  widget.analysis,
                  extraCount: bothActive ? 1 : 0,
                );
        } else if (q?.lastBatchOutcome case final dlDone?) {
          final queue = q!;
          card = _buildOutcomeCard(
            context,
            key: const ValueKey('outcome_dl'),
            title: dlDone.title,
            detail: dlDone.detail,
            isError: dlDone.hasFailures,
            actionLabel:
                dlDone.hasFailures && queue.retryableFailures().isNotEmpty
                ? 'Retry'
                : null,
            onAction: dlDone.hasFailures
                ? () {
                    final n = queue.retryFailed();
                    queue.clearOutcome();
                    if (n > 0) {
                      widget.onSnack('Retrying $n download${n != 1 ? 's' : ''}.');
                    }
                  }
                : null,
            onTap: () {
              queue.clearOutcome();
              showDownloadSheet(context, queue, widget.onSnack);
            },
            onDismiss: queue.clearOutcome,
          );
        } else if (widget.analysis.lastOutcome case final dspDone?) {
          card = _buildOutcomeCard(
            context,
            key: const ValueKey('outcome_dsp'),
            title: dspDone.title,
            detail: dspDone.detail,
            isError: dspDone.isError,
            onTap: () {
              widget.analysis.clearOutcome();
              widget.onOpenAudioSettings?.call();
            },
            onDismiss: widget.analysis.clearOutcome,
          );
        }

        final visible = card != null;
        return AnimatedSize(
          duration: Tokens.motionBase,
          curve: Tokens.curveIn,
          alignment: Alignment.bottomCenter,
          child: AnimatedSwitcher(
            duration: Tokens.motionFast,
            switchInCurve: Tokens.curveIn,
            switchOutCurve: Tokens.curveOut,
            transitionBuilder: (child, anim) => FadeTransition(
              opacity: anim,
              child: SlideTransition(
                position: Tween(
                  begin: const Offset(0, 0.25),
                  end: Offset.zero,
                ).animate(anim),
                child: child,
              ),
            ),
            child: visible ? card : const SizedBox.shrink(),
          ),
        );
      },
    );
  }

  Widget _buildDownloadCard(
    BuildContext context,
    DownloadQueue queue, {
    required int extraCount,
  }) {
    final accent = Theme.of(context).colorScheme.primary;
    final active = queue.activeCount;
    final current = queue.currentJob;
    final String headline;
    final String detail;
    final double? fraction;
    if (current != null) {
      headline = active > 1 ? '$active downloading' : current.title;
      final status = current.status.isEmpty ? 'Downloading' : current.status;
      detail = active > 1
          ? '$status · ${current.title}'
          : '$status${current.detail.isNotEmpty ? ' · ${current.detail}' : ''}';
      fraction = current.percent >= 0 ? (current.percent / 100).clamp(0, 1) : null;
    } else {
      headline = '$active queued';
      detail = 'Waiting to start…';
      fraction = null;
    }
    final pct = fraction != null ? '${(fraction * 100).round()}%' : null;

    return _PillFrame(
      key: const ValueKey('active_dl'),
      grouped: widget.grouped,
      progress: fraction,
      progressColor: accent,
      onTap: () => showDownloadSheet(context, queue, widget.onSnack),
      leading: _IconBadge(
        color: accent,
        child: Icon(Icons.arrow_downward_rounded, color: accent, size: 17),
      ),
      headline: headline,
      detail: detail,
      pctText: pct,
      pctColor: accent,
      trailing: [
        if (extraCount > 0) _ExtraBadge(count: extraCount, onTap: _toggleKind),
        const SizedBox(width: 4),
        const Icon(
          Icons.keyboard_arrow_up_rounded,
          color: Tokens.dim,
          size: 20,
        ),
      ],
    );
  }

  Widget _buildAnalysisCard(
    BuildContext context,
    DspAnalysis analysis, {
    required int extraCount,
  }) {
    final accent = Theme.of(context).colorScheme.primary;
    final headline = analysis.stageTitle.isNotEmpty
        ? analysis.stageTitle
        : 'Analysing library';
    final progress = analysis.progress;
    final pct = progress != null ? '${(progress * 100).round()}%' : null;

    final detailParts = <String>[];
    if (analysis.totalCount > 0) {
      detailParts.add('${analysis.doneCount} / ${analysis.totalCount} tracks');
    }
    if (analysis.etaText != null && analysis.etaText!.isNotEmpty) {
      detailParts.add(analysis.etaText!);
    }
    if (analysis.failedCount > 0) {
      detailParts.add('${analysis.failedCount} failed');
    }
    final detail = detailParts.isNotEmpty
        ? detailParts.join(' · ')
        : (analysis.status.isNotEmpty
              ? analysis.status
              : 'Extracting acoustic features…');

    return _PillFrame(
      key: const ValueKey('active_dsp'),
      grouped: widget.grouped,
      progress: progress,
      progressColor: accent,
      onTap: widget.onOpenAudioSettings,
      leading: _IconBadge(
        color: accent,
        child: _EqualizerPulse(color: accent),
      ),
      headline: headline,
      detail: detail,
      pctText: pct,
      pctColor: accent,
      trailing: [
        if (extraCount > 0) ...[
          _ExtraBadge(count: extraCount, onTap: _toggleKind),
          const SizedBox(width: 2),
        ],
        IconButton(
          icon: const Icon(Icons.close_rounded, size: 18),
          color: Tokens.dim,
          tooltip: 'Cancel analysis',
          visualDensity: VisualDensity.compact,
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
          onPressed: analysis.cancel,
        ),
      ],
    );
  }

  Widget _buildOutcomeCard(
    BuildContext context, {
    required Key key,
    required String title,
    required String detail,
    required bool isError,
    required VoidCallback onDismiss,
    String? actionLabel,
    VoidCallback? onAction,
    VoidCallback? onTap,
  }) {
    final color = isError ? Tokens.amber : Tokens.accentGreen;
    return Dismissible(
      key: key,
      direction: DismissDirection.horizontal,
      onDismissed: (_) => onDismiss(),
      child: _PillFrame(
        grouped: widget.grouped,
        progress: 1.0,
        progressColor: color.withValues(alpha: 0.6),
        onTap: onTap,
        leading: _IconBadge(
          color: color,
          child: Icon(
            isError ? Icons.warning_amber_rounded : Icons.check_rounded,
            color: color,
            size: 17,
          ),
        ),
        headline: title,
        detail: detail,
        trailing: [
          if (actionLabel != null && onAction != null) ...[
            InkWell(
              borderRadius: BorderRadius.circular(10),
              onTap: onAction,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
                decoration: BoxDecoration(
                  color: color.withValues(alpha: 0.18),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Text(
                  actionLabel,
                  style: TextStyle(
                    color: color,
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
            ),
            const SizedBox(width: 4),
          ],
          IconButton(
            icon: const Icon(Icons.close_rounded, size: 16),
            color: Tokens.dim,
            tooltip: 'Dismiss',
            visualDensity: VisualDensity.compact,
            padding: EdgeInsets.zero,
            constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
            onPressed: onDismiss,
          ),
        ],
      ),
    );
  }

  void _toggleKind() {
    setState(() {
      _preferred = _preferred == _ActivityKind.downloads
          ? _ActivityKind.analysis
          : _ActivityKind.downloads;
    });
  }
}

class _PillFrame extends StatelessWidget {
  const _PillFrame({
    super.key,
    required this.grouped,
    required this.leading,
    required this.headline,
    required this.detail,
    required this.progressColor,
    this.progress,
    this.pctText,
    this.pctColor,
    this.trailing = const [],
    this.onTap,
  });

  final bool grouped;
  final Widget leading;
  final String headline;
  final String detail;
  final double? progress;
  final Color progressColor;
  final String? pctText;
  final Color? pctColor;
  final List<Widget> trailing;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final hPad = Tokens.dockPad(context);
    return GlassSurface(
      radius: 16,
      grouped: grouped,
      margin: EdgeInsets.fromLTRB(hPad, 0, hPad, 8),
      child: Material(
        type: MaterialType.transparency,
        child: InkWell(
          onTap: onTap,
          child: Stack(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(12, 9, 8, 10),
                child: Row(
                  children: [
                    leading,
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(
                            children: [
                              Expanded(
                                child: Text(
                                  headline,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    color: Tokens.text,
                                    fontSize: 13,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                              ),
                              if (pctText != null) ...[
                                const SizedBox(width: 8),
                                Text(
                                  pctText!,
                                  style: TextStyle(
                                    color: pctColor ?? progressColor,
                                    fontSize: 11,
                                    fontWeight: FontWeight.w700,
                                    fontFeatures: const [
                                      FontFeature.tabularFigures(),
                                    ],
                                  ),
                                ),
                              ],
                            ],
                          ),
                          const SizedBox(height: 2),
                          Text(
                            detail,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Tokens.dim,
                              fontSize: 11,
                              height: 1.22,
                            ),
                          ),
                        ],
                      ),
                    ),
                    if (trailing.isNotEmpty) ...[
                      const SizedBox(width: 6),
                      ...trailing,
                    ],
                  ],
                ),
              ),
              Positioned(
                left: 0,
                right: 0,
                bottom: 0,
                child: _SmoothProgressBar(
                  value: progress,
                  color: progressColor,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _SmoothProgressBar extends StatelessWidget {
  const _SmoothProgressBar({required this.value, required this.color});

  final double? value;
  final Color color;

  @override
  Widget build(BuildContext context) {
    final bg = Colors.white.withValues(alpha: 0.08);
    if (value == null) {
      return LinearProgressIndicator(
        minHeight: 2.5,
        color: color,
        backgroundColor: bg,
      );
    }
    return TweenAnimationBuilder<double>(
      tween: Tween(end: value!.clamp(0.0, 1.0)),
      duration: const Duration(milliseconds: 260),
      curve: Curves.easeOutCubic,
      builder: (_, v, _) => LinearProgressIndicator(
        value: v,
        minHeight: 2.5,
        color: color,
        backgroundColor: bg,
      ),
    );
  }
}

class _IconBadge extends StatelessWidget {
  const _IconBadge({required this.color, required this.child});

  final Color color;
  final Widget child;

  @override
  Widget build(BuildContext context) => Container(
    width: 32,
    height: 32,
    decoration: BoxDecoration(
      color: color.withValues(alpha: 0.16),
      borderRadius: BorderRadius.circular(9),
    ),
    child: Center(child: child),
  );
}

class _ExtraBadge extends StatelessWidget {
  const _ExtraBadge({required this.count, required this.onTap});

  final int count;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Tooltip(
      message: 'Switch active task',
      child: InkWell(
        borderRadius: BorderRadius.circular(10),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
          decoration: BoxDecoration(
            color: accent.withValues(alpha: 0.18),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: accent.withValues(alpha: 0.35), width: 0.7),
          ),
          child: Text(
            '+$count',
            style: TextStyle(
              color: accent,
              fontSize: 11,
              fontWeight: FontWeight.w700,
              fontFeatures: const [FontFeature.tabularFigures()],
            ),
          ),
        ),
      ),
    );
  }
}

/// 3-bar living equalizer pulse shown while DSP analysis is running.
class _EqualizerPulse extends StatefulWidget {
  const _EqualizerPulse({required this.color});

  final Color color;

  @override
  State<_EqualizerPulse> createState() => _EqualizerPulseState();
}

class _EqualizerPulseState extends State<_EqualizerPulse>
    with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 900),
  )..repeat();

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: _ctrl,
    builder: (_, _) {
      final t = _ctrl.value * 2 * math.pi;
      final heights = [
        5.0 + 9.0 * (0.5 + 0.5 * math.sin(t)),
        5.0 + 11.0 * (0.5 + 0.5 * math.sin(t + 2.1)),
        5.0 + 9.0 * (0.5 + 0.5 * math.sin(t + 4.2)),
      ];
      return Row(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          for (var i = 0; i < 3; i++) ...[
            if (i > 0) const SizedBox(width: 2.5),
            Container(
              width: 3,
              height: heights[i],
              decoration: BoxDecoration(
                color: widget.color,
                borderRadius: BorderRadius.circular(1.5),
              ),
            ),
          ],
        ],
      );
    },
  );
}
