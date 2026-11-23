import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../data/library_repository.dart';
import '../player/player_controller.dart';
import '../player/ui/mini_player.dart';
import '../ui/tokens.dart';
import 'library_controller.dart';
import 'track_query.dart';

const _appChannel = MethodChannel('mai_an/app');

class LibraryScreen extends StatefulWidget {
  const LibraryScreen({
    super.key,
    required this.controller,
    required this.player,
  });

  final LibraryController controller;

  /// Null until the player has started (after the permission prompts).
  final ValueListenable<PlayerController?> player;

  @override
  State<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends State<LibraryScreen> {
  LibraryController get controller => widget.controller;
  StreamSubscription<String>? _messages;

  @override
  void initState() {
    super.initState();
    widget.player.addListener(_bindMessages);
    _bindMessages();
  }

  void _bindMessages() {
    _messages?.cancel();
    _messages = widget.player.value?.messages.listen(_snack);
  }

  void _snack(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(text)));
  }

  @override
  void dispose() {
    widget.player.removeListener(_bindMessages);
    _messages?.cancel();
    super.dispose();
  }

  void _play(Track t) {
    final player = widget.player.value;
    if (player == null) {
      _snack('The player is still starting.');
      return;
    }
    player.playTrack(
      t,
      context: controller.visible,
      generalView: controller.isGeneralView && !controller.closestMatch,
    );
  }

  @override
  Widget build(BuildContext context) {
    // Back on the root screen sends the app to the background (like Home)
    // instead of finishing it; playback carries on either way.
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) _appChannel.invokeMethod('moveTaskToBack').ignore();
      },
      child: Scaffold(
        appBar: AppBar(
          title: ListenableBuilder(
            listenable: controller,
            builder: (context, _) => _Header(controller: controller),
          ),
          actions: [
            ListenableBuilder(
              listenable: controller,
              builder: (context, _) => PopupMenuButton<LibrarySort>(
                icon: const Icon(Icons.sort),
                tooltip: 'Sort',
                initialValue: controller.sort,
                onSelected: controller.setSort,
                itemBuilder: (context) => [
                  for (final s in LibrarySort.values)
                    CheckedPopupMenuItem(
                      value: s,
                      checked: s == controller.sort,
                      child: Text(s.label),
                    ),
                ],
              ),
            ),
          ],
        ),
        body: Column(
          children: [
            Expanded(
              child: ListenableBuilder(
                listenable: controller,
                builder: (context, _) => switch (controller.state) {
                  LoadState.loading => const Center(
                    child: CircularProgressIndicator(),
                  ),
                  LoadState.error => _ErrorView(message: controller.error ?? ''),
                  LoadState.ready => Column(
                    children: [
                      Padding(
                        padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
                        child: TextField(
                          onChanged: controller.setQuery,
                          decoration: const InputDecoration(
                            hintText: 'Search title, artist, album',
                            prefixIcon: Icon(Icons.search, color: Tokens.dim),
                            isDense: true,
                          ),
                        ),
                      ),
                      Expanded(
                        child: ValueListenableBuilder(
                          valueListenable: widget.player,
                          builder: (context, player, _) => ListenableBuilder(
                            // Rebuilds the visible rows when the playing
                            // track changes (highlight), nothing more.
                            listenable: player ?? controller,
                            builder: (context, _) {
                              final playing = player?.stopped == false
                                  ? player?.nowPlaying?.path
                                  : null;
                              return ListView.builder(
                                // A fixed extent lets Flutter lay out only the
                                // visible rows and jump-scroll without
                                // measuring the rest.
                                itemExtent: _TrackRow.height,
                                itemCount: controller.visible.length,
                                itemBuilder: (context, i) {
                                  final t = controller.visible[i];
                                  return _TrackRow(
                                    track: t,
                                    playing: t.path == playing,
                                    onTap: () => _play(t),
                                    onLongPress: () => _trackMenu(t),
                                  );
                                },
                              );
                            },
                          ),
                        ),
                      ),
                    ],
                  ),
                },
              ),
            ),
            ValueListenableBuilder(
              valueListenable: widget.player,
              builder: (context, player, _) => player == null
                  ? const SizedBox.shrink()
                  : SafeArea(top: false, child: MiniPlayer(player: player)),
            ),
          ],
        ),
      ),
    );
  }

  void _trackMenu(Track t) {
    HapticFeedback.selectionClick();
    final player = widget.player.value;
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: Tokens.surface,
      showDragHandle: true,
      builder: (sheet) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              title: Text(t.title, maxLines: 1, overflow: TextOverflow.ellipsis),
              subtitle: Text(
                '${t.artist} · ${t.album}',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: Tokens.dim),
              ),
            ),
            const Divider(height: 1, color: Tokens.borderSubtle),
            ListTile(
              leading: const Icon(Icons.playlist_play_rounded),
              title: const Text('Play next'),
              enabled: player != null,
              onTap: () {
                Navigator.pop(sheet);
                player!.queueNext(t);
                _snack('Playing next: ${t.title}');
              },
            ),
            ListTile(
              leading: const Icon(Icons.playlist_add_rounded),
              title: const Text('Add to queue'),
              enabled: player != null,
              onTap: () {
                Navigator.pop(sheet);
                player!.queueLast(t);
                _snack('Added to queue: ${t.title}');
              },
            ),
            ListTile(
              leading: const Icon(Icons.graphic_eq_rounded),
              title: const Text('Nearest by sound'),
              onTap: () {
                Navigator.pop(sheet);
                _showSimilar(t);
              },
            ),
          ],
        ),
      ),
    );
  }

  void _showSimilar(Track seed) {
    final similar = controller.similarTo(seed);
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: Tokens.surface,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (sheet) => SizedBox(
        height: MediaQuery.sizeOf(sheet).height * 0.6,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
              child: Text(
                // A plain nearest-neighbour walk, not yet Flet's Play Similar.
                'Nearest by sound: ${seed.title}',
                style: Theme.of(sheet).textTheme.titleMedium,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
            ),
            Expanded(
              child: similar == null
                  ? Center(
                      child: Text(
                        controller.graphError != null
                            ? 'Similarity is unavailable: the acoustic graph '
                                  'could not be loaded.\n\n${controller.graphError}'
                            : controller.graph == null
                            ? 'The acoustic graph is still loading.'
                            : 'This track has no acoustic coordinates yet.\n'
                                  'Analyse it in the desktop app first.',
                        textAlign: TextAlign.center,
                        style: const TextStyle(color: Tokens.dim),
                      ),
                    )
                  : ListView.builder(
                      itemExtent: _TrackRow.height,
                      itemCount: similar.length,
                      itemBuilder: (context, i) => _TrackRow(
                        track: similar[i],
                        onTap: () {
                          Navigator.pop(sheet);
                          widget.player.value?.playTrack(
                            similar[i],
                            context: similar,
                          );
                        },
                      ),
                    ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Header extends StatelessWidget {
  const _Header({required this.controller});
  final LibraryController controller;

  @override
  Widget build(BuildContext context) {
    final c = controller;
    final parts = <String>[
      if (c.state == LoadState.ready)
        '${c.visible.length} / ${c.total} tracks'
            '${c.closestMatch ? ' (closest match)' : ''}',
      if (c.loadTime != null) 'loaded in ${c.loadTime!.inMilliseconds} ms',
      if (c.graph case final g?) 'graph ${g.length}×${g.dim}',
      if (c.graphError != null) 'no graph',
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text('Library'),
        if (parts.isNotEmpty)
          Text(
            parts.join(' · '),
            style: const TextStyle(fontSize: 12, color: Tokens.dim),
          ),
      ],
    );
  }
}

/// Deliberately light: one InkWell, one Row, three Texts.
class _TrackRow extends StatelessWidget {
  const _TrackRow({
    required this.track,
    this.playing = false,
    this.onTap,
    this.onLongPress,
  });

  static const height = 60.0;
  final Track track;
  final bool playing;
  final VoidCallback? onTap;
  final VoidCallback? onLongPress;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return InkWell(
      onTap: onTap,
      onLongPress: onLongPress,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: Row(
          children: [
            Expanded(
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    track.title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      color: playing ? accent : Tokens.text,
                      fontSize: 15,
                      fontWeight: playing ? FontWeight.w600 : null,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    '${track.artist} · ${track.album}',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: Tokens.dim, fontSize: 13),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 12),
            if (playing)
              Padding(
                padding: const EdgeInsets.only(right: 8),
                child: Icon(Icons.graphic_eq_rounded, color: accent, size: 16),
              ),
            Text(
              track.durationLabel,
              style: const TextStyle(
                color: Tokens.textTertiary,
                fontSize: 13,
                fontFeatures: [FontFeature.tabularFigures()],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ErrorView extends StatelessWidget {
  const _ErrorView({required this.message});
  final String message;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Text(
          'Could not open the library.\n\n$message\n\n'
          'Android: run tool/push_db.sh from the Mac, then restart.\n'
          'Desktop: --dart-define=LIBRARY_DB=/path/to/library.db',
          textAlign: TextAlign.center,
          style: const TextStyle(color: Tokens.dim),
        ),
      ),
    );
  }
}
