import 'package:flutter/material.dart';

import '../../ui/tokens.dart';
import '../player_controller.dart';
import 'artwork.dart';
import 'now_playing.dart';

/// ui/player/mini_player.py: the floating card above the bottom edge.
/// Hidden until something has played; dimmed when playback stopped.
class MiniPlayer extends StatelessWidget {
  const MiniPlayer({super.key, required this.player});

  final PlayerController player;

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: player,
      builder: (context, _) {
        final row = player.nowPlaying;
        if (!player.hasEverPlayed || row == null) {
          return const SizedBox.shrink();
        }
        final accent = Theme.of(context).colorScheme.primary;
        return AnimatedOpacity(
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOut,
          opacity: player.stopped ? 0.55 : 1,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(10, 0, 10, 8),
            child: AnimatedContainer(
              duration: const Duration(milliseconds: 200),
              decoration: BoxDecoration(
                color: Tokens.surface,
                borderRadius: BorderRadius.circular(18),
                border: Border.all(
                  color: player.playing
                      ? Colors.white.withValues(alpha: 0.7)
                      : Tokens.border,
                ),
                boxShadow: const [
                  BoxShadow(
                    blurRadius: 16,
                    spreadRadius: -2,
                    color: Color(0x4D000000),
                  ),
                ],
              ),
              clipBehavior: Clip.antiAlias,
              child: Stack(
                children: [
                  GestureDetector(
                    behavior: HitTestBehavior.opaque,
                    onTap: () => NowPlaying.open(context, player),
                    onVerticalDragEnd: (d) {
                      if ((d.primaryVelocity ?? 0) < 0) {
                        NowPlaying.open(context, player);
                      }
                    },
                    child: Padding(
                      padding: const EdgeInsets.fromLTRB(10, 12, 10, 8),
                      child: Row(
                        children: [
                          Artwork(
                            path: player.art,
                            size: 42,
                            radius: Tokens.radiusThumb,
                            placeholder: Icons.music_note_rounded,
                            placeholderSize: 22,
                            cacheWidth: 128,
                          ),
                          const SizedBox(width: 8),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  row.title,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    color: Tokens.text,
                                    fontSize: 14,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                                const SizedBox(height: 2),
                                Text(
                                  row.artist,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    color: Tokens.dim,
                                    fontSize: 12,
                                  ),
                                ),
                              ],
                            ),
                          ),
                          IconButton(
                            icon: const Icon(Icons.skip_previous_rounded),
                            color: Tokens.dim,
                            iconSize: 22,
                            onPressed: player.previous,
                          ),
                          IconButton(
                            icon: Icon(
                              player.playing
                                  ? Icons.pause_rounded
                                  : Icons.play_arrow_rounded,
                            ),
                            color: accent,
                            iconSize: 28,
                            onPressed: player.toggle,
                          ),
                          IconButton(
                            icon: const Icon(Icons.skip_next_rounded),
                            color: Tokens.dim,
                            iconSize: 22,
                            onPressed: player.next,
                          ),
                        ],
                      ),
                    ),
                  ),
                  // Progress along the top edge.
                  Positioned(
                    top: 0,
                    left: 14,
                    right: 14,
                    child: ValueListenableBuilder(
                      valueListenable: player.position,
                      builder: (context, pos, _) {
                        final d = player.duration.inMilliseconds;
                        return LinearProgressIndicator(
                          value: d > 0
                              ? (pos.inMilliseconds / d).clamp(0.0, 1.0)
                              : 0,
                          minHeight: 2,
                          color: accent,
                          backgroundColor: Colors.transparent,
                        );
                      },
                    ),
                  ),
                ],
              ),
            ),
          ),
        );
      },
    );
  }
}
