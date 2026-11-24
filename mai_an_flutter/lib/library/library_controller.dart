import 'package:flutter/foundation.dart';

import 'dart:async';
import 'dart:io';

import '../core/db_worker.dart';
import '../core/progress_notify.dart';
import '../data/library_repository.dart';
import '../data/play_ledger.dart';
import '../player/autoplay.dart';
import '../player/dsp.dart';
import '../scan/library_scanner.dart';
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
  LibraryController(
    this._open, {
    LibrarySort sort = LibrarySort.date,
    Future<String> Function()? libraryFolder,
  }) : _sort = sort,
       _libraryFolder = libraryFolder;

  final Future<LibraryRepository> Function() _open;
  LibraryRepository? _repo;

  /// The folder a scan indexes, resolved fresh for each scan.
  final Future<String> Function()? _libraryFolder;

  /// Bumped each time the rows are reloaded from the database (after a
  /// scan), so the other views drop what they cached.
  int dataGeneration = 0;

  /// One-off notices for the screen (scan results).
  Stream<String> get messages => _messages.stream;
  final _messages = StreamController<String>.broadcast();

  // library.py's scan banner: a progress bar (indeterminate while the
  // folder is walked) and a status line.
  bool scanning = false;
  double? scanProgress;
  String scanStatus = '';

  /// The database is the port's own and may be written (a scan, the
  /// ledger drain); false for a LIBRARY_DB override.
  bool get canScan => _libraryFolder != null && (_repo?.writable ?? false);

  /// The database is the port's own: playlists and deletes may write it.
  bool get canWrite => _repo?.writable ?? false;

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

  /// The search box as typed. One box serves every library view, as in Flet.
  String get query => _rawQuery;

  /// The open database, once loaded (the other library views query it).
  LibraryRepository? get repository => _repo;

  /// Albums (or, with [byArtist], artists) with a track matching every word:
  /// the Flet album and artist searches also match through their tracks
  /// (`fts_search`).
  Set<int> idsWithMatchingTracks(List<String> words, {bool byArtist = false}) {
    final out = <int>{};
    for (final (t, key) in _sorted) {
      final id = byArtist ? t.artistId : t.albumId;
      if (id != null && !out.contains(id) && words.every(key.contains)) {
        out.add(id);
      }
    }
    return out;
  }

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

  /// The station graphs once built (the latest, after a scan); null when
  /// they couldn't be.
  Future<StationSource?> stations() async {
    await _stations.future;
    return graph;
  }

  /// library.py `start_scan` (a scan the user asked for): index the library
  /// folder, then reload every view. `_on_scan_complete`'s snackbar says
  /// how it went.
  ///
  /// [quiet] is the scan nobody asked for, after downloads or Mac transfers
  /// land: no banner and no snackbar. When it indexed anything, the "not
  /// analysed yet" card follows (`_announce_unanalysed`).
  Future<void> scan({bool quiet = false}) async {
    final repo = _repo;
    final folder = _libraryFolder;
    if (_scanBusy || repo == null || folder == null || !repo.writable) return;
    _scanBusy = true;
    if (!quiet) {
      scanning = true;
      scanProgress = null;
      scanStatus = 'Initializing active scan…';
      notifyListeners();
    }
    int? count;
    try {
      final target = await folder();
      count = await LibraryScanner(
        targetFolder: target,
        repo: repo,
        onProgress: (percent, message) {
          if (quiet) return;
          if (percent == -1) {
            scanProgress = null;
            scanStatus = 'Searching: $message';
          } else {
            scanProgress = percent / 100;
            scanStatus = message;
          }
          notifyListeners();
        },
      ).run();
    } catch (e) {
      // Flet's error boundary reports it; the banner must not stick.
      debugPrint('library scan failed: $e');
      _messages.add('Scan failed: ${e is DbWorkerException ? e.message : e}');
    }
    _scanBusy = false;
    scanning = false;
    notifyListeners();
    await reload();
    if (quiet) {
      if ((count ?? 0) > 0) await _announceUnanalysed(repo);
    } else if (count != null) {
      _messages.add(
        count > 0
            ? 'Scan complete. Indexed $count items.'
            : 'Library is up to date.',
      );
    }
  }

  /// A scan is running, the user's or a quiet one (library.py
  /// `_is_scanning`).
  bool _scanBusy = false;

  /// library.py `_announce_unanalysed`: until new tracks are analysed,
  /// auto-play and the similar-track picks can't use them, and nothing
  /// outside the Library says so. Flet's card has an Analyse button (on-
  /// phone analysis); the port's has none until decision B.
  Future<void> _announceUnanalysed(LibraryRepository repo) async {
    final int n;
    try {
      n = (await repo.featureCoverage()).$2;
    } catch (e) {
      debugPrint('unanalysed-track count failed: $e');
      return;
    }
    if (n == 0) {
      cancelAlert('scan');
      return;
    }
    final many = n != 1;
    postAlert(
      'scan',
      'New music in your library',
      '$n track${many ? 's' : ''} ${many ? 'are' : 'is'} not analysed yet, '
          "so auto-play and similar-track picks can't use "
          '${many ? 'them' : 'it'}.',
      navigateTo: 'library',
      icon: 'dsp',
    );
  }

  /// main.py `_delete_track`, after the player has let go of [path]: the
  /// file, then its rows (playlist entries, track, emptied album and
  /// artist), then every view reloads. Returns the snackbar text.
  Future<String> deleteTrack(String path) async {
    final repo = _repo;
    if (repo == null || !repo.writable) return 'The library is read-only.';
    try {
      final f = File(path);
      if (await f.exists()) {
        await f.delete();
      } else {
        // Gone from disk already: the rows still go.
        debugPrint('delete: file not found on disk: $path');
      }
    } on FileSystemException catch (e) {
      final code = e.osError?.errorCode;
      if (code == 1 || code == 13) {
        // EPERM / EACCES: Python's PermissionError.
        return "Permission denied: Android requires 'Manage All Files' "
            'access to delete files.';
      }
      return 'Could not delete: ${e.osError?.message ?? e.message}';
    }
    try {
      await repo.deleteTracksByPaths([path]);
    } catch (e) {
      return 'File deleted but database update failed: '
          '${e is DbWorkerException ? e.message : e}';
    }
    await reload();
    return 'Track deleted permanently.';
  }

  /// `load_library` after a change on disk: the rows again, then the
  /// station graphs.
  Future<void> reload() async {
    final repo = _repo;
    if (repo == null) return;
    try {
      _resort(await repo.allTracks());
      state = LoadState.ready;
    } catch (e) {
      debugPrint('library reload failed: $e');
      return;
    }
    dataGeneration++;
    notifyListeners();
    // The station graphs follow; nothing waits for them (Flet's
    // load_library doesn't either).
    unawaited(_rebuildGraph(repo));
  }

  int _graphGen = 0;
  bool _disposed = false;

  Future<void> _rebuildGraph(LibraryRepository repo) async {
    final gen = ++_graphGen;
    StationSource? built;
    String? error;
    try {
      built = await repo.stations();
    } on DbWorkerException catch (e) {
      error = e.message;
    } catch (e) {
      error = '$e';
    }
    // A newer reload owns the result; a closed library has none.
    if (_disposed || gen != _graphGen) return;
    if (error == null) graph = built;
    graphError = error;
    notifyListeners();
  }

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
    _disposed = true;
    _messages.close();
    _repo?.close();
    super.dispose();
  }
}
