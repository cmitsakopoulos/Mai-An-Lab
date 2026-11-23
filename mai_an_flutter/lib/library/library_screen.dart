import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/haptics.dart';
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
    required this.haptics,
  });

  final LibraryController controller;
  final Haptics haptics;

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
                  LoadState.error => _ErrorView(
                    message: controller.error ?? '',
                  ),
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
    widget.haptics.trigger('long_press');
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
              title: Text(
                t.title,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
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
                widget.haptics.trigger('swipe_queue');
              },
            ),
            ListTile(
              leading: const Icon(Icons.playlist_add_rounded),
              title: const Text('Add to queue'),
              enabled: player != null,
              onTap: () {
                Navigator.pop(sheet);
                player!.queueLast(t);
                widget.haptics.trigger('swipe_queue');
              },
            ),
            ListTile(
              leading: const Icon(Icons.all_inclusive_rounded),
              title: const Text('Start radio'),
              subtitle: const Text(
                'Play similar tracks automatically',
                style: TextStyle(color: Tokens.dim, fontSize: 12),
              ),
              enabled: player?.autoplay != null,
              onTap: () {
                Navigator.pop(sheet);
                player!.autoplay!.startRadio(t);
                widget.haptics.trigger('swipe_queue');
              },
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

/// library.py `_track_row`: format badge, title, "artist · album", duration.
/// The playing row is tinted and drawn in the accent. Kept light: one
/// InkWell, one Row.
class _TrackRow extends StatelessWidget {
  const _TrackRow({
    required this.track,
    this.playing = false,
    this.onTap,
    this.onLongPress,
  });

  /// library.py ROW_H.
  static const height = 64.0;
  final Track track;
  final bool playing;
  final VoidCallback? onTap;
  final VoidCallback? onLongPress;

  /// The subtitle rules of `_track_row`: "Unknown" and a value equal to the
  /// title are left out.
  static String subtitle(Track t) {
    bool keep(String v) =>
        v.isNotEmpty &&
        v != 'Unknown' &&
        v.trim().toLowerCase() != t.title.trim().toLowerCase();
    final parts = [if (keep(t.artist)) t.artist, if (keep(t.album)) t.album];
    if (parts.isNotEmpty) return parts.join('  ·  ');
    return (t.artist.isNotEmpty && t.artist != 'Unknown') ? t.artist : '';
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Material(
      color: playing ? accent.withValues(alpha: 0.1) : Colors.transparent,
      child: InkWell(
        onTap: onTap,
        onLongPress: onLongPress,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Row(
            children: [
              Container(
                width: 40,
                height: 20,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: playing
                      ? accent.withValues(alpha: 0.22)
                      : Tokens.text.withValues(alpha: 0.08),
                  border: Border.all(
                    color: playing ? accent : Tokens.borderSubtle,
                  ),
                  borderRadius: BorderRadius.circular(5),
                ),
                child: Text(
                  track.formatBadge,
                  style: TextStyle(
                    fontSize: 9,
                    fontWeight: FontWeight.w700,
                    color: playing ? accent : Tokens.dim,
                  ),
                ),
              ),
              const SizedBox(width: 16),
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
                        fontSize: 14,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      subtitle(track),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: playing ? accent : Tokens.dim,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              ),
              if (track.duration > 0) ...[
                const SizedBox(width: 12),
                Text(
                  track.durationLabel,
                  style: TextStyle(
                    color: playing ? accent : Tokens.textTertiary,
                    fontSize: 12,
                    fontFeatures: const [FontFeature.tabularFigures()],
                  ),
                ),
              ],
            ],
          ),
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
