import 'package:flutter/foundation.dart';

import '../core/db_worker.dart';
import '../data/library_repository.dart';
import '../graph/coord_graph.dart';
import 'track_query.dart';

enum LoadState { loading, ready, error }

/// State for the Tracks tab. In Flet the view mutated controls and called
/// `update()`; here the controller owns the state, and widgets rebuild from it
/// through ListenableBuilder.
///
/// Behaviour follows the Flet tracks tab (ui/views/library.py +
/// db_manager.get_all_tracks): same sort modes and default, same search
/// fallback to a "closest match" list. Search runs in memory on every
/// keystroke, so the Flet 300 ms debounce isn't needed.
class LibraryController extends ChangeNotifier {
  LibraryController(this._open, {LibrarySort sort = LibrarySort.date})
    : _sort = sort;

  final Future<LibraryRepository> Function() _open;
  LibraryRepository? _repo;

  LoadState state = LoadState.loading;
  String? error;
  Duration? loadTime;

  /// Every track, in the current sort order, with its search key alongside.
  List<(Track, String)> _sorted = const [];
  List<Track> visible = const [];
  Map<String, Track> _byPath = const {};

  LibrarySort _sort;
  LibrarySort get sort => _sort;
  String _rawQuery = '';

  /// True when [visible] is the fuzzy fallback, not real matches.
  bool closestMatch = false;

  CoordGraph? graph;

  /// Set when the coordinate graph couldn't be built (e.g. an older database
  /// without `play_counts.pca_coords`). The library stays usable without it.
  String? graphError;

  int get total => _sorted.length;

  /// Every track in the current sort order, ignoring the search.
  List<Track> get allTracks => [for (final (t, _) in _sorted) t];

  /// The search box is empty: Flet's "general view", where tapping a track
  /// that is already queued jumps to it instead of rebuilding the queue.
  bool get isGeneralView => queryWords(_rawQuery).isEmpty;

  Future<void> load() async {
    final sw = Stopwatch()..start();
    try {
      final repo = _repo = await _open();
      final tracks = await repo.allTracks();
      _byPath = {for (final t in tracks) t.path: t};
      _resort(tracks);
      state = LoadState.ready;
      loadTime = sw.elapsed;
      notifyListeners();
    } catch (e) {
      state = LoadState.error;
      error = '$e';
      notifyListeners();
      return;
    }
    // The list is usable already; the graph arrives a moment later, and a
    // failure here only disables "similar tracks".
    try {
      graph = await _repo!.coordGraph();
    } on DbWorkerException catch (e) {
      graphError = e.message;
    } catch (e) {
      graphError = '$e';
    }
    notifyListeners();
  }

  void setSort(LibrarySort sort) {
    if (sort == _sort) return;
    _sort = sort;
    _resort([for (final (t, _) in _sorted) t]);
    notifyListeners();
  }

  void setQuery(String q) {
    if (q == _rawQuery) return;
    _rawQuery = q;
    _filter();
    notifyListeners();
  }

  void _resort(List<Track> tracks) {
    _sorted = [for (final t in sortTracks(tracks, _sort)) (t, searchKey(t))];
    _filter();
  }

  void _filter() {
    final words = queryWords(_rawQuery);
    closestMatch = false;
    if (words.isEmpty) {
      visible = [for (final (t, _) in _sorted) t];
      return;
    }
    visible = [
      for (final (t, key) in _sorted)
        if (words.every(key.contains)) t,
    ];
    if (visible.isEmpty) {
      visible = closestMatches([
        for (final (t, _) in _sorted) t,
      ], _rawQuery.trim());
      closestMatch = true;
    }
  }

  /// A cosine-nearest walk over the acoustic coordinates. NOT yet the Flet
  /// app's "Play Similar" station (track_graph.open_station + artist_graph
  /// Drift); see docs/PORT_MAP.md, phase 2.
  ///
  /// Null when the graph hasn't loaded or this track has no coordinates yet.
  List<Track>? similarTo(Track seed, {int length = 15}) {
    final g = graph;
    final idx = g?.indexOf[seed.path];
    if (g == null || idx == null) return null;
    return [for (final i in g.walk(idx, length: length)) ?_byPath[g.paths[i]]];
  }

  @override
  void dispose() {
    _repo?.close();
    super.dispose();
  }
}
