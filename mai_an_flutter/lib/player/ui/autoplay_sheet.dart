import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';

import '../../ui/tokens.dart';
import '../autoplay.dart';

/// ui/player/autoplay_sheet.py: on/off and what the station is playing from.
/// Opened from the queue sheet's Auto-play header (Now Playing's chain icon
/// is a plain toggle). All behaviour lives in [AutoPlay].
class AutoPlaySheet extends StatelessWidget {
  const AutoPlaySheet({super.key, required this.autoplay});

  final AutoPlay autoplay;

  static Future<void> open(BuildContext context, AutoPlay autoplay) =>
      showModalBottomSheet<void>(
        context: context,
        backgroundColor: Tokens.surface,
        builder: (_) => AutoPlaySheet(autoplay: autoplay),
      );

  @override
  Widget build(BuildContext context) {
    final cyan = Theme.of(context).colorScheme.primary;
    return ListenableBuilder(
      listenable: Listenable.merge([autoplay, autoplay.player]),
      builder: (context, _) {
        final enabled = autoplay.enabled;
        final label = anchorLabel(autoplay);
        final n = autoplay.bufferPaths().length;
        final linedUp = n > 0
            ? '$n song${n == 1 ? '' : 's'} lined up'
            : 'Finding similar songs…';
        final genre = autoplay.currentGenre;
        final detail = genre != null && n > 0
            ? '$linedUp · now in $genre'
            : linedUp;
        return SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 28),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.only(top: 8, bottom: 14),
                  child: Center(
                    child: Container(
                      width: 36,
                      height: 5,
                      decoration: BoxDecoration(
                        color: Tokens.border,
                        borderRadius: BorderRadius.circular(3),
                      ),
                    ),
                  ),
                ),
                Row(
                  children: [
                    Container(
                      width: 40,
                      height: 40,
                      decoration: BoxDecoration(
                        color: cyan.withValues(alpha: 0.14),
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: Icon(
                        Icons.all_inclusive_rounded,
                        color: cyan,
                        size: 20,
                      ),
                    ),
                    const SizedBox(width: 12),
                    const Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            'Auto-play',
                            style: TextStyle(
                              color: Tokens.text,
                              fontSize: 17,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          SizedBox(height: 2),
                          Text(
                            'After the songs you queued, similar songs from '
                            'the same genre keep playing.',
                            style: TextStyle(color: Tokens.dim, fontSize: 12),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(width: 12),
                    CupertinoSwitch(
                      value: enabled,
                      activeTrackColor: cyan,
                      onChanged: autoplay.setMode,
                    ),
                  ],
                ),
                if (enabled && label.isNotEmpty) ...[
                  const SizedBox(height: 18),
                  const Text(
                    'PLAYING FROM',
                    style: TextStyle(
                      color: Tokens.dim,
                      fontSize: 11,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 14,
                      vertical: 12,
                    ),
                    decoration: BoxDecoration(
                      color: Tokens.surface2,
                      border: Border.all(color: Tokens.borderSubtle),
                      borderRadius: BorderRadius.circular(Tokens.radiusCard),
                    ),
                    child: Row(
                      children: [
                        Icon(
                          Icons.all_inclusive_rounded,
                          color: cyan,
                          size: 18,
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                label,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  color: Tokens.text,
                                  fontSize: 14,
                                  fontWeight: FontWeight.w500,
                                ),
                              ),
                              const SizedBox(height: 2),
                              Text(
                                detail,
                                style: const TextStyle(
                                  color: Tokens.dim,
                                  fontSize: 12,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ],
            ),
          ),
        );
      },
    );
  }
}

/// `anchor_label`: "Title — Artist" of the track the station is playing
/// from, or "".
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
