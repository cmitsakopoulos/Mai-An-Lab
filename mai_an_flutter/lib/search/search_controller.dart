import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../core/haptics.dart';
import '../data/library_repository.dart';
import '../player/playback_backend.dart';
import '../player/player_controller.dart';
import '../player/queue_entry.dart';
import '../player/queue_store.dart';
import '../settings/config_controller.dart';
import '../sources/source_config.dart';
import '../sources/streamrip_api.dart';
import '../sources/streamrip_search.dart';
import 'local_search.dart';
import 'search_history.dart';

/// Port of `StreamripApp/ui/views/search.py` state & orchestration:
/// Qobuz/Deezer source switching, per-source result caching, unified All view
/// hero picking, 35-item pagination, expandable artist/album tree nodes,
/// multi-select batch state, and stream/download audio previews.

const kProgressSearching = 'Searching\u2026';
const kProgressContacting = 'Contacting API\u2026';
const kProgressConnecting = 'Connecting\u2026';

const kSearchItemsPerPage = 35;

const kMissingCredsHint = <String, String>{
  'qobuz': 'Qobuz credentials not set. Add your User ID and token in Settings.',
  'deezer': 'Deezer ARL cookie not set. Add it in Settings.',
};

/// `search.py` `_configured_sources`: sources with usable credentials, in pill
/// order.
List<String> configuredSourcesFor(SourceConfig cfg) {
  final out = <String>[];
  if (cfg.qobuz.emailOrUserid.trim().isNotEmpty &&
      cfg.qobuz.passwordOrToken.trim().isNotEmpty) {
    out.add('qobuz');
  }
  if (cfg.deezer.arl.trim().isNotEmpty) {
    out.add('deezer');
  }
  return out;
}

/// Human-readable reason [source] can't be searched yet, or null when ready.
String? missingCredentialsFor(String source, SourceConfig cfg) {
  final src = source.toLowerCase();
  if (configuredSourcesFor(cfg).contains(src)) return null;
  return kMissingCredsHint[src] ??
      '${src.isEmpty ? 'Source' : '${src[0].toUpperCase()}${src.substring(1)}'} is not configured.';
}

/// `search.py` `_pick_hero_result`: scores exact, prefix, word-boundary, and
/// substring matches across artists, tracks, and albums to pick the unified
/// `"All"` view's top hero card.
Map<String, Object?>? pickHeroResult(
  String query, {
  required List<Map<String, Object?>> tracks,
  required List<Map<String, Object?>> albums,
  required List<Map<String, Object?>> artists,
}) {
  final q = query.trim().toLowerCase();
  if (q.isEmpty) return null;

  String getName(Map<String, Object?> item) =>
      '${item['ui_title'] ?? item['name'] ?? item['title'] ?? ''}'
          .trim()
          .toLowerCase();

  final candidates = <(double, int, Map<String, Object?>)>[];
  var seq = 0;

  // 1. Exact matches
  for (final art in artists) {
    final name = '${art['name'] ?? ''}'.trim().toLowerCase();
    if (name.isNotEmpty && name == q) {
      candidates.add((100.5, seq++, art));
    }
  }
  for (final trk in tracks) {
    final name = getName(trk);
    if (name.isNotEmpty && name == q) {
      candidates.add((100.2, seq++, trk));
    }
  }
  for (final alb in albums) {
    final name = getName(alb);
    if (name.isNotEmpty && name == q) {
      candidates.add((100.1, seq++, alb));
    }
  }

  // 2. Prefix & token matches
  for (final trk in tracks) {
    final name = getName(trk);
    if (name.isEmpty || name == q) continue;
    if (name.startsWith(q)) {
      candidates.add((75.0 + 15.0 * (q.length / name.length), seq++, trk));
    } else if (' $name '.contains(' $q ')) {
      candidates.add((60.0, seq++, trk));
    } else if (name.contains(q)) {
      candidates.add((45.0, seq++, trk));
    }
  }
  for (final alb in albums) {
    final name = getName(alb);
    if (name.isEmpty || name == q) continue;
    if (name.startsWith(q)) {
      candidates.add((74.0 + 15.0 * (q.length / name.length), seq++, alb));
    } else if (' $name '.contains(' $q ')) {
      candidates.add((59.0, seq++, alb));
    } else if (name.contains(q)) {
      candidates.add((44.0, seq++, alb));
    }
  }
  for (final art in artists) {
    final name = '${art['name'] ?? ''}'.trim().toLowerCase();
    if (name.isEmpty || name == q) continue;
    if (name.startsWith(q)) {
      candidates.add((74.5 + 15.0 * (q.length / name.length), seq++, art));
    } else if (' $name '.contains(' $q ')) {
      candidates.add((58.0, seq++, art));
    } else if (name.contains(q)) {
      candidates.add((43.0, seq++, art));
    }
  }

  if (candidates.isNotEmpty) {
    // Python's sort(key=lambda c: c[0], reverse=True) is stable.
    candidates.sort((a, b) {
      final cmp = b.$1.compareTo(a.$1);
      return cmp != 0 ? cmp : a.$2.compareTo(b.$2);
    });
    return candidates.first.$3;
  }

  if (tracks.isNotEmpty) return tracks.first;
  if (albums.isNotEmpty) return albums.first;
  if (artists.isNotEmpty) return artists.first;
  return null;
}

/// Flattened visible tree row (used by typed Artists/Albums/Tracks pages).
class SearchTreeRow {
  const SearchTreeRow({required this.item, required this.depth});

  final Map<String, Object?> item;
  final int depth;
}

/// Signal bar state (`ConnectionSignal` in `search.py`).
class SignalState {
  const SignalState({
    this.visible = false,
    this.level = 0,
    this.connected = false,
  });

  final bool visible;
  final int level;
  final bool connected;
}

/// Download preview callback signature (overridable in tests).
typedef PreviewDownloader =
    Future<List<String>> Function({
      required String url,
      required String downloadDir,
      required SourceConfig config,
      required SourceDownloadProgress progressCallback,
      required bool Function() isCancelled,
    });

class SearchTabController extends ChangeNotifier {
  SearchTabController({
    required this.repository,
    this.searcher,
    this.config,
    this.prefs,
    this.searchHistory,
    this.player,
    this.haptics,
    this.dataDir,
    this.onSnack,
    this.previewDownloader,
    this.previewStreamTimeout = const Duration(seconds: 6),
  }) {
    final saved = prefs?['search_source'];
    if (saved is String && (saved == 'qobuz' || saved == 'deezer')) {
      _selectedSource = saved;
    }
    _syncSourceToConfig();
    config?.addListener(_onConfigChanged);
    player?.addListener(_bindPlayer);
    _bindPlayer();
  }

  final LibraryRepository? Function() repository;
  final StreamripSearcher? searcher;
  final ConfigController? config;
  final Prefs? prefs;
  final SearchHistoryStore? searchHistory;
  final ValueListenable<PlayerController?>? player;
  final Haptics? haptics;
  final String? dataDir;
  final void Function(String message)? onSnack;
  final PreviewDownloader? previewDownloader;
  final Duration previewStreamTimeout;

  PlayerController? _boundPlayer;
  bool _disposed = false;

  // ── Source & search state ────────────────────────────────────────────────

  String _selectedSource = 'qobuz';
  String get selectedSource => _selectedSource;
  String get sourceLabel =>
      '${_selectedSource[0].toUpperCase()}${_selectedSource.substring(1)}';

  SourceConfig get sourceConfig =>
      config?.config.sources ?? const SourceConfig();

  List<String> get configuredSources => configuredSourcesFor(sourceConfig);

  /// True when remote search is wired AND at least one source has credentials.
  bool get hasAnyCredentials => configuredSources.isNotEmpty;

  /// True when remote search is enabled (searcher provided).
  bool get remoteEnabled => searcher != null;

  int _searchId = 0;
  String _activeQuery = '';
  String get activeQuery => _activeQuery;
  bool get hasActiveQuery => _activeQuery.isNotEmpty;

  SearchViewMode _viewMode = SearchViewMode.all;
  SearchViewMode get viewMode => _viewMode;

  /// Per-source result cache: `source -> {'track': [...], 'album': [...], 'artist': [...]}`.
  final Map<String, Map<String, List<Map<String, Object?>>>> _sourceCache = {};
  final Map<String, String> _cacheQuery = {};

  Map<String, List<Map<String, Object?>>> get cachedResults =>
      _sourceCache.putIfAbsent(
        _selectedSource,
        () => {'track': [], 'album': [], 'artist': []},
      );

  List<Map<String, Object?>> get tracks => cachedResults['track']!;
  List<Map<String, Object?>> get albums => cachedResults['album']!;
  List<Map<String, Object?>> get artists => cachedResults['artist']!;

  bool get hasRemoteResults =>
      tracks.isNotEmpty || albums.isNotEmpty || artists.isNotEmpty;

  bool _cacheIsFresh(String source, String query) {
    if (_cacheQuery[source] != query) return false;
    final b = _sourceCache[source];
    if (b == null) return false;
    return b.values.any((list) => list.isNotEmpty);
  }

  // ── Local matches ────────────────────────────────────────────────────────

  LocalMatches _localMatches = LocalMatches.empty;
  LocalMatches get localMatches => _localMatches;

  bool _localExpanded = false;
  bool get localExpanded => _localExpanded;

  bool _searchingLocal = false;
  bool get searchingLocal => _searchingLocal;

  // ── Remote progress & error ──────────────────────────────────────────────

  bool _remotePending = false;
  bool get remotePending => _remotePending;

  String? _remoteError;
  String? get remoteError => _remoteError;

  bool _searchProgressVisible = false;
  bool get searchProgressVisible => _searchProgressVisible;
  String _searchProgressStatus = kProgressSearching;
  String get searchProgressStatus => _searchProgressStatus;
  String _searchProgressDetail = kProgressContacting;
  String get searchProgressDetail => _searchProgressDetail;
  SignalState _signal = const SignalState();
  SignalState get signal => _signal;

  // ── Pagination & tree expansion ──────────────────────────────────────────

  int _currentPage = 0;
  int get currentPage => _currentPage;

  int get totalPages {
    if (_viewMode == SearchViewMode.all) return 1;
    final len = _activeTypeList().length;
    return len == 0
        ? 1
        : (len + kSearchItemsPerPage - 1) ~/ kSearchItemsPerPage;
  }

  final Set<String> _expandedNodes = {};
  final Set<String> _loadingNodes = {};
  final Map<String, List<Map<String, Object?>>> _nodeCache = {};
  final Set<String> _loadingMoreKeys = {};

  bool isNodeExpanded(Map<String, Object?> item) =>
      _expandedNodes.contains(_nodeId(item));

  bool isNodeLoading(Map<String, Object?> item) =>
      _loadingNodes.contains(_nodeId(item));

  bool isLoadingMore(Map<String, Object?> sentinel) =>
      _loadingMoreKeys.contains('${sentinel['id']}:${sentinel['offset']}');

  static String _nodeId(Map<String, Object?> item) =>
      '${item['media_type'] ?? 'track'}_${item['id'] ?? ''}';

  List<Map<String, Object?>> _activeTypeList() => switch (_viewMode) {
    SearchViewMode.tracks => tracks,
    SearchViewMode.albums => albums,
    SearchViewMode.artists => artists,
    SearchViewMode.all => const [],
  };

  /// Flattened page rows for typed modes (`artists`, `albums`, `tracks`),
  /// including any inline expanded children.
  List<SearchTreeRow> currentPageRows() {
    final list = _activeTypeList();
    if (list.isEmpty) return const [];
    final safePage = _currentPage.clamp(0, totalPages - 1);
    final start = safePage * kSearchItemsPerPage;
    final end = (start + kSearchItemsPerPage).clamp(start, list.length);
    final out = <SearchTreeRow>[];

    void appendWithChildren(Map<String, Object?> item, int depth) {
      out.add(SearchTreeRow(item: item, depth: depth));
      final mType = '${item['media_type'] ?? ''}';
      if (mType != 'artist' && mType != 'album') return;
      final nid = _nodeId(item);
      if (!_expandedNodes.contains(nid)) return;
      final children = _nodeCache[nid];
      if (children == null) return;
      for (final child in children) {
        appendWithChildren(child, depth + 1);
      }
    }

    for (var i = start; i < end; i++) {
      appendWithChildren(list[i], 0);
    }
    return out;
  }

  /// Hero result for unified `"All"` mode.
  Map<String, Object?>? get heroResult => pickHeroResult(
    _activeQuery,
    tracks: tracks,
    albums: albums,
    artists: artists,
  );

  // ── Multi-select ─────────────────────────────────────────────────────────

  bool _selectionMode = false;
  bool get selectionMode => _selectionMode;

  final Set<String> _selectedKeys = {};
  final Map<String, Map<String, Object?>> _selectedRecords = {};

  int get selectedCount => _selectedKeys.length;
  List<Map<String, Object?>> get selectedRecords =>
      List.unmodifiable(_selectedRecords.values);

  String resultKey(Map<String, Object?> r) =>
      '${r['source'] ?? _selectedSource}:${r['media_type'] ?? 'track'}:${r['id'] ?? ''}';

  static bool isSelectable(Map<String, Object?> r) {
    final t = '${r['media_type'] ?? ''}';
    return t == 'track' || t == 'album';
  }

  bool isSelected(Map<String, Object?> r) =>
      _selectedKeys.contains(resultKey(r));

  void enterSelection([Map<String, Object?>? r]) {
    if (r != null && !isSelectable(r)) return;
    if (!_selectionMode) {
      _selectionMode = true;
      haptics?.trigger('long_press');
    }
    if (r != null) {
      _toggleSelectionInternal(r);
    }
    _notify();
  }

  void exitSelection() {
    if (!_selectionMode && _selectedKeys.isEmpty) return;
    _selectionMode = false;
    _selectedKeys.clear();
    _selectedRecords.clear();
    _notify();
  }

  void toggleSelection(Map<String, Object?> r) {
    if (!isSelectable(r)) return;
    _toggleSelectionInternal(r);
    haptics?.trigger('selection');
    _notify();
  }

  void _toggleSelectionInternal(Map<String, Object?> r) {
    final k = resultKey(r);
    if (_selectedKeys.contains(k)) {
      _selectedKeys.remove(k);
      _selectedRecords.remove(k);
    } else {
      _selectedKeys.add(k);
      _selectedRecords[k] = r;
    }
  }

  /// Select every selectable item on the current page (or visible Top Songs in
  /// All mode).
  void selectAllOnPage() {
    final visibleItems = _viewMode == SearchViewMode.all
        ? tracks.take(4).toList()
        : [for (final row in currentPageRows()) row.item];
    var added = 0;
    for (final r in visibleItems) {
      if (!isSelectable(r)) continue;
      final k = resultKey(r);
      if (_selectedKeys.add(k)) {
        _selectedRecords[k] = r;
        added++;
      }
    }
    if (added > 0) haptics?.trigger('selection');
    _notify();
  }

  // ── Stream previews ──────────────────────────────────────────────────────

  String? _activePreviewKey;
  String _activePreviewState = 'idle'; // 'idle', 'loading', 'playing'
  bool _previewCardVisible = false;
  String _previewStatus = 'Loading Preview...';
  String _previewDetail = kProgressConnecting;
  int _previewGen = 0;
  bool _previewCancelled = false;

  bool get previewCardVisible => _previewCardVisible;
  String get previewStatus => _previewStatus;
  String get previewDetail => _previewDetail;

  String previewStateFor(Map<String, Object?> r) {
    if (_activePreviewKey == resultKey(r)) return _activePreviewState;
    return 'idle';
  }

  /// True when the audio player is currently playing this search track row.
  bool isRowPlaying(Map<String, Object?> r) {
    final pl = player?.value;
    if (pl == null || pl.stopped) return false;
    final cur = pl.nowPlaying;
    if (cur == null) return false;
    final title = stripMarkup('${r['ui_title'] ?? r['name'] ?? 'Unknown'}');
    final artist = stripMarkup('${r['ui_subtitle'] ?? r['artist'] ?? ''}');
    return (cur.title == title || cur.title == '(Preview) $title') &&
        cur.artist == artist;
  }

  // ── Actions ──────────────────────────────────────────────────────────────

  void _onConfigChanged() {
    _syncSourceToConfig();
    _notify();
  }

  void _syncSourceToConfig() {
    final configured = configuredSources;
    if (configured.isNotEmpty && !configured.contains(_selectedSource)) {
      _selectedSource = configured.first;
      unawaited(prefs?.set('search_source', _selectedSource));
    }
  }

  void _bindPlayer() {
    _boundPlayer?.removeListener(_onPlayerChanged);
    _boundPlayer = player?.value;
    _boundPlayer?.addListener(_onPlayerChanged);
  }

  void _onPlayerChanged() {
    if (_activePreviewState == 'playing') {
      final pl = _boundPlayer;
      if (pl == null || pl.stopped) {
        _activePreviewState = 'idle';
        _activePreviewKey = null;
      }
    }
    _notify();
  }

  void setViewMode(SearchViewMode mode) {
    if (_viewMode == mode) return;
    _viewMode = mode;
    _expandedNodes.clear();
    _currentPage = 0;
    _notify();
  }

  void setPage(int page) {
    final clamped = page.clamp(0, totalPages - 1);
    if (clamped == _currentPage) return;
    _currentPage = clamped;
    _expandedNodes.clear();
    _notify();
  }

  void toggleLocalExpanded() {
    _localExpanded = !_localExpanded;
    _notify();
  }

  /// `search.py` `_set_source`: switches Qobuz <-> Deezer, preserving per-source
  /// caches so switching back to the same query is instant.
  Future<void> setSource(String source) async {
    final src = source.toLowerCase();
    if (src != 'qobuz' && src != 'deezer') return;
    if (src == _selectedSource) return;
    _selectedSource = src;
    unawaited(prefs?.set('search_source', src));

    exitSelection();
    _searchId++;
    _expandedNodes.clear();
    _loadingNodes.clear();
    _nodeCache.clear();
    _activePreviewKey = null;
    _activePreviewState = 'idle';
    _remoteError = null;
    _remotePending = false;
    _searchProgressVisible = false;

    if (_activeQuery.isEmpty) {
      _notify();
      return;
    }

    if (_cacheIsFresh(src, _activeQuery)) {
      _currentPage = 0;
      _notify();
      return;
    }

    await search(_activeQuery);
  }

  /// Entry point from Library → Long-press → Redownload.
  Future<void> startRedownloadSearch(String query) =>
      search(query, mode: SearchViewMode.tracks);

  void clearSearch() {
    _searchId++;
    exitSelection();
    _activeQuery = '';
    _localMatches = LocalMatches.empty;
    _localExpanded = false;
    _searchingLocal = false;
    _remotePending = false;
    _remoteError = null;
    _searchProgressVisible = false;
    _signal = const SignalState();
    _sourceCache.clear();
    _cacheQuery.clear();
    _expandedNodes.clear();
    _loadingNodes.clear();
    _nodeCache.clear();
    _currentPage = 0;
    _notify();
  }

  Future<void> search(String rawQuery, {SearchViewMode? mode}) async {
    final query = rawQuery.trim();
    if (query.isEmpty) {
      clearSearch();
      return;
    }

    final id = ++_searchId;
    exitSelection();
    _activeQuery = query;
    if (mode != null) _viewMode = mode;
    _localMatches = LocalMatches.empty;
    _localExpanded = searcher == null; // collapsed by default when remote runs
    _searchingLocal = true;
    _remoteError = null;
    _expandedNodes.clear();
    _loadingNodes.clear();
    _nodeCache.clear();
    _currentPage = 0;
    _activePreviewKey = null;
    _activePreviewState = 'idle';

    unawaited(searchHistory?.add(query));
    _cleanPreviewDir();

    final remote = searcher;
    String? missingHint;
    if (remote != null) {
      missingHint = missingCredentialsFor(_selectedSource, sourceConfig);
      if (missingHint != null) {
        _remotePending = false;
        _remoteError = missingHint;
        _searchProgressVisible = false;
        _signal = const SignalState();
        _sourceCache[_selectedSource] = {
          'track': [],
          'album': [],
          'artist': [],
        };
        onSnack?.call(missingHint);
      } else {
        _remotePending = true;
        _searchProgressVisible = true;
        _searchProgressStatus = kProgressSearching;
        _searchProgressDetail = kProgressConnecting;
        _signal = const SignalState(visible: true, level: 1);
      }
    } else {
      _remotePending = false;
      _searchProgressVisible = false;
    }
    _notify();

    // 1. Run local library search in parallel with remote search.
    final localFuture = _runLocalSearch(query, id);
    if (remote == null || missingHint != null) {
      await localFuture;
      if (!_disposed && _searchId == id && _localMatches.isNotEmpty) {
        _localExpanded = true;
        _notify();
      }
      return;
    }

    // 2. Run remote source search.
    final res = await remote.search(
      query,
      source: _selectedSource,
      mediaTypes: const ['track', 'album', 'artist'],
      limit: 250,
      offset: 0,
      progressCallback: (status, detail) {
        if (_disposed || _searchId != id) return;
        _updateSearchProgress(status, detail);
      },
    );

    // Wait for local search so final render has both halves.
    await localFuture;
    if (_disposed || _searchId != id) return;

    _remotePending = false;
    _searchProgressVisible = false;

    if (res.error != null) {
      _remoteError = res.error;
      _sourceCache[_selectedSource] = {'track': [], 'album': [], 'artist': []};
      _signal = const SignalState();
      if (_localMatches.isNotEmpty) _localExpanded = true;
      _notify();
      return;
    }

    _signal = const SignalState(visible: true, level: 4, connected: true);

    final items = [for (final r in res.items) Map<String, Object?>.of(r)];
    await _annotateAll(items);
    if (_disposed || _searchId != id) return;

    final buckets = <String, List<Map<String, Object?>>>{
      'track': [],
      'album': [],
      'artist': [],
    };
    for (final item in items) {
      final mType = '${item['media_type'] ?? 'track'}';
      buckets[mType]?.add(item);
    }
    _sourceCache[_selectedSource] = buckets;
    _cacheQuery[_selectedSource] = query;
    _currentPage = 0;
    _notify();
  }

  Future<void> _runLocalSearch(String query, int id) async {
    final repo = repository();
    if (repo == null) {
      if (!_disposed && _searchId == id) {
        _searchingLocal = false;
        _notify();
      }
      return;
    }
    try {
      final matches = await loadLocalMatches(repo, query);
      if (_disposed || _searchId != id) return;
      _localMatches = matches;
      _searchingLocal = false;
      _notify();
    } catch (e) {
      debugPrint('Library half of the search failed: $e');
      if (_disposed || _searchId != id) return;
      _searchingLocal = false;
      _notify();
    }
  }

  Future<void> _annotateAll(List<Map<String, Object?>> items) async {
    final repo = repository();
    if (repo == null || items.isEmpty) return;
    await Future.wait([for (final r in items) _annotateOne(repo, r)]);
  }

  Future<void> _annotateOne(
    LibraryRepository repo,
    Map<String, Object?> r,
  ) async {
    final mType = '${r['media_type'] ?? 'track'}';
    if (mType != 'track' && mType != 'album' && mType != 'artist') return;
    final info = await annotateOwnership(
      repo,
      mediaType: mType,
      title: '${r['ui_title'] ?? r['name'] ?? ''}',
      artist: '${r['ui_subtitle'] ?? r['artist'] ?? ''}',
      numTracks: (r['num_tracks'] as num?)?.toInt() ?? 0,
    );
    r['is_in_library'] = info.isInLibrary;
    r['owned_tracks'] = info.ownedTracks;
  }

  void _updateSearchProgress(String status, String detail) {
    var level = 1;
    if (status.contains('DNS')) {
      level = 1;
    } else if (status.contains('TCP')) {
      level = 2;
    } else if (status.contains('TLS') ||
        status.contains('SSL') ||
        status.contains('Handshake')) {
      level = 3;
    } else if (status.contains('HTTP') ||
        status.contains('Request') ||
        status.contains('Data') ||
        status.contains('Streaming') ||
        status.contains('Searching') ||
        status.contains('Processing')) {
      level = 4;
    }
    _searchProgressVisible = true;
    _searchProgressStatus = status;
    _searchProgressDetail = detail;
    _signal = SignalState(visible: true, level: level);
    _notify();
  }

  /// Expand or collapse an artist (fetches albums) or album (fetches tracks).
  Future<void> toggleNode(Map<String, Object?> item) async {
    final mType = '${item['media_type'] ?? ''}';
    if (mType != 'artist' && mType != 'album') return;
    final nid = _nodeId(item);
    if (_expandedNodes.contains(nid)) {
      _expandedNodes.remove(nid);
      // Collapse any expanded descendants too.
      final children = _nodeCache[nid];
      if (children != null) {
        for (final c in children) {
          _expandedNodes.remove(_nodeId(c));
        }
      }
      _notify();
      return;
    }

    _expandedNodes.add(nid);
    if (_nodeCache.containsKey(nid)) {
      _notify();
      return;
    }
    final remote = searcher;
    if (remote == null) {
      _notify();
      return;
    }

    _loadingNodes.add(nid);
    _notify();
    final source = '${item['source'] ?? _selectedSource}';
    final id = '${item['id'] ?? ''}';
    try {
      final rawChildren = mType == 'artist'
          ? await remote.getArtistAlbums(id, source: source)
          : await remote.getAlbumTracks(id, source: source);
      final children = [
        for (final c in rawChildren) Map<String, Object?>.of(c),
      ];
      await _annotateAll(children);
      if (_disposed) return;
      _nodeCache[nid] = children;
    } catch (e) {
      debugPrint('Node expansion failed for $nid: $e');
      _expandedNodes.remove(nid);
      onSnack?.call('Could not load details: $e');
    } finally {
      _loadingNodes.remove(nid);
      _notify();
    }
  }

  /// Fetch the next page of albums under an expanded artist node.
  Future<void> loadMoreArtistAlbums(Map<String, Object?> sentinel) async {
    final remote = searcher;
    if (remote == null) return;
    final artistId = '${sentinel['id'] ?? ''}';
    final offset = (sentinel['offset'] as num?)?.toInt() ?? 30;
    final limit = (sentinel['limit'] as num?)?.toInt() ?? 30;
    final source = '${sentinel['source'] ?? _selectedSource}';
    final loadKey = '$artistId:$offset';
    if (_loadingMoreKeys.contains(loadKey)) return;

    _loadingMoreKeys.add(loadKey);
    _notify();
    try {
      final rawMore = await remote.getArtistAlbums(
        artistId,
        limit: limit,
        offset: offset,
        source: source,
      );
      final more = [for (final c in rawMore) Map<String, Object?>.of(c)];
      await _annotateAll(more);
      if (_disposed) return;

      final nid = 'artist_$artistId';
      final existing = _nodeCache[nid];
      if (existing != null) {
        final idx = existing.indexOf(sentinel);
        if (idx >= 0) {
          existing.replaceRange(idx, idx + 1, more);
        }
      }
    } catch (e) {
      debugPrint('Load more albums failed: $e');
      onSnack?.call('Could not load more albums: $e');
    } finally {
      _loadingMoreKeys.remove(loadKey);
      _notify();
    }
  }

  // ── Stream preview playback ──────────────────────────────────────────────

  Future<void> togglePreview(int index, Map<String, Object?> item) async {
    final key = resultKey(item);
    final pl = player?.value;
    if (pl == null) {
      onSnack?.call('The player is still starting.');
      return;
    }

    if (_activePreviewKey == key && _activePreviewState == 'playing') {
      pl.stop();
      _activePreviewKey = null;
      _activePreviewState = 'idle';
      _previewCardVisible = false;
      _notify();
      return;
    }

    // Cancel any in-flight preview.
    _previewCancelled = true;
    final gen = ++_previewGen;
    _previewCancelled = false;
    _activePreviewKey = key;
    _activePreviewState = 'loading';
    final title = stripMarkup(
      '${item['ui_title'] ?? item['name'] ?? ''}',
    ).trim();
    final artist = stripMarkup(
      '${item['ui_subtitle'] ?? item['artist'] ?? ''}',
    );
    final source = '${item['source'] ?? _selectedSource}'.toLowerCase();
    final trackId = '${item['id'] ?? ''}';
    final streamable = source != 'deezer';

    _previewCardVisible = true;
    _previewStatus = 'Connecting...';
    _previewDetail = streamable
        ? "Resolving stream URL for '$title'..."
        : "Fetching preview for '$title'...";
    _notify();

    try {
      String? streamUrl;
      if (streamable && trackId.isNotEmpty && searcher != null) {
        try {
          streamUrl = await searcher!.getTrackStreamUrl(
            trackId,
            quality: 1,
            source: source,
          );
        } catch (e) {
          debugPrint('Preview stream URL failed, falling back to download: $e');
          if (_isPreviewStale(gen)) return;
          _previewStatus = 'Streaming Unavailable';
          _previewDetail = 'Falling back to preview download...';
          _notify();
        }
      }

      if (_isPreviewStale(gen)) return;

      if (streamUrl != null && streamUrl.isNotEmpty) {
        final ok = await _playPreviewSource(
          pl,
          path: streamUrl,
          title: title,
          artist: artist,
          imageUrl: '${item['image_url'] ?? item['image'] ?? ''}',
          gen: gen,
        );
        if (_isPreviewStale(gen)) return;
        if (ok) {
          _activePreviewState = 'playing';
          _previewCardVisible = false;
          _notify();
          return;
        }
        _previewStatus = 'Stream Playback Failed';
        _previewDetail = 'Falling back to preview download...';
        _notify();
      }

      // Fallback (and Deezer Blowfish path): download preview at quality 1.
      final url = '${item['url'] ?? ''}';
      if (url.isEmpty) {
        throw Exception('Missing track URL for preview.');
      }
      final base = dataDir ?? Directory.systemTemp.path;
      final safe = title
          .substring(0, title.length > 20 ? 20 : title.length)
          .replaceAll(RegExp(r'[^A-Za-z0-9]'), '_');
      final pdir =
          '$base${Platform.pathSeparator}previews${Platform.pathSeparator}${index}_$safe';
      await Directory(pdir).create(recursive: true);

      var audioFile = await _findAudioFile(pdir);
      if (audioFile == null) {
        _previewStatus = 'Downloading...';
        _previewDetail = "Fetching preview for '$title'...";
        _notify();

        void onProg(String status, int pct, String msg) {
          if (_isPreviewStale(gen)) return;
          _previewStatus = 'Downloading ($pct%)...';
          _previewDetail = msg;
          _notify();
        }

        if (previewDownloader != null) {
          await previewDownloader!(
            url: url,
            downloadDir: pdir,
            config: sourceConfig,
            progressCallback: onProg,
            isCancelled: () => _isPreviewStale(gen),
          );
        } else {
          await downloadFromSource(
            url: url,
            downloadDir: pdir,
            config: sourceConfig,
            quality: 1,
            progressCallback: onProg,
            isCancelled: () => _isPreviewStale(gen),
          );
        }
        if (_isPreviewStale(gen)) return;
        audioFile = await _findAudioFile(pdir);
      }

      if (audioFile == null) {
        throw Exception('Preview audio file was not created.');
      }

      final entry = QueueEntry({
        'path': audioFile,
        'track_title': '(Preview) $title',
        'artist_name': artist,
        'album_title': 'Streamrip Search',
        'image_url': '${item['image_url'] ?? item['image'] ?? ''}',
      });
      pl.setQueueEntries([entry], startIndex: 0);
      _activePreviewState = 'playing';
      _previewCardVisible = false;
      _notify();
    } catch (e) {
      if (_isPreviewStale(gen)) return;
      debugPrint('Preview failed: $e');
      _activePreviewKey = null;
      _activePreviewState = 'idle';
      _previewCardVisible = false;
      onSnack?.call('Preview failed: $e');
      _notify();
    }
  }

  /// Play a remote stream URL and monitor up to [previewStreamTimeout] for
  /// playback readiness or failure.
  Future<bool> _playPreviewSource(
    PlayerController pl, {
    required String path,
    required String title,
    required String artist,
    required String imageUrl,
    required int gen,
  }) async {
    var failed = false;
    final sub = pl.backend.failures.listen((_) => failed = true);
    try {
      final entry = QueueEntry({
        'path': path,
        'track_title': '(Preview) $title',
        'artist_name': artist,
        'album_title': 'Streamrip Search',
        'image_url': imageUrl,
      });
      pl.setQueueEntries([entry], startIndex: 0);

      final deadline = DateTime.now().add(previewStreamTimeout);
      while (DateTime.now().isBefore(deadline)) {
        if (_isPreviewStale(gen)) return false;
        if (failed) return false;
        if (pl.playing &&
            (pl.duration > Duration.zero ||
                pl.position.value > Duration.zero ||
                pl.processing == Processing.ready)) {
          return true;
        }
        await Future<void>.delayed(const Duration(milliseconds: 100));
      }
      return !failed && pl.playing;
    } finally {
      await sub.cancel();
    }
  }

  void cancelPreview() {
    _previewCancelled = true;
    _previewGen++;
    player?.value?.stop();
    _activePreviewKey = null;
    _activePreviewState = 'idle';
    _previewCardVisible = false;
    _notify();
  }

  bool _isPreviewStale(int gen) =>
      _disposed || _previewCancelled || gen != _previewGen;

  Future<String?> _findAudioFile(String dirPath) async {
    final dir = Directory(dirPath);
    if (!await dir.exists()) return null;
    const exts = ['.mp3', '.m4a', '.flac', '.wav'];
    await for (final entity in dir.list(recursive: true)) {
      if (entity is File) {
        final low = entity.path.toLowerCase();
        if (exts.any(low.endsWith)) return entity.path;
      }
    }
    return null;
  }

  void _cleanPreviewDir() {
    final base = dataDir;
    if (base == null || base.isEmpty) return;
    unawaited(
      _deletePreviewDir(Directory('$base${Platform.pathSeparator}previews')),
    );
  }

  Future<void> _deletePreviewDir(Directory dir) async {
    try {
      if (await dir.exists()) {
        await dir.delete(recursive: true);
      }
    } catch (_) {}
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _previewCancelled = true;
    config?.removeListener(_onConfigChanged);
    player?.removeListener(_bindPlayer);
    _boundPlayer?.removeListener(_onPlayerChanged);
    super.dispose();
  }
}
