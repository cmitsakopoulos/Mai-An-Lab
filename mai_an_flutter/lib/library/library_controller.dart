import 'package:flutter/foundation.dart';

import 'dart:async';

import '../core/db_worker.dart';
import '../data/library_repository.dart';
import '../data/play_ledger.dart';
import '../player/autoplay.dart';
import '../player/dsp.dart';
import '../stations/station.dart';
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
class LibraryController extends ChangeNotifier implements AutoPlayLibrary {
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

  LibrarySort _sort;
  LibrarySort get sort => _sort;
  String _rawQuery = '';

  /// True when [visible] is the fuzzy fallback, not real matches.
  bool closestMatch = false;

  /// The stations' graphs, once built.
  StationSource? graph;
  final Completer<StationSource?> _stations = Completer();

  /// Set when the station graphs couldn't be built (e.g. an older database
  /// without `play_counts.pca_coords`). The library stays usable without it.
  String? graphError;

  /// Auto-play can start from some track: at least one has coordinates.
  /// (Flet's chain-button availability check: some track is analysed.)
  @override
  bool get stationsAvailable => (graph?.length ?? 0) > 0;

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
      _resort(tracks);
      state = LoadState.ready;
      loadTime = sw.elapsed;
      notifyListeners();
    } catch (e) {
      state = LoadState.error;
      error = '$e';
      if (!_stations.isCompleted) _stations.complete(null);
      notifyListeners();
      return;
    }
    // The list is usable already; the graph arrives a moment later, and a
    // failure here only disables auto-play.
    try {
      graph = await _repo!.stations();
    } on DbWorkerException catch (e) {
      graphError = e.message;
    } catch (e) {
      graphError = '$e';
    }
    if (!_stations.isCompleted) _stations.complete(graph);
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

  // ── AutoPlayLibrary ──────────────────────────────────────────────────────

  /// The station graphs once built; null when they couldn't be.
  Future<StationSource?> stations() => _stations.future;

  @override
  Future<StationHandle?> openStation(
    String seed, {
    required Set<String> avoid,
    required bool focused,
    required int maxPerArtist,
    required int maxPerAlbum,
    required int window,
  }) async => (await stations())?.open(
    seed,
    avoid: avoid,
    focused: focused,
    maxPerArtist: maxPerArtist,
    maxPerAlbum: maxPerAlbum,
    window: window,
  );

  @override
  Future<Map<String, Track>> tracksBrief(List<String> paths) async =>
      await _repo?.tracksBrief(paths) ?? const {};

  @override
  Future<bool> hasCurrentFeatures(String path) async =>
      await _repo?.hasCurrentFeatures(path) ?? false;

  @override
  Future<List<String>> recentPaths(int limit) async =>
      await _repo?.recentTrackPaths(limit) ?? const [];

  /// The play_counts audio features of [path] for the player's per-track
  /// sound settings; null when the track has none or the library isn't open.
  Future<DspFeatures?> dspFeatures(String path) async =>
      _repo?.dspFeatures(path);

  /// The ledger drain's write. Throws until the library is open (writable),
  /// which keeps the ledger file for the next drain.
  Future<int> recordListens(List<LedgerEntry> entries) {
    final repo = _repo;
    if (repo == null) throw StateError('library not open yet');
    return repo.recordListens(entries);
  }

  @override
  void dispose() {
    _repo?.close();
    super.dispose();
  }
}
