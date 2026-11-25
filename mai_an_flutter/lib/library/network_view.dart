import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../analysis/library_analysis.dart';
import '../data/library_repository.dart';
import '../player/player_controller.dart';
import '../ui/tokens.dart';
import 'network_controller.dart';
import 'network_layout.dart';

/// library.py's Acoustic Network view: the graph (pan and zoom, tap to
/// inspect, the readout's play / explore / walk buttons, the mode, density
/// and fit chips, the genre legend, the now-playing pulse) over the ordered
/// list of the walk (steps chip, Play and Next, a menu per row).
class NetworkView extends StatefulWidget {
  const NetworkView({
    super.key,
    required this.controller,
    required this.player,
    this.onAddToPlaylist,
  });

  final NetworkController controller;
  final ValueListenable<PlayerController?> player;

  /// The row menu's Add to Playlist; null hides it (read-only library).
  final void Function(Track)? onAddToPlaylist;

  @override
  State<NetworkView> createState() => _NetworkViewState();
}

class _NetworkViewState extends State<NetworkView>
    with SingleTickerProviderStateMixin {
  NetworkController get c => widget.controller;
  final _transform = TransformationController();
  late final _reset = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 220),
  );
  Animation<Matrix4>? _resetAnim;

  /// The node under the last tap-down (acting on tap-up keeps pans clean).
  NetNode? _pressed;

  @override
  void initState() {
    super.initState();
    c.attach();
    _reset.addListener(() {
      if (_resetAnim case final a?) _transform.value = a.value;
    });
  }

  @override
  void dispose() {
    c.detach();
    _reset.dispose();
    _transform.dispose();
    super.dispose();
  }

  /// The viewer's zoom, clamped as library.py clamps it.
  double get _k => 1.0 / _transform.value.getMaxScaleOnAxis().clamp(0.5, 6.0);

  /// `_reset_net_view`: back to the fitted, centred view.
  void _fit() {
    c.haptics.trigger('network_tap');
    _resetAnim = Matrix4Tween(
      begin: _transform.value.clone(),
      end: Matrix4.identity(),
    ).animate(CurvedAnimation(parent: _reset, curve: Curves.easeOut));
    _reset.forward(from: 0);
  }

  NetNode? _nodeAt(Offset p) {
    final g = c.graph;
    if (g == null) return null;
    final k = _k;
    NetNode? best;
    var bestDist = double.infinity;
    for (final n in g.nodes) {
      final dist = (p - Offset(n.px, n.py)).distance;
      if (dist <= (n.radius + 14) * k && dist < bestDist) {
        best = n;
        bestDist = dist;
      }
    }
    return best;
  }

  @override
  Widget build(BuildContext context) {
    final screenH = MediaQuery.sizeOf(context).height;
    return LayoutBuilder(
      builder: (context, box) {
        final w = math.max(200, (box.maxWidth - 8).floor());
        final (_, h) = netCanvasSize(box.maxWidth, screenH);
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted) c.ensureBuilt(w, h);
        });
        return ListenableBuilder(
          listenable: Listenable.merge([c, c.library.analysis]),
          builder: (context, _) => c.library.analysis.running
              ? _AnalysingCard(analysis: c.library.analysis)
              : switch (c.status) {
                  NetStatus.idle => const Center(
                    child: CircularProgressIndicator(),
                  ),
                  NetStatus.noCoords => _SetupCard(
                    onCompute: c.library.canWrite
                        ? c.library.analyseLibrary
                        : null,
                  ),
                  NetStatus.empty => const _EmptyGraph(),
                  NetStatus.ready => Column(
                    children: [
                      if (!c.collapsed) _graphPane(context),
                      Expanded(
                        child: _TrackPanel(
                          controller: c,
                          player: widget.player,
                          onAddToPlaylist: widget.onAddToPlaylist,
                        ),
                      ),
                    ],
                  ),
                },
        );
      },
    );
  }

  Widget _graphPane(BuildContext context) {
    final g = c.graph!;
    final cyan = Theme.of(context).colorScheme.primary;
    final w = g.width.toDouble(), h = g.height.toDouble();
    final legend = g.legend();
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
      decoration: BoxDecoration(
        color: Tokens.bg,
        border: Border.all(color: cyan.withValues(alpha: 0.14)),
        borderRadius: BorderRadius.circular(14),
      ),
      clipBehavior: Clip.antiAlias,
      child: SizedBox(
        width: w,
        height: h,
        child: Stack(
          clipBehavior: Clip.hardEdge,
          children: [
            InteractiveViewer(
              transformationController: _transform,
              minScale: kNetMinZoom,
              maxScale: kNetMaxZoom,
              boundaryMargin: const EdgeInsets.all(240),
              child: SizedBox(
                width: w,
                height: h,
                child: Stack(
                  children: [
                    GestureDetector(
                      onTapDown: (d) => _pressed = _nodeAt(d.localPosition),
                      onTap: () {
                        // Empty space keeps the selection: the readout is the
                        // graph's only name for a node.
                        if (_pressed case final n?) c.select(n.path);
                      },
                      child: CustomPaint(
                        size: Size(w, h),
                        painter: _NetworkPainter(
                          graph: g,
                          selected: c.selected,
                          accent: cyan,
                          transform: _transform,
                        ),
                      ),
                    ),
                    _PulseRing(
                      graph: g,
                      player: widget.player,
                      transform: _transform,
                      accent: cyan,
                    ),
                  ],
                ),
              ),
            ),
            if (legend.isNotEmpty)
              Positioned(left: 8, top: 44, child: _Legend(legend, cyan)),
            Positioned(left: 6, right: 6, top: 6, child: _topControls(cyan)),
            Positioned(
              left: 8,
              top: math.max(8, h - 52),
              width: math.min(292, w - 16),
              child: _Readout(controller: c, accent: cyan),
            ),
          ],
        ),
      ),
    );
  }

  Widget _topControls(Color cyan) {
    final live = c.mode == NetMode.live;
    final accent = live ? kNetWalkColor : cyan;
    final mode = Tooltip(
      message: live
          ? 'Now Playing — mirrors the real queue and follows the track'
          : 'Explore — speculative walks; the graph stays where you put it',
      child: _Chip(
        accent: accent,
        borderAlpha: 0.35,
        onTap: c.toggleMode,
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              live ? Icons.my_location : Icons.travel_explore_rounded,
              size: 13,
              color: accent,
            ),
            const SizedBox(width: 4),
            Text(
              live ? 'Now Playing' : 'Explore',
              style: TextStyle(
                fontSize: 9.5,
                fontWeight: FontWeight.w800,
                color: accent,
              ),
            ),
          ],
        ),
      ),
    );
    final density = _ParamChip(
      options: kNetDensities,
      current: c.density,
      label: (o) => 'Density: $o',
      item: (o) => 'Density: $o tracks',
      tooltip: 'Neighbourhood density',
      accent: cyan,
      onPick: c.setDensity,
    );
    final fit = Tooltip(
      message: 'Reset view (fit & centre)',
      child: _Chip(
        accent: cyan,
        borderAlpha: 0.22,
        padding: const EdgeInsets.all(5),
        onTap: _fit,
        child: Icon(Icons.filter_center_focus_rounded, size: 13, color: cyan),
      ),
    );
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
      decoration: BoxDecoration(
        color: Tokens.surface2.withValues(alpha: 0.88),
        border: Border.all(color: cyan.withValues(alpha: 0.18)),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        children: [
          mode,
          const SizedBox(width: 3),
          density,
          const Spacer(),
          fit,
        ],
      ),
    );
  }
}

/// The chips' shared face.
class _Chip extends StatelessWidget {
  const _Chip({
    required this.accent,
    required this.borderAlpha,
    required this.child,
    this.onTap,
    this.padding = const EdgeInsets.symmetric(horizontal: 7, vertical: 4),
  });

  final Color accent;
  final double borderAlpha;
  final Widget child;
  final VoidCallback? onTap;
  final EdgeInsets padding;

  @override
  Widget build(BuildContext context) => GestureDetector(
    behavior: HitTestBehavior.opaque,
    onTap: onTap,
    child: Container(
      padding: padding,
      decoration: BoxDecoration(
        color: Tokens.surface2.withValues(alpha: 0.85),
        border: Border.all(color: accent.withValues(alpha: borderAlpha)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: child,
    ),
  );
}

/// `_net_param_chip`: a dropdown for one integer setting.
class _ParamChip extends StatelessWidget {
  const _ParamChip({
    required this.options,
    required this.current,
    required this.label,
    required this.item,
    required this.tooltip,
    required this.accent,
    required this.onPick,
    this.leading,
  });

  final List<int> options;
  final int current;
  final String Function(int) label;
  final String Function(int) item;
  final String tooltip;
  final Color accent;
  final ValueChanged<int> onPick;
  final IconData? leading;

  @override
  Widget build(BuildContext context) => PopupMenuButton<int>(
    tooltip: tooltip,
    color: Tokens.surface2,
    onSelected: onPick,
    itemBuilder: (context) => [
      for (final o in options)
        PopupMenuItem(
          value: o,
          height: 36,
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                o == current ? Icons.check_rounded : Icons.tune_rounded,
                size: 13,
                color: o == current ? accent : Tokens.dim,
              ),
              const SizedBox(width: 6),
              Text(
                item(o),
                style: TextStyle(
                  fontSize: 11,
                  color: o == current ? Tokens.text : Tokens.dim,
                  fontWeight: o == current ? FontWeight.w700 : FontWeight.w400,
                ),
              ),
            ],
          ),
        ),
    ],
    child: _Chip(
      accent: accent,
      borderAlpha: 0.25,
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (leading case final icon?) ...[
            Icon(icon, size: 12, color: accent),
            const SizedBox(width: 2),
          ],
          Text(
            label(current),
            style: TextStyle(
              fontSize: 9.5,
              fontWeight: FontWeight.w700,
              color: accent,
            ),
          ),
          Icon(Icons.arrow_drop_down_rounded, size: 14, color: accent),
        ],
      ),
    ),
  );
}

class _Legend extends StatelessWidget {
  const _Legend(this.rows, this.accent);
  final List<(String, Color)> rows;
  final Color accent;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
    decoration: BoxDecoration(
      color: Tokens.surface2.withValues(alpha: 0.82),
      border: Border.all(color: accent.withValues(alpha: 0.16)),
      borderRadius: BorderRadius.circular(8),
    ),
    child: Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (final (label, color) in rows)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 1),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  width: 8,
                  height: 8,
                  decoration: BoxDecoration(
                    color: color,
                    shape: BoxShape.circle,
                  ),
                ),
                const SizedBox(width: 4),
                Text(
                  label.length > 14 ? '${label.substring(0, 14)}…' : label,
                  style: const TextStyle(fontSize: 8, color: Tokens.text),
                ),
              ],
            ),
          ),
      ],
    ),
  );
}

/// The fixed bottom-left readout: the selected node's name, its role, and
/// the three things to do with it (hear it, re-frame on it, walk from it).
class _Readout extends StatelessWidget {
  const _Readout({required this.controller, required this.accent});
  final NetworkController controller;
  final Color accent;

  @override
  Widget build(BuildContext context) {
    final c = controller;
    final nd = c.selectedNode;
    if (nd == null) return const SizedBox.shrink();
    final String suffix;
    if (nd.path == c.nowPlaying) {
      suffix = ' · playing';
    } else if (nd.walkStep > 0) {
      suffix = ' · step ${nd.walkStep}';
    } else if (nd.isSeed) {
      suffix = ' · seed';
    } else {
      suffix = '';
    }
    Widget button(
      IconData icon,
      String tip,
      VoidCallback onTap, {
      Color? fill,
      Color? fg,
    }) => Tooltip(
      message: tip,
      child: GestureDetector(
        onTap: onTap,
        child: Container(
          width: 24,
          height: 24,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: fill ?? accent.withValues(alpha: 0.16),
            border: fill == null
                ? Border.all(color: accent.withValues(alpha: 0.35))
                : null,
            borderRadius: BorderRadius.circular(12),
          ),
          child: Icon(icon, size: 14, color: fg ?? accent),
        ),
      ),
    );
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: Tokens.surface2.withValues(alpha: 0.92),
        border: Border.all(color: accent.withValues(alpha: 0.3)),
        borderRadius: BorderRadius.circular(8),
        boxShadow: const [BoxShadow(blurRadius: 12, color: Color(0x4D000000))],
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  nd.title.isNotEmpty ? nd.title : 'Unknown',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                    color: Tokens.text,
                  ),
                ),
                Text(
                  '${nd.artist.isNotEmpty ? nd.artist : 'Unknown Artist'}$suffix',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontSize: 9.5, color: Tokens.dim),
                ),
              ],
            ),
          ),
          const SizedBox(width: 6),
          button(
            Icons.play_arrow_rounded,
            'Play this track',
            c.playSelection,
            fill: accent,
            fg: Tokens.bg,
          ),
          const SizedBox(width: 4),
          button(
            Icons.zoom_in_rounded,
            'Explore from here — rebuild the map on this node',
            c.reseedSelection,
          ),
          if (c.canWalkFromSelection) ...[
            const SizedBox(width: 4),
            button(
              Icons.route_rounded,
              'Walk from here',
              c.walkFromSelection,
              fill: kNetWalkColor,
              fg: Tokens.bg,
            ),
          ],
        ],
      ),
    );
  }
}

/// The now-playing ring: on the playing node when it is on the graph,
/// pulsing while it plays (library.py `_run_net_pulse`: 700 ms beats,
/// scale 1.0 ↔ 1.4, opacity 0.9 ↔ 0.25), gliding when the track changes.
class _PulseRing extends StatefulWidget {
  const _PulseRing({
    required this.graph,
    required this.player,
    required this.transform,
    required this.accent,
  });

  final NetworkGraph graph;
  final ValueListenable<PlayerController?> player;
  final TransformationController transform;
  final Color accent;

  @override
  State<_PulseRing> createState() => _PulseRingState();
}

class _PulseRingState extends State<_PulseRing>
    with SingleTickerProviderStateMixin {
  late final _beat = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 700),
  );
  PlayerController? _player;

  @override
  void initState() {
    super.initState();
    widget.player.addListener(_bind);
    _bind();
  }

  void _bind() {
    _player?.removeListener(_sync);
    _player = widget.player.value;
    _player?.addListener(_sync);
    _sync();
  }

  void _sync() {
    if (!mounted) return;
    final playing = _player?.playing ?? false;
    if (playing && !_beat.isAnimating) {
      _beat.repeat(reverse: true);
    } else if (!playing && _beat.isAnimating) {
      // Settle to a calm ring rather than freezing mid-fade.
      _beat
        ..stop()
        ..value = 0;
    }
    setState(() {});
  }

  @override
  void dispose() {
    widget.player.removeListener(_bind);
    _player?.removeListener(_sync);
    _beat.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.transform,
    builder: (context, _) {
      final path = _player?.currentPath ?? '';
      final nd = path.isEmpty ? null : widget.graph.byPath[path];
      final k =
          1.0 / widget.transform.value.getMaxScaleOnAxis().clamp(0.5, 6.0);
      final d = nd == null ? 24.0 : (nd.radius + 9) * 2 * k;
      return AnimatedPositioned(
        duration: const Duration(milliseconds: 280),
        curve: Curves.easeOut,
        left: nd == null ? -100 : nd.px - d / 2,
        top: nd == null ? -100 : nd.py - d / 2,
        width: d,
        height: d,
        child: IgnorePointer(
          child: nd == null
              ? const SizedBox.shrink()
              : AnimatedBuilder(
                  animation: _beat,
                  builder: (context, child) {
                    final t = Curves.easeInOut.transform(_beat.value);
                    return Opacity(
                      opacity: 0.9 - 0.65 * t,
                      child: Transform.scale(
                        scale: 1.0 + 0.4 * t,
                        child: child,
                      ),
                    );
                  },
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      border: Border.all(color: widget.accent, width: 2),
                    ),
                  ),
                ),
        ),
      );
    },
  );
}

/// `_emit_net_shapes`. Every size is divided by the viewer's zoom, so
/// magnifying separates nodes instead of scaling the crowding with them.
class _NetworkPainter extends CustomPainter {
  _NetworkPainter({
    required this.graph,
    required this.selected,
    required this.accent,
    required this.transform,
  }) : super(repaint: transform);

  final NetworkGraph graph;
  final String? selected;
  final Color accent;
  final TransformationController transform;

  @override
  void paint(Canvas canvas, Size size) {
    final k = 1.0 / transform.value.getMaxScaleOnAxis().clamp(0.5, 6.0);
    final g = graph;
    canvas.drawRect(Offset.zero & size, Paint()..color = Tokens.bg);

    // The neighbourhood recedes; the walk is drawn over it.
    for (final e in g.edges) {
      if (e.kind == NetEdgeKind.walk) continue;
      final a = g.byPath[e.src], b = g.byPath[e.dst];
      if (a == null || b == null) continue;
      final w = e.weight.clamp(0.0, 1.0);
      canvas.drawLine(
        Offset(a.px, a.py),
        Offset(b.px, b.py),
        Paint()
          ..color = kNetEdgeColor.withValues(alpha: 0.45 + 0.55 * w)
          ..strokeWidth = (0.9 + 1.6 * w) * k
          ..strokeCap = StrokeCap.round
          ..style = PaintingStyle.stroke,
      );
    }
    final walk = [
      for (final e in g.edges)
        if (e.kind == NetEdgeKind.walk) e,
    ];
    final nWalk = math.max(1, walk.length);
    for (final (i, e) in walk.indexed) {
      final a = g.byPath[e.src], b = g.byPath[e.dst];
      if (a == null || b == null) continue;
      // Brightest at the start, fading along the trajectory.
      final fade = math.max(0.42, 1.0 - i / nWalk * 0.55);
      final paint = Paint()
        ..color = kNetWalkColor.withValues(alpha: fade)
        ..strokeWidth = 2.4 * k
        ..strokeCap = StrokeCap.round
        ..style = PaintingStyle.stroke;
      final dx = b.px - a.px, dy = b.py - a.py;
      final len = math.sqrt(dx * dx + dy * dy);
      final l = len == 0 ? 1.0 : len;
      final ux = dx / l, uy = dy / l;
      // Stop at the node's edge so the arrowhead isn't under the circle.
      final bx = b.px - ux * (b.radius + 1.5) * k;
      final by = b.py - uy * (b.radius + 1.5) * k;
      final al = 7 * k;
      canvas
        ..drawLine(Offset(a.px, a.py), Offset(bx, by), paint)
        ..drawLine(
          Offset(bx, by),
          Offset(
            bx - al * (ux * 0.866 + uy * 0.5),
            by - al * (-ux * 0.5 + uy * 0.866),
          ),
          paint,
        )
        ..drawLine(
          Offset(bx, by),
          Offset(
            bx - al * (ux * 0.866 - uy * 0.5),
            by - al * (ux * 0.5 + uy * 0.866),
          ),
          paint,
        );
    }

    // Auras and rings under the nodes.
    for (final n in g.nodes) {
      final p = Offset(n.px, n.py);
      final r = n.radius * k;
      if (n.isSeed) {
        canvas
          ..drawCircle(
            p,
            r + 9 * k,
            Paint()..color = n.color.withValues(alpha: 0.20),
          )
          ..drawCircle(
            p,
            r + 3 * k,
            Paint()
              ..color = const Color(0xBFFFFFFF)
              ..strokeWidth = 1.8 * k
              ..style = PaintingStyle.stroke,
          );
      }
      if (n.walkStep > 0) {
        canvas.drawCircle(
          p,
          r + 2.5 * k,
          Paint()
            ..color = kNetWalkColor.withValues(alpha: 0.9)
            ..strokeWidth = 1.8 * k
            ..style = PaintingStyle.stroke,
        );
      }
      if (n.path == selected) {
        canvas
          ..drawCircle(
            p,
            r + 11 * k,
            Paint()..color = accent.withValues(alpha: 0.30),
          )
          ..drawCircle(
            p,
            r + 4.5 * k,
            Paint()
              ..color = accent
              ..strokeWidth = 2.4 * k
              ..style = PaintingStyle.stroke,
          );
      }
    }

    // The nodes, and the walk's step numbers (the only text on the canvas).
    for (final n in g.nodes) {
      final r = n.radius * k;
      canvas.drawCircle(Offset(n.px, n.py), r, Paint()..color = n.color);
      if (n.walkStep == 0) continue;
      final label = '${n.walkStep}';
      final sw = (label.length * 6.0 + 9) * k;
      final rect = Rect.fromLTWH(n.px - sw / 2, n.py - r - 16 * k, sw, 13 * k);
      canvas.drawRRect(
        RRect.fromRectAndRadius(rect, Radius.circular(6 * k)),
        Paint()..color = kNetWalkColor.withValues(alpha: 0.92),
      );
      final tp = TextPainter(
        text: TextSpan(
          text: label,
          style: TextStyle(
            fontSize: 8.5 * k,
            fontWeight: FontWeight.w800,
            color: Tokens.bg,
            height: 1,
          ),
        ),
        textDirection: TextDirection.ltr,
      )..layout();
      tp.paint(canvas, rect.center - Offset(tp.width / 2, tp.height / 2));
      tp.dispose();
    }
  }

  @override
  bool shouldRepaint(_NetworkPainter old) => true;
}

/// The ordered list under the graph: the walk from the selection (Graph
/// Walk) or what is queued ahead (Up Next).
class _TrackPanel extends StatelessWidget {
  const _TrackPanel({
    required this.controller,
    required this.player,
    this.onAddToPlaylist,
  });

  final NetworkController controller;
  final ValueListenable<PlayerController?> player;
  final void Function(Track)? onAddToPlaylist;

  @override
  Widget build(BuildContext context) {
    final c = controller;
    final cyan = Theme.of(context).colorScheme.primary;
    final nodes = c.graph?.walkNodes ?? const <NetNode>[];
    final live = c.mode == NetMode.live;

    Widget batch(
      String label,
      IconData icon,
      VoidCallback onTap, {
      bool primary = false,
    }) => GestureDetector(
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 7),
        decoration: BoxDecoration(
          color: primary ? cyan : cyan.withValues(alpha: 0.14),
          border: primary
              ? null
              : Border.all(color: cyan.withValues(alpha: 0.35)),
          borderRadius: BorderRadius.circular(9),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 14, color: primary ? Tokens.bg : cyan),
            const SizedBox(width: 5),
            Text(
              label,
              style: TextStyle(
                fontSize: 11,
                fontWeight: FontWeight.w800,
                color: primary ? Tokens.bg : cyan,
              ),
            ),
          ],
        ),
      ),
    );

    final header = Padding(
      padding: const EdgeInsets.fromLTRB(4, 6, 6, 8),
      child: Row(
        children: [
          Tooltip(
            message: c.collapsed ? 'Show graph' : 'Hide graph — focus list',
            child: InkWell(
              borderRadius: BorderRadius.circular(8),
              onTap: c.toggleCollapsed,
              child: Padding(
                padding: const EdgeInsets.all(2),
                child: Icon(
                  c.collapsed
                      ? Icons.keyboard_arrow_up_rounded
                      : Icons.keyboard_arrow_down_rounded,
                  size: 22,
                  color: cyan,
                ),
              ),
            ),
          ),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              live ? 'UP NEXT' : 'GRAPH WALK',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: 11,
                fontWeight: FontWeight.w800,
                color: live ? kNetWalkColor : cyan,
              ),
            ),
          ),
          const SizedBox(width: 6),
          _ParamChip(
            options: kNetWalkLengths,
            current: c.walkLength,
            label: (o) => '$o',
            item: (o) => '$o tracks',
            tooltip: 'How many tracks to show',
            accent: kNetWalkColor,
            leading: Icons.route_rounded,
            onPick: c.setWalkLength,
          ),
          // The live list is already queued: Play and Next would only add
          // duplicates of what is about to play.
          if (nodes.isNotEmpty && !live) ...[
            const SizedBox(width: 6),
            batch('Play', Icons.play_arrow_rounded, c.playAll, primary: true),
            const SizedBox(width: 6),
            batch('Next', Icons.queue_play_next_rounded, c.addAll),
          ],
        ],
      ),
    );

    return Column(
      children: [
        header,
        Expanded(
          child: nodes.isEmpty
              ? Padding(
                  padding: const EdgeInsets.all(20),
                  child: Text(
                    live
                        ? 'Nothing queued ahead of this track.'
                        : 'No walk from here — this track has no eligible '
                              'neighbours.',
                    textAlign: TextAlign.center,
                    style: const TextStyle(fontSize: 11, color: Tokens.dim),
                  ),
                )
              : ValueListenableBuilder(
                  valueListenable: player,
                  builder: (context, p, _) => ListenableBuilder(
                    listenable: p ?? c,
                    builder: (context, _) => ListView.separated(
                      padding: const EdgeInsets.fromLTRB(4, 0, 4, 12),
                      itemCount: nodes.length,
                      separatorBuilder: (_, _) => const SizedBox(height: 4),
                      itemBuilder: (context, i) => _row(
                        context,
                        i,
                        nodes[i],
                        cyan,
                        p?.currentPath ?? '',
                      ),
                    ),
                  ),
                ),
        ),
      ],
    );
  }

  Widget _row(
    BuildContext context,
    int i,
    NetNode n,
    Color cyan,
    String playing,
  ) {
    final c = controller;
    final sel = n.path == c.selected;
    final now = n.path.isNotEmpty && n.path == playing;
    return InkWell(
      borderRadius: BorderRadius.circular(10),
      onTap: () => c.select(n.path),
      onLongPress: () => _menu(context, i, n, cyan),
      child: Container(
        padding: const EdgeInsets.fromLTRB(6, 5, 2, 5),
        decoration: BoxDecoration(
          color: sel
              ? cyan.withValues(alpha: 0.14)
              : now
              ? cyan.withValues(alpha: 0.07)
              : Tokens.surface,
          border: Border.all(
            color: sel
                ? cyan.withValues(alpha: 0.4)
                : now
                ? cyan.withValues(alpha: 0.16)
                : Tokens.border,
          ),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Row(
          children: [
            SizedBox(
              width: 20,
              child: Text(
                '${n.walkStep > 0 ? n.walkStep : i + 1}',
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontSize: 10,
                  fontWeight: FontWeight.w800,
                  color: sel || now ? cyan : Tokens.dim,
                ),
              ),
            ),
            const SizedBox(width: 8),
            Container(
              width: 9,
              height: 9,
              decoration: BoxDecoration(color: n.color, shape: BoxShape.circle),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    n.title.isNotEmpty ? n.title : 'Unknown',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: 12.5,
                      fontWeight: sel || now
                          ? FontWeight.w700
                          : FontWeight.w500,
                      color: now ? cyan : Tokens.text,
                    ),
                  ),
                  Text(
                    n.artist.isNotEmpty ? n.artist : 'Unknown Artist',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontSize: 10, color: Tokens.dim),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 8),
            if (now)
              Padding(
                padding: const EdgeInsets.all(10),
                child: Icon(Icons.graphic_eq_rounded, size: 16, color: cyan),
              )
            else
              IconButton(
                icon: const Icon(Icons.queue_play_next_rounded, size: 18),
                color: cyan,
                tooltip: 'Play next',
                visualDensity: VisualDensity.compact,
                onPressed: () => c.playNext(n),
              ),
          ],
        ),
      ),
    );
  }

  /// `_net_row_context_menu`.
  void _menu(BuildContext context, int index, NetNode n, Color cyan) {
    final c = controller;
    c.haptics.trigger('long_press');
    final add = onAddToPlaylist;
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: Tokens.surface,
      showDragHandle: true,
      builder: (sheet) {
        Widget tile(
          IconData icon,
          String label,
          VoidCallback onTap, {
          Color? color,
        }) => ListTile(
          leading: Icon(icon, color: color ?? cyan),
          title: Text(label),
          onTap: () {
            Navigator.pop(sheet);
            onTap();
          },
        );
        return SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              ListTile(
                title: Text(
                  n.title.isNotEmpty ? n.title : 'this track',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    color: cyan,
                    fontWeight: FontWeight.w800,
                    fontSize: 14,
                  ),
                ),
              ),
              const Divider(height: 1, color: Tokens.border),
              tile(
                Icons.play_arrow_rounded,
                'Play from here',
                () => c.playFrom(index, n),
              ),
              tile(
                Icons.center_focus_weak_rounded,
                'Reseed graph here',
                () => c.reseedAt(n.path),
              ),
              tile(
                Icons.shuffle_rounded,
                'Start a walk here',
                () => c.walkHere(n.path),
              ),
              if (add != null)
                tile(
                  Icons.playlist_add_rounded,
                  'Add to Playlist',
                  () => add(nodeTrack(n)),
                  color: Tokens.libPlaylist,
                ),
            ],
          ),
        );
      },
    );
  }
}

/// The frame of the setup and analysing cards.
class _Card extends StatelessWidget {
  const _Card({required this.icon, required this.title, required this.body});

  final IconData icon;
  final String title;
  final List<Widget> body;

  @override
  Widget build(BuildContext context) {
    final cyan = Theme.of(context).colorScheme.primary;
    return Center(
      child: Container(
        margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 24),
        padding: const EdgeInsets.all(32),
        decoration: BoxDecoration(
          color: const Color(0x0DFFFFFF),
          border: Border.all(color: Tokens.text.withValues(alpha: 0.1)),
          borderRadius: BorderRadius.circular(16),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(
                color: cyan.withValues(alpha: 0.1),
                borderRadius: BorderRadius.circular(20),
              ),
              child: Icon(icon, color: cyan, size: 40),
            ),
            const SizedBox(height: 12),
            Text(
              title,
              textAlign: TextAlign.center,
              style: const TextStyle(
                fontSize: 18,
                fontWeight: FontWeight.w700,
                color: Tokens.text,
              ),
            ),
            for (final w in body) ...[const SizedBox(height: 12), w],
          ],
        ),
      ),
    );
  }
}

/// library.py's "Computing Acoustic Features" card, while the on-phone
/// analysis runs.
class _AnalysingCard extends StatelessWidget {
  const _AnalysingCard({required this.analysis});

  final DspAnalysis analysis;

  @override
  Widget build(BuildContext context) {
    final cyan = Theme.of(context).colorScheme.primary;
    final p = analysis.progress;
    return _Card(
      icon: Icons.graphic_eq_rounded,
      title: 'Computing Acoustic Features',
      body: [
        Text(
          analysis.status,
          textAlign: TextAlign.center,
          maxLines: 3,
          style: const TextStyle(fontSize: 13, color: Tokens.dim),
        ),
        if (p != null)
          SizedBox(
            width: 200,
            child: LinearProgressIndicator(
              value: p,
              color: cyan,
              backgroundColor: Tokens.surface2,
            ),
          )
        else
          CircularProgressIndicator(color: cyan),
        if (p != null)
          TextButton(onPressed: analysis.cancel, child: const Text('Cancel')),
      ],
    );
  }
}

/// No track has coordinates yet; its button starts the on-phone analysis
/// (null for a read-only library).
class _SetupCard extends StatelessWidget {
  const _SetupCard({this.onCompute});

  final VoidCallback? onCompute;

  @override
  Widget build(BuildContext context) {
    final cyan = Theme.of(context).colorScheme.primary;
    return _Card(
      icon: Icons.hub_rounded,
      title: 'Acoustic Network Graph',
      body: [
        const Text(
          'No PCA coordinates found. Please analyze your library first '
          'to construct the network.',
          textAlign: TextAlign.center,
          maxLines: 3,
          style: TextStyle(fontSize: 13, color: Tokens.dim),
        ),
        if (onCompute != null)
          FilledButton.icon(
            onPressed: onCompute,
            icon: const Icon(Icons.play_arrow_rounded),
            label: const Text('Compute DSP Features'),
            style: FilledButton.styleFrom(
              backgroundColor: cyan,
              foregroundColor: Tokens.bg,
            ),
          ),
      ],
    );
  }
}

class _EmptyGraph extends StatelessWidget {
  const _EmptyGraph();

  @override
  Widget build(BuildContext context) {
    final cyan = Theme.of(context).colorScheme.primary;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(40),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.hub_rounded,
              color: cyan.withValues(alpha: 0.4),
              size: 36,
            ),
            const SizedBox(height: 8),
            const Text(
              'Play a track to seed the network.',
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: 13, color: Tokens.dim),
            ),
          ],
        ),
      ),
    );
  }
}
