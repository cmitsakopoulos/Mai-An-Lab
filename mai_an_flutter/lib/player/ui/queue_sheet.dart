import 'package:flutter/material.dart';

import '../../ui/frost.dart';
import '../../ui/sliding_segmented.dart';
import '../../ui/tokens.dart';
import '../autoplay.dart';
import '../playback_backend.dart';
import '../player_controller.dart';
import '../queue_entry.dart';
import 'queue_roadmap.dart';

enum _QueueViewMode { list, roadmap }

/// ui/player/queue_sheet.py: "Playing Next". The playing row and the next
/// rows in play order (15 at most, as in Flet), reorderable outside shuffle,
/// with an alternative live Roadmap view for visual steering.
class QueueSheet extends StatefulWidget {
  const QueueSheet({super.key, required this.player});

  final PlayerController player;

  static const visibleRows = 15;

  static Future<void> open(BuildContext context, PlayerController player) =>
      showFrostedSheet<void>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        backgroundColor: Tokens.surface,
        shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
        ),
        builder: (_) => QueueSheet(player: player),
      );

  @override
  State<QueueSheet> createState() => _QueueSheetState();
}

class _QueueSheetState extends State<QueueSheet> {
  PlayerController get player => widget.player;
  _QueueViewMode _mode = _QueueViewMode.list;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return SizedBox(
      height: MediaQuery.sizeOf(context).height,
      child: ListenableBuilder(
        listenable: Listenable.merge([player, ?player.autoplay]),
        builder: (context, _) {
          final q = player.queue;
          final remaining = q.isEmpty ? 0 : q.length - q.currentIndex - 1;
          // After a stop the queue is kept (from row 0), and Flet still
          // lists it.
          final rows = q.isEmpty
              ? const <(int, QueueEntry)>[]
              : q
                    .upcoming(player.shuffleIndices)
                    .take(QueueSheet.visibleRows)
                    .toList();
          final notice = _notice(player);
          // Auto-play, Apple Music style: above the first recommendation, or
          // at the end while the next block is on its way.
          final ap = player.autoplay;
          int? header;
          var pending = false;
          if (ap != null && ap.enabled && rows.isNotEmpty) {
            final first = [
              for (var p = 1; p < rows.length; p++)
                if (rows[p].$2.autoplay) p,
            ].firstOrNull;
            header = first ?? rows.length;
            pending = first == null;
          }
          int toTrack(int c) => header != null && header < c ? c - 1 : c;
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
                padding: const EdgeInsets.fromLTRB(20, 4, 14, 6),
                child: Row(
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            _mode == _QueueViewMode.list
                                ? 'Playing Next'
                                : 'Queue Roadmap',
                            style: const TextStyle(
                              color: Tokens.text,
                              fontSize: 17,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          const SizedBox(height: 2),
                          Text(
                            q.isEmpty
                                ? 'Nothing queued'
                                : '$remaining track${remaining == 1 ? '' : 's'} remaining',
                            style: const TextStyle(
                              color: Tokens.dim,
                              fontSize: 12,
                              fontFeatures: [FontFeature.tabularFigures()],
                            ),
                          ),
                        ],
                      ),
                    ),
                    TextButton(
                      onPressed: () {
                        player.clearQueue();
                        Navigator.of(context).pop();
                      },
                      child: Text(
                        'Clear',
                        style: TextStyle(
                          color: accent,
                          fontSize: 13,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    IconButton(
                      icon: const Icon(Icons.close_rounded),
                      color: Tokens.dim,
                      iconSize: 18,
                      onPressed: () => Navigator.of(context).pop(),
                    ),
                  ],
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 2, 16, 10),
                child: SlidingSegmented<_QueueViewMode>(
                  selected: _mode,
                  accent: accent,
                  height: 36,
                  iconSize: 16,
                  onSelect: (m) => setState(() => _mode = m),
                  segments: const [
                    Segment(
                      value: _QueueViewMode.list,
                      label: 'List',
                      icon: Icons.format_list_bulleted_rounded,
                    ),
                    Segment(
                      value: _QueueViewMode.roadmap,
                      label: 'Roadmap',
                      icon: Icons.hub_rounded,
                    ),
                  ],
                ),
              ),
              const Divider(color: Tokens.borderSubtle, height: 1),
              if (_mode == _QueueViewMode.roadmap)
                Expanded(child: QueueRoadmap(player: player))
              else ...[
                if (notice != null)
                  Container(
                    margin: const EdgeInsets.symmetric(
                      horizontal: 12,
                      vertical: 6,
                    ),
                    padding: const EdgeInsets.symmetric(
                      vertical: 8,
                      horizontal: 12,
                    ),
                    decoration: BoxDecoration(
                      color: accent.withValues(alpha: 0.1),
                      border: Border.all(color: accent.withValues(alpha: 0.2)),
                      borderRadius: BorderRadius.circular(Tokens.radiusCard),
                    ),
                    child: Row(
                      children: [
                        Icon(notice.$1, color: accent, size: 16),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            notice.$2,
                            style: TextStyle(
                              color: accent,
                              fontSize: 12,
                              fontWeight: FontWeight.w500,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                Expanded(
                  child: rows.isEmpty
                      ? const Center(
                          child: Column(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              Icon(
                                Icons.queue_music_rounded,
                                color: Tokens.dim,
                                size: 48,
                              ),
                              SizedBox(height: 8),
                              Text(
                                'Queue is empty',
                                style: TextStyle(
                                  color: Tokens.dim,
                                  fontSize: 14,
                                ),
                              ),
                            ],
                          ),
                        )
                      : ReorderableListView.builder(
                          buildDefaultDragHandles: false,
                          padding: const EdgeInsets.symmetric(
                            horizontal: 12,
                            vertical: 6,
                          ),
                          itemCount: rows.length + (header == null ? 0 : 1),
                          onReorder: (a, b) {
                            if (a == header) return;
                            _reorder(rows, toTrack(a), toTrack(b));
                          },
                          itemBuilder: (context, c) {
                            if (c == header) {
                              return _AutoPlayHeader(
                                key: const ValueKey('q_autoplay_header'),
                                autoplay: ap!,
                                pending: pending,
                                onOpenRoadmap: () => setState(
                                  () => _mode = _QueueViewMode.roadmap,
                                ),
                              );
                            }
                            final pos = header != null && header < c
                                ? c - 1
                                : c;
                            final (i, row) = rows[pos];
                            return _QueueRow(
                              key: ObjectKey(row),
                              player: player,
                              row: row,
                              position: pos,
                              dragIndex: c,
                              active: i == q.currentIndex,
                            );
                          },
                        ),
                ),
              ],
            ],
          );
        },
      ),
    );
  }

  /// `_handle_queue_reorder`: the playing row neither moves nor is moved
  /// past; rows are found again by identity, not position.
  void _reorder(List<(int, QueueEntry)> rows, int oldPos, int newPos) {
    if (newPos > oldPos) newPos -= 1;
    if (oldPos == 0 || newPos == 0 || oldPos == newPos) return;
    if (newPos >= rows.length) return;
    final from = player.queue.indexOfRow(rows[oldPos].$2);
    final to = player.queue.indexOfRow(rows[newPos].$2);
    if (from == null || to == null) return;
    final cur = player.queue.currentIndex;
    if (from == cur || to == cur) return;
    player.moveRow(from, to);
  }

  static (IconData, String)? _notice(PlayerController p) {
    final all = p.repeat == Repeat.all;
    if (p.repeat == Repeat.one) {
      return (
        Icons.repeat_one_rounded,
        'Repeat Current Song is active. Normal queue progression is paused.',
      );
    }
    if (p.shuffle && all) {
      return (
        Icons.shuffle_rounded,
        'Shuffle & Repeat Queue is active. Tracks will play in a randomized loop indefinitely.',
      );
    }
    if (p.shuffle) {
      return (
        Icons.shuffle_rounded,
        'Shuffle Play is active. Tracks will play in a randomized order.',
      );
    }
    if (all) {
      return (
        Icons.repeat_rounded,
        'Repeat Queue is active. The queue will loop back to the beginning after the last song.',
      );
    }
    return null;
  }
}

class _QueueRow extends StatelessWidget {
  const _QueueRow({
    super.key,
    required this.player,
    required this.row,
    required this.position,
    required this.dragIndex,
    required this.active,
  });

  final PlayerController player;
  final QueueEntry row;
  final int position;

  /// The row's index among the list's children (the header included).
  final int dragIndex;
  final bool active;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final curArtist = player.nowPlaying?.artist ?? '';
    final sameArtist =
        !active && curArtist.isNotEmpty && row.artist == curArtist;
    final sameArtistAccent = CategoryPalette.of(context).track;
    final showArtist =
        row.artist.trim().toLowerCase() != row.title.trim().toLowerCase();
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Opacity(
        opacity: player.repeat == Repeat.one ? 0.4 : 1,
        child: Container(
          height: 56,
          padding: const EdgeInsets.fromLTRB(8, 4, 6, 4),
          decoration: BoxDecoration(
            color: active
                ? accent.withValues(alpha: 0.12)
                : sameArtist
                ? sameArtistAccent.withValues(alpha: 0.06)
                : Tokens.surface2,
            border: Border.all(
              color: active
                  ? accent.withValues(alpha: 0.35)
                  : Tokens.borderSubtle,
            ),
            borderRadius: BorderRadius.circular(Tokens.radiusCard),
          ),
          child: Row(
            children: [
              SizedBox(
                width: 28,
                child: Center(
                  child: active
                      ? Icon(Icons.play_arrow_rounded, color: accent, size: 16)
                      : Text(
                          '$position',
                          style: const TextStyle(
                            color: Tokens.dim,
                            fontSize: 12,
                            fontWeight: FontWeight.w500,
                          ),
                        ),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      row.title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: active ? accent : Tokens.text,
                        fontSize: 13,
                        fontWeight: active ? FontWeight.w600 : FontWeight.w500,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      showArtist ? row.artist : '',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: active
                            ? accent
                            : sameArtist
                            ? sameArtistAccent
                            : Tokens.dim,
                        fontSize: 11,
                      ),
                    ),
                  ],
                ),
              ),
              if (!active && !player.shuffle)
                ReorderableDragStartListener(
                  index: dragIndex,
                  child: const Padding(
                    padding: EdgeInsets.all(8),
                    child: Icon(
                      Icons.reorder_rounded,
                      color: Tokens.dim,
                      size: 18,
                    ),
                  ),
                ),
              IconButton(
                icon: const Icon(Icons.remove_circle_outline_rounded),
                color: Tokens.accentRed,
                iconSize: 16,
                onPressed: () => player.removeRow(row),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Auto-play divider in the queue list: tapping switches to the live Roadmap
/// view instead of opening the old Auto-play pop-up.
class _AutoPlayHeader extends StatelessWidget {
  const _AutoPlayHeader({
    super.key,
    required this.autoplay,
    required this.pending,
    required this.onOpenRoadmap,
  });

  final AutoPlay autoplay;
  final bool pending;
  final VoidCallback onOpenRoadmap;

  @override
  Widget build(BuildContext context) {
    final cyan = Theme.of(context).colorScheme.primary;
    final label = anchorLabel(autoplay);
    final detail = pending
        ? 'finding similar songs…'
        : (label.isNotEmpty ? 'from $label' : 'similar songs');
    return InkWell(
      borderRadius: BorderRadius.circular(10),
      onTap: onOpenRoadmap,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(10, 12, 6, 6),
        child: Row(
          children: [
            Icon(Icons.all_inclusive_rounded, color: cyan, size: 14),
            const SizedBox(width: 6),
            Text(
              'AUTO-PLAY',
              style: TextStyle(
                color: cyan,
                fontSize: 11,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(width: 6),
            Expanded(
              child: Text(
                detail,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: Tokens.dim, fontSize: 12),
              ),
            ),
            const SizedBox(width: 6),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
              decoration: BoxDecoration(
                color: cyan.withValues(alpha: 0.14),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: cyan.withValues(alpha: 0.3)),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.hub_rounded, color: cyan, size: 12),
                  const SizedBox(width: 4),
                  Text(
                    'Roadmap',
                    style: TextStyle(
                      color: cyan,
                      fontSize: 10.5,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// "Title — Artist" of the track the station is playing from, or "".
String anchorLabel(AutoPlay autoplay) {
  final path = autoplay.anchor;
  if (path.isEmpty) return '';
  for (final t in autoplay.player.queue.entries) {
    if (t.path == path) {
      final artist = t.data['artist_name']?.toString() ?? '';
      return artist.isNotEmpty ? '${t.title} — $artist' : t.title;
    }
  }
  final base = path.split('/').last;
  final dot = base.lastIndexOf('.');
  return dot > 0 ? base.substring(0, dot) : base;
}
