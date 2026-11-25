import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../data/browse_queries.dart';
import '../data/library_repository.dart';
import '../player/player_controller.dart';
import '../ui/sliding_segmented.dart';
import '../ui/tokens.dart';
import 'browse_controller.dart';
import 'genre_colors.dart';
import 'selection.dart';
import 'track_row.dart';

/// The Genres, Artists, Albums and Playlists views: library.py's expandable
/// rows (`_genre_row`, `_artist_row`, `_album_row`, `_playlist_row`, and
/// `_track_row` under an album or a playlist). Tapping a row opens or closes
/// it. A playlist's tracks reorder by their drag handle, within their
/// playlist only (Flet's DragTarget group). With the metadata workbench
/// wired in, an artist row carries "Fix artist info" and a genre row
/// "Curate in Workbench".
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
    this.onEditArtist,
    this.onCurateGenre,
    this.topPadding = 0,
    this.bottomPadding = 20,
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

  /// `open_artist_metadata_editor`: the workbench focused on one artist.
  final void Function(String artist)? onEditArtist;

  /// `open_genre_metadata_workbench`: the workbench filtered to a genre
  /// ("" from the empty Genres view: the whole workbench).
  final void Function(String genre)? onCurateGenre;

  final double topPadding;
  final double bottomPadding;

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
        final noGenres =
            controller.view == LibraryView.genres &&
            !controller.loading &&
            controller.rows.isEmpty;
        return ValueListenableBuilder(
          valueListenable: player,
          builder: (context, p, _) => ListenableBuilder(
            // Only the track rows depend on the playing track and the
            // selection.
            listenable: Listenable.merge([?p, controller, selection]),
            builder: (context, _) {
              final playing = p?.stopped == false ? p?.nowPlaying?.path : null;
              final rows = controller.rows;
              final palette = CategoryPalette.of(context);
              return ReorderableListView.builder(
                padding: EdgeInsets.only(
                  top: topPadding,
                  bottom: bottomPadding,
                ),
                buildDefaultDragHandles: false,
                header: noPlaylists
                    ? _EmptyBrowse(
                        icon: Icons.queue_music_rounded,
                        accent: palette.playlist,
                        title: 'No playlists yet.',
                        body: 'Create your first playlist below.',
                        actionIcon: Icons.add_rounded,
                        actionLabel: 'CREATE PLAYLIST',
                        onAction: onCreatePlaylist,
                      )
                    : noGenres
                    ? _EmptyBrowse(
                        icon: Icons.style_rounded,
                        accent: palette.genre,
                        title: 'No genres found.',
                        body:
                            'Enrich artist tags in Settings to classify genres.',
                        actionIcon: Icons.tune_rounded,
                        actionLabel: 'METADATA WORKBENCH',
                        onAction: onCurateGenre == null
                            ? null
                            : () => onCurateGenre!(''),
                      )
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
      onCurate: onCurateGenre == null ? null : () => onCurateGenre!(genre.name),
    ),
    ArtistNode(:final artist) => _ArtistTile(
      key: ValueKey(n.key),
      artist: artist,
      depth: n.depth,
      expanded: controller.isExpanded(n),
      onTap: () => controller.toggle(n),
      onEdit: onEditArtist == null || artist.name == null
          ? null
          : () => onEditArtist!(artist.name!),
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
    this.onCurate,
  });

  final GenreRow genre;
  final bool expanded;
  final VoidCallback onTap;
  final VoidCallback onPlay;
  final VoidCallback? onCurate;

  @override
  Widget build(BuildContext context) {
    final palette = CategoryPalette.of(context);
    final accent = palette.unified ? palette.genre : genreColor(genre.name);
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
        if (onCurate != null)
          IconButton(
            icon: const Icon(Icons.tune_rounded, size: 18),
            color: Tokens.dim.withValues(alpha: 0.6),
            tooltip: 'Curate ${genre.name} in Workbench',
            onPressed: onCurate,
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
    this.onEdit,
  });

  final ArtistRow artist;
  final int depth;
  final bool expanded;
  final VoidCallback onTap;
  final VoidCallback? onEdit;

  @override
  Widget build(BuildContext context) {
    final palette = CategoryPalette.of(context);
    return _Tile(
      icon: Icons.person_rounded,
      accent: palette.atDepth(palette.artist, depth),
      indent: depth * 16.0,
      title: artist.displayName,
      titleLines: 3,
      subtitle:
          '${artist.albumCount ?? 0} albums  ·  ${artist.trackCount ?? 0} tracks',
      expanded: expanded,
      tint: 0.07,
      trailing: [
        if (onEdit != null)
          IconButton(
            icon: const Icon(Icons.edit_outlined, size: 20),
            color: Tokens.dim.withValues(alpha: 0.6),
            tooltip: 'Fix artist info',
            onPressed: onEdit,
          ),
      ],
      onTap: onTap,
    );
  }
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
  Widget build(BuildContext context) {
    final palette = CategoryPalette.of(context);
    return _Tile(
      icon: Icons.album_rounded,
      accent: palette.atDepth(palette.album, depth),
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
    final palette = CategoryPalette.of(context);
    final accent = playlistColor(playlist.color) ?? palette.playlist;
    return _Tile(
      icon: Icons.queue_music_rounded,
      accent: accent,
      indent: 0,
      title: playlist.displayName,
      titleLines: 3,
      subtitle: '$n ${n == 1 ? 'track' : 'tracks'}',
      expanded: expanded,
      tint: 0.06,
      trailing: [
        IconButton(
          icon: const Icon(Icons.edit_outlined, size: 16),
          color: accent.withValues(alpha: 0.65),
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
  Widget build(BuildContext context) {
    final accent = CategoryPalette.of(context).playlist;
    return ListTile(
      tileColor: accent.withValues(alpha: 0.05),
      leading: Icon(Icons.add_circle_outline_rounded, color: accent),
      title: Text(
        'Create New Playlist',
        style: TextStyle(
          color: accent,
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
}

/// library.py's `_empty_label` inside a browse view: "No playlists yet."
/// (CREATE PLAYLIST) or "No genres found." (METADATA WORKBENCH, when the
/// workbench is available).
class _EmptyBrowse extends StatelessWidget {
  const _EmptyBrowse({
    required this.icon,
    required this.accent,
    required this.title,
    required this.body,
    required this.actionIcon,
    required this.actionLabel,
    required this.onAction,
  });
  final IconData icon;
  final Color accent;
  final String title;
  final String body;
  final IconData actionIcon;
  final String actionLabel;
  final VoidCallback? onAction;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 40),
    child: Column(
      children: [
        Icon(icon, color: accent.withValues(alpha: 0.3), size: 80),
        const SizedBox(height: 12),
        Text(
          title,
          style: const TextStyle(
            color: Tokens.text,
            fontSize: 20,
            fontWeight: FontWeight.w800,
          ),
        ),
        const SizedBox(height: 12),
        Text(
          body,
          textAlign: TextAlign.center,
          style: const TextStyle(color: Tokens.dim, fontSize: 14),
        ),
        if (onAction != null) ...[
          const SizedBox(height: 22),
          TextButton.icon(
            onPressed: onAction,
            style: TextButton.styleFrom(foregroundColor: accent),
            icon: Icon(actionIcon, size: 16),
            label: Text(actionLabel, style: const TextStyle(fontSize: 13)),
          ),
        ],
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
    this.grouped = false,
  });

  final List<LibraryView> views;
  final LibraryView selected;
  final bool unified;
  final ValueChanged<LibraryView> onSelect;
  final bool grouped;

  static IconData icon(LibraryView v) => switch (v) {
    LibraryView.playlists => Icons.queue_music_rounded,
    LibraryView.genres => Icons.style_rounded,
    LibraryView.artists => Icons.person_rounded,
    LibraryView.albums => Icons.album_rounded,
    LibraryView.tracks => Icons.music_note_rounded,
  };

  static Color? color(LibraryView v) => switch (v) {
    LibraryView.playlists => Tokens.libPlaylist,
    LibraryView.genres => Tokens.libGenre,
    LibraryView.artists => Tokens.libArtist,
    LibraryView.albums => Tokens.libAlbum,
    LibraryView.tracks => Tokens.libTrack,
  };

  @override
  Widget build(BuildContext context) {
    final cyan = Theme.of(context).colorScheme.primary;
    final isUnified = unified || CategoryPalette.of(context).unified;
    return SlidingSegmented<LibraryView>(
      segments: [
        for (final v in views)
          Segment(
            value: v,
            label: v.label,
            icon: icon(v),
            color: isUnified ? cyan : color(v),
          ),
      ],
      selected: selected,
      onSelect: onSelect,
      accent: cyan,
      height: 42,
      iconSize: 20,
      iconOnly: true,
      grouped: grouped,
    );
  }
}
