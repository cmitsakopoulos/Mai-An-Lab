import 'package:flutter/material.dart';

import '../data/library_repository.dart';
import '../ui/tokens.dart';

/// library.py `_track_row`: format badge, title, "artist · album", duration.
/// The playing row is tinted and drawn in the accent. Kept light: one
/// InkWell, one Row.
class TrackRow extends StatelessWidget {
  const TrackRow({
    super.key,
    required this.track,
    this.playing = false,
    this.depth = 0,
    this.inAlbum = false,
    this.onTap,
    this.onLongPress,
    this.selected,
    this.trailing,
  });

  /// library.py ROW_H.
  static const height = 64.0;
  final Track track;
  final bool playing;

  /// Tree depth under an artist or album row: 16 px of indent per level.
  final int depth;

  /// Listed under its album (`album_context`): the subtitle leaves the album
  /// out.
  final bool inAlbum;
  final VoidCallback? onTap;
  final VoidCallback? onLongPress;

  /// Selection mode (library.py `_selection_mode`): a checkbox in place of
  /// the badge, ticked when true. Null outside selection mode. [onTap] then
  /// toggles it.
  final bool? selected;

  /// In place of the duration: a playlist row's drag handle and remove
  /// button.
  final Widget? trailing;

  /// The subtitle rules of `_track_row`: "Unknown" and a value equal to the
  /// title are left out, and the album when the row sits under it.
  static String subtitle(Track t, {bool inAlbum = false}) {
    bool keep(String v) =>
        v.isNotEmpty &&
        v != 'Unknown' &&
        v.trim().toLowerCase() != t.title.trim().toLowerCase();
    final parts = [
      if (keep(t.artist)) t.artist,
      if (!inAlbum && keep(t.album)) t.album,
    ];
    if (parts.isNotEmpty) return parts.join('  ·  ');
    return (t.artist.isNotEmpty && t.artist != 'Unknown') ? t.artist : '';
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Material(
      color: playing ? accent.withValues(alpha: 0.1) : Colors.transparent,
      child: InkWell(
        onTap: onTap,
        onLongPress: onLongPress,
        child: Padding(
          padding: EdgeInsets.only(left: 16.0 + depth * 16, right: 16),
          child: Row(
            children: [
              if (selected case final ticked?)
                SizedBox(
                  width: 40,
                  child: Checkbox(
                    value: ticked,
                    onChanged: onTap == null ? null : (_) => onTap!(),
                  ),
                )
              else
                Container(
                  width: 40,
                  height: 20,
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: playing
                        ? accent.withValues(alpha: 0.22)
                        : Tokens.text.withValues(alpha: 0.08),
                    border: Border.all(
                      color: playing ? accent : Tokens.borderSubtle,
                    ),
                    borderRadius: BorderRadius.circular(5),
                  ),
                  child: Text(
                    track.formatBadge,
                    style: TextStyle(
                      fontSize: 9,
                      fontWeight: FontWeight.w700,
                      color: playing ? accent : Tokens.dim,
                    ),
                  ),
                ),
              const SizedBox(width: 16),
              Expanded(
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      track.title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: playing ? accent : Tokens.text,
                        fontSize: 14,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      subtitle(track, inAlbum: inAlbum),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: playing ? accent : Tokens.dim,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              ),
              if (trailing case final t?)
                t
              else if (track.duration > 0) ...[
                const SizedBox(width: 12),
                Text(
                  track.durationLabel,
                  style: TextStyle(
                    color: playing ? accent : Tokens.textTertiary,
                    fontSize: 12,
                    fontFeatures: const [FontFeature.tabularFigures()],
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
