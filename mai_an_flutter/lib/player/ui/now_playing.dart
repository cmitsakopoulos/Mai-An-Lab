import 'package:flutter/material.dart';

import '../../ui/tokens.dart';
import '../playback_backend.dart';
import '../player_controller.dart';
import 'artwork.dart';
import 'queue_sheet.dart';

/// ui/player/now_playing.py: the full-screen sheet, swiped down to close.
class NowPlaying extends StatefulWidget {
  const NowPlaying({super.key, required this.player});

  final PlayerController player;

  static Future<void> open(BuildContext context, PlayerController player) =>
      showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        backgroundColor: Tokens.bg,
        showDragHandle: false,
        builder: (_) => NowPlaying(player: player),
      );

  @override
  State<NowPlaying> createState() => _NowPlayingState();
}

class _NowPlayingState extends State<NowPlaying> {
  PlayerController get p => widget.player;

  /// While dragging, the slider shows the thumb, not the player.
  double? _scrub;

  /// The play/pause glyph flashed over the art after a tap on it.
  bool _overlay = false;

  void _tapArt() {
    p.toggle();
    setState(() => _overlay = true);
    Future.delayed(const Duration(milliseconds: 600), () {
      if (mounted) setState(() => _overlay = false);
    });
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return SizedBox(
      height: MediaQuery.sizeOf(context).height,
      child: ListenableBuilder(
        listenable: p,
        builder: (context, _) {
          final row = p.nowPlaying;
          final title = row?.title ?? 'Unknown';
          final artist = row?.artist ?? 'Unknown';
          final album = row?.album ?? 'Unknown';
          return Padding(
            padding: const EdgeInsets.only(top: 10, bottom: 20),
            child: Column(
              children: [
                Padding(
                  padding: const EdgeInsets.only(top: 8, bottom: 4),
                  child: Container(
                    width: 36,
                    height: 5,
                    decoration: BoxDecoration(
                      color: Tokens.border,
                      borderRadius: BorderRadius.circular(3),
                    ),
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.fromLTRB(14, 4, 14, 4),
                  child: Row(
                    children: [
                      IconButton(
                        icon: const Icon(Icons.keyboard_arrow_down_rounded),
                        color: Tokens.dim,
                        iconSize: 30,
                        onPressed: () => Navigator.of(context).pop(),
                      ),
                      const Spacer(),
                      _QueuePill(onTap: () => QueueSheet.open(context, p)),
                    ],
                  ),
                ),
                const Spacer(),
                Padding(
                  padding: const EdgeInsets.all(36),
                  child: AspectRatio(
                    aspectRatio: 1,
                    child: GestureDetector(
                      onTap: _tapArt,
                      child: DecoratedBox(
                        decoration: BoxDecoration(
                          borderRadius: BorderRadius.circular(22),
                          boxShadow: [
                            BoxShadow(
                              blurRadius: 32,
                              spreadRadius: -4,
                              color: accent.withValues(alpha: 0.15),
                            ),
                          ],
                        ),
                        child: Stack(
                          fit: StackFit.expand,
                          children: [
                            Artwork(
                              path: p.art,
                              size: null,
                              radius: 22,
                              placeholder: Icons.album_rounded,
                              placeholderSize: 96,
                              cacheWidth: 1024,
                            ),
                            Center(
                              child: AnimatedOpacity(
                                opacity: _overlay ? 1 : 0,
                                duration: const Duration(milliseconds: 400),
                                child: Icon(
                                  p.playing ? Icons.pause : Icons.play_arrow,
                                  size: 64,
                                  color: Tokens.text,
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
                const Spacer(),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 24),
                  child: Column(
                    children: [
                      Text(
                        'NOW PLAYING',
                        style: TextStyle(
                          color: accent.withValues(alpha: 0.7),
                          fontSize: 10,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        title,
                        textAlign: TextAlign.center,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: Tokens.text,
                          fontSize: 20,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        '$artist  ·  $album',
                        textAlign: TextAlign.center,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(color: Tokens.dim, fontSize: 14),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 16),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 24),
                  child: _Scrubber(
                    player: p,
                    scrub: _scrub,
                    onScrub: (v) => setState(() => _scrub = v),
                    onCommit: (v) {
                      setState(() => _scrub = null);
                      p.seek(p.duration * v);
                    },
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 24),
                  child: Row(
                    children: [
                      IconButton(
                        icon: const Icon(Icons.shuffle_rounded),
                        iconSize: 20,
                        color: p.shuffle ? accent : Tokens.dim,
                        onPressed: p.toggleShuffle,
                      ),
                      const Spacer(),
                      // Auto-play (the chain): phase 2. Shown disabled, as
                      // Flet does while no track has been analysed.
                      const Opacity(
                        opacity: 0.4,
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Text(
                              'AUTO-PLAY',
                              style: TextStyle(
                                fontSize: 8,
                                fontWeight: FontWeight.w700,
                                color: Tokens.dim,
                              ),
                            ),
                            IconButton(
                              icon: Icon(Icons.link_off_rounded),
                              iconSize: 20,
                              color: Tokens.dim,
                              onPressed: null,
                            ),
                          ],
                        ),
                      ),
                      const Spacer(),
                      IconButton(
                        icon: Icon(
                          p.repeat == Repeat.one
                              ? Icons.repeat_one_rounded
                              : Icons.repeat_rounded,
                        ),
                        iconSize: 20,
                        color: p.repeat == Repeat.none ? Tokens.dim : accent,
                        onPressed: p.cycleRepeat,
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 8),
                Padding(
                  padding: const EdgeInsets.only(bottom: 36),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                    children: [
                      IconButton(
                        icon: const Icon(Icons.replay_10_rounded),
                        color: accent,
                        iconSize: 26,
                        onPressed: () => p.seekBy(const Duration(seconds: -10)),
                      ),
                      IconButton(
                        icon: const Icon(Icons.skip_previous_rounded),
                        color: Tokens.text,
                        iconSize: 34,
                        onPressed: p.previous,
                      ),
                      Container(
                        width: 68,
                        height: 68,
                        decoration: const BoxDecoration(
                          color: Tokens.surfaceElevated,
                          shape: BoxShape.circle,
                        ),
                        child: IconButton(
                          icon: Icon(
                            p.playing
                                ? Icons.pause_rounded
                                : Icons.play_arrow_rounded,
                          ),
                          color: Tokens.text,
                          iconSize: 38,
                          onPressed: p.toggle,
                        ),
                      ),
                      IconButton(
                        icon: const Icon(Icons.skip_next_rounded),
                        color: Tokens.text,
                        iconSize: 34,
                        onPressed: p.next,
                      ),
                      IconButton(
                        icon: const Icon(Icons.forward_10_rounded),
                        color: accent,
                        iconSize: 26,
                        onPressed: () => p.seekBy(const Duration(seconds: 10)),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          );
        },
      ),
    );
  }
}

class _Scrubber extends StatelessWidget {
  const _Scrubber({
    required this.player,
    required this.scrub,
    required this.onScrub,
    required this.onCommit,
  });

  final PlayerController player;
  final double? scrub;
  final ValueChanged<double> onScrub;
  final ValueChanged<double> onCommit;

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder(
      valueListenable: player.position,
      builder: (context, pos, _) {
        final dur = player.duration;
        final frac =
            scrub ??
            (dur.inMilliseconds > 0
                ? (pos.inMilliseconds / dur.inMilliseconds).clamp(0.0, 1.0)
                : 0.0);
        final shown = scrub != null ? dur * scrub! : pos;
        const time = TextStyle(
          color: Tokens.dim,
          fontSize: 12,
          fontFeatures: [FontFeature.tabularFigures()],
        );
        return Column(
          children: [
            SizedBox(
              height: 32,
              child: Slider(
                value: frac,
                onChanged: dur > Duration.zero ? onScrub : null,
                onChangeEnd: dur > Duration.zero ? onCommit : null,
              ),
            ),
            Row(
              children: [
                Text(fmtTime(shown), style: time),
                const Spacer(),
                Text(fmtTime(dur), style: time),
              ],
            ),
          ],
        );
      },
    );
  }
}

class _QueuePill extends StatelessWidget {
  const _QueuePill({required this.onTap});
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Tooltip(
      message: 'Playback Queue',
      child: Material(
        color: accent.withValues(alpha: 0.14),
        shape: StadiumBorder(
          side: BorderSide(color: accent.withValues(alpha: 0.35)),
        ),
        child: InkWell(
          customBorder: const StadiumBorder(),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(Icons.queue_music_rounded, color: accent, size: 17),
                const SizedBox(width: 6),
                const Text(
                  'Queue',
                  style: TextStyle(
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                    color: Tokens.text,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
