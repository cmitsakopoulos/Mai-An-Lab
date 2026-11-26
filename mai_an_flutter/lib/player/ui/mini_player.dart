import 'package:flutter/material.dart';

import '../../ui/frost.dart';
import '../../ui/tokens.dart';
import '../player_controller.dart';
import 'art_tint.dart';
import 'artwork.dart';
import 'now_playing.dart';

/// ui/player/mini_player.py: the floating frosted glass pill above the bottom
/// tab bar. Hidden until something has played; dimmed when playback stopped.
class MiniPlayer extends StatelessWidget {
  const MiniPlayer({super.key, required this.player, this.grouped = false});

  final PlayerController player;

  /// True when inside [AppShell]'s bottom [BackdropGroup].
  final bool grouped;

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: Listenable.merge([player, ?player.autoplay]),
      builder: (context, _) {
        final row = player.nowPlaying;
        if (!player.hasEverPlayed || row == null) {
          return const SizedBox.shrink();
        }
        final base = Theme.of(context).colorScheme.primary;
        final similar = player.autoplay?.enabled ?? false;
        final hPad = Tokens.dockPad(context);
        return ArtTint(
          path: player.art,
          fallback: base,
          builder: (context, accent) => AnimatedOpacity(
            duration: Tokens.motionBase,
            curve: Tokens.curveIn,
            opacity: player.stopped ? 0.55 : 1,
            child: GlassSurface(
              radius: 20,
              grouped: grouped,
              margin: EdgeInsets.fromLTRB(hPad, 0, hPad, 8),
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
                    onHorizontalDragEnd: (d) {
                      final v = d.primaryVelocity ?? 0;
                      if (v < -250) {
                        player.next();
                      } else if (v > 250) {
                        player.previous();
                      }
                    },
                    child: Padding(
                      padding: const EdgeInsets.fromLTRB(10, 10, 6, 8),
                      child: Row(
                        children: [
                          Hero(
                            tag: 'now_playing_artwork',
                            child: DecoratedBox(
                              position: DecorationPosition.foreground,
                              decoration: BoxDecoration(
                                borderRadius: BorderRadius.circular(
                                  Tokens.radiusThumb,
                                ),
                                border: similar
                                    ? Border.all(color: base, width: 1.5)
                                    : null,
                              ),
                              child: Artwork(
                                path: player.art,
                                size: 42,
                                radius: Tokens.radiusThumb,
                                placeholder: Icons.music_note_rounded,
                                placeholderSize: 22,
                                cacheWidth: 128,
                              ),
                            ),
                          ),
                          const SizedBox(width: 10),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              mainAxisSize: MainAxisSize.min,
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
                  // Accent-colored progress hairline along the top edge so it
                  // stays legible even over dark album art.
                  Positioned(
                    top: 0,
                    left: 16,
                    right: 16,
                    child: ValueListenableBuilder(
                      valueListenable: player.position,
                      builder: (context, pos, _) {
                        final d = player.duration.inMilliseconds;
                        return ClipRRect(
                          borderRadius: BorderRadius.circular(2),
                          child: LinearProgressIndicator(
                            value: d > 0
                                ? (pos.inMilliseconds / d).clamp(0.0, 1.0)
                                : 0,
                            minHeight: 2.5,
                            color: accent,
                            backgroundColor: Colors.white.withValues(
                              alpha: 0.08,
                            ),
                          ),
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
