import 'dart:math' as math;
import 'dart:ui' show Color;

import '../core/py_num.dart';
import '../data/network_queries.dart';
import 'genre_colors.dart';

/// The Acoustic Network's graph: library.py's `_build_interactive_network_canvas`
/// (the node and edge half), `_mk_net_node`, `_node_radius`,
/// `_place_net_node`, `_declutter_net_nodes`, `_apply_walk_seq` and
/// `_rebuild_walk_edges`. Pure: the controller feeds it rows, the painter
/// reads it. Golden-tested against Python's own builder
/// (tool/export_network_goldens.py).

/// library.py's canvas edge padding.
const kNetPad = 32;

/// `_NET_MIN_ZOOM`, `_NET_MAX_ZOOM`: the viewer's zoom bounds.
const kNetMinZoom = 0.8;
const kNetMaxZoom = 5.0;

/// `_NET_EDGE_COLOR` (neighbourhood edges) and `_NET_WALK_COLOR` (the walk:
/// green means the walk everywhere in this view).
const kNetEdgeColor = Color(0xFF2E2E36);
const kNetWalkColor = Color(0xFF00E676);

/// Density chip values (neighbours drawn) and the steps chip's (walk length).
const kNetDensities = [12, 16, 24, 36];
const kNetWalkLengths = [5, 10, 15, 20];

/// The canvas size library.py derives from the page: the full width less 24
/// (at least 260), and 42% of the height between 200 and 400.
(int, int) netCanvasSize(double pageWidth, double pageHeight) {
  final w = math.max(260.0, pageWidth - 24).toInt();
  final h = math.max(200, math.min((pageHeight * 0.42).toInt(), 400));
  return (w, h);
}

/// `_node_radius`: grows with plays (sqrt, capped at +7) so favourites read
/// as hubs.
double netNodeRadius(double base, int? playCount) {
  final pc = math.max(0, playCount ?? 0);
  return base + math.min(7.0, 2.2 * math.sqrt(pc));
}

class NetNode {
  NetNode({
    required this.rx,
    required this.ry,
    required this.path,
    required this.title,
    required this.artist,
    required this.album,
    required this.duration,
    required this.genre,
    required this.radius,
    required this.isSeed,
    required this.playCount,
  }) : color = genreColor(genre);

  /// Raw (PCA) coordinates; [px], [py] are canvas pixels.
  final double rx, ry;
  double px = 0, py = 0;
  final String path;
  final String title;
  final String artist;
  final String album;
  final double? duration;
  final String? genre;
  final Color color;
  final double radius;
  final bool isSeed;
  final int playCount;

  /// 1-based position on the drawn walk, 0 when off it. The canvas pills
  /// and the list rows both read it, so they can't disagree.
  int walkStep = 0;
}

enum NetEdgeKind { local, walk }

class NetEdge {
  const NetEdge(this.src, this.dst, this.weight, this.kind);
  final String src, dst;
  final double weight;
  final NetEdgeKind kind;
}

/// One built graph: nodes placed by a projection pinned at build time, so
/// merging walk steps later never moves what is on screen.
class NetworkGraph {
  NetworkGraph._(this.width, this.height, this.focalX, this.focalY, this.scale);

  final int width, height;
  final double focalX, focalY, scale;

  List<NetNode> nodes = [];
  Map<String, NetNode> byPath = {};
  List<NetEdge> edges = [];

  /// The seed and its neighbours: they survive re-walks.
  Set<String> localPaths = {};

  /// The drawn walk, in order, and the node it starts from.
  List<String> walkSeq = [];
  String? walkSeedPath;

  /// The inspected node (`_net_selected_path`).
  String? selected;

  /// library.py's builder. [currentPath] is the graph's seed, [nowPlaying]
  /// the playing track (the focal point when it is on the graph),
  /// [selected] the selection carried over from the last build, [walkSeed]
  /// the node the walk was computed from. Null when there is nothing to
  /// draw ("Play a track to seed the network.").
  ///
  /// One deliberate difference: a [walkSeed] outside the neighbourhood and
  /// the walk joins the graph, so the selection the walk was computed from
  /// survives a rebuild (Flet drops it and draws the walk from the seed;
  /// PORT_MAP §7).
  static NetworkGraph? build({
    required List<NetworkTrackRow> rows,
    required String currentPath,
    required List<NeighborRow> neighbors,
    required List<String> walkPaths,
    required String nowPlaying,
    required String? selected,
    String? walkSeed,
    required int width,
    required int height,
  }) {
    final pathToRow = {
      for (final r in rows)
        if (r.placeable) r.path: r,
    };
    final raw = <NetNode>[];
    final seen = <String>{};
    final rawEdges = <NetEdge>[];
    void add(NetNode n) {
      if (n.path.isNotEmpty && seen.add(n.path)) raw.add(n);
    }

    var seedRow = currentPath.isNotEmpty ? pathToRow[currentPath] : null;
    if (seedRow == null) {
      for (final r in rows) {
        if (r.placeable) {
          seedRow = r;
          break;
        }
      }
    }
    if (seedRow != null) {
      final sc = seedRow.coords!;
      add(
        NetNode(
          rx: sc[0],
          ry: sc[1],
          isSeed: true,
          radius: netNodeRadius(14, seedRow.playCount),
          genre: seedRow.genre,
          playCount: seedRow.playCount ?? 0,
          path: seedRow.path,
          title: _or(seedRow.title, 'Active Seed'),
          artist: _or(seedRow.artist, 'Unknown'),
          album: seedRow.album ?? '',
          duration: seedRow.duration,
        ),
      );
      for (final (idx, n) in neighbors.indexed) {
        final weight = n.weight == 0 ? 0.5 : n.weight; // `weight or 0.5`
        final row = pathToRow[n.path];
        if (row != null) {
          add(
            NetNode(
              rx: row.coords![0],
              ry: row.coords![1],
              isSeed: false,
              radius: netNodeRadius(8.5, row.playCount),
              genre: row.genre,
              playCount: row.playCount ?? 0,
              path: n.path,
              title: _or(row.title, _or(n.title, 'Neighbor')),
              artist: _or(row.artist, _or(n.artist, 'Unknown')),
              album: _orNull(row.album, n.album) ?? '',
              duration: row.duration,
            ),
          );
        } else {
          // No coordinates: on a ring around the seed.
          final theta = 2 * math.pi * idx / math.max(1, neighbors.length);
          add(
            NetNode(
              rx: sc[0] + 0.8 * math.cos(theta),
              ry: sc[1] + 0.8 * math.sin(theta),
              isSeed: false,
              radius: netNodeRadius(8.5, 0),
              genre: null,
              playCount: 0,
              path: n.path,
              title: _or(n.title, 'Neighbor'),
              artist: _or(n.artist, 'Unknown'),
              album: n.album ?? '',
              duration: null,
            ),
          );
        }
        rawEdges.add(NetEdge(seedRow.path, n.path, weight, NetEdgeKind.local));
      }
    }
    final local = {...seen};

    NetNode stepNode(NetworkTrackRow r) => NetNode(
      rx: r.coords![0],
      ry: r.coords![1],
      isSeed: false,
      radius: netNodeRadius(8, r.playCount),
      genre: r.genre,
      playCount: r.playCount ?? 0,
      path: r.path,
      title: _or(r.title, _basename(r.path)),
      artist: _or(r.artist, 'Unknown'),
      album: r.album ?? '',
      duration: r.duration,
    );
    for (final p in walkPaths) {
      final r = pathToRow[p];
      if (r != null) add(stepNode(r));
    }
    final seedStep = walkSeed == null ? null : pathToRow[walkSeed];
    if (seedStep != null) add(stepNode(seedStep));
    if (raw.isEmpty) return null;

    // Focal-centred, uniform scale, framing the farthest node: every node
    // lands inside the canvas.
    final focal =
        raw.where((n) => n.path == nowPlaying).firstOrNull ??
        raw.where((n) => n.isSeed).firstOrNull ??
        raw.first;
    final fcx = focal.rx, fcy = focal.ry;
    var spanX = raw.map((n) => (n.rx - fcx).abs()).reduce(math.max);
    var spanY = raw.map((n) => (n.ry - fcy).abs()).reduce(math.max);
    var halfSpan = math.max(spanX, spanY);
    if (halfSpan == 0) halfSpan = 1.0;
    final scale = (math.min(width, height) / 2 - kNetPad) / halfSpan;

    final g = NetworkGraph._(width, height, fcx, fcy, scale);
    for (final n in raw) {
      g._place(n);
      g.nodes.add(n);
      g.byPath[n.path] = n;
    }
    g.edges = rawEdges;
    g.localPaths = local;
    g.declutter();

    var sel = selected;
    if (sel == null || sel.isEmpty || !g.byPath.containsKey(sel)) {
      if (currentPath.isNotEmpty && g.byPath.containsKey(currentPath)) {
        sel = currentPath;
      } else {
        sel = g.nodes.first.path;
      }
    }
    g.selected = sel;
    g.walkSeq = [
      for (final p in walkPaths)
        if (g.byPath.containsKey(p)) p,
    ];
    g.walkSeedPath = sel;
    g.rebuildWalkEdges(sel);
    return g;
  }

  void _place(NetNode n) {
    n.px = width / 2 + (n.rx - focalX) * scale;
    n.py = height / 2 + (n.ry - focalY) * scale;
  }

  /// `_declutter_net_nodes`: pairs closer than r1 + r2 + [minGap] push apart
  /// in small damped steps, but no node ever moves more than [maxDisp] px
  /// from its true position (clamped every pass). [movable] freezes every
  /// other node.
  void declutter({
    double minGap = 3.0,
    int iterations = 24,
    double maxDisp = 12.0,
    Set<String>? movable,
  }) {
    final n = nodes.length;
    if (n < 2) return;
    bool canMove(NetNode nd) => movable == null || movable.contains(nd.path);
    if (!nodes.any(canMove)) return;
    final ox = [for (final nd in nodes) nd.px];
    final oy = [for (final nd in nodes) nd.py];
    for (var it = 0; it < iterations; it++) {
      var moved = false;
      for (var i = 0; i < n; i++) {
        final a = nodes[i];
        for (var j = i + 1; j < n; j++) {
          final b = nodes[j];
          var dx = b.px - a.px;
          var dy = b.py - a.py;
          var d = pyHypot(dx, dy);
          final want = a.radius + b.radius + minGap;
          if (d >= want) continue;
          if (d < 1.0) {
            // Coincident: fan out by the golden angle, deterministically.
            final ang = (i * 2.399963) % (2 * math.pi);
            dx = math.cos(ang);
            dy = math.sin(ang);
            d = 1.0;
          }
          final push = math.min((want - d) * 0.25, 2.0);
          final ux = dx / d, uy = dy / d;
          final aFree = canMove(a), bFree = canMove(b);
          if (!(aFree || bFree)) continue;
          double pa, pb;
          if (aFree && bFree) {
            pa = pb = push;
          } else if (aFree) {
            pa = push * 2.0;
            pb = 0.0;
          } else {
            pa = 0.0;
            pb = push * 2.0;
          }
          a.px -= ux * pa;
          a.py -= uy * pa;
          b.px += ux * pb;
          b.py += uy * pb;
          moved = true;
        }
      }
      if (!moved) break;
      for (var i = 0; i < n; i++) {
        final nd = nodes[i];
        if (!canMove(nd)) continue;
        final ddx = nd.px - ox[i];
        final ddy = nd.py - oy[i];
        final dist = pyHypot(ddx, ddy);
        if (dist > maxDisp) {
          final k = maxDisp / dist;
          nd.px = ox[i] + ddx * k;
          nd.py = oy[i] + ddy * k;
        }
      }
    }
  }

  /// `_rebuild_walk_edges`: swap the walk edges (from [seed] through
  /// [walkSeq]) and renumber the steps.
  void rebuildWalkEdges(String seed) {
    final out = [
      for (final e in edges)
        if (e.kind != NetEdgeKind.walk) e,
    ];
    final chain = [if (byPath.containsKey(seed)) seed, ...walkSeq];
    for (var i = 0; i < chain.length - 1; i++) {
      out.add(NetEdge(chain[i], chain[i + 1], 1.0, NetEdgeKind.walk));
    }
    edges = out;
    final stepOf = <String, int>{};
    for (final (i, p) in walkSeq.indexed) {
      stepOf[p] = i + 1;
    }
    for (final nd in nodes) {
      nd.walkStep = stepOf[nd.path] ?? 0;
    }
  }

  /// `_apply_walk_seq`: merge a fresh walk from [seed] into the graph
  /// ([newRows]: coordinates for its steps not yet on it), letting only the
  /// newcomers move, then drop nodes that are neither the neighbourhood nor
  /// the walk.
  void applyWalk(String seed, List<String> seq, List<NetworkTrackRow> newRows) {
    final added = <String>{};
    for (final r in newRows) {
      if (!r.placeable || byPath.containsKey(r.path)) continue;
      final nd = NetNode(
        rx: r.coords![0],
        ry: r.coords![1],
        isSeed: false,
        radius: netNodeRadius(8, r.playCount),
        genre: r.genre,
        playCount: r.playCount ?? 0,
        path: r.path,
        title: _or(r.title, _basename(r.path)),
        artist: _or(r.artist, 'Unknown'),
        album: r.album ?? '',
        duration: r.duration,
      );
      _place(nd);
      nodes.add(nd);
      byPath[nd.path] = nd;
      added.add(nd.path);
    }
    if (added.isNotEmpty) declutter(movable: added);

    walkSeq = [
      for (final p in seq)
        if (byPath.containsKey(p)) p,
    ];
    walkSeedPath = seed;

    final keep = {...localPaths, ...walkSeq, if (seed.isNotEmpty) seed};
    if (keep.length < nodes.length) {
      nodes = [
        for (final nd in nodes)
          if (keep.contains(nd.path)) nd,
      ];
      byPath = {for (final nd in nodes) nd.path: nd};
      edges = [
        for (final e in edges)
          if (byPath.containsKey(e.src) && byPath.containsKey(e.dst)) e,
      ];
    }
    rebuildWalkEdges(seed);
  }

  /// The walk's nodes, in order: what the list under the graph shows.
  List<NetNode> get walkNodes => [for (final p in walkSeq) ?byPath[p]];

  /// The genre legend: one row per family present (by lower-cased label),
  /// when there is more than one, sorted, at most six.
  List<(String, Color)> legend() {
    final present = <String, (String, Color)>{};
    for (final nd in nodes) {
      final g = nd.genre;
      if (g == null || g.isEmpty) continue;
      final (key, label) = genreGroup(g);
      if (key.isEmpty) continue;
      present.putIfAbsent(label.toLowerCase().trim(), () => (label, nd.color));
    }
    if (present.length < 2) return const [];
    final rows = present.values.toList()
      ..sort((a, b) {
        final c = a.$1.compareTo(b.$1);
        return c != 0 ? c : colorHex(a.$2).compareTo(colorHex(b.$2));
      });
    return rows.take(6).toList();
  }
}

/// Python's colour strings ('#RRGGBB'), for sorting and the goldens.
String colorHex(Color c) =>
    '#${(c.toARGB32() & 0xFFFFFF).toRadixString(16).padLeft(6, '0').toUpperCase()}';

String _or(String? v, String fallback) =>
    (v == null || v.isEmpty) ? fallback : v;

String? _orNull(String? a, String? b) => (a == null || a.isEmpty) ? b : a;

String _basename(String path) => path.split('/').last;
