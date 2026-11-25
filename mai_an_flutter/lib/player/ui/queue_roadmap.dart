import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../data/library_repository.dart';
import '../../library/genre_colors.dart';
import '../../library/library_controller.dart';
import '../../library/network_layout.dart';
import '../../ui/tokens.dart';
import '../player_controller.dart';
import '../queue_entry.dart';

/// Live Queue Roadmap: plots Now Playing at the center, the real upcoming queue
/// as a numbered path, immediate neighbours as individual nodes, and — as the
/// user drags the Distance slider outward — distant pockets of the library as
/// cluster bubbles. Tapping any node or cluster previews where reseeding from
/// it leads (via the existing in-memory station walk) without altering AutoPlay
/// or Drift.
class QueueRoadmap extends StatefulWidget {
  const QueueRoadmap({super.key, required this.player});

  final PlayerController player;

  @override
  State<QueueRoadmap> createState() => _QueueRoadmapState();
}

class _QueueRoadmapState extends State<QueueRoadmap>
    with TickerProviderStateMixin {
  static const _closeNeighborLimit = 16;
  static const _maxQueueSteps = 12;
  static const _maxClusters = 10;
  static const _previewSteps = 5;

  final _transform = TransformationController();
  late final _resetAnimCtrl = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 220),
  );
  Animation<Matrix4>? _resetAnim;

  late final _pulseCtrl = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 700),
  );

  /// 0.0 = Close (only immediate neighbours), 1.0 = Far (full library clusters).
  double _distance = 0.35;

  bool _loading = true;
  bool _hasPca = false;
  List<NetworkTrackRow> _allPca = const [];
  Map<String, NetworkTrackRow> _pcaByPath = const {};
  List<NeighborRow> _neighbors = const [];
  String _loadedForPath = '';
  int _loadToken = 0;

  /// Selected target on the map (either a track node or a distant cluster).
  String? _selectedPath;
  _RoadCluster? _selectedCluster;

  /// Preview walk computed on demand when tapping an off-queue node/cluster.
  List<String> _previewPaths = const [];
  int _previewToken = 0;

  /// Cached layout for hit testing.
  _RoadmapLayout? _layout;
  Object? _pressedTarget;

  LibraryController? get _library {
    final lib = widget.player.autoplay?.library;
    return lib is LibraryController ? lib : null;
  }

  @override
  void initState() {
    super.initState();
    _resetAnimCtrl.addListener(() {
      if (_resetAnim case final a?) _transform.value = a.value;
    });
    widget.player.addListener(_onPlayerChanged);
    _syncPulse();
    unawaited(_loadData());
  }

  @override
  void dispose() {
    widget.player.removeListener(_onPlayerChanged);
    _resetAnimCtrl.dispose();
    _pulseCtrl.dispose();
    _transform.dispose();
    super.dispose();
  }

  void _syncPulse() {
    final playing = widget.player.playing;
    if (playing && !_pulseCtrl.isAnimating) {
      _pulseCtrl.repeat(reverse: true);
    } else if (!playing && _pulseCtrl.isAnimating) {
      _pulseCtrl
        ..stop()
        ..value = 0;
    }
  }

  void _onPlayerChanged() {
    if (!mounted) return;
    _syncPulse();
    final cur = widget.player.currentPath;
    if (cur != _loadedForPath) {
      _selectedPath = null;
      _selectedCluster = null;
      _previewPaths = const [];
      unawaited(_loadNeighbors(cur));
    } else {
      setState(() {});
    }
  }

  Future<void> _loadData() async {
    final repo = _library?.repository;
    if (repo == null) {
      if (mounted) setState(() => _loading = false);
      return;
    }
    final token = ++_loadToken;
    try {
      final hasPca = await repo.hasPcaCoords();
      if (!mounted || token != _loadToken) return;
      if (!hasPca) {
        setState(() {
          _hasPca = false;
          _loading = false;
        });
        return;
      }
      final all = await repo.allPcaCoords();
      if (!mounted || token != _loadToken) return;
      final placeable = [
        for (final r in all)
          if (r.placeable) r,
      ];
      _allPca = placeable;
      _pcaByPath = {for (final r in placeable) r.path: r};
      _hasPca = placeable.isNotEmpty;
      await _loadNeighbors(widget.player.currentPath, token: token);
    } catch (e) {
      debugPrint('QueueRoadmap: load failed: $e');
      if (mounted && token == _loadToken) setState(() => _loading = false);
    }
  }

  Future<void> _loadNeighbors(String path, {int? token}) async {
    final repo = _library?.repository;
    if (repo == null) return;
    final t = token ?? ++_loadToken;
    try {
      final neighbors = path.isEmpty
          ? const <NeighborRow>[]
          : await repo.networkNeighbors(path, _closeNeighborLimit);
      if (!mounted || t != _loadToken) return;
      setState(() {
        _loadedForPath = path;
        _neighbors = neighbors;
        _loading = false;
      });
    } catch (e) {
      debugPrint('QueueRoadmap: neighbors failed: $e');
      if (mounted && t == _loadToken) setState(() => _loading = false);
    }
  }

  void _fit() {
    _resetAnim = Matrix4Tween(
      begin: _transform.value.clone(),
      end: Matrix4.identity(),
    ).animate(CurvedAnimation(parent: _resetAnimCtrl, curve: Curves.easeOut));
    _resetAnimCtrl.forward(from: 0);
  }

  double get _invScale =>
      1.0 / _transform.value.getMaxScaleOnAxis().clamp(0.5, 6.0);

  Object? _hitTest(Offset p) {
    final layout = _layout;
    if (layout == null) return null;
    final k = _invScale;
    Object? best;
    var bestDist = double.infinity;
    for (final n in layout.nodes) {
      final d = (p - Offset(n.px, n.py)).distance;
      if (d <= (n.radius + 14) * k && d < bestDist) {
        best = n;
        bestDist = d;
      }
    }
    for (final c in layout.clusters) {
      final d = (p - Offset(c.px, c.py)).distance;
      if (d <= (c.radius + 16) * k && d < bestDist) {
        best = c;
        bestDist = d;
      }
    }
    return best;
  }

  void _selectTarget(Object target) {
    if (target is _RoadNode) {
      setState(() {
        _selectedPath = target.path;
        _selectedCluster = null;
        _previewPaths = const [];
      });
      if (!target.isCurrent && target.queueStep == 0) {
        unawaited(_computePreview(target.path));
      }
    } else if (target is _RoadCluster) {
      setState(() {
        _selectedCluster = target;
        _selectedPath = null;
        _previewPaths = const [];
      });
      unawaited(_computePreview(target.representative.path));
    }
  }

  Future<void> _computePreview(String seedPath) async {
    final lib = _library;
    if (lib == null || seedPath.isEmpty) return;
    final token = ++_previewToken;
    try {
      final stations = await lib.stations();
      final seq = stations?.walk(seedPath, length: _previewSteps) ?? const [];
      if (!mounted || token != _previewToken) return;
      setState(() => _previewPaths = seq);
    } catch (e) {
      debugPrint('QueueRoadmap: preview walk failed: $e');
    }
  }

  Track _rowToTrack(NetworkTrackRow r) => Track(
    id: 0,
    path: r.path,
    title: (r.title != null && r.title!.isNotEmpty)
        ? r.title!
        : r.path.split('/').last,
    artist: (r.artist != null && r.artist!.isNotEmpty)
        ? r.artist!
        : 'Unknown Artist',
    album: r.album ?? '',
    duration: r.duration ?? 0,
    format: '',
  );

  void _playNext(NetworkTrackRow r) {
    widget.player.queueNext(_rowToTrack(r));
    setState(() {
      _selectedPath = null;
      _selectedCluster = null;
      _previewPaths = const [];
    });
  }

  void _reseedHere(NetworkTrackRow r) {
    final track = _rowToTrack(r);
    final ap = widget.player.autoplay;
    if (ap != null) {
      ap.startRadio(track);
    } else {
      widget.player.playBlock([track], replace: true);
    }
    setState(() {
      _selectedPath = null;
      _selectedCluster = null;
      _previewPaths = const [];
    });
  }

  void _playQueuedNow(QueueEntry entry) {
    final idx = widget.player.queue.indexOfRow(entry);
    if (idx != null) widget.player.playTrackAt(idx);
    setState(() {
      _selectedPath = null;
      _selectedCluster = null;
    });
  }

  void _removeQueued(QueueEntry entry) {
    widget.player.removeRow(entry);
    setState(() {
      _selectedPath = null;
      _selectedCluster = null;
    });
  }

  @override
  Widget build(BuildContext context) {
    final lib = _library;
    final accent = Theme.of(context).colorScheme.primary;

    if (lib != null) {
      return ListenableBuilder(
        listenable: Listenable.merge([
          widget.player,
          ?widget.player.autoplay,
          lib.analysis,
        ]),
        builder: (context, _) {
          if (lib.analysis.running) {
            return _RoadmapStatusCard(
              icon: Icons.graphic_eq_rounded,
              title: 'Computing Acoustic Features',
              subtitle: lib.analysis.status.isEmpty
                  ? 'Analysing library…'
                  : lib.analysis.status,
              progress: lib.analysis.progress,
            );
          }
          return _buildBody(context, accent);
        },
      );
    }
    return _buildBody(context, accent);
  }

  Widget _buildBody(BuildContext context, Color accent) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (!_hasPca) {
      final lib = _library;
      return _RoadmapStatusCard(
        icon: Icons.hub_rounded,
        title: 'Acoustic Roadmap Unavailable',
        subtitle:
            'Analyse your library in Audio & DSP settings to map tracks and steer your queue.',
        actionLabel: (lib != null && lib.canWrite) ? 'Analyse Library' : null,
        onAction: lib?.analyseLibrary,
      );
    }

    final currentPath = widget.player.currentPath;
    if (currentPath.isEmpty || !_pcaByPath.containsKey(currentPath)) {
      return const _RoadmapStatusCard(
        icon: Icons.hub_rounded,
        title: 'Nothing Mapped Yet',
        subtitle:
            'Play an analysed track to anchor your Queue Roadmap and explore nearby clusters.',
      );
    }

    final upcomingEntries = widget.player.queue.isEmpty
        ? const <(int, QueueEntry)>[]
        : widget.player.queue
              .upcoming(widget.player.shuffleIndices)
              .skip(1)
              .take(_maxQueueSteps)
              .toList();

    return Column(
      children: [
        Expanded(
          child: LayoutBuilder(
            builder: (context, box) {
              final w = math.max(200.0, box.maxWidth);
              final h = math.max(200.0, box.maxHeight);
              final layout = _buildLayout(
                width: w,
                height: h,
                currentPath: currentPath,
                upcoming: upcomingEntries,
              );
              _layout = layout;
              final currentNode = layout.byPath[currentPath];
              return Stack(
                fit: StackFit.expand,
                children: [
                  Container(
                    margin: const EdgeInsets.fromLTRB(12, 8, 12, 6),
                    decoration: BoxDecoration(
                      color: Tokens.bg,
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(
                        color: accent.withValues(alpha: 0.16),
                      ),
                    ),
                    clipBehavior: Clip.antiAlias,
                    child: Stack(
                      fit: StackFit.expand,
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
                                  behavior: HitTestBehavior.opaque,
                                  onTapDown: (d) =>
                                      _pressedTarget = _hitTest(d.localPosition),
                                  onTap: () {
                                    final hit = _pressedTarget;
                                    if (hit != null) {
                                      _selectTarget(hit);
                                    } else {
                                      setState(() {
                                        _selectedPath = null;
                                        _selectedCluster = null;
                                        _previewPaths = const [];
                                      });
                                    }
                                  },
                                  child: CustomPaint(
                                    size: Size(w, h),
                                    painter: _RoadmapPainter(
                                      layout: layout,
                                      selectedPath: _selectedPath,
                                      selectedClusterKey: _selectedCluster?.key,
                                      accent: accent,
                                      transform: _transform,
                                    ),
                                  ),
                                ),
                                if (currentNode != null)
                                  ListenableBuilder(
                                    listenable: _transform,
                                    builder: (context, _) {
                                      final k = _invScale;
                                      final d =
                                          (currentNode.radius + 9) * 2 * k;
                                      return Positioned(
                                        left: currentNode.px - d / 2,
                                        top: currentNode.py - d / 2,
                                        width: d,
                                        height: d,
                                        child: IgnorePointer(
                                          child: AnimatedBuilder(
                                            animation: _pulseCtrl,
                                            builder: (context, child) {
                                              final t = Curves.easeInOut
                                                  .transform(_pulseCtrl.value);
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
                                                border: Border.all(
                                                  color: accent,
                                                  width: 2,
                                                ),
                                              ),
                                            ),
                                          ),
                                        ),
                                      );
                                    },
                                  ),
                              ],
                            ),
                          ),
                        ),
                        // Top-right fit button & cluster count pill
                        Positioned(
                          top: 10,
                          left: 10,
                          right: 10,
                          child: Row(
                            children: [
                              Container(
                                padding: const EdgeInsets.symmetric(
                                  horizontal: 10,
                                  vertical: 5,
                                ),
                                decoration: BoxDecoration(
                                  color: Tokens.surface2.withValues(alpha: 0.88),
                                  borderRadius: BorderRadius.circular(10),
                                  border: Border.all(
                                    color: Tokens.borderSubtle,
                                  ),
                                ),
                                child: Row(
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    Container(
                                      width: 7,
                                      height: 7,
                                      decoration: const BoxDecoration(
                                        color: kNetWalkColor,
                                        shape: BoxShape.circle,
                                      ),
                                    ),
                                    const SizedBox(width: 6),
                                    Text(
                                      '${upcomingEntries.length} up next'
                                      '${layout.clusters.isNotEmpty ? ' · ${layout.clusters.length} clusters' : ''}',
                                      style: const TextStyle(
                                        color: Tokens.text,
                                        fontSize: 11,
                                        fontWeight: FontWeight.w600,
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                              const Spacer(),
                              Tooltip(
                                message: 'Reset zoom',
                                child: Material(
                                  color: Tokens.surface2.withValues(alpha: 0.88),
                                  borderRadius: BorderRadius.circular(10),
                                  child: InkWell(
                                    borderRadius: BorderRadius.circular(10),
                                    onTap: _fit,
                                    child: Container(
                                      padding: const EdgeInsets.all(7),
                                      decoration: BoxDecoration(
                                        borderRadius: BorderRadius.circular(10),
                                        border: Border.all(
                                          color: accent.withValues(alpha: 0.25),
                                        ),
                                      ),
                                      child: Icon(
                                        Icons.filter_center_focus_rounded,
                                        size: 16,
                                        color: accent,
                                      ),
                                    ),
                                  ),
                                ),
                              ),
                            ],
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              );
            },
          ),
        ),
        // Bottom steering readout or hint
        _buildReadout(context, accent, upcomingEntries),
        // Distance slider bar
        _buildDistanceBar(context, accent),
      ],
    );
  }

  Widget _buildReadout(
    BuildContext context,
    Color accent,
    List<(int, QueueEntry)> upcoming,
  ) {
    final cluster = _selectedCluster;
    final selPath = _selectedPath;

    if (cluster != null) {
      final rep = cluster.representative;
      final repTitle = (rep.title != null && rep.title!.isNotEmpty)
          ? rep.title!
          : rep.path.split('/').last;
      final repArtist = (rep.artist != null && rep.artist!.isNotEmpty)
          ? rep.artist!
          : 'Unknown Artist';
      return _ReadoutFrame(
        accent: cluster.color,
        leading: Container(
          width: 12,
          height: 12,
          decoration: BoxDecoration(
            color: cluster.color,
            shape: BoxShape.circle,
          ),
        ),
        title: '${cluster.label} · ${cluster.count} tracks',
        subtitle: 'Seed: $repTitle — $repArtist',
        actions: [
          _ActionPill(
            icon: Icons.queue_play_next_rounded,
            label: 'Next',
            color: accent,
            onTap: () => _playNext(rep),
          ),
          const SizedBox(width: 6),
          _ActionPill(
            icon: Icons.alt_route_rounded,
            label: 'Reseed',
            color: kNetWalkColor,
            filled: true,
            onTap: () => _reseedHere(rep),
          ),
        ],
      );
    }

    if (selPath != null) {
      final node = _layout?.byPath[selPath];
      final row = _pcaByPath[selPath];
      if (node != null && row != null) {
        final queueMatch = upcoming
            .where((e) => e.$2.path == selPath)
            .firstOrNull;
        final role = node.isCurrent
            ? 'Now Playing'
            : node.queueStep > 0
            ? 'Up Next #${node.queueStep}'
            : 'Nearby Track';
        return _ReadoutFrame(
          accent: node.isCurrent
              ? accent
              : node.queueStep > 0
              ? kNetWalkColor
              : node.color,
          leading: Container(
            width: 10,
            height: 10,
            decoration: BoxDecoration(
              color: node.color,
              shape: BoxShape.circle,
            ),
          ),
          title: node.title,
          subtitle: '${node.artist} · $role',
          actions: [
            if (node.isCurrent)
              const SizedBox.shrink()
            else if (queueMatch != null) ...[
              _ActionPill(
                icon: Icons.play_arrow_rounded,
                label: 'Play',
                color: accent,
                filled: true,
                onTap: () => _playQueuedNow(queueMatch.$2),
              ),
              const SizedBox(width: 6),
              _ActionPill(
                icon: Icons.close_rounded,
                label: 'Remove',
                color: Tokens.accentRed,
                onTap: () => _removeQueued(queueMatch.$2),
              ),
            ] else ...[
              _ActionPill(
                icon: Icons.queue_play_next_rounded,
                label: 'Next',
                color: accent,
                onTap: () => _playNext(row),
              ),
              const SizedBox(width: 6),
              _ActionPill(
                icon: Icons.alt_route_rounded,
                label: 'Reseed',
                color: kNetWalkColor,
                filled: true,
                onTap: () => _reseedHere(row),
              ),
            ],
          ],
        );
      }
    }

    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 4),
      child: Row(
        children: [
          Icon(
            Icons.touch_app_rounded,
            size: 14,
            color: Tokens.dim.withValues(alpha: 0.8),
          ),
          const SizedBox(width: 6),
          const Expanded(
            child: Text(
              'Tap any nearby track or distant cluster to preview & reseed',
              style: TextStyle(color: Tokens.dim, fontSize: 11.5),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildDistanceBar(BuildContext context, Color accent) {
    final label = _distance < 0.15
        ? 'Close'
        : _distance < 0.65
        ? 'Medium'
        : 'Wide';
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 2, 16, 12),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
          decoration: BoxDecoration(
            color: Tokens.surface2,
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: Tokens.borderSubtle),
          ),
          child: Row(
            children: [
              Icon(Icons.radar_rounded, size: 16, color: accent),
              const SizedBox(width: 8),
              const Text(
                'Distance',
                style: TextStyle(
                  color: Tokens.text,
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(width: 6),
              Expanded(
                child: SliderTheme(
                  data: SliderTheme.of(context).copyWith(
                    activeTrackColor: accent,
                    inactiveTrackColor: Tokens.border,
                    thumbColor: Tokens.text,
                    trackHeight: 3,
                    overlayShape: const RoundSliderOverlayShape(
                      overlayRadius: 14,
                    ),
                    thumbShape: const RoundSliderThumbShape(
                      enabledThumbRadius: 7,
                    ),
                  ),
                  child: Slider(
                    value: _distance,
                    onChanged: (v) => setState(() {
                      _distance = v;
                      // Keep selected cluster valid if it disappears at closer range.
                      if (_distance <= 0.02) {
                        _selectedCluster = null;
                        if (_selectedPath == null) _previewPaths = const [];
                      }
                    }),
                  ),
                ),
              ),
              SizedBox(
                width: 48,
                child: Text(
                  label,
                  textAlign: TextAlign.right,
                  style: TextStyle(
                    color: accent,
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  /// Builds the live roadmap projection in-memory (fast enough for 60fps
  /// slider drags).
  _RoadmapLayout _buildLayout({
    required double width,
    required double height,
    required String currentPath,
    required List<(int, QueueEntry)> upcoming,
  }) {
    final centerRow = _pcaByPath[currentPath]!;
    final fcx = centerRow.coords![0];
    final fcy = centerRow.coords![1];

    final nodes = <_RoadNode>[];
    final byPath = <String, _RoadNode>{};
    final localEdges = <(String, String, double)>[];

    void addNode(
      NetworkTrackRow r, {
      bool isCurrent = false,
      int queueStep = 0,
      double baseRadius = 8.0,
    }) {
      if (byPath.containsKey(r.path)) {
        if (queueStep > 0) byPath[r.path]!.queueStep = queueStep;
        return;
      }
      final nd = _RoadNode(
        path: r.path,
        rx: r.coords![0],
        ry: r.coords![1],
        title: (r.title != null && r.title!.isNotEmpty)
            ? r.title!
            : r.path.split('/').last,
        artist: (r.artist != null && r.artist!.isNotEmpty)
            ? r.artist!
            : 'Unknown Artist',
        color: genreColor(r.genre),
        radius: netNodeRadius(baseRadius, r.playCount),
        isCurrent: isCurrent,
        queueStep: queueStep,
      );
      nodes.add(nd);
      byPath[r.path] = nd;
    }

    // 1. Center: Now Playing
    addNode(centerRow, isCurrent: true, baseRadius: 13.0);

    // 2. Real upcoming queue trail
    final queuePath = <String>[currentPath];
    for (var i = 0; i < upcoming.length; i++) {
      final p = upcoming[i].$2.path;
      final r = _pcaByPath[p];
      if (r == null) continue;
      addNode(r, queueStep: i + 1, baseRadius: 9.0);
      queuePath.add(p);
    }

    // 3. Immediate neighbours (normal individual nodes)
    var closeMaxDist = 0.0;
    for (final nb in _neighbors) {
      final r = _pcaByPath[nb.path];
      if (r == null) continue;
      addNode(r, baseRadius: 7.5);
      localEdges.add((currentPath, nb.path, nb.weight));
      final d = _dist(r.coords![0] - fcx, r.coords![1] - fcy);
      if (d > closeMaxDist) closeMaxDist = d;
    }

    // 4. Distant clusters (controlled by _distance slider)
    final clusters = <_RoadCluster>[];
    if (_distance > 0.02 && _allPca.length > byPath.length) {
      final outside = <(NetworkTrackRow, double)>[];
      for (final r in _allPca) {
        if (byPath.containsKey(r.path)) continue;
        final d = _dist(r.coords![0] - fcx, r.coords![1] - fcy);
        outside.add((r, d));
      }
      if (outside.isNotEmpty) {
        outside.sort((a, b) => a.$2.compareTo(b.$2));
        final p95 = outside[(outside.length * 0.95).floor().clamp(
          0,
          outside.length - 1,
        )].$2;
        final minSpan = closeMaxDist > 0
            ? closeMaxDist * 0.85
            : outside.first.$2;
        final visibleRadius = minSpan + _distance * math.max(0.0, p95 - minSpan);

        final grouped = <String, List<NetworkTrackRow>>{};
        final labels = <String, String>{};
        for (final (r, d) in outside) {
          if (d > visibleRadius) break;
          final (gKey, gLabel) = genreGroup(r.genre);
          final key = gKey.isNotEmpty
              ? gKey
              : (r.clusterId != null ? 'c_${r.clusterId}' : 'other');
          final label = gLabel.isNotEmpty
              ? gLabel
              : (r.clusterId != null ? 'Cluster ${r.clusterId}' : 'Other');
          (grouped[key] ??= []).add(r);
          labels[key] = label;
        }

        final entries = grouped.entries.toList()
          ..sort((a, b) => b.value.length.compareTo(a.value.length));
        for (final e in entries.take(_maxClusters)) {
          final members = e.value;
          if (members.isEmpty) continue;
          var sx = 0.0, sy = 0.0;
          for (final m in members) {
            sx += m.coords![0];
            sy += m.coords![1];
          }
          final cx = sx / members.length;
          final cy = sy / members.length;
          // Representative track: closest to centroid.
          NetworkTrackRow rep = members.first;
          var bestD = double.infinity;
          for (final m in members) {
            final d = _dist(m.coords![0] - cx, m.coords![1] - cy);
            if (d < bestD) {
              bestD = d;
              rep = m;
            }
          }
          clusters.add(
            _RoadCluster(
              key: e.key,
              label: labels[e.key]!,
              count: members.length,
              rx: cx,
              ry: cy,
              color: genreColor(rep.genre),
              radius: 12.0 + math.min(8.0, math.sqrt(members.length) * 2.0),
              representative: rep,
            ),
          );
        }
      }
    }

    // 5. Uniform focal-centred projection framing all visible nodes + clusters
    var halfSpan = 0.0;
    for (final n in nodes) {
      halfSpan = math.max(halfSpan, (n.rx - fcx).abs());
      halfSpan = math.max(halfSpan, (n.ry - fcy).abs());
    }
    for (final c in clusters) {
      halfSpan = math.max(halfSpan, (c.rx - fcx).abs());
      halfSpan = math.max(halfSpan, (c.ry - fcy).abs());
    }
    if (halfSpan < 1e-6) halfSpan = 1.0;

    const pad = 46.0;
    final usable = math.max(40.0, math.min(width, height) / 2 - pad);
    final scale = usable / halfSpan;

    Offset project(double rx, double ry) =>
        Offset(width / 2 + (rx - fcx) * scale, height / 2 + (ry - fcy) * scale);

    for (final n in nodes) {
      final p = project(n.rx, n.ry);
      n.px = p.dx;
      n.py = p.dy;
    }
    for (final c in clusters) {
      final p = project(c.rx, c.ry);
      c.px = p.dx;
      c.py = p.dy;
    }

    // 6. Preview branch coordinates (reseed preview for tapped node or cluster)
    final previewPoints = <Offset>[];
    if (_previewPaths.isNotEmpty) {
      Offset? origin;
      if (_selectedCluster != null) {
        final c = clusters
            .where((x) => x.key == _selectedCluster!.key)
            .firstOrNull;
        if (c != null) origin = Offset(c.px, c.py);
      } else if (_selectedPath != null) {
        final n = byPath[_selectedPath!];
        if (n != null) origin = Offset(n.px, n.py);
      }
      if (origin != null) {
        previewPoints.add(origin);
        for (final p in _previewPaths) {
          final r = _pcaByPath[p];
          if (r == null) continue;
          previewPoints.add(project(r.coords![0], r.coords![1]));
        }
      }
    }

    return _RoadmapLayout(
      nodes: nodes,
      byPath: byPath,
      clusters: clusters,
      localEdges: localEdges,
      queuePath: queuePath,
      previewPoints: previewPoints,
    );
  }

  static double _dist(double dx, double dy) => math.sqrt(dx * dx + dy * dy);
}

class _RoadNode {
  _RoadNode({
    required this.path,
    required this.rx,
    required this.ry,
    required this.title,
    required this.artist,
    required this.color,
    required this.radius,
    required this.isCurrent,
    required this.queueStep,
  });

  final String path;
  final double rx, ry;
  double px = 0, py = 0;
  final String title;
  final String artist;
  final Color color;
  final double radius;
  final bool isCurrent;
  int queueStep;
}

class _RoadCluster {
  _RoadCluster({
    required this.key,
    required this.label,
    required this.count,
    required this.rx,
    required this.ry,
    required this.color,
    required this.radius,
    required this.representative,
  });

  final String key;
  final String label;
  final int count;
  final double rx, ry;
  double px = 0, py = 0;
  final Color color;
  final double radius;
  final NetworkTrackRow representative;
}

class _RoadmapLayout {
  const _RoadmapLayout({
    required this.nodes,
    required this.byPath,
    required this.clusters,
    required this.localEdges,
    required this.queuePath,
    required this.previewPoints,
  });

  final List<_RoadNode> nodes;
  final Map<String, _RoadNode> byPath;
  final List<_RoadCluster> clusters;
  final List<(String, String, double)> localEdges;
  final List<String> queuePath;
  final List<Offset> previewPoints;
}

class _RoadmapPainter extends CustomPainter {
  _RoadmapPainter({
    required this.layout,
    required this.selectedPath,
    required this.selectedClusterKey,
    required this.accent,
    required this.transform,
  }) : super(repaint: transform);

  final _RoadmapLayout layout;
  final String? selectedPath;
  final String? selectedClusterKey;
  final Color accent;
  final TransformationController transform;

  @override
  void paint(Canvas canvas, Size size) {
    final k = 1.0 / transform.value.getMaxScaleOnAxis().clamp(0.5, 6.0);
    canvas.drawRect(Offset.zero & size, Paint()..color = Tokens.bg);

    final center = layout.nodes.where((n) => n.isCurrent).firstOrNull;

    // 1. Faint radial tethers to distant clusters
    if (center != null) {
      final cp = Offset(center.px, center.py);
      for (final c in layout.clusters) {
        final isSel = c.key == selectedClusterKey;
        _drawDashedLine(
          canvas,
          cp,
          Offset(c.px, c.py),
          Paint()
            ..color = (isSel ? accent : c.color).withValues(
              alpha: isSel ? 0.45 : 0.16,
            )
            ..strokeWidth = (isSel ? 1.5 : 1.0) * k
            ..style = PaintingStyle.stroke,
          dash: 5 * k,
          gap: 5 * k,
        );
      }
    }

    // 2. Immediate neighbourhood edges
    for (final (src, dst, weight) in layout.localEdges) {
      final a = layout.byPath[src], b = layout.byPath[dst];
      if (a == null || b == null) continue;
      final w = weight.clamp(0.0, 1.0);
      canvas.drawLine(
        Offset(a.px, a.py),
        Offset(b.px, b.py),
        Paint()
          ..color = kNetEdgeColor.withValues(alpha: 0.45 + 0.55 * w)
          ..strokeWidth = (0.9 + 1.4 * w) * k
          ..strokeCap = StrokeCap.round
          ..style = PaintingStyle.stroke,
      );
    }

    // 3. Preview branch (dashed, showing where reseeding from tapped target leads)
    if (layout.previewPoints.length >= 2) {
      final previewPaint = Paint()
        ..color = accent.withValues(alpha: 0.85)
        ..strokeWidth = 2.0 * k
        ..strokeCap = StrokeCap.round
        ..style = PaintingStyle.stroke;
      for (var i = 0; i < layout.previewPoints.length - 1; i++) {
        _drawDashedLine(
          canvas,
          layout.previewPoints[i],
          layout.previewPoints[i + 1],
          previewPaint,
          dash: 6 * k,
          gap: 4 * k,
        );
      }
      for (var i = 1; i < layout.previewPoints.length; i++) {
        canvas.drawCircle(
          layout.previewPoints[i],
          4.0 * k,
          Paint()..color = accent.withValues(alpha: 0.9),
        );
      }
    }

    // 4. Live queue roadmap trail (green arrows)
    final qPath = layout.queuePath;
    final nSeg = math.max(1, qPath.length - 1);
    for (var i = 0; i < qPath.length - 1; i++) {
      final a = layout.byPath[qPath[i]], b = layout.byPath[qPath[i + 1]];
      if (a == null || b == null) continue;
      final fade = math.max(0.42, 1.0 - i / nSeg * 0.55);
      final paint = Paint()
        ..color = kNetWalkColor.withValues(alpha: fade)
        ..strokeWidth = 2.4 * k
        ..strokeCap = StrokeCap.round
        ..style = PaintingStyle.stroke;
      final dx = b.px - a.px, dy = b.py - a.py;
      final len = math.sqrt(dx * dx + dy * dy);
      final l = len == 0 ? 1.0 : len;
      final ux = dx / l, uy = dy / l;
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

    // 5. Distant cluster bubbles
    for (final c in layout.clusters) {
      final p = Offset(c.px, c.py);
      final r = c.radius * k;
      final isSel = c.key == selectedClusterKey;
      canvas
        ..drawCircle(
          p,
          r + 5 * k,
          Paint()..color = c.color.withValues(alpha: isSel ? 0.28 : 0.14),
        )
        ..drawCircle(
          p,
          r,
          Paint()..color = c.color.withValues(alpha: 0.28),
        )
        ..drawCircle(
          p,
          r,
          Paint()
            ..color = isSel ? accent : c.color.withValues(alpha: 0.85)
            ..strokeWidth = (isSel ? 2.2 : 1.4) * k
            ..style = PaintingStyle.stroke,
        )
        ..drawCircle(p, 4.0 * k, Paint()..color = c.color);

      // Compact cluster pill label underneath
      final shortLabel = c.label.length > 12
          ? '${c.label.substring(0, 11)}…'
          : c.label;
      final text = '$shortLabel · ${c.count}';
      final tp = TextPainter(
        text: TextSpan(
          text: text,
          style: TextStyle(
            fontSize: 8.5 * k,
            fontWeight: FontWeight.w700,
            color: Tokens.text,
            height: 1.0,
          ),
        ),
        textDirection: TextDirection.ltr,
      )..layout();
      final pillW = tp.width + 10 * k;
      final pillH = tp.height + 5 * k;
      final rect = Rect.fromLTWH(
        p.dx - pillW / 2,
        p.dy + r + 3 * k,
        pillW,
        pillH,
      );
      canvas.drawRRect(
        RRect.fromRectAndRadius(rect, Radius.circular(6 * k)),
        Paint()..color = Tokens.surface2.withValues(alpha: 0.88),
      );
      tp.paint(canvas, rect.center - Offset(tp.width / 2, tp.height / 2));
      tp.dispose();
    }

    // 6. Individual track nodes + rings + queue step badges
    for (final n in layout.nodes) {
      final p = Offset(n.px, n.py);
      final r = n.radius * k;
      if (n.isCurrent) {
        canvas
          ..drawCircle(
            p,
            r + 8 * k,
            Paint()..color = n.color.withValues(alpha: 0.22),
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
      if (n.queueStep > 0) {
        canvas.drawCircle(
          p,
          r + 2.5 * k,
          Paint()
            ..color = kNetWalkColor.withValues(alpha: 0.9)
            ..strokeWidth = 1.8 * k
            ..style = PaintingStyle.stroke,
        );
      }
      if (n.path == selectedPath) {
        canvas
          ..drawCircle(
            p,
            r + 10 * k,
            Paint()..color = accent.withValues(alpha: 0.28),
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
      canvas.drawCircle(p, r, Paint()..color = n.color);

      if (n.queueStep > 0) {
        final label = '${n.queueStep}';
        final sw = (label.length * 6.0 + 9) * k;
        final rect = Rect.fromLTWH(
          n.px - sw / 2,
          n.py - r - 16 * k,
          sw,
          13 * k,
        );
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
  }

  static void _drawDashedLine(
    Canvas canvas,
    Offset a,
    Offset b,
    Paint paint, {
    required double dash,
    required double gap,
  }) {
    final dx = b.dx - a.dx, dy = b.dy - a.dy;
    final dist = math.sqrt(dx * dx + dy * dy);
    if (dist < 1e-3) return;
    final ux = dx / dist, uy = dy / dist;
    var pos = 0.0;
    while (pos < dist) {
      final end = math.min(pos + dash, dist);
      canvas.drawLine(
        Offset(a.dx + ux * pos, a.dy + uy * pos),
        Offset(a.dx + ux * end, a.dy + uy * end),
        paint,
      );
      pos += dash + gap;
    }
  }

  @override
  bool shouldRepaint(_RoadmapPainter old) => true;
}

class _ReadoutFrame extends StatelessWidget {
  const _ReadoutFrame({
    required this.accent,
    required this.leading,
    required this.title,
    required this.subtitle,
    required this.actions,
  });

  final Color accent;
  final Widget leading;
  final String title;
  final String subtitle;
  final List<Widget> actions;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(12, 2, 12, 6),
    child: Container(
      padding: const EdgeInsets.fromLTRB(12, 8, 10, 8),
      decoration: BoxDecoration(
        color: Tokens.surface2,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: accent.withValues(alpha: 0.35)),
      ),
      child: Row(
        children: [
          leading,
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 12.5,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 1),
                Text(
                  subtitle,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(color: Tokens.dim, fontSize: 11),
                ),
              ],
            ),
          ),
          const SizedBox(width: 8),
          ...actions,
        ],
      ),
    ),
  );
}

class _ActionPill extends StatelessWidget {
  const _ActionPill({
    required this.icon,
    required this.label,
    required this.color,
    required this.onTap,
    this.filled = false,
  });

  final IconData icon;
  final String label;
  final Color color;
  final VoidCallback onTap;
  final bool filled;

  @override
  Widget build(BuildContext context) => Material(
    color: filled ? color : color.withValues(alpha: 0.14),
    borderRadius: BorderRadius.circular(9),
    child: InkWell(
      borderRadius: BorderRadius.circular(9),
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(9),
          border: filled
              ? null
              : Border.all(color: color.withValues(alpha: 0.35)),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 14, color: filled ? Tokens.bg : color),
            const SizedBox(width: 4),
            Text(
              label,
              style: TextStyle(
                fontSize: 11,
                fontWeight: FontWeight.w800,
                color: filled ? Tokens.bg : color,
              ),
            ),
          ],
        ),
      ),
    ),
  );
}

class _RoadmapStatusCard extends StatelessWidget {
  const _RoadmapStatusCard({
    required this.icon,
    required this.title,
    required this.subtitle,
    this.progress,
    this.actionLabel,
    this.onAction,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final double? progress;
  final String? actionLabel;
  final VoidCallback? onAction;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(28),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, color: accent.withValues(alpha: 0.55), size: 40),
            const SizedBox(height: 12),
            Text(
              title,
              textAlign: TextAlign.center,
              style: const TextStyle(
                color: Tokens.text,
                fontSize: 16,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 6),
            Text(
              subtitle,
              textAlign: TextAlign.center,
              style: const TextStyle(color: Tokens.dim, fontSize: 12.5),
            ),
            if (progress != null) ...[
              const SizedBox(height: 14),
              SizedBox(
                width: 200,
                child: LinearProgressIndicator(
                  value: progress,
                  color: accent,
                  backgroundColor: Tokens.surface2,
                ),
              ),
            ],
            if (actionLabel != null && onAction != null) ...[
              const SizedBox(height: 16),
              FilledButton(
                style: FilledButton.styleFrom(
                  backgroundColor: accent,
                  foregroundColor: Tokens.bg,
                ),
                onPressed: onAction,
                child: Text(actionLabel!),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
