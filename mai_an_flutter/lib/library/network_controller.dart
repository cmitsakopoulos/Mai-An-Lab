import 'dart:async';

import 'package:flutter/foundation.dart';

import '../core/haptics.dart';
import '../data/library_repository.dart';
import '../player/player_controller.dart';
import 'library_controller.dart';
import 'network_layout.dart';

/// What the pane is (library.py `_net_mode`). Explore: speculative walks
/// from the selection; the graph never moves on its own. Live ("Now
/// Playing"): the list mirrors what is queued ahead and the graph follows
/// the playing track.
enum NetMode { explore, live }

enum NetStatus {
  /// Not built yet.
  idle,

  /// No track has coordinates: the setup card.
  noCoords,

  /// Coordinates exist but nothing could be placed: "Play a track to seed
  /// the network."
  empty,
  ready,
}

/// State for the Acoustic Network view: library.py's network half of
/// `load_library`, `_compute_walk_seq`, `_schedule_net_walk_refresh`, the
/// readout's three actions, the chips, the list's actions and
/// `_sync_network_now_playing`. The graph itself is [NetworkGraph].
class NetworkController extends ChangeNotifier {
  NetworkController({
    required this.library,
    required this.player,
    required this.haptics,
  }) {
    player.addListener(_bindPlayer);
    _bindPlayer();
    library.addListener(_onLibrary);
    _generation = library.dataGeneration;
  }

  final LibraryController library;
  final ValueListenable<PlayerController?> player;
  final Haptics haptics;

  NetMode mode = NetMode.explore;

  /// Density: how many neighbours are drawn.
  int density = 24;

  /// Steps: how long the walk (or the live list) is.
  int walkLength = 10;

  NetStatus status = NetStatus.idle;
  NetworkGraph? graph;

  /// "n TRACKS": the coordinate rows the build read.
  int trackCount = 0;

  /// A build is under way (the header spinner).
  bool loading = false;

  /// The graph is hidden so the list takes the pane.
  bool collapsed = false;

  /// The seed pinned by "Explore from here" / "Reseed graph here"; null
  /// follows the playing track.
  String? seedPath;

  /// The inspected node. Survives rebuilds when it is still on the graph.
  String? selected;

  /// Canvas size of the last build; a different size rebuilds.
  (int, int)? dims;

  int _loadToken = 0;
  int _walkToken = 0;
  Timer? _walkTimer;
  Timer? _reseedTimer;
  int _attached = 0;
  int _generation = 0;
  PlayerController? _player;
  String _lastPath = '';

  String get nowPlaying => player.value?.currentPath ?? '';

  NetNode? get selectedNode =>
      selected == null ? null : graph?.byPath[selected!];

  /// The readout's walk button: hidden when the drawn walk already starts
  /// at the selection (the walk is deterministic) and in live mode (the
  /// list is the real queue there).
  bool get canWalkFromSelection =>
      mode != NetMode.live &&
      selected != null &&
      selected != graph?.walkSeedPath;

  /// The view is on screen: build when the size is known. Track changes
  /// are followed while attached (Flet only while the view is the open one).
  void attach() {
    _attached++;
  }

  void detach() {
    _attached--;
  }

  /// The view laid out at [width] × [height]: build if never built or the
  /// size changed.
  void ensureBuilt(int width, int height) {
    if (dims == (width, height) && (status != NetStatus.idle || loading)) {
      return;
    }
    dims = (width, height);
    unawaited(load());
  }

  /// library.py `load_library`, the network branch.
  Future<void> load() async {
    final repo = library.repository;
    final size = dims;
    if (repo == null || size == null) return;
    final token = ++_loadToken;
    _walkTimer?.cancel();
    _walkToken++;
    loading = true;
    notifyListeners();
    try {
      if (!await repo.hasPcaCoords()) {
        if (token != _loadToken) return;
        graph = null;
        trackCount = 0;
        status = NetStatus.noCoords;
        return;
      }
      final np = nowPlaying;
      final b = await buildNetwork(
        repo,
        seedPath: seedPath,
        nowPlaying: np,
        selected: selected,
        density: density,
        live: mode == NetMode.live,
        walk: _walkSeq,
        width: size.$1,
        height: size.$2,
      );
      if (token != _loadToken) return;
      trackCount = b.rows.length;
      final built = b.graph;
      graph = built;
      if (built != null) selected = built.selected;
      status = built == null ? NetStatus.empty : NetStatus.ready;
      _lastPath = np;
    } catch (e) {
      debugPrint('network: build failed: $e');
    } finally {
      if (token == _loadToken) {
        loading = false;
        notifyListeners();
      }
    }
  }

  /// `_compute_walk_seq`: live is the real queue ahead (never resampled;
  /// empty is a true answer), explore a deterministic walk from [seed].
  Future<List<String>> _walkSeq(String seed) async {
    if (mode == NetMode.live) return queueAhead().take(walkLength).toList();
    if (seed.isEmpty) return const [];
    final stations = await library.stations();
    return stations?.walk(seed, length: walkLength) ?? const [];
  }

  /// `_live_queue_ahead`: auto-play's buffer when it is running, else the
  /// plain queue after the playing row.
  List<String> queueAhead() {
    final p = player.value;
    if (p == null) return const [];
    final buffer = p.autoplay?.bufferPaths() ?? const [];
    if (buffer.isNotEmpty) return buffer;
    final q = p.queue;
    return [
      for (final r in q.entries.skip(q.currentIndex + 1))
        if (r.path.isNotEmpty) r.path,
    ];
  }

  /// `_schedule_net_walk_refresh`: re-walk from [seed] in place (the graph
  /// keeps its pan, zoom and layout; new steps merge in).
  void _rewalk(String seed, {Duration delay = Duration.zero}) {
    _walkTimer?.cancel();
    final token = ++_walkToken;
    Future<void> run() async {
      try {
        final repo = library.repository;
        final seq = await _walkSeq(seed);
        final g = graph;
        if (token != _walkToken || g == null || repo == null) return;
        final missing = [
          for (final p in seq)
            if (!g.byPath.containsKey(p)) p,
        ];
        final rows = missing.isEmpty
            ? const <NetworkTrackRow>[]
            : await repo.pcaCoordsForPaths(missing);
        if (token != _walkToken || !identical(g, graph)) return;
        g.applyWalk(seed, seq, rows);
        notifyListeners();
      } catch (e) {
        debugPrint('network: walk refresh failed: $e');
      }
    }

    if (delay == Duration.zero) {
      unawaited(run());
    } else {
      _walkTimer = Timer(delay, () => unawaited(run()));
    }
  }

  /// `_schedule_net_reseed`: rebuild around the playing track, debounced so
  /// a burst of skips is one rebuild.
  void _reseedLive({Duration delay = const Duration(milliseconds: 350)}) {
    seedPath = null;
    selected = null;
    _reseedTimer?.cancel();
    if (delay == Duration.zero) {
      unawaited(load());
    } else {
      _reseedTimer = Timer(delay, () => unawaited(load()));
    }
  }

  // ── The user's actions ────────────────────────────────────────────────

  /// Tap on a node or a row: inspect only (the readout names it, the halo
  /// moves); the graph doesn't change.
  void select(String path) {
    haptics.trigger('network_tap');
    selected = path;
    notifyListeners();
  }

  /// The readout's play button: the node now, non-destructively.
  void playSelection() {
    final nd = selectedNode;
    if (nd == null) return;
    haptics.trigger('network_walk');
    player.value?.playBlock([nodeTrack(nd)], replace: true);
  }

  /// The readout's "Explore from here": rebuild the map on the selection.
  /// Re-framing is exploring, so it leaves live mode.
  void reseedSelection() {
    final path = selected;
    if (path == null) return;
    haptics.trigger('network_reseed');
    // In live mode the next track change would reseed back onto the
    // playing track and undo this.
    mode = NetMode.explore;
    seedPath = path;
    selected = path;
    unawaited(load());
  }

  /// The row menu's "Reseed graph here" (the mode stays, as in Flet).
  void reseedAt(String path) {
    haptics.trigger('network_reseed');
    seedPath = path;
    selected = path;
    unawaited(load());
  }

  /// The readout's walk button.
  void walkFromSelection() {
    final path = selected;
    if (path == null) return;
    haptics.trigger('network_walk');
    _rewalk(path);
  }

  /// The row menu's "Start a walk here": asking for a walk is asking to
  /// explore.
  void walkHere(String path) {
    haptics.trigger('network_walk');
    mode = NetMode.explore;
    selected = path;
    notifyListeners();
    _rewalk(path);
  }

  void setDensity(int k) {
    haptics.trigger('network_tap');
    density = k;
    unawaited(load());
  }

  void setWalkLength(int n) {
    haptics.trigger('network_tap');
    walkLength = n;
    notifyListeners();
    final seed = (selected?.isNotEmpty ?? false)
        ? selected!
        : (graph?.walkSeedPath ?? nowPlaying);
    _rewalk(seed);
  }

  /// `_toggle_net_mode`: live snaps to the playing track (a rebuild when it
  /// isn't on the graph); either way the list is re-resolved.
  void toggleMode() {
    haptics.trigger('network_tap');
    mode = mode == NetMode.explore ? NetMode.live : NetMode.explore;
    final cur = nowPlaying;
    if (mode == NetMode.live) {
      if (cur.isNotEmpty && graph?.byPath[cur] == null) {
        notifyListeners();
        _reseedLive(delay: Duration.zero);
        return;
      }
      if (cur.isNotEmpty) selected = cur;
    }
    notifyListeners();
    _rewalk(selected ?? cur);
  }

  void toggleCollapsed() {
    haptics.trigger('network_tap');
    collapsed = !collapsed;
    notifyListeners();
  }

  /// The header's Play: the walk now, in order.
  void playAll() {
    haptics.trigger('network_walk');
    player.value?.playBlock([
      for (final n in graph?.walkNodes ?? const <NetNode>[]) nodeTrack(n),
    ], replace: true);
  }

  /// The header's Next: the walk as the listener's picks.
  void addAll() {
    haptics.trigger('swipe_queue');
    player.value?.playBlock([
      for (final n in graph?.walkNodes ?? const <NetNode>[]) nodeTrack(n),
    ], replace: false);
  }

  /// A row's play-next button.
  void playNext(NetNode n) {
    haptics.trigger('swipe_queue');
    player.value?.queueNext(nodeTrack(n));
  }

  /// The row menu's "Play from here": the walk from row [index] on.
  void playFrom(int index, NetNode n) {
    haptics.trigger('network_walk');
    final g = graph;
    final tail = [
      if (g != null)
        for (final p in g.walkSeq.skip(index))
          if (g.byPath[p] case final nd?) nodeTrack(nd),
    ];
    player.value?.playBlock(
      tail.isEmpty ? [nodeTrack(n)] : tail,
      replace: true,
    );
  }

  // ── Following playback ────────────────────────────────────────────────

  void _bindPlayer() {
    _player?.removeListener(_onPlayer);
    _player = player.value;
    _player?.addListener(_onPlayer);
    _lastPath = nowPlaying;
  }

  void _onPlayer() {
    final cur = nowPlaying;
    if (cur == _lastPath) return;
    final prev = _lastPath;
    _lastPath = cur;
    if (_attached > 0) _trackChanged(prev, cur);
  }

  /// `_sync_network_now_playing`. The pulse ring follows by itself (the
  /// view draws it on the playing node); only live mode re-anchors.
  void _trackChanged(String prev, String cur) {
    final g = graph;
    if (g == null || g.nodes.isEmpty) {
      if (mode == NetMode.live && cur.isNotEmpty) _reseedLive();
      return;
    }
    if (g.byPath.containsKey(cur)) {
      if (mode == NetMode.live && cur.isNotEmpty) {
        selected = cur;
        _rewalk(cur, delay: const Duration(milliseconds: 200));
      }
      notifyListeners();
      return;
    }
    if (mode == NetMode.live && cur.isNotEmpty) {
      _reseedLive();
    } else {
      notifyListeners();
    }
  }

  /// A scan or a delete changed the rows: rebuild now if shown, else on
  /// the next show.
  void _onLibrary() {
    if (library.dataGeneration == _generation) return;
    _generation = library.dataGeneration;
    if (_attached > 0) {
      unawaited(load());
    } else {
      status = NetStatus.idle;
    }
  }

  @override
  void dispose() {
    _walkTimer?.cancel();
    _reseedTimer?.cancel();
    _loadToken++;
    _walkToken++;
    player.removeListener(_bindPlayer);
    _player?.removeListener(_onPlayer);
    library.removeListener(_onLibrary);
    super.dispose();
  }
}

/// What one build read and made (the golden tests compare every part).
typedef NetworkBuild = ({
  String current,
  String walkSeed,
  List<NeighborRow> neighbors,
  List<String> walk,
  List<String> needed,
  List<NetworkTrackRow> rows,
  NetworkGraph? graph,
});

/// library.py `load_library`'s network branch after the coordinate check:
/// the seed (the pinned one, else the playing track, else any analysed
/// track), its [density] heaviest neighbours, the walk from the selection
/// (live: from the playing track) by [walk], their coordinates, and the
/// graph.
Future<NetworkBuild> buildNetwork(
  LibraryRepository repo, {
  required String? seedPath,
  required String nowPlaying,
  required String? selected,
  required int density,
  required bool live,
  required Future<List<String>> Function(String seed) walk,
  required int width,
  required int height,
}) async {
  var current = seedPath ?? (nowPlaying.isNotEmpty ? nowPlaying : '');
  if (current.isEmpty) current = await repo.anyPcaPath();
  var neighbors = <NeighborRow>[];
  var walkPaths = <String>[];
  var walkSeed = '';
  if (current.isNotEmpty) {
    neighbors = await repo.networkNeighbors(current, density);
    walkSeed = live
        ? (nowPlaying.isNotEmpty ? nowPlaying : current)
        : ((selected?.isNotEmpty ?? false) ? selected! : current);
    walkPaths = await walk(walkSeed);
  }
  final needed = [
    if (current.isNotEmpty) current,
    if (walkSeed.isNotEmpty) walkSeed,
    for (final n in neighbors) n.path,
    ...walkPaths,
  ];
  final rows = await repo.pcaCoordsForPaths(needed);
  final graph = NetworkGraph.build(
    rows: rows,
    currentPath: current,
    neighbors: neighbors,
    walkPaths: walkPaths,
    nowPlaying: nowPlaying,
    selected: selected,
    walkSeed: walkSeed.isEmpty ? null : walkSeed,
    width: width,
    height: height,
  );
  return (
    current: current,
    walkSeed: walkSeed,
    neighbors: neighbors,
    walk: walkPaths,
    needed: needed,
    rows: rows,
    graph: graph,
  );
}

/// `_node_to_track`: a queue row for a node.
Track nodeTrack(NetNode n) => Track(
  id: 0,
  path: n.path,
  title: n.title.isNotEmpty
      ? n.title
      : (n.path.split('/').last.isNotEmpty
            ? n.path.split('/').last
            : 'Unknown'),
  artist: n.artist.isNotEmpty ? n.artist : 'Unknown Artist',
  album: n.album.isNotEmpty ? n.album : 'Unknown Album',
  duration: n.duration ?? 0,
  format: '',
);
