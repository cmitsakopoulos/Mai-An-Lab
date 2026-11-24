import 'package:flutter/material.dart';

import '../ui/tokens.dart';
import 'download_queue.dart';

/// ui/player/download_dock.py: the app-wide download surface. A collapsed
/// pill above the mini player while anything is active, and a full-height
/// sheet with every job. Both read the same [DownloadQueue], so they can't
/// disagree.

/// tokens.py `SOURCE_COLORS` (widgets.py `src_color`).
Color sourceColor(String source) => switch (source.toLowerCase()) {
  'qobuz' => const Color(0xFFFF9F0A),
  'deezer' => const Color(0xFFBF5AF2),
  'tidal' => const Color(0xFF0A84FF),
  'soundcloud' => const Color(0xFFFF5500),
  'companion' => const Color(0xFF30D158),
  _ => Tokens.text,
};

/// `STATE_VISUALS`: one row per state.
(IconData, Color) _stateVisual(JobState s, Color cyan) => switch (s) {
  JobState.queued => (Icons.schedule_rounded, Tokens.dim),
  JobState.downloading => (Icons.arrow_circle_down_rounded, cyan),
  JobState.done => (Icons.check_circle_rounded, Tokens.accentGreen),
  JobState.failed => (Icons.error_rounded, Tokens.accentRed),
  JobState.cancelled => (Icons.remove_circle_rounded, Tokens.dim),
};

double? _fraction(double pct) => pct >= 0 ? pct / 100 : null;

/// The collapsed pill: fades and slides in while jobs are active.
class DownloadPill extends StatelessWidget {
  const DownloadPill({super.key, required this.queue, required this.onSnack});

  final DownloadQueue queue;
  final void Function(String text) onSnack;

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: queue,
      builder: (context, _) {
        final active = queue.activeCount;
        final shown = active > 0;
        return AnimatedSlide(
          offset: shown ? Offset.zero : const Offset(0, 0.3),
          duration: const Duration(milliseconds: 220),
          curve: Curves.easeOutCubic,
          child: AnimatedOpacity(
            opacity: shown ? 1 : 0,
            duration: const Duration(milliseconds: 180),
            curve: Curves.easeOut,
            // Flet hides it outright when idle; no space is kept.
            child: shown ? _pill(context, active) : const SizedBox.shrink(),
          ),
        );
      },
    );
  }

  Widget _pill(BuildContext context, int active) {
    final cyan = Theme.of(context).colorScheme.primary;
    final current = queue.currentJob;
    String headline, detail;
    double? fraction;
    if (current != null) {
      headline = active > 1 ? '$active downloading' : current.title;
      final status = current.status.isEmpty ? 'Downloading' : current.status;
      detail = active > 1
          ? '$status · ${current.title}'
          : '$status${current.detail.isNotEmpty ? ' · ${current.detail}' : ''}';
      fraction = _fraction(current.percent);
    } else {
      headline = '$active queued';
      detail = 'Waiting to start…';
    }
    return Padding(
      padding: const EdgeInsets.only(left: 10, right: 10, bottom: 6),
      child: Material(
        color: Tokens.surface,
        clipBehavior: Clip.antiAlias,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(14),
          side: const BorderSide(color: Tokens.borderSubtle),
        ),
        child: InkWell(
          onTap: () => showDownloadSheet(context, queue, onSnack),
          child: Stack(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(14, 9, 10, 8),
                child: Row(
                  children: [
                    SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(
                        value: fraction,
                        strokeWidth: 2.5,
                        color: cyan,
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            headline,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Tokens.text,
                              fontSize: 13,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          const SizedBox(height: 1),
                          Text(
                            detail,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Tokens.dim,
                              fontSize: 11,
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(width: 10),
                    const Icon(
                      Icons.keyboard_arrow_up_rounded,
                      color: Tokens.dim,
                      size: 20,
                    ),
                  ],
                ),
              ),
              Positioned(
                left: 0,
                right: 0,
                bottom: 0,
                child: LinearProgressIndicator(
                  value: fraction,
                  color: cyan,
                  backgroundColor: Tokens.surface2,
                  minHeight: 2,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// `expand`: the full-height sheet.
Future<void> showDownloadSheet(
  BuildContext context,
  DownloadQueue queue,
  void Function(String text) onSnack,
) => showModalBottomSheet<void>(
  context: context,
  isScrollControlled: true,
  useSafeArea: true,
  backgroundColor: Tokens.surface,
  shape: const RoundedRectangleBorder(
    borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
  ),
  builder: (_) => DownloadSheet(queue: queue, onSnack: onSnack),
);

class DownloadSheet extends StatelessWidget {
  const DownloadSheet({super.key, required this.queue, required this.onSnack});

  final DownloadQueue queue;
  final void Function(String text) onSnack;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: MediaQuery.sizeOf(context).height,
      child: ListenableBuilder(
        listenable: queue,
        builder: (context, _) {
          final jobs = queue.jobs;
          final active = queue.activeCount;
          final finished = jobs.length - active;
          final count = active > 0
              ? '$active in progress'
                    '${finished > 0 ? ' · $finished finished' : ''}'
              : (finished > 0 ? '$finished finished' : 'Nothing queued');
          return Column(
            children: [
              Padding(
                padding: const EdgeInsets.only(top: 10, bottom: 6),
                child: Container(
                  width: 36,
                  height: 5,
                  decoration: BoxDecoration(
                    color: Tokens.surfaceElevated,
                    borderRadius: BorderRadius.circular(3),
                  ),
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 2, 20, 6),
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text(
                        'Downloads',
                        style: TextStyle(
                          color: Tokens.text,
                          fontSize: 17,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        count,
                        style: const TextStyle(color: Tokens.dim, fontSize: 12),
                      ),
                    ],
                  ),
                ),
              ),
              const Divider(color: Tokens.borderSubtle, height: 1),
              Expanded(
                child: jobs.isEmpty
                    ? const Center(
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Icon(
                              Icons.download_done_rounded,
                              color: Tokens.dim,
                              size: 48,
                            ),
                            SizedBox(height: 8),
                            Text(
                              'Nothing downloading',
                              style: TextStyle(color: Tokens.dim, fontSize: 14),
                            ),
                          ],
                        ),
                      )
                    : Scrollbar(
                        child: ListView.separated(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 12,
                            vertical: 8,
                          ),
                          itemCount: jobs.length,
                          separatorBuilder: (_, _) => const SizedBox(height: 8),
                          itemBuilder: (context, i) => _JobRow(
                            job: jobs[i],
                            queue: queue,
                            onSnack: onSnack,
                          ),
                        ),
                      ),
              ),
            ],
          );
        },
      ),
    );
  }
}

class _JobRow extends StatelessWidget {
  const _JobRow({
    required this.job,
    required this.queue,
    required this.onSnack,
  });

  final DownloadJob job;
  final DownloadQueue queue;
  final void Function(String text) onSnack;

  @override
  Widget build(BuildContext context) {
    final cyan = Theme.of(context).colorScheme.primary;
    final state = job.state;
    final (icon, tint) = _stateVisual(state, cyan);
    final downloading = state == JobState.downloading;
    final accent = sourceColor(job.source);
    final thumb = Container(
      width: 44,
      height: 44,
      decoration: BoxDecoration(
        color: Tokens.surface2,
        borderRadius: BorderRadius.circular(Tokens.radiusThumb),
        border: Border.all(color: Tokens.borderSubtle),
      ),
      clipBehavior: Clip.antiAlias,
      // Mac transfers carry no artwork URL; remote art comes with the
      // sources (phase 6).
      child: job.image.startsWith('http')
          ? Image.network(
              job.image,
              fit: BoxFit.cover,
              errorBuilder: (_, _, _) => Icon(icon, color: tint, size: 22),
            )
          : Icon(icon, color: tint, size: 22),
    );
    Widget badge(String text, Color color, double alpha) => Container(
      padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
      decoration: BoxDecoration(
        color: color.withValues(alpha: alpha),
        borderRadius: BorderRadius.circular(4),
      ),
      child: Text(
        text,
        style: TextStyle(
          color: color == Tokens.dim ? Tokens.dim : color,
          fontSize: 9,
          fontWeight: FontWeight.w700,
        ),
      ),
    );
    final statusLine = job.detail.isNotEmpty
        ? '${job.status} · ${job.detail}'
        : job.status;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
      decoration: BoxDecoration(
        color: downloading ? tint.withValues(alpha: 0.10) : Tokens.surface2,
        borderRadius: BorderRadius.circular(Tokens.radiusCard),
        border: Border.all(
          color: downloading
              ? tint.withValues(alpha: 0.3)
              : Tokens.borderSubtle,
        ),
      ),
      child: Column(
        children: [
          Row(
            children: [
              thumb,
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      job.title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Tokens.text,
                        fontSize: 14,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Row(
                      children: [
                        Expanded(
                          child: Text(
                            job.artist,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Tokens.dim,
                              fontSize: 12,
                            ),
                          ),
                        ),
                        const SizedBox(width: 6),
                        badge(job.source.toUpperCase(), accent, 0.15),
                        const SizedBox(width: 5),
                        badge(job.qualityLabel, Tokens.dim, 0.10),
                      ],
                    ),
                    const SizedBox(height: 2),
                    Text(
                      statusLine,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(color: tint, fontSize: 10),
                    ),
                  ],
                ),
              ),
              if (state == JobState.failed || state == JobState.cancelled)
                IconButton(
                  icon: const Icon(Icons.refresh_rounded),
                  color: cyan,
                  iconSize: 18,
                  tooltip: 'Retry',
                  onPressed: () {
                    if (queue.retry(job.id) != null) {
                      onSnack('Retrying download…');
                    }
                  },
                ),
              IconButton(
                icon: const Icon(Icons.close_rounded),
                color: Tokens.dim,
                iconSize: 18,
                tooltip: state.terminal ? 'Dismiss' : 'Cancel',
                onPressed: () => queue.remove(job.id),
              ),
            ],
          ),
          const SizedBox(height: 6),
          if (downloading)
            LinearProgressIndicator(
              value: _fraction(job.percent),
              color: tint,
              backgroundColor: Tokens.surface2,
              minHeight: 3,
            )
          else
            const SizedBox(height: 3),
        ],
      ),
    );
  }
}
