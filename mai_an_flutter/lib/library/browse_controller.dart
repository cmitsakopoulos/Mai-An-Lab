import 'package:flutter/foundation.dart';

import '../core/db_worker.dart';
import '../core/text_fold.dart';
import '../data/browse_queries.dart';
import '../data/library_repository.dart';
import 'library_controller.dart';
import 'track_query.dart';

/// The Library tab's views. Bar order and the initial view follow library.py
/// (`_visible_view_modes`, `__init__`).
enum LibraryView {
  playlists('Playlists'),
  genres('Genres'),
  artists('Artists'),
  albums('Albums'),
  tracks('Tracks');

  const LibraryView(this.label);
  final String label;

  /// The ported views among config's [visible] ones (library.py mode
  /// names), in bar order; Tracks when none is left.
  static List<LibraryView> fromConfig(List<String> visible) {
    final out = [
      for (final m in visible)
        for (final v in values)
          if (v.name == m) v,
    ];
    return out.isEmpty ? const [LibraryView.tracks] : out;
  }

  /// library.py's starting view: the first shown of tracks, albums, artists,
  /// genres, playlists.
  static LibraryView initial(List<LibraryView> shown) => [
    tracks,
    albums,
    artists,
    genres,
    playlists,
  ].firstWhere(shown.contains, orElse: () => shown.first);
}

/// Artists tab sort (library.py `_open_sort_menu`).
enum ArtistSort {
  name('Artist (A–Z)'),
  tracks('Most Tracks'),
  albums('Most Albums');

  const ArtistSort(this.label);
  final String label;
}

/// Genres tab sort.
enum GenreSort {
  name('Genre (A–Z)'),
  tracks('Most Tracks'),
  albums('Most Albums'),
  artists('Most Artists');

  const GenreSort(this.label);
  final String label;
}

/// Playlists tab sort (db_manager `get_all_playlists`). Flet coerces the
/// shared sort to "date" on entering the tab unless it is already "name" or
/// "date", which makes Date Created the usual start.
enum PlaylistSort {
  name('Name (A–Z)'),
  date('Date Created');

  const PlaylistSort(this.label);
  final String label;
}

/// One row of the Genres / Artists / Albums / Playlists tree (library.py's flat list
/// with `depth`): a genre opens onto its artists, an artist onto its albums,
/// an album onto its tracks.
sealed class BrowseNode {
  const BrowseNode(this.key, this.depth);

  /// Unique within the tree; a child's key extends its parent's with '/'.
  final String key;
  final int depth;
}

final class GenreNode extends BrowseNode {
  const GenreNode(super.key, super.depth, this.genre);
  final GenreRow genre;
}

final class ArtistNode extends BrowseNode {
  const ArtistNode(super.key, super.depth, this.artist, {this.genre});
  final ArtistRow artist;

  /// Set under a genre: only the artist's albums in that genre open.
  final String? genre;
}

final class AlbumNode extends BrowseNode {
  const AlbumNode(super.key, super.depth, this.album);
  final AlbumRow album;
}

final class TrackNode extends BrowseNode {
  const TrackNode(
    super.key,
    super.depth,
    this.track,
    this.context, {
    this.playlistId,
  });
  final Track track;

  /// The album or playlist the row sits in: tapping plays it from this track
  /// (`source=("album", …)` / `("playlist", id)`).
  final List<Track> context;

  /// Set under a playlist: the row has the drag handle and the remove
  /// button, and the subtitle keeps the album.
  final int? playlistId;
}

final class PlaylistNode extends BrowseNode {
  const PlaylistNode(super.key, super.depth, this.playlist);
  final PlaylistRow playlist;
}

/// library.py `_empty_playlist_widget`: "Playlist is empty".
final class EmptyPlaylistNode extends BrowseNode {
  const EmptyPlaylistNode(super.key, super.depth);
}

/// library.py `_new_playlist_row`, last in the Playlists tab (not while
/// searching).
final class NewPlaylistNode extends BrowseNode {
  const NewPlaylistNode() : super('new-playlist', 0);
}

/// State for the Albums, Artists and Genres views. The Tracks view stays in
/// [LibraryController], which also owns the shared search box: this listens
/// to it.
///
/// Rows come from db_manager's queries in their SQL order (see
/// browse_queries.dart). Search runs in memory, with the Tracks view's
/// semantics: every word, accent-blind; an album or artist also matches
/// through its tracks, as Flet's FTS subquery does; then the same
/// closest-match fallback.
class BrowseController extends ChangeNotifier {
  BrowseController(this.library, {required this.views})
    : view = LibraryView.initial(views) {
    library.addListener(_onLibrary);
    _onLibrary();
  }

  final LibraryController library;

  /// The views the bar shows.
  List<LibraryView> views;
  LibraryView view;

  /// Settings → Appearance changed which views show (Flet rebuilds the UI,
  /// `restart_ui`). The open view stays when it is still shown.
  void setViews(List<LibraryView> shown) {
    if (listEquals(shown, views)) return;
    views = shown;
    if (views.contains(view)) {
      notifyListeners();
    } else {
      view = LibraryView.initial(views);
      _reset();
    }
  }

  ArtistSort artistSort = ArtistSort.name;
  GenreSort genreSort = GenreSort.name;
  PlaylistSort playlistSort = PlaylistSort.date;

  /// Top-level rows after the search, then the expanded children: what the
  /// list shows. Empty in the Tracks view.
  List<BrowseNode> rows = const [];

  /// Top-level rows shown, and in all.
  int shown = 0;
  int total = 0;
  bool closestMatch = false;
  bool loading = false;
  String? error;

  // Top-level lists by sort mode, each row with its folded search key.
  final _albums = <String, List<(AlbumRow, String)>>{};
  final _artists = <String, List<(ArtistRow, String)>>{};
  final _genres = <String, List<(GenreRow, String)>>{};
  final _playlists = <String, List<(PlaylistRow, String)>>{};

  List<BrowseNode> _top = const [];
  final Set<String> _expanded = {};
  final Map<String, List<BrowseNode>> _children = {};
  final Set<String> _toggling = {};

  /// Bumped whenever the visible tree is rebuilt from scratch (view, sort or
  /// search changed), so a late expansion doesn't land in the new tree.
  int _generation = 0;
  String? _query;
  LibrarySort? _trackSort;
  bool _opened = false;
  int _dataGeneration = 0;

  bool isExpanded(BrowseNode n) => _expanded.contains(n.key);

  void setView(LibraryView v) {
    if (v == view) return;
    view = v;
    _reset();
  }

  void setArtistSort(ArtistSort s) {
    if (s == artistSort) return;
    artistSort = s;
    _reset();
  }

  void setGenreSort(GenreSort s) {
    if (s == genreSort) return;
    genreSort = s;
    _reset();
  }

  void setPlaylistSort(PlaylistSort s) {
    if (s == playlistSort) return;
    playlistSort = s;
    _reset();
  }

  /// library.py `_toggle_node`: open a row onto its children (loaded once),
  /// or close it and everything under it.
  Future<void> toggle(BrowseNode n) async {
    if (n is TrackNode || n is EmptyPlaylistNode || n is NewPlaylistNode) {
      return;
    }
    if (_expanded.remove(n.key)) {
      _expanded.removeWhere((k) => k.startsWith('${n.key}/'));
      _flatten();
      notifyListeners();
      return;
    }
    if (!_toggling.add(n.key)) return;
    final gen = _generation;
    try {
      _children[n.key] ??= await _load(n);
      if (gen != _generation) return;
      _expanded.add(n.key);
      _flatten();
      notifyListeners();
    } catch (e) {
      debugPrint('library: could not open ${n.key}: $e');
    } finally {
      _toggling.remove(n.key);
    }
  }

  /// An album's tracks in disc order (`get_tracks_by_album`).
  Future<List<Track>> albumTracks(AlbumRow a) async =>
      await library.repository?.tracksByAlbum(a.title, a.artist) ?? const [];

  /// A genre's tracks, newest first (`get_tracks_by_genre`): what its play
  /// button queues.
  Future<List<Track>> genreTracks(String genre) async =>
      await library.repository?.tracksByGenre(genre) ?? const [];

  // ── internals ───────────────────────────────────────────────────────────

  void _onLibrary() {
    var changed = false;
    if (library.query != _query) {
      _query = library.query;
      changed = true; // Flet collapses everything on every keystroke.
    }
    // Albums share the Tracks view's sort, as in Flet.
    if (library.sort != _trackSort) {
      _trackSort = library.sort;
      if (view == LibraryView.albums) changed = true;
    }
    if (library.repository != null && !_opened) {
      _opened = true;
      changed = true;
    }
    // A scan changed the rows: what was cached is stale.
    if (library.dataGeneration != _dataGeneration) {
      _dataGeneration = library.dataGeneration;
      _albums.clear();
      _artists.clear();
      _genres.clear();
      _playlists.clear();
      _children.clear();
      changed = true;
    }
    if (changed) _reset();
  }

  void _reset() {
    _generation++;
    _expanded.clear();
    if (view == LibraryView.tracks) {
      rows = const [];
      notifyListeners();
      return;
    }
    _refresh(_generation);
  }

  Future<void> _refresh(int gen) async {
    final repo = library.repository;
    if (repo == null || _disposed) return;
    loading = true;
    try {
      switch (view) {
        case LibraryView.albums:
          final sort = library.sort.name;
          final list = _albums[sort] ??= [
            for (final a in await repo.allAlbums(sort))
              (a, fold('${a.title ?? ''}\u0000${a.artist ?? ''}')),
          ];
          if (gen != _generation) return;
          _top = _filter(
            list,
            matchedIds: (w) => library.idsWithMatchingTracks(w),
            id: (a) => a.id,
            fields: (a) => [a.title ?? '', a.artist ?? ''],
            node: (a) => AlbumNode('al\u0001${a.id}', 0, a),
          );
        case LibraryView.artists:
          final sort = artistSort.name;
          final list = _artists[sort] ??= [
            for (final a in await repo.allArtists(sort))
              (a, fold(a.name ?? '')),
          ];
          if (gen != _generation) return;
          _top = _filter(
            list,
            matchedIds: (w) => library.idsWithMatchingTracks(w, byArtist: true),
            id: (a) => a.id,
            fields: (a) => [a.name ?? ''],
            node: (a) => ArtistNode('a\u0001${a.id}', 0, a),
          );
        case LibraryView.genres:
          final sort = genreSort.name;
          final list = _genres[sort] ??= [
            for (final g in await repo.allGenres(sort)) (g, fold(g.name)),
          ];
          if (gen != _generation) return;
          _top = _filter(
            list,
            fields: (g) => [g.name],
            node: (g) => GenreNode('g\u0001${g.name}', 0, g),
          );
        case LibraryView.playlists:
          final sort = playlistSort.name;
          final list = _playlists[sort] ??= [
            for (final p in await repo.allPlaylists(sort))
              (p, fold(p.name ?? '')),
          ];
          if (gen != _generation) return;
          _top = [
            ..._filter(
              list,
              fields: (p) => [p.name ?? ''],
              node: (p) => PlaylistNode('p\u0001${p.id}', 0, p),
            ),
            if (queryWords(library.query).isEmpty) const NewPlaylistNode(),
          ];
        case LibraryView.tracks:
          return;
      }
      // Rows left open across a reload (a playlist edited) open again.
      for (final n in _top) {
        if (_expanded.contains(n.key) && !_children.containsKey(n.key)) {
          _children[n.key] = await _load(n);
          if (gen != _generation) return;
        }
      }
      error = null;
    } catch (e) {
      if (gen != _generation) return;
      error = '$e';
      _top = const [];
      total = 0;
    } finally {
      if (gen == _generation) loading = false;
    }
    _flatten();
    notifyListeners();
  }

  List<BrowseNode> _filter<T>(
    List<(T, String)> list, {
    Set<int> Function(List<String> words)? matchedIds,
    int Function(T)? id,
    required List<String> Function(T) fields,
    required BrowseNode Function(T) node,
  }) {
    total = list.length;
    final (hits, closest) = searchRows(
      list,
      library.query,
      fields: fields,
      matchedIds: matchedIds,
      id: id,
    );
    closestMatch = closest;
    shown = hits.length;
    return [for (final r in hits) node(r)];
  }

  Future<List<BrowseNode>> _load(BrowseNode n) async {
    final repo = library.repository;
    if (repo == null) return const [];
    final d = n.depth + 1;
    switch (n) {
      case GenreNode(:final genre):
        return [
          for (final a in await repo.artistsByGenre(genre.name))
            ArtistNode('${n.key}/a\u0001${a.id}', d, a, genre: genre.name),
        ];
      case ArtistNode(:final artist, :final genre):
        return [
          for (final a in await repo.albumsByArtist(artist.name, genre: genre))
            AlbumNode('${n.key}/al\u0001${a.id}', d, a),
        ];
      case AlbumNode(:final album):
        final tracks = await albumTracks(album);
        return [
          for (final t in tracks)
            TrackNode('${n.key}/t\u0001${t.id}', d, t, tracks),
        ];
      case PlaylistNode(:final playlist):
        final tracks = await repo.tracksInPlaylist(playlist.id);
        if (tracks.isEmpty) return [EmptyPlaylistNode('${n.key}/empty', d)];
        return [
          for (final t in tracks)
            TrackNode(
              '${n.key}/t\u0001${t.path}',
              d,
              t,
              tracks,
              playlistId: playlist.id,
            ),
        ];
      case TrackNode() || EmptyPlaylistNode() || NewPlaylistNode():
        return const [];
    }
  }

  void _flatten() {
    final out = <BrowseNode>[];
    void add(BrowseNode n) {
      out.add(n);
      if (!_expanded.contains(n.key)) return;
      for (final c in _children[n.key] ?? const <BrowseNode>[]) {
        add(c);
      }
    }

    for (final n in _top) {
      add(n);
    }
    rows = out;
  }

  // ── playlist writes ──────────────────────────────────────────────────────
  // Each returns null when it worked, else the error text (SQLite's message,
  // as Python's str(exc)) for the screen's snackbar.

  /// The playlists, by name, for the Add to Playlist sheet.
  Future<List<PlaylistRow>> playlistsByName() async =>
      await library.repository?.allPlaylists('name') ?? const [];

  /// `create_playlist` (+ `add_track_to_playlist` for [paths]). Returns the
  /// new id, or the error.
  Future<(int?, String?)> createPlaylist(
    String name, {
    List<String> paths = const [],
  }) async {
    final repo = library.repository;
    if (repo == null) return (null, 'The library is not open.');
    try {
      final id = await repo.createPlaylist(name);
      if (paths.isNotEmpty) await repo.addTracksToPlaylist(id, paths);
      await _playlistsChanged();
      return (id, null);
    } catch (e) {
      return (null, _err(e));
    }
  }

  Future<String?> addToPlaylist(int id, List<String> paths) =>
      _playlistWrite((r) => r.addTracksToPlaylist(id, paths), reopen: id);

  Future<String?> updatePlaylist(int id, {String? name, String? color}) =>
      _playlistWrite((r) => r.updatePlaylist(id, name: name, color: color));

  Future<String?> deletePlaylist(int id) =>
      _playlistWrite((r) => r.deletePlaylist(id));

  /// `_remove_playlist_track_in_place`: the row goes at once ("Playlist is
  /// empty" if it was the last), then the write.
  Future<String?> removeFromPlaylist(int id, String path) {
    final key = 'p\u0001$id';
    final kids = _children[key];
    if (kids != null) {
      final left = [
        for (final c in kids)
          if (!(c is TrackNode && c.track.path == path)) c,
      ];
      _children[key] = left.whereType<TrackNode>().isEmpty
          ? [EmptyPlaylistNode('$key/empty', 1)]
          : _withContext(left);
      _flatten();
      notifyListeners();
    }
    return _playlistWrite(
      (r) => r.removeTrackFromPlaylist(id, path),
      keepChildren: id,
    );
  }

  /// `_move_playlist_track_to_target`: [from] and [to] are positions among
  /// the playlist's rows as shown, [to] counted after [from] is taken out.
  /// The list moves at once, then the write.
  Future<String?> movePlaylistTrack(int id, int from, int to) {
    final key = 'p\u0001$id';
    final kids = _children[key];
    if (kids == null || from == to) return Future.value();
    final rows = kids.whereType<TrackNode>().toList();
    if (from < 0 || from >= rows.length) return Future.value();
    final moved = rows.removeAt(from);
    rows.insert(to.clamp(0, rows.length), moved);
    _children[key] = _withContext(rows);
    _flatten();
    notifyListeners();
    return _playlistWrite(
      (r) => r.movePlaylistTrack(id, from, to),
      keepChildren: id,
    );
  }

  /// A playlist's track rows with the play context matching their order.
  List<BrowseNode> _withContext(List<BrowseNode> rows) {
    final tracks = [for (final r in rows.whereType<TrackNode>()) r.track];
    return [
      for (final r in rows)
        r is TrackNode
            ? TrackNode(
                r.key,
                r.depth,
                r.track,
                tracks,
                playlistId: r.playlistId,
              )
            : r,
    ];
  }

  Future<String?> _playlistWrite(
    Future<void> Function(LibraryRepository) write, {
    int? reopen,
    int? keepChildren,
  }) async {
    final repo = library.repository;
    if (repo == null) return 'The library is not open.';
    try {
      await write(repo);
    } catch (e) {
      // The list may be ahead of the database now: reload it.
      await _playlistsChanged();
      return _err(e);
    }
    await _playlistsChanged(keepChildren: keepChildren, reopen: reopen);
    return null;
  }

  /// Playlist rows changed: the listing (counts, names, colours) is reloaded
  /// in the Playlists tab, keeping what is open. [keepChildren]'s rows are
  /// already right (an in-place edit); [reopen]'s are fetched again.
  Future<void> _playlistsChanged({int? keepChildren, int? reopen}) async {
    _playlists.clear();
    _children.removeWhere(
      (k, _) =>
          k.startsWith('p\u0001') &&
          (keepChildren == null || k != 'p\u0001$keepChildren'),
    );
    if (reopen != null) _children.remove('p\u0001$reopen');
    if (view != LibraryView.playlists) return;
    await _refresh(++_generation);
  }

  static String _err(Object e) => e is DbWorkerException ? e.message : '$e';

  bool _disposed = false;

  @override
  void dispose() {
    library.removeListener(_onLibrary);
    // A query still in flight finds its generation stale and drops out.
    _disposed = true;
    _generation++;
    super.dispose();
  }
}
