import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../library/library_controller.dart';
import '../../ui/app_switch.dart';
import '../../ui/frost.dart';
import '../../ui/tokens.dart';
import '../autoplay.dart';
import '../playback_backend.dart';
import '../player_controller.dart';
import 'artwork.dart';
import 'queue_sheet.dart';
import 'art_tint.dart';

/// ui/player/now_playing.py: the full-screen sheet, swiped down to close.
class NowPlaying extends StatefulWidget {
  const NowPlaying({super.key, required this.player});

  final PlayerController player;

  static Future<void> open(BuildContext context, PlayerController player) =>
      Navigator.of(context).push<void>(
        PageRouteBuilder<void>(
          transitionDuration: Tokens.motionSlow,
          reverseTransitionDuration: Tokens.motionBase,
          opaque: true,
          pageBuilder: (_, _, _) => NowPlaying(player: player),
          transitionsBuilder: (_, anim, _, child) {
            final curved = CurvedAnimation(
              parent: anim,
              curve: Tokens.curveIn,
              reverseCurve: Tokens.curveOut,
            );
            return FadeTransition(
              opacity: curved,
              child: SlideTransition(
                position: Tween<Offset>(
                  begin: const Offset(0, 0.08),
                  end: Offset.zero,
                ).animate(curved),
                child: child,
              ),
            );
          },
        ),
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
    return Scaffold(
      backgroundColor: Tokens.bg,
      body: GestureDetector(
        behavior: HitTestBehavior.translucent,
        onVerticalDragEnd: (d) {
          if ((d.primaryVelocity ?? 0) > 320) {
            Navigator.of(context).maybePop();
          }
        },
        child: ListenableBuilder(
          listenable: Listenable.merge([p, ?p.autoplay, ?p.autoplay?.library]),
          builder: (context, _) {
            final row = p.nowPlaying;
            final ap = p.autoplay;
            final similar = ap?.enabled ?? false;
            // update_meta: `value or "Unknown"`.
            String or(String? v) => (v == null || v.isEmpty) ? 'Unknown' : v;
            final title = or(row?.title);
            final artist = or(row?.artist);
            final album = or(row?.album);
            return ArtTint(
              path: p.art,
              fallback: accent,
              builder: (context, tint) => Stack(
                fit: StackFit.expand,
                children: [
                  Positioned.fill(
                    child: BlurredArtBackdrop(path: p.art, tint: tint),
                  ),
                  LayoutBuilder(
                    builder: (context, box) {
                      final h = box.maxHeight;
                      final w = box.maxWidth;
                      final artSize = math
                          .min(w - 56, h * 0.40)
                          .clamp(140.0, 360.0);
                      final compactH = h < 700;
                      final hPad = Tokens.pagePad(context) + 8;
                      return SafeArea(
                        child: Padding(
                          padding: EdgeInsets.only(
                            top: compactH ? 4 : 10,
                            bottom: compactH ? 12 : 20,
                          ),
                          child: Column(
                            children: [
                              Padding(
                                padding: const EdgeInsets.only(
                                  top: 8,
                                  bottom: 4,
                                ),
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
                                padding: const EdgeInsets.fromLTRB(
                                  14,
                                  2,
                                  14,
                                  4,
                                ),
                                child: Row(
                                  children: [
                                    IconButton(
                                      icon: const Icon(
                                        Icons.keyboard_arrow_down_rounded,
                                      ),
                                      color: Tokens.dim,
                                      iconSize: 30,
                                      onPressed: () =>
                                          Navigator.of(context).pop(),
                                    ),
                                    const Spacer(),
                                    _QueuePill(
                                      onTap: () => QueueSheet.open(context, p),
                                    ),
                                  ],
                                ),
                              ),
                              const Spacer(),
                              SizedBox(
                                width: artSize,
                                height: artSize,
                                child: GestureDetector(
                                  onTap: _tapArt,
                                  onHorizontalDragEnd: (d) {
                                    final v = d.primaryVelocity ?? 0;
                                    if (v < -250) {
                                      p.next();
                                    } else if (v > 250) {
                                      p.previous();
                                    }
                                  },
                                  child: Hero(
                                    tag: 'now_playing_artwork',
                                    child: DecoratedBox(
                                      // update_play_similar: a 3 px border while
                                      // auto-play is on.
                                      position: DecorationPosition.foreground,
                                      decoration: BoxDecoration(
                                        borderRadius: BorderRadius.circular(22),
                                        border: similar
                                            ? Border.all(
                                                color: accent,
                                                width: 3,
                                              )
                                            : null,
                                      ),
                                      child: DecoratedBox(
                                        decoration: BoxDecoration(
                                          borderRadius: BorderRadius.circular(
                                            22,
                                          ),
                                          boxShadow: const [
                                            BoxShadow(
                                              blurRadius: 32,
                                              spreadRadius: -4,
                                              offset: Offset(0, 14),
                                              color: Color(0x80000000),
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
                                                duration: const Duration(
                                                  milliseconds: 400,
                                                ),
                                                child: Icon(
                                                  p.playing
                                                      ? Icons.pause
                                                      : Icons.play_arrow,
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
                              ),
                              const Spacer(),
                              Padding(
                                padding: EdgeInsets.symmetric(horizontal: hPad),
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
                                      style: const TextStyle(
                                        color: Tokens.dim,
                                        fontSize: 14,
                                      ),
                                    ),
                                    ValueListenableBuilder(
                                      valueListenable: p.loudnessBoost,
                                      builder: (context, boost, _) => boost > 0
                                          ? Padding(
                                              padding: const EdgeInsets.only(
                                                top: 8,
                                              ),
                                              child: _DynamismBadge(
                                                boostDb: boost,
                                              ),
                                            )
                                          : const SizedBox.shrink(),
                                    ),
                                  ],
                                ),
                              ),
                              SizedBox(height: compactH ? 10 : 16),
                              Padding(
                                padding: EdgeInsets.symmetric(horizontal: hPad),
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
                                padding: EdgeInsets.symmetric(horizontal: hPad),
                                child: Row(
                                  children: [
                                    IconButton(
                                      icon: const Icon(Icons.shuffle_rounded),
                                      iconSize: 20,
                                      color: p.shuffle ? accent : Tokens.dim,
                                      onPressed: p.toggleShuffle,
                                    ),
                                    const Spacer(),
                                    _AutoPlayButton(
                                      autoplay: ap,
                                      accent: accent,
                                    ),
                                    const Spacer(),
                                    IconButton(
                                      icon: Icon(
                                        p.repeat == Repeat.one
                                            ? Icons.repeat_one_rounded
                                            : Icons.repeat_rounded,
                                      ),
                                      iconSize: 20,
                                      color: p.repeat == Repeat.none
                                          ? Tokens.dim
                                          : accent,
                                      onPressed: p.cycleRepeat,
                                    ),
                                  ],
                                ),
                              ),
                              SizedBox(height: compactH ? 4 : 8),
                              Padding(
                                padding: EdgeInsets.only(
                                  bottom: compactH ? 12 : 24,
                                ),
                                child: Row(
                                  mainAxisAlignment:
                                      MainAxisAlignment.spaceEvenly,
                                  children: [
                                    IconButton(
                                      icon: const Icon(Icons.replay_10_rounded),
                                      color: accent,
                                      iconSize: 26,
                                      onPressed: () => p.seekBy(
                                        const Duration(seconds: -10),
                                      ),
                                    ),
                                    IconButton(
                                      icon: const Icon(
                                        Icons.skip_previous_rounded,
                                      ),
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
                                      icon: const Icon(
                                        Icons.forward_10_rounded,
                                      ),
                                      color: accent,
                                      iconSize: 26,
                                      onPressed: () =>
                                          p.seekBy(const Duration(seconds: 10)),
                                    ),
                                  ],
                                ),
                              ),
                            ],
                          ),
                        ),
                      );
                    },
                  ),
                ],
              ),
            );
          },
        ),
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

/// now_playing.py `_dynamism_badge`: shown while Dynamism boosts the track.
class _DynamismBadge extends StatelessWidget {
  const _DynamismBadge({required this.boostDb});
  final double boostDb;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: Tokens.amber.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(Icons.flash_on_rounded, color: Tokens.amber, size: 14),
          const SizedBox(width: 6),
          const Text(
            'DYNAMISM ACTIVE',
            style: TextStyle(
              color: Tokens.amber,
              fontSize: 10,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(width: 6),
          Text(
            '|',
            style: TextStyle(
              color: Tokens.amber.withValues(alpha: 0.2),
              fontSize: 10,
            ),
          ),
          const SizedBox(width: 6),
          Text(
            '+${boostDb.toStringAsFixed(1)} dB',
            style: const TextStyle(
              color: Tokens.text,
              fontSize: 10,
              fontWeight: FontWeight.w700,
              fontFeatures: [FontFeature.tabularFigures()],
            ),
          ),
        ],
      ),
    );
  }
}

/// The chain: on/off toggle for auto-play with a subtle amber dot whenever some
/// tracks still lack acoustic features. Tapping when unavailable (0 analysed)
/// or long-pressing anytime opens a frosted sheet with exact coverage and a
/// 1-tap Analyse button.
class _AutoPlayButton extends StatelessWidget {
  const _AutoPlayButton({required this.autoplay, required this.accent});

  final AutoPlay? autoplay;
  final Color accent;

  @override
  Widget build(BuildContext context) {
    final ap = autoplay;
    final lib = ap?.library is LibraryController
        ? ap!.library as LibraryController
        : null;
    final on = ap?.enabled ?? false;
    final available = ap != null && ap.library.stationsAvailable;
    final missing = lib?.dspMissing ?? 0;
    final color = on ? accent : Tokens.dim;

    void openSheet() {
      if (ap == null || lib == null) return;
      _showAutoPlaySheet(context, ap, lib, accent);
    }

    return GestureDetector(
      onLongPress: lib != null ? openSheet : null,
      child: Opacity(
        opacity: available ? 1 : 0.55,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              'AUTO-PLAY',
              style: TextStyle(
                fontSize: 8,
                fontWeight: FontWeight.w700,
                color: color,
              ),
            ),
            Stack(
              clipBehavior: Clip.none,
              children: [
                IconButton(
                  icon: Icon(on ? Icons.link_rounded : Icons.link_off_rounded),
                  iconSize: 20,
                  color: color,
                  tooltip: missing > 0
                      ? 'Auto-Play ($missing unanalysed — hold for details)'
                      : 'Auto-Play',
                  onPressed: available
                      ? () {
                          final enabling = !ap.enabled;
                          ap.setMode(enabling);
                          ap.player.say(
                            enabling
                                ? 'Auto-play on. Your queue is on hold.'
                                : 'Auto-play off. Back to your queue.',
                          );
                        }
                      : (lib != null ? openSheet : null),
                ),
                if (missing > 0)
                  Positioned(
                    top: 7,
                    right: 7,
                    child: Container(
                      width: 7,
                      height: 7,
                      decoration: const BoxDecoration(
                        color: Tokens.amber,
                        shape: BoxShape.circle,
                      ),
                    ),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

void _showAutoPlaySheet(
  BuildContext context,
  AutoPlay ap,
  LibraryController lib,
  Color accent,
) {
  showFrostedSheet<void>(
    context: context,
    showDragHandle: true,
    builder: (ctx) => ListenableBuilder(
      listenable: Listenable.merge([ap, lib, lib.analysis]),
      builder: (ctx, _) {
        final total = lib.dspTotal;
        final missing = lib.dspMissing;
        final analysed = lib.dspAnalysed;
        final ratio = total > 0 ? (analysed / total).clamp(0.0, 1.0) : 0.0;
        final analysis = lib.analysis;
        final available = lib.stationsAvailable;

        return SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 4, 20, 20),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  children: [
                    Icon(Icons.link_rounded, color: accent, size: 20),
                    const SizedBox(width: 8),
                    const Expanded(
                      child: Text(
                        'Auto-Play & Acoustic Index',
                        style: TextStyle(
                          color: Tokens.text,
                          fontSize: 16,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                    if (available)
                      AppSwitch(
                        value: ap.enabled,
                        onChanged: (v) => ap.setMode(v),
                      ),
                  ],
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        total == 0
                            ? 'No tracks in library yet'
                            : '$analysed of $total tracks indexed',
                        style: const TextStyle(
                          color: Tokens.text,
                          fontSize: 13,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    Text(
                      missing == 0 && total > 0
                          ? '100% Ready'
                          : '$missing missing',
                      style: TextStyle(
                        color: missing == 0 && total > 0
                            ? Tokens.accentGreen
                            : Tokens.amber,
                        fontSize: 12,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                ClipRRect(
                  borderRadius: BorderRadius.circular(3),
                  child: LinearProgressIndicator(
                    value: analysis.running ? analysis.progress : ratio,
                    minHeight: 5,
                    color: missing == 0 && total > 0
                        ? Tokens.accentGreen
                        : accent,
                    backgroundColor: Tokens.surface2,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  analysis.running
                      ? (analysis.status.isEmpty
                            ? 'Analysing acoustic features…'
                            : analysis.status)
                      : missing > 0
                      ? 'Unanalysed tracks are skipped by Auto-Play until their acoustic features are computed.'
                      : 'Every track in your library is ready for Auto-Play recommendations.',
                  style: const TextStyle(color: Tokens.dim, fontSize: 12),
                ),
                if (analysis.running || (missing > 0 && lib.canWrite)) ...[
                  const SizedBox(height: 16),
                  if (analysis.running)
                    OutlinedButton.icon(
                      icon: const Icon(Icons.close_rounded, size: 17),
                      label: const Text('Cancel Analysis'),
                      style: OutlinedButton.styleFrom(
                        foregroundColor: Tokens.accentRed,
                        side: BorderSide(
                          color: Tokens.accentRed.withValues(alpha: 0.45),
                        ),
                      ),
                      onPressed: analysis.cancel,
                    )
                  else
                    FilledButton.icon(
                      icon: const Icon(Icons.graphic_eq_rounded, size: 18),
                      label: Text(
                        'Analyse $missing Track${missing == 1 ? '' : 's'} Now',
                      ),
                      style: FilledButton.styleFrom(
                        backgroundColor: accent,
                        foregroundColor: Colors.black,
                        padding: const EdgeInsets.symmetric(vertical: 12),
                      ),
                      onPressed: () {
                        lib.analyseLibrary().ignore();
                        Navigator.pop(ctx);
                      },
                    ),
                ],
              ],
            ),
          ),
        );
      },
    ),
  );
}
