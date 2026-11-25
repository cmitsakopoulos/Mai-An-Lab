import 'dart:io';

import 'package:flutter/material.dart';

import '../data/library_repository.dart';
import '../player/ui/artwork.dart';
import '../ui/format_badge.dart';
import '../ui/tokens.dart';
import 'track_row.dart';
import '../ui/frost.dart';

/// What the long-press menu can do; a null action is shown disabled.
typedef TrackMenuActions = ({
  VoidCallback? select,
  VoidCallback? playNext,
  VoidCallback? addToQueue,
  VoidCallback? startRadio,
  VoidCallback? addToPlaylist,
  VoidCallback? delete,
});

/// library.py `_open_track_context_menu`: the track card, then Select…,
/// Play Next, Add to Queue, Start Radio, Add to Playlist, Redownload
/// (Different Quality), Send to the Mac and Delete Track. Each action runs
/// after the sheet has closed.
Future<void> showTrackMenu(
  BuildContext context,
  Track t,
  TrackMenuActions a, {
  VoidCallback? redownload,
  String? sendLabel,
  VoidCallback? send,
}) async {
  final accent = Theme.of(context).colorScheme.primary;
  final picked = await showFrostedSheet<VoidCallback>(
    context: context,
    backgroundColor: Tokens.surface,
    showDragHandle: true,
    isScrollControlled: true,
    builder: (sheet) {
      Widget item(
        IconData icon,
        String title,
        VoidCallback? run, {
        String? subtitle,
        Color? iconColor,
        Color? titleColor,
      }) => ListTile(
        leading: Icon(icon, color: iconColor ?? accent),
        title: Text(title, style: TextStyle(color: titleColor)),
        subtitle: subtitle == null
            ? null
            : Text(
                subtitle,
                style: const TextStyle(color: Tokens.dim, fontSize: 12),
              ),
        enabled: run != null,
        onTap: run == null ? null : () => Navigator.pop(sheet, run),
      );
      return SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TrackMenuCard(track: t),
              const SizedBox(height: 6),
              item(
                Icons.checklist_rounded,
                'Select…',
                a.select,
                subtitle: 'Play, queue or send several tracks',
              ),
              item(Icons.queue_music_rounded, 'Play Next', a.playNext),
              item(
                Icons.playlist_add_check_rounded,
                'Add to Queue',
                a.addToQueue,
              ),
              item(
                Icons.all_inclusive_rounded,
                'Start Radio',
                a.startRadio,
                subtitle: 'Play similar tracks automatically',
              ),
              item(
                Icons.playlist_add_rounded,
                'Add to Playlist',
                a.addToPlaylist,
                iconColor: CategoryPalette.of(context).playlist,
              ),
              if (redownload != null)
                item(
                  Icons.download_rounded,
                  'Redownload (Different Quality)',
                  redownload,
                  subtitle: 'Select alternate quality tier',
                  iconColor: Tokens.dim,
                ),
              if (send != null)
                item(
                  Icons.laptop_mac_rounded,
                  sendLabel ?? 'Send to Mac',
                  send,
                  iconColor: Tokens.accentGreen,
                ),
              item(
                Icons.delete_outline_rounded,
                'Delete Track',
                a.delete,
                iconColor: const Color(0xFFFF4444),
                titleColor: const Color(0xFFFF4444),
              ),
            ],
          ),
        ),
      );
    },
  );
  picked?.call();
}

/// library.py `_build_context_menu_track_card`: art, title, "artist ·
/// album", and the format, duration and bitrate badges.
class TrackMenuCard extends StatelessWidget {
  const TrackMenuCard({super.key, required this.track});
  final Track track;

  @override
  Widget build(BuildContext context) {
    final t = track;
    final sub = TrackRow.subtitle(t);
    final bitrate = t.bitrate;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: Tokens.surface2,
        border: Border.all(color: Tokens.borderSubtle),
        borderRadius: BorderRadius.circular(Tokens.radiusCard),
      ),
      child: Row(
        children: [
          Container(
            decoration: BoxDecoration(
              border: Border.all(color: Tokens.borderSubtle),
              borderRadius: BorderRadius.circular(10),
            ),
            child: FutureBuilder<String?>(
              future: sidecarCover(t.path),
              builder: (context, snap) => Artwork(
                path: snap.data,
                size: 54,
                radius: 10,
                placeholder: Icons.music_note_rounded,
                placeholderSize: 26,
                cacheWidth: 162,
              ),
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  t.title,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontWeight: FontWeight.w700,
                    fontSize: 15,
                  ),
                ),
                if (sub.isNotEmpty) ...[
                  const SizedBox(height: 3),
                  Text(
                    sub,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: Tokens.dim, fontSize: 12),
                  ),
                ],
                const SizedBox(height: 3),
                Wrap(
                  spacing: 8,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    FormatBadge(label: t.formatBadge, compact: true),
                    if (t.duration > 0)
                      Text(
                        t.durationLabel,
                        style: const TextStyle(
                          fontSize: 11,
                          color: Tokens.textTertiary,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    if (bitrate != null && bitrate > 0)
                      Text(
                        '${bitrate.round()} kbps',
                        style: const TextStyle(fontSize: 10, color: Tokens.dim),
                      ),
                  ],
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// The card's art: the first of library.py's sidecar names in the track's
/// folder, or null for the placeholder.
Future<String?> sidecarCover(String path) async {
  if (path.isEmpty) return null;
  final dir = File(path).parent.path;
  for (final name in const [
    'cover.jpg',
    'cover.png',
    'folder.jpg',
    'folder.png',
    'Artwork.jpg',
  ]) {
    final f = File('$dir${Platform.pathSeparator}$name');
    if (await f.exists()) return f.path;
  }
  return null;
}
