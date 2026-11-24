import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../data/browse_queries.dart';
import '../data/library_repository.dart';
import '../player/player_controller.dart';
import '../ui/tokens.dart';
import 'browse_controller.dart';
import 'genre_colors.dart';
import 'selection.dart';
import 'track_row.dart';

/// The Genres, Artists, Albums and Playlists views: library.py's expandable
/// rows (`_genre_row`, `_artist_row`, `_album_row`, `_playlist_row`, and
/// `_track_row` under an album or a playlist). Tapping a row opens or closes
/// it. A playlist's tracks reorder by their drag handle, within their
/// playlist only (Flet's DragTarget group).
///
/// Not here yet: the artist row's "Fix artist info" and the genre row's
/// "Curate in Workbench" buttons (metadata workbench, phase 7).
class BrowseList extends StatelessWidget {
  const BrowseList({
    super.key,
    required this.controller,
    required this.player,
    required this.onPlayTrack,
    required this.onTrackMenu,
    required this.onAlbumMenu,
    required this.onPlayGenre,
    required this.selection,
    required this.onCreatePlaylist,
    required this.onEditPlaylist,
    required this.onDeletePlaylist,
    required this.onRemoveFromPlaylist,
  });

  final BrowseController controller;
  final ValueListenable<PlayerController?> player;

  /// A track row tapped: [context] is its album or playlist, as shown.
  final void Function(Track track, List<Track> context) onPlayTrack;
  final void Function(Track track) onTrackMenu;
  final void Function(AlbumRow album) onAlbumMenu;
  final void Function(GenreRow genre) onPlayGenre;
  final SelectionController selection;
  final VoidCallback onCreatePlaylist;
  final void Function(PlaylistRow playlist) onEditPlaylist;
  final void Function(PlaylistRow playlist) onDeletePlaylist;
  final void Function(int playlistId, Track track) onRemoveFromPlaylist;

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: controller,
      builder: (context, _) {
        if (controller.error case final e?) {
          return Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Text(
                e,
                textAlign: TextAlign.center,
                style: const TextStyle(color: Tokens.dim),
              ),
            ),
          );
        }
        if (controller.loading && controller.rows.isEmpty) {
          return const Center(child: CircularProgressIndicator());
        }
        final noPlaylists =
            controller.view == LibraryView.playlists &&
            !controller.loading &&
            !controller.rows.any((r) => r is PlaylistNode);
        return ValueListenableBuilder(
          valueListenable: player,
          builder: (context, p, _) => ListenableBuilder(
            // Only the track rows depend on the playing track and the
            // selection.
            listenable: Listenable.merge([?p, controller, selection]),
            builder: (context, _) {
              final playing = p?.stopped == false ? p?.nowPlaying?.path : null;
              final rows = controller.rows;
              return ReorderableListView.builder(
                padding: const EdgeInsets.only(bottom: 20),
                buildDefaultDragHandles: false,
                header: noPlaylists
                    ? _NoPlaylists(onCreate: onCreatePlaylist)
                    : null,
                itemCount: rows.length,
                onReorder: (from, to) => _reorder(rows, from, to),
                itemBuilder: (context, i) => _row(rows[i], i, playing),
              );
            },
          ),
        );
      },
    );
  }

  /// A playlist track dropped at [to] (ReorderableListView's index, before
  /// the row is taken out). Only moves inside its own playlist count.
  void _reorder(List<BrowseNode> rows, int from, int to) {
    final src = rows[from];
    if (src is! TrackNode || src.playlistId == null) return;
    final id = src.playlistId;
    bool inBlock(int i) =>
        i >= 0 &&
        i < rows.length &&
        rows[i] is TrackNode &&
        (rows[i] as TrackNode).playlistId == id;
    var first = from;
    while (inBlock(first - 1)) {
      first--;
    }
    var last = from;
    while (inBlock(last + 1)) {
      last++;
    }
    final target = to > from ? to - 1 : to;
    if (target < first || target > last || target == from) return;
    controller.movePlaylistTrack(id!, from - first, target - first);
  }

  Widget _track(TrackNode n, int index, String? playing) {
    final t = n.track;
    final picking = selection.active;
    return SizedBox(
      key: ValueKey(n.key),
      height: TrackRow.height,
      child: TrackRow(
        track: t,
        depth: n.depth,
        inAlbum: n.playlistId == null,
        playing: t.path == playing,
        selected: picking ? selection.contains(t.path) : null,
        onTap: picking
            ? () => selection.toggle(t)
            : () => onPlayTrack(t, n.context),
        onLongPress: () => onTrackMenu(t),
        trailing: n.playlistId == null
            ? null
            : Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  ReorderableDragStartListener(
                    index: index,
                    child: const Padding(
                      padding: EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                      child: Icon(
                        Icons.drag_handle_rounded,
                        color: Tokens.dim,
                        size: 18,
                      ),
                    ),
                  ),
                  IconButton(
                    icon: const Icon(Icons.remove_circle_outline, size: 18),
                    color: const Color(0xFFFF4444),
                    tooltip: 'Remove from Playlist',
                    onPressed: () => onRemoveFromPlaylist(n.playlistId!, t),
                  ),
                ],
              ),
      ),
    );
  }

  Widget _row(BrowseNode n, int index, String? playing) => switch (n) {
    GenreNode(:final genre) => _GenreTile(
      key: ValueKey(n.key),
      genre: genre,
      expanded: controller.isExpanded(n),
      onTap: () => controller.toggle(n),
      onPlay: () => onPlayGenre(genre),
    ),
    ArtistNode(:final artist) => _ArtistTile(
      key: ValueKey(n.key),
      artist: artist,
      depth: n.depth,
      expanded: controller.isExpanded(n),
      onTap: () => controller.toggle(n),
    ),
    AlbumNode(:final album) => _AlbumTile(
      key: ValueKey(n.key),
      album: album,
      depth: n.depth,
      expanded: controller.isExpanded(n),
      onTap: () => controller.toggle(n),
      onLongPress: () => onAlbumMenu(album),
    ),
    TrackNode() => _track(n, index, playing),
    PlaylistNode(:final playlist) => _PlaylistTile(
      key: ValueKey(n.key),
      playlist: playlist,
      expanded: controller.isExpanded(n),
      onTap: () => controller.toggle(n),
      onEdit: () => onEditPlaylist(playlist),
      onDelete: () => onDeletePlaylist(playlist),
    ),
    EmptyPlaylistNode() => _EmptyPlaylistRow(
      key: ValueKey(n.key),
      depth: n.depth,
    ),
    NewPlaylistNode() => _NewPlaylistRow(
      key: ValueKey(n.key),
      onTap: onCreatePlaylist,
    ),
  };
}

/// The chevron that turns a quarter when its row opens (Flet: 200 ms,
/// decelerate).
class _Chevron extends StatelessWidget {
  const _Chevron({required this.expanded, required this.accent});
  final bool expanded;
  final Color accent;

  @override
  Widget build(BuildContext context) => AnimatedRotation(
    turns: expanded ? 0.25 : 0,
    duration: const Duration(milliseconds: 200),
    curve: Curves.decelerate,
    child: Icon(
      Icons.chevron_right_rounded,
      size: 18,
      color: expanded ? accent : Tokens.dim,
    ),
  );
}

class _Tile extends StatelessWidget {
  const _Tile({
    required this.icon,
    required this.accent,
    required this.indent,
    required this.title,
    required this.titleLines,
    required this.subtitle,
    required this.expanded,
    required this.tint,
    required this.trailing,
    required this.onTap,
    this.onLongPress,
    this.afterChevron = const [],
  });

  final IconData icon;
  final Color accent;
  final double indent;
  final String title;
  final int titleLines;
  final String subtitle;
  final bool expanded;

  /// Background opacity of the accent while open (artist, genre 0.07;
  /// album 0.06).
  final double tint;
  final List<Widget> trailing;
  final VoidCallback onTap;
  final VoidCallback? onLongPress;

  /// Buttons after the chevron (the playlist row's delete).
  final List<Widget> afterChevron;

  @override
  Widget build(BuildContext context) => ListTile(
    contentPadding: EdgeInsets.only(left: 16 + indent, right: 12),
    tileColor: expanded ? accent.withValues(alpha: tint) : null,
    leading: Icon(icon, color: accent),
    title: Text(
      title,
      maxLines: titleLines,
      overflow: TextOverflow.ellipsis,
      style: const TextStyle(
        color: Tokens.text,
        fontSize: 14,
        fontWeight: FontWeight.w600,
      ),
    ),
    subtitle: Text(
      subtitle,
      maxLines: 2,
      overflow: TextOverflow.ellipsis,
      style: const TextStyle(color: Tokens.dim, fontSize: 12),
    ),
    trailing: Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        ...trailing,
        _Chevron(expanded: expanded, accent: accent),
        ...afterChevron,
      ],
    ),
    onTap: onTap,
    onLongPress: onLongPress,
  );
}

class _GenreTile extends StatelessWidget {
  const _GenreTile({
    super.key,
    required this.genre,
    required this.expanded,
    required this.onTap,
    required this.onPlay,
  });

  final GenreRow genre;
  final bool expanded;
  final VoidCallback onTap;
  final VoidCallback onPlay;

  @override
  Widget build(BuildContext context) {
    final accent = genreColor(genre.name);
    return _Tile(
      icon: Icons.style_rounded,
      accent: accent,
      indent: 0,
      title: genre.name,
      titleLines: 2,
      subtitle:
          '${genre.artistCount} artists  ·  ${genre.albumCount} albums  ·  '
          '${genre.trackCount} tracks',
      expanded: expanded,
      tint: 0.07,
      trailing: [
        IconButton(
          icon: const Icon(Icons.play_arrow_rounded, size: 20),
          color: accent.withValues(alpha: 0.85),
          tooltip: 'Play ${genre.name}',
          onPressed: onPlay,
        ),
      ],
      onTap: onTap,
    );
  }
}

class _ArtistTile extends StatelessWidget {
  const _ArtistTile({
    super.key,
    required this.artist,
    required this.depth,
    required this.expanded,
    required this.onTap,
  });

  final ArtistRow artist;
  final int depth;
  final bool expanded;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => _Tile(
    icon: Icons.person_rounded,
    accent: Tokens.libArtist,
    indent: depth * 16.0,
    title: artist.displayName,
    titleLines: 3,
    subtitle:
        '${artist.albumCount ?? 0} albums  ·  ${artist.trackCount ?? 0} tracks',
    expanded: expanded,
    tint: 0.07,
    trailing: const [],
    onTap: onTap,
  );
}

class _AlbumTile extends StatelessWidget {
  const _AlbumTile({
    super.key,
    required this.album,
    required this.depth,
    required this.expanded,
    required this.onTap,
    required this.onLongPress,
  });

  final AlbumRow album;
  final int depth;
  final bool expanded;
  final VoidCallback onTap;
  final VoidCallback onLongPress;

  @override
  Widget build(BuildContext context) => _Tile(
    icon: Icons.album_rounded,
    accent: Tokens.libAlbum,
    indent: depth * 20.0,
    title: album.displayTitle,
    titleLines: 3,
    subtitle: '${album.displayArtist}  ·  ${album.trackCount ?? '?'} tracks',
    expanded: expanded,
    tint: 0.06,
    trailing: const [],
    onTap: onTap,
    onLongPress: onLongPress,
  );
}

/// Flet's `#RRGGBB` playlist colour, or null when unset or malformed.
Color? playlistColor(String? hex) {
  if (hex == null || !RegExp(r'^#[0-9a-fA-F]{6}$').hasMatch(hex)) return null;
  return Color(0xFF000000 | int.parse(hex.substring(1), radix: 16));
}

class _PlaylistTile extends StatelessWidget {
  const _PlaylistTile({
    super.key,
    required this.playlist,
    required this.expanded,
    required this.onTap,
    required this.onEdit,
    required this.onDelete,
  });

  final PlaylistRow playlist;
  final bool expanded;
  final VoidCallback onTap;
  final VoidCallback onEdit;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    final n = playlist.trackCount;
    return _Tile(
      icon: Icons.queue_music_rounded,
      accent: playlistColor(playlist.color) ?? Tokens.libPlaylist,
      indent: 0,
      title: playlist.displayName,
      titleLines: 3,
      subtitle: '$n ${n == 1 ? 'track' : 'tracks'}',
      expanded: expanded,
      tint: 0.06,
      trailing: [
        IconButton(
          icon: const Icon(Icons.edit_outlined, size: 16),
          color: Tokens.libPlaylist.withValues(alpha: 0.6),
          tooltip: 'Edit playlist',
          onPressed: onEdit,
        ),
      ],
      afterChevron: [
        IconButton(
          icon: const Icon(Icons.delete_outline, size: 16),
          color: const Color(0xFFFF5555).withValues(alpha: 0.5),
          tooltip: 'Delete playlist',
          onPressed: onDelete,
        ),
      ],
      onTap: onTap,
    );
  }
}

class _EmptyPlaylistRow extends StatelessWidget {
  const _EmptyPlaylistRow({super.key, required this.depth});
  final int depth;

  @override
  Widget build(BuildContext context) => Padding(
    padding: EdgeInsets.fromLTRB(20.0 * depth + 12 + 16, 13, 25, 13),
    child: const Row(
      children: [
        Icon(Icons.playlist_remove_rounded, color: Tokens.dim, size: 18),
        SizedBox(width: 8),
        Expanded(
          child: Text(
            'Playlist is empty',
            style: TextStyle(
              color: Tokens.dim,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ],
    ),
  );
}

class _NewPlaylistRow extends StatelessWidget {
  const _NewPlaylistRow({super.key, required this.onTap});
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => ListTile(
    tileColor: Tokens.libPlaylist.withValues(alpha: 0.05),
    leading: const Icon(
      Icons.add_circle_outline_rounded,
      color: Tokens.libPlaylist,
    ),
    title: const Text(
      'Create New Playlist',
      style: TextStyle(
        color: Tokens.libPlaylist,
        fontWeight: FontWeight.w800,
        fontSize: 14,
      ),
    ),
    subtitle: const Text(
      'Organize your music collection',
      style: TextStyle(color: Tokens.dim, fontSize: 12),
    ),
    onTap: onTap,
  );
}

/// library.py's empty state in the Playlists tab: "No playlists yet."
class _NoPlaylists extends StatelessWidget {
  const _NoPlaylists({required this.onCreate});
  final VoidCallback onCreate;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 40),
    child: Column(
      children: [
        Icon(
          Icons.queue_music_rounded,
          color: Tokens.libPlaylist.withValues(alpha: 0.3),
          size: 80,
        ),
        const SizedBox(height: 12),
        const Text(
          'No playlists yet.',
          style: TextStyle(
            color: Tokens.text,
            fontSize: 20,
            fontWeight: FontWeight.w800,
          ),
        ),
        const SizedBox(height: 12),
        const Text(
          'Create your first playlist below.',
          textAlign: TextAlign.center,
          style: TextStyle(color: Tokens.dim, fontSize: 14),
        ),
        const SizedBox(height: 22),
        TextButton.icon(
          onPressed: onCreate,
          style: TextButton.styleFrom(foregroundColor: Tokens.libPlaylist),
          icon: const Icon(Icons.add_rounded, size: 16),
          label: const Text('CREATE PLAYLIST', style: TextStyle(fontSize: 13)),
        ),
      ],
    ),
  );
}

/// library.py `CupertinoSegmentedBar` for the view tabs: one capsule, the
/// active segment tinted in its category colour (or the accent with
/// `pill_style = "unified"`).
class ViewBar extends StatelessWidget {
  const ViewBar({
    super.key,
    required this.views,
    required this.selected,
    required this.unified,
    required this.onSelect,
  });

  final List<LibraryView> views;
  final LibraryView selected;
  final bool unified;
  final ValueChanged<LibraryView> onSelect;

  static IconData icon(LibraryView v) => switch (v) {
    LibraryView.playlists => Icons.queue_music_rounded,
    LibraryView.genres => Icons.style_rounded,
    LibraryView.artists => Icons.person_rounded,
    LibraryView.albums => Icons.album_rounded,
    LibraryView.tracks => Icons.music_note_rounded,
  };

  static Color color(LibraryView v) => switch (v) {
    LibraryView.playlists => Tokens.libPlaylist,
    LibraryView.genres => Tokens.libGenre,
    LibraryView.artists => Tokens.libArtist,
    LibraryView.albums => Tokens.libAlbum,
    LibraryView.tracks => Tokens.libTrack,
  };

  @override
  Widget build(BuildContext context) {
    final cyan = Theme.of(context).colorScheme.primary;
    final padH = views.length >= 5 ? 4.0 : (views.length == 4 ? 6.0 : 10.0);
    return Container(
      height: 40,
      padding: const EdgeInsets.all(3),
      decoration: BoxDecoration(
        color: Tokens.surface,
        border: Border.all(color: Tokens.borderSubtle),
        borderRadius: BorderRadius.circular(Tokens.radiusPill),
      ),
      child: Row(
        children: [
          for (final (i, v) in views.indexed) ...[
            if (i > 0) const SizedBox(width: 2),
            Expanded(
              child: _Segment(
                label: v.label,
                icon: icon(v),
                accent: unified ? cyan : color(v),
                active: v == selected,
                padH: padH,
                onTap: () => onSelect(v),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _Segment extends StatelessWidget {
  const _Segment({
    required this.label,
    required this.icon,
    required this.accent,
    required this.active,
    required this.padH,
    required this.onTap,
  });

  final String label;
  final IconData icon;
  final Color accent;
  final bool active;
  final double padH;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => GestureDetector(
    behavior: HitTestBehavior.opaque,
    onTap: onTap,
    child: AnimatedContainer(
      duration: const Duration(milliseconds: 140),
      curve: Curves.easeOut,
      alignment: Alignment.center,
      padding: EdgeInsets.symmetric(horizontal: padH, vertical: 6),
      decoration: BoxDecoration(
        color: active ? accent.withValues(alpha: 0.14) : Colors.transparent,
        border: Border.all(
          color: active ? accent.withValues(alpha: 0.35) : Colors.transparent,
        ),
        borderRadius: BorderRadius.circular(Tokens.radiusPill - 4),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 15, color: active ? accent : Tokens.dim),
          const SizedBox(width: 4),
          Flexible(
            child: Text(
              label,
              maxLines: 1,
              softWrap: false,
              overflow: TextOverflow.fade,
              style: TextStyle(
                fontSize: 11,
                fontWeight: active ? FontWeight.w600 : FontWeight.w500,
                color: active ? Tokens.text : Tokens.dim,
              ),
            ),
          ),
        ],
      ),
    ),
  );
}
