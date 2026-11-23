import 'package:flutter/material.dart';

import '../../ui/tokens.dart';
import '../playback_backend.dart';
import '../player_controller.dart';
import '../queue_entry.dart';

/// ui/player/queue_sheet.py: "Playing Next". The playing row and the next
/// rows in play order (15 at most, as in Flet), reorderable outside shuffle.
class QueueSheet extends StatelessWidget {
  const QueueSheet({super.key, required this.player});

  final PlayerController player;

  static const visibleRows = 15;

  static Future<void> open(BuildContext context, PlayerController player) =>
      showModalBottomSheet<void>(
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
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return SizedBox(
      height: MediaQuery.sizeOf(context).height,
      child: ListenableBuilder(
        listenable: player,
        builder: (context, _) {
          final q = player.queue;
          final remaining = q.isEmpty ? 0 : q.length - q.currentIndex - 1;
          // After a stop the queue is kept (from row 0), and Flet still
          // lists it.
          final rows = q.isEmpty
              ? const <(int, QueueEntry)>[]
              : q.upcoming(player.shuffleIndices).take(visibleRows).toList();
          final notice = _notice(player);
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
                          const Text(
                            'Playing Next',
                            style: TextStyle(
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
              const Divider(color: Tokens.borderSubtle, height: 1),
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
                              style: TextStyle(color: Tokens.dim, fontSize: 14),
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
                        itemCount: rows.length,
                        onReorder: (a, b) => _reorder(rows, a, b),
                        itemBuilder: (context, pos) {
                          final (i, row) = rows[pos];
                          return _QueueRow(
                            key: ObjectKey(row),
                            player: player,
                            row: row,
                            position: pos,
                            active: i == q.currentIndex,
                          );
                        },
                      ),
              ),
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
    required this.active,
  });

  final PlayerController player;
  final QueueEntry row;
  final int position;
  final bool active;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final curArtist = player.nowPlaying?.artist ?? '';
    final sameArtist =
        !active && curArtist.isNotEmpty && row.artist == curArtist;
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
                ? Tokens.libTrack.withValues(alpha: 0.06)
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
                            ? Tokens.libTrack
                            : Tokens.dim,
                        fontSize: 11,
                      ),
                    ),
                  ],
                ),
              ),
              if (!active && !player.shuffle)
                ReorderableDragStartListener(
                  index: position,
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
