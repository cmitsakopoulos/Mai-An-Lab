import 'package:flutter/material.dart';

import '../core/py_text.dart';
import '../core/text_fold.dart';
import '../data/browse_queries.dart';
import '../data/library_repository.dart';
import '../library/browse_controller.dart';
import '../library/track_query.dart';
import '../settings/settings_search.dart' show mergeSort;
import '../ui/tokens.dart';

/// Port of `ui/widgets.py` `strip_markup` and the local-library & ownership
/// pieces of `ui/views/search.py` (`_annotate_ownership`, `_ownership_badge`,
/// `_load_local_matches`, `_build_local_section`, `_local_row`).

final _markupRe = RegExp(r'\[/?[^\]]*\]');

/// Removes Kivy-style `[b]...[/b]` markup tags from search metadata.
String stripMarkup(String text) => text.replaceAll(_markupRe, '');

/// The Search tab's media filter modes (`all`, `tracks`, `albums`, `artists`).
enum SearchViewMode {
  all('All'),
  tracks('Tracks'),
  albums('Albums'),
  artists('Artists');

  const SearchViewMode(this.label);
  final String label;
}

/// Ownership state stamped onto a remote search result (`_annotate_ownership`).
class OwnershipInfo {
  const OwnershipInfo({
    required this.mediaType,
    required this.isInLibrary,
    this.ownedTracks = 0,
    this.numTracks = 0,
  });

  /// `'track'`, `'album'`, or `'artist'`.
  final String mediaType;
  final bool isInLibrary;
  final int ownedTracks;
  final int numTracks;

  /// `search.py` `_ownership_badge` label, or null when not owned.
  String? get badgeLabel {
    if (mediaType == 'track' && isInLibrary) return 'IN LIBRARY';
    if (mediaType == 'album' && ownedTracks > 0) {
      return isInLibrary ? 'IN LIBRARY' : 'YOU HAVE $ownedTracks/$numTracks';
    }
    if (mediaType == 'artist' && ownedTracks > 0) {
      return '$ownedTracks TRACK${ownedTracks != 1 ? 'S' : ''} OWNED';
    }
    return null;
  }
}

/// `search.py` `_annotate_ownership`: checks what the library already holds
/// for a remote track, album, or artist.
Future<OwnershipInfo> annotateOwnership(
  LibraryRepository repo, {
  required String mediaType,
  required String title,
  required String artist,
  int numTracks = 0,
}) async {
  final cleanTitle = stripMarkup(title);
  final cleanArtist = stripMarkup(artist);
  if (mediaType == 'track') {
    final owned = await repo.hasTrackByMeta(cleanTitle, cleanArtist);
    return OwnershipInfo(mediaType: 'track', isInLibrary: owned);
  }
  if (mediaType == 'album') {
    final owned = await repo.albumOwnedCount(cleanTitle, cleanArtist);
    return OwnershipInfo(
      mediaType: 'album',
      ownedTracks: owned,
      numTracks: numTracks,
      isInLibrary: owned > 0 && (numTracks == 0 || owned >= numTracks),
    );
  }
  if (mediaType == 'artist') {
    final owned = await repo.artistOwnedCount(cleanTitle);
    return OwnershipInfo(
      mediaType: 'artist',
      ownedTracks: owned,
      isInLibrary: false,
    );
  }
  return OwnershipInfo(mediaType: mediaType, isInLibrary: false);
}

/// Compact pill badge shown on owned or partially owned search results.
class OwnershipBadge extends StatelessWidget {
  const OwnershipBadge({super.key, required this.ownership});

  final OwnershipInfo ownership;

  @override
  Widget build(BuildContext context) {
    final label = ownership.badgeLabel;
    if (label == null) return const SizedBox.shrink();
    final accent = Theme.of(context).colorScheme.primary;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 1),
      decoration: BoxDecoration(
        color: accent.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(3),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: accent,
          fontSize: 8,
          fontWeight: FontWeight.w700,
        ),
      ),
    );
  }
}

/// Library-aware local search results (`search.py` `_local_matches`).
class LocalMatches {
  const LocalMatches({
    this.tracks = const [],
    this.albums = const [],
    this.artists = const [],
  });

  static const empty = LocalMatches();

  final List<Track> tracks;
  final List<AlbumRow> albums;
  final List<ArtistRow> artists;

  int get totalCount => tracks.length + albums.length + artists.length;
  bool get isEmpty => totalCount == 0;
  bool get isNotEmpty => totalCount > 0;
}

/// `search.py` `_load_local_matches`: word-substring matching (accent &
/// case-blind, matching the Library tab) WITHOUT the closest-match fallback,
/// ranked exact match first, then prefix match, then substring match.
Future<LocalMatches> loadLocalMatches(
  LibraryRepository repo,
  String query,
) async {
  final words = queryWords(query);
  if (words.isEmpty) return LocalMatches.empty;

  final (allTracks, allAlbums, allArtists) = await (
    repo.allTracks(),
    repo.allAlbums('album'),
    repo.allArtists('name'),
  ).wait;

  final matchedTracks = <Track>[];
  final matchedAlbumIds = <int>{};
  final matchedArtistIds = <int>{};
  for (final t in allTracks) {
    final key = searchKey(t);
    if (words.every(key.contains)) {
      matchedTracks.add(t);
      if (t.albumId != null) matchedAlbumIds.add(t.albumId!);
      if (t.artistId != null) matchedArtistIds.add(t.artistId!);
    }
  }

  final matchedAlbums = [
    for (final a in allAlbums)
      if (words.every(
            fold('${a.title ?? ''}\u0000${a.artist ?? ''}').contains,
          ) ||
          matchedAlbumIds.contains(a.id))
        a,
  ];

  final matchedArtists = [
    for (final a in allArtists)
      if (words.every(fold(a.name ?? '').contains) ||
          matchedArtistIds.contains(a.id))
        a,
  ];

  final q = pyLower(query.trim());
  int rankOf(String? raw) {
    final text = pyLower(raw ?? '');
    if (text == q) return 0;
    if (text.startsWith(q)) return 1;
    return 2;
  }

  mergeSort(
    matchedTracks,
    compare: (a, b) => rankOf(a.title).compareTo(rankOf(b.title)),
  );
  mergeSort(
    matchedAlbums,
    compare: (a, b) => rankOf(a.title).compareTo(rankOf(b.title)),
  );
  mergeSort(
    matchedArtists,
    compare: (a, b) => rankOf(a.name).compareTo(rankOf(b.name)),
  );

  return LocalMatches(
    tracks: matchedTracks,
    albums: matchedAlbums,
    artists: matchedArtists,
  );
}

/// `search.py` `_build_local_section`: expandable "In your library" card shown
/// above remote search results (and alone when offline or before remote search
/// is wired).
class LocalLibrarySection extends StatelessWidget {
  const LocalLibrarySection({
    super.key,
    required this.query,
    required this.matches,
    required this.mode,
    required this.expanded,
    required this.onToggle,
    required this.onPlayTrack,
    required this.onOpenInLibrary,
  });

  /// `SearchView.LOCAL_PREVIEW_ROWS`.
  static const previewRows = 3;

  final String query;
  final LocalMatches matches;
  final SearchViewMode mode;
  final bool expanded;
  final VoidCallback onToggle;
  final ValueChanged<String> onPlayTrack;
  final void Function(LibraryView view, String name) onOpenInLibrary;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final rows = <Widget>[];
    final int matchCount;
    final LibraryView seeAllView;

    if (mode == SearchViewMode.all) {
      matchCount = matches.totalCount;
      if (matchCount == 0) return const SizedBox.shrink();
      seeAllView = LibraryView.tracks;

      // One of each type first, then fill up to previewRows.
      void addTrack(Track t) => rows.add(_trackRow(context, t));
      void addAlbum(AlbumRow a) => rows.add(_albumRow(a));
      void addArtist(ArtistRow a) => rows.add(_artistRow(a));

      if (matches.tracks.isNotEmpty && rows.length < previewRows) {
        addTrack(matches.tracks.first);
      }
      if (matches.albums.isNotEmpty && rows.length < previewRows) {
        addAlbum(matches.albums.first);
      }
      if (matches.artists.isNotEmpty && rows.length < previewRows) {
        addArtist(matches.artists.first);
      }
      for (final t in matches.tracks.skip(1)) {
        if (rows.length >= previewRows) break;
        addTrack(t);
      }
      for (final a in matches.albums.skip(1)) {
        if (rows.length >= previewRows) break;
        addAlbum(a);
      }
      for (final a in matches.artists.skip(1)) {
        if (rows.length >= previewRows) break;
        addArtist(a);
      }
    } else if (mode == SearchViewMode.tracks) {
      matchCount = matches.tracks.length;
      if (matchCount == 0) return const SizedBox.shrink();
      seeAllView = LibraryView.tracks;
      for (final t in matches.tracks.take(previewRows)) {
        rows.add(_trackRow(context, t));
      }
    } else if (mode == SearchViewMode.albums) {
      matchCount = matches.albums.length;
      if (matchCount == 0) return const SizedBox.shrink();
      seeAllView = LibraryView.albums;
      for (final a in matches.albums.take(previewRows)) {
        rows.add(_albumRow(a));
      }
    } else {
      matchCount = matches.artists.length;
      if (matchCount == 0) return const SizedBox.shrink();
      seeAllView = LibraryView.artists;
      for (final a in matches.artists.take(previewRows)) {
        rows.add(_artistRow(a));
      }
    }

    if (matchCount > rows.length) {
      rows.add(
        InkWell(
          borderRadius: BorderRadius.circular(8),
          onTap: () => onOpenInLibrary(seeAllView, query),
          child: Padding(
            padding: const EdgeInsets.only(top: 6, bottom: 2),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  'See all $matchCount in Library',
                  style: TextStyle(
                    color: accent,
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(width: 2),
                Icon(Icons.chevron_right_rounded, color: accent, size: 16),
              ],
            ),
          ),
        ),
      );
    }

    final toggleLabel = expanded ? 'Hide' : 'Show ($matchCount)';
    final toggleIcon = expanded
        ? Icons.keyboard_arrow_up_rounded
        : Icons.keyboard_arrow_down_rounded;
    final toggleColor = expanded ? Tokens.dim : accent;

    return Container(
      margin: const EdgeInsets.only(bottom: 6),
      decoration: BoxDecoration(
        color: Tokens.surface,
        border: Border.all(color: Tokens.borderSubtle),
        borderRadius: BorderRadius.circular(Tokens.radiusCard),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          InkWell(
            borderRadius: BorderRadius.circular(Tokens.radiusCard),
            onTap: onToggle,
            child: Padding(
              padding: expanded
                  ? const EdgeInsets.fromLTRB(12, 10, 12, 6)
                  : const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(
                        Icons.library_music_rounded,
                        color: accent,
                        size: 15,
                      ),
                      const SizedBox(width: 7),
                      const Text(
                        'In your library',
                        style: TextStyle(
                          color: Tokens.text,
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      const SizedBox(width: 7),
                      Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 6,
                          vertical: 1,
                        ),
                        decoration: BoxDecoration(
                          color: accent.withValues(alpha: 0.15),
                          borderRadius: BorderRadius.circular(8),
                        ),
                        child: Text(
                          '$matchCount',
                          style: TextStyle(
                            color: accent,
                            fontSize: 10,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ),
                    ],
                  ),
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text(
                        toggleLabel,
                        style: TextStyle(
                          color: toggleColor,
                          fontSize: 11,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      const SizedBox(width: 2),
                      Icon(toggleIcon, color: toggleColor, size: 15),
                    ],
                  ),
                ],
              ),
            ),
          ),
          if (expanded) ...[
            Container(
              height: 1,
              margin: const EdgeInsets.symmetric(horizontal: 12),
              color: Tokens.borderSubtle,
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(12, 4, 12, 10),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: rows,
              ),
            ),
          ],
        ],
      ),
    );
  }

  Widget _trackRow(BuildContext context, Track t) {
    final accent = Theme.of(context).colorScheme.primary;
    final sub = [
      if (t.artist.isNotEmpty) t.artist,
      if (t.album.isNotEmpty) t.album,
    ].join(' · ');
    return _rowShell(
      icon: Icons.music_note_rounded,
      iconColor: Tokens.libTrack,
      title: t.title.isEmpty ? 'Unknown' : t.title,
      subtitle: sub,
      onTap: () => onOpenInLibrary(LibraryView.tracks, t.title),
      trailing: t.path.isNotEmpty
          ? IconButton(
              icon: const Icon(Icons.play_circle_outline_rounded, size: 20),
              color: accent,
              tooltip: 'Play Track',
              padding: EdgeInsets.zero,
              constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
              onPressed: () => onPlayTrack(t.path),
            )
          : null,
    );
  }

  Widget _albumRow(AlbumRow a) {
    final n = a.trackCount ?? 0;
    final artist = a.artist ?? '';
    final title = (a.title == null || a.title!.isEmpty) ? 'Unknown' : a.title!;
    final sub = [
      if (artist.isNotEmpty) artist,
      if (n > 0) '$n track${n != 1 ? 's' : ''}',
    ].join(' · ');
    return _rowShell(
      icon: Icons.album_rounded,
      iconColor: Tokens.libAlbum,
      title: title,
      subtitle: sub,
      onTap: () => onOpenInLibrary(LibraryView.albums, title),
    );
  }

  Widget _artistRow(ArtistRow a) {
    final albums = a.albumCount ?? 0;
    final tracks = a.trackCount ?? 0;
    final name = (a.name == null || a.name!.isEmpty) ? 'Unknown' : a.name!;
    final sub =
        '$albums album${albums != 1 ? 's' : ''} · '
        '$tracks track${tracks != 1 ? 's' : ''}';
    return _rowShell(
      icon: Icons.person_rounded,
      iconColor: Tokens.libArtist,
      title: name,
      subtitle: sub,
      onTap: () => onOpenInLibrary(LibraryView.artists, name),
    );
  }

  Widget _rowShell({
    required IconData icon,
    required Color iconColor,
    required String title,
    required String subtitle,
    required VoidCallback onTap,
    Widget? trailing,
  }) => InkWell(
    borderRadius: BorderRadius.circular(8),
    onTap: onTap,
    child: Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        children: [
          Icon(icon, color: iconColor, size: 18),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                if (subtitle.isNotEmpty) ...[
                  const SizedBox(height: 1),
                  Text(
                    subtitle,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: Tokens.dim, fontSize: 11),
                  ),
                ],
              ],
            ),
          ),
          if (trailing != null) ...[const SizedBox(width: 8), trailing],
        ],
      ),
    ),
  );
}
