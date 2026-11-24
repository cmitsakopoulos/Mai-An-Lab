import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/haptics.dart';
import '../data/browse_queries.dart';
import '../data/library_repository.dart';
import '../player/player_controller.dart';
import '../player/ui/mini_player.dart';
import '../settings/settings_search.dart';
import '../ui/tokens.dart';
import 'browse_controller.dart';
import 'browse_view.dart';
import 'library_controller.dart';
import 'library_dialogs.dart';
import 'selection.dart';
import 'track_menu.dart';
import 'track_query.dart';
import 'track_row.dart';

const _appChannel = MethodChannel('mai_an/app');

class LibraryScreen extends StatefulWidget {
  const LibraryScreen({
    super.key,
    required this.controller,
    required this.browse,
    required this.player,
    required this.haptics,
    this.unifiedPills = false,
    this.selection,
    this.onOpenSettings,
  });

  final LibraryController controller;

  /// The Genres, Artists and Albums views, and which view is open.
  final BrowseController browse;
  final Haptics haptics;

  /// `appearance.pill_style = "unified"`.
  final bool unifiedPills;

  /// Null until the player has started (after the permission prompts).
  final ValueListenable<PlayerController?> player;

  /// The shell's (AppShell owns back, and with it leaving selection mode).
  /// Null when the screen stands alone (tests): then it owns both, and the
  /// mini player.
  final SelectionController? selection;

  /// The header's settings button and the empty library's ENTER PATHS
  /// (main.py `switch_to_settings`); null leaves them out.
  final void Function([SettingsPage?])? onOpenSettings;

  @override
  State<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends State<LibraryScreen> {
  LibraryController get controller => widget.controller;
  BrowseController get browse => widget.browse;
  StreamSubscription<String>? _messages;
  StreamSubscription<String>? _libraryMessages;

  /// library.py's multi-select, shared by every view.
  late final SelectionController selection =
      widget.selection ?? SelectionController();
  bool get _standalone => widget.selection == null;

  /// The search box. `show_filtered` (the Search landing) sets the query
  /// from outside, so the box follows the controller.
  late final _search = TextEditingController(text: controller.query);

  @override
  void initState() {
    super.initState();
    widget.player.addListener(_bindMessages);
    _bindMessages();
    _libraryMessages = controller.messages.listen(_snack);
    controller.addListener(_syncSearch);
  }

  void _syncSearch() {
    if (_search.text != controller.query) _search.text = controller.query;
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
    _libraryMessages?.cancel();
    controller.removeListener(_syncSearch);
    _search.dispose();
    if (_standalone) selection.dispose();
    super.dispose();
  }

  PlayerController? _playerOrSnack() {
    final player = widget.player.value;
    if (player == null) _snack('The player is still starting.');
    return player;
  }

  void _play(Track t) {
    _playerOrSnack()?.playTrack(
      t,
      context: controller.visible,
      generalView: controller.isGeneralView && !controller.closestMatch,
    );
  }

  /// A track under an album or playlist row plays that album or playlist
  /// from it (`source=("album", artist, album)` / `("playlist", id)`).
  void _playInContext(Track t, List<Track> context) {
    _playerOrSnack()?.playTrack(t, context: context);
  }

  /// The genre row's play button: its tracks, newest first, from the top.
  Future<void> _playGenre(GenreRow g) async {
    final player = _playerOrSnack();
    if (player == null) return;
    final tracks = await browse.genreTracks(g.name);
    if (tracks.isEmpty) {
      _snack('No tracks found for ${g.name}');
      return;
    }
    await player.playTrack(tracks.first, context: tracks);
  }

  @override
  Widget build(BuildContext context) {
    final screen = _screen();
    if (!_standalone) return screen;
    // Standalone, the screen is the root: back leaves selection mode, then
    // sends the app to the background (AppShell does this in the app).
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        if (selection.active) {
          selection.exit();
          return;
        }
        _appChannel.invokeMethod('moveTaskToBack').ignore();
      },
      child: screen,
    );
  }

  Widget _screen() {
    return Scaffold(
      appBar: AppBar(
        title: ListenableBuilder(
          listenable: Listenable.merge([controller, browse]),
          builder: (context, _) =>
              _Header(controller: controller, browse: browse),
        ),
        actions: [
          ListenableBuilder(
            listenable: Listenable.merge([controller, browse]),
            builder: (context, _) => _sortMenu(),
          ),
          // library.py's scan button (Flet: refresh icon in the accent).
          ListenableBuilder(
            listenable: controller,
            builder: (context, _) => controller.canScan
                ? IconButton(
                    icon: const Icon(Icons.refresh),
                    color: Theme.of(context).colorScheme.primary,
                    tooltip: 'Scan library',
                    onPressed: controller.scanning ? null : controller.scan,
                  )
                : const SizedBox.shrink(),
          ),
          if (widget.onOpenSettings case final open?)
            IconButton(
              icon: const Icon(Icons.settings_outlined, size: 22),
              color: Tokens.dim,
              tooltip: 'Settings',
              onPressed: () => open(),
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
                        controller: _search,
                        onChanged: controller.setQuery,
                        decoration: const InputDecoration(
                          hintText: 'Search artists, albums, tracks…',
                          prefixIcon: Icon(Icons.search, color: Tokens.dim),
                          isDense: true,
                        ),
                      ),
                    ),
                    ListenableBuilder(
                      listenable: browse,
                      builder: (context, _) => browse.views.length < 2
                          ? const SizedBox.shrink()
                          : Padding(
                              padding: const EdgeInsets.fromLTRB(14, 0, 14, 8),
                              child: ViewBar(
                                views: browse.views,
                                selected: browse.view,
                                unified: widget.unifiedPills,
                                onSelect: browse.setView,
                              ),
                            ),
                    ),
                    ListenableBuilder(
                      listenable: controller,
                      builder: (context, _) => controller.scanning
                          ? _ScanBanner(controller: controller)
                          : const SizedBox.shrink(),
                    ),
                    Expanded(
                      child: ListenableBuilder(
                        listenable: Listenable.merge([browse, controller]),
                        builder: (context, _) =>
                            controller.total == 0 && !controller.scanning
                            ? _EmptyLibrary(
                                onEnterPaths: widget.onOpenSettings == null
                                    ? null
                                    : () => widget.onOpenSettings!(
                                        SettingsPage.storage,
                                      ),
                              )
                            : browse.view == LibraryView.tracks
                            ? _tracksList()
                            : BrowseList(
                                controller: browse,
                                player: widget.player,
                                onPlayTrack: _playInContext,
                                onTrackMenu: _trackMenu,
                                onAlbumMenu: _albumMenu,
                                onPlayGenre: _playGenre,
                                selection: selection,
                                onCreatePlaylist: _createPlaylist,
                                onEditPlaylist: _editPlaylist,
                                onDeletePlaylist: _deletePlaylist,
                                onRemoveFromPlaylist: _removeFromPlaylist,
                              ),
                      ),
                    ),
                  ],
                ),
              },
            ),
          ),
          ListenableBuilder(
            listenable: selection,
            builder: (context, _) => selection.active
                ? _SelectionBar(
                    selection: selection,
                    onPlay: () => _selectionPlay(replace: true),
                    onQueue: () => _selectionPlay(replace: false),
                    onAddToPlaylist: () =>
                        _addToPlaylist(selection.tracks, fromSelection: true),
                    canWritePlaylists: controller.canWrite,
                  )
                : const SizedBox.shrink(),
          ),
          if (_standalone)
            ValueListenableBuilder(
              valueListenable: widget.player,
              builder: (context, player, _) => player == null
                  ? const SizedBox.shrink()
                  : SafeArea(top: false, child: MiniPlayer(player: player)),
            ),
        ],
      ),
    );
  }

  Widget _tracksList() => ValueListenableBuilder(
    valueListenable: widget.player,
    builder: (context, player, _) => ListenableBuilder(
      // Rebuilds the visible rows when the playing track changes
      // (highlight), nothing more.
      listenable: Listenable.merge([player ?? controller, selection]),
      builder: (context, _) {
        final playing = player?.stopped == false
            ? player?.nowPlaying?.path
            : null;
        final picking = selection.active;
        return ListView.builder(
          // A fixed extent lets Flutter lay out only the visible rows and
          // jump-scroll without measuring the rest.
          itemExtent: TrackRow.height,
          itemCount: controller.visible.length,
          itemBuilder: (context, i) {
            final t = controller.visible[i];
            return TrackRow(
              track: t,
              playing: t.path == playing,
              selected: picking ? selection.contains(t.path) : null,
              onTap: picking ? () => selection.toggle(t) : () => _play(t),
              onLongPress: () => _trackMenu(t),
            );
          },
        );
      },
    ),
  );

  /// library.py `_open_sort_menu`: the options depend on the view. Tracks
  /// and Albums share one sort, as in Flet.
  Widget _sortMenu() {
    Widget menu<T>(
      List<T> values,
      T current,
      String Function(T) label,
      ValueChanged<T> onSelected,
    ) => PopupMenuButton<T>(
      icon: const Icon(Icons.sort),
      tooltip: 'Sort',
      initialValue: current,
      onSelected: onSelected,
      itemBuilder: (context) => [
        for (final v in values)
          CheckedPopupMenuItem(
            value: v,
            checked: v == current,
            child: Text(label(v)),
          ),
      ],
    );
    return switch (browse.view) {
      LibraryView.tracks || LibraryView.albums => menu(
        LibrarySort.values,
        controller.sort,
        (s) => s.label,
        controller.setSort,
      ),
      LibraryView.artists => menu(
        ArtistSort.values,
        browse.artistSort,
        (s) => s.label,
        browse.setArtistSort,
      ),
      LibraryView.genres => menu(
        GenreSort.values,
        browse.genreSort,
        (s) => s.label,
        browse.setGenreSort,
      ),
      LibraryView.playlists => menu(
        PlaylistSort.values,
        browse.playlistSort,
        (s) => s.label,
        browse.setPlaylistSort,
      ),
    };
  }

  /// library.py `_open_album_context_menu`: Play Album, Add Album to Queue.
  /// (Send Album arrives with sync, phase 4.)
  void _albumMenu(AlbumRow a) {
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
                a.displayTitle,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  fontWeight: FontWeight.w700,
                  fontSize: 15,
                ),
              ),
              subtitle: Text(
                '${a.displayArtist}  ·  ${a.trackCount ?? '?'} tracks',
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: Tokens.dim, fontSize: 12),
              ),
            ),
            const Divider(height: 1, color: Tokens.borderSubtle),
            ListTile(
              leading: const Icon(Icons.play_arrow_rounded),
              title: const Text('Play Album'),
              enabled: player != null,
              onTap: () async {
                Navigator.pop(sheet);
                final tracks = await browse.albumTracks(a);
                if (tracks.isNotEmpty) {
                  await player!.playTrack(tracks.first, context: tracks);
                }
              },
            ),
            ListTile(
              leading: const Icon(Icons.playlist_add_rounded),
              title: const Text('Add Album to Queue'),
              enabled: player != null,
              onTap: () async {
                Navigator.pop(sheet);
                final tracks = await browse.albumTracks(a);
                for (final t in tracks) {
                  player!.queueLast(t);
                }
                widget.haptics.trigger('swipe_queue');
                _snack('Added ${tracks.length} tracks to queue.');
              },
            ),
          ],
        ),
      ),
    );
  }

  void _trackMenu(Track t) {
    widget.haptics.trigger('long_press');
    final player = widget.player.value;
    final writable = controller.canWrite;
    showTrackMenu(context, t, (
      select: () => selection.enable(t),
      playNext: player == null
          ? null
          : () {
              player.queueNext(t);
              widget.haptics.trigger('swipe_queue');
            },
      addToQueue: player == null
          ? null
          : () {
              player.queueLast(t);
              widget.haptics.trigger('swipe_queue');
            },
      startRadio: player?.autoplay == null
          ? null
          : () {
              player!.autoplay!.startRadio(t);
              widget.haptics.trigger('swipe_queue');
            },
      addToPlaylist: writable ? () => _addToPlaylist([t]) : null,
      delete: writable && t.path.isNotEmpty ? () => _deleteTrack(t) : null,
    ));
  }

  /// library.py `_selection_play`: the selection, in the order it was
  /// picked, played now or added to the queue; selection mode ends.
  void _selectionPlay({required bool replace}) {
    final player = _playerOrSnack();
    if (player == null) return;
    final n = player.playBlock(selection.tracks, replace: replace);
    if (n > 0) widget.haptics.trigger('swipe_queue');
    selection.exit();
  }

  /// library.py `_open_add_to_playlist_sheet`, for one track (the track
  /// menu) or the selection.
  Future<void> _addToPlaylist(
    List<Track> tracks, {
    bool fromSelection = false,
  }) async {
    final paths = [
      for (final t in tracks)
        if (t.path.isNotEmpty) t.path,
    ];
    if (paths.isEmpty) return;
    final playlists = await browse.playlistsByName();
    if (!mounted) return;
    final picked = await showAddToPlaylistSheet(context, playlists);
    if (!mounted || picked == null) return;
    switch (picked) {
      case (null, true):
        await showPlaylistNameDialog(
          context,
          onSubmit: (name) async {
            final (_, err) = await browse.createPlaylist(name, paths: paths);
            if (err != null) return 'Could not create playlist: $err';
            if (fromSelection) selection.exit();
            return null;
          },
        );
      case (final PlaylistRow p, _):
        final err = await browse.addToPlaylist(p.id, paths);
        if (err != null) {
          _snack('Failed to add track: $err');
        } else if (fromSelection) {
          _snack('Added ${paths.length} tracks to ${p.displayName}.');
          selection.exit();
        }
      default:
    }
  }

  /// library.py `_create_playlist_dialog` (the Playlists tab).
  void _createPlaylist() => showPlaylistNameDialog(
    context,
    closeFirst: true,
    withHint: true,
    onSubmit: (name) async => (await browse.createPlaylist(name)).$2,
    onError: (err) => _snack('Failed: $err'),
  );

  void _editPlaylist(PlaylistRow p) => showPlaylistEditor(
    context,
    playlist: p,
    onSave: (name, color) async {
      final err = await browse.updatePlaylist(p.id, name: name, color: color);
      if (err != null) _snack('Could not save playlist: $err');
    },
  );

  /// main.py `confirm_delete_playlist` + `_delete_playlist`.
  Future<void> _deletePlaylist(PlaylistRow p) async {
    final ok = await confirmDelete(
      context,
      title: 'Delete Playlist?',
      message:
          "Permanently delete playlist '${p.displayName}'? Tracks will "
          'remain in your library.',
    );
    if (!ok) return;
    final err = await browse.deletePlaylist(p.id);
    _snack(
      err == null ? 'Playlist deleted.' : 'Could not delete playlist: $err',
    );
  }

  Future<void> _removeFromPlaylist(int playlistId, Track t) async {
    final err = await browse.removeFromPlaylist(playlistId, t.path);
    if (err != null) _snack('Could not remove track: $err');
  }

  /// main.py `confirm_delete_track` + `_delete_track`.
  Future<void> _deleteTrack(Track t) async {
    final ok = await confirmDelete(
      context,
      title: 'Delete Track?',
      message: "Permanently delete '${t.title}' from your device?",
    );
    if (!ok) return;
    widget.player.value?.removePath(t.path);
    _snack(await controller.deleteTrack(t.path));
  }
}

/// library.py `_selection_bar`: cancel, the count, Play, Add to Queue, Add
/// to Playlist (Send arrives with sync, phase 4). The actions are disabled
/// while nothing is ticked.
class _SelectionBar extends StatelessWidget {
  const _SelectionBar({
    required this.selection,
    required this.onPlay,
    required this.onQueue,
    required this.onAddToPlaylist,
    required this.canWritePlaylists,
  });

  final SelectionController selection;
  final VoidCallback onPlay;
  final VoidCallback onQueue;
  final VoidCallback onAddToPlaylist;
  final bool canWritePlaylists;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final some = selection.count > 0;
    return Container(
      margin: const EdgeInsets.fromLTRB(14, 0, 14, 8),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: Tokens.surface2,
        border: Border.all(color: Tokens.borderSubtle),
        borderRadius: BorderRadius.circular(Tokens.radiusCard),
      ),
      child: Row(
        children: [
          IconButton(
            icon: const Icon(Icons.close_rounded),
            color: Tokens.dim,
            tooltip: 'Cancel Selection',
            onPressed: selection.exit,
          ),
          Expanded(
            child: Text(
              '${selection.count} selected',
              style: const TextStyle(
                color: Tokens.text,
                fontWeight: FontWeight.w600,
                fontSize: 13,
              ),
            ),
          ),
          IconButton(
            icon: const Icon(Icons.play_arrow_rounded),
            color: accent,
            tooltip: 'Play',
            onPressed: some ? onPlay : null,
          ),
          IconButton(
            icon: const Icon(Icons.playlist_add_check_rounded),
            color: accent,
            tooltip: 'Add to Queue',
            onPressed: some ? onQueue : null,
          ),
          IconButton(
            icon: const Icon(Icons.playlist_add_rounded),
            color: Tokens.libPlaylist,
            tooltip: 'Add to Playlist',
            onPressed: some && canWritePlaylists ? onAddToPlaylist : null,
          ),
        ],
      ),
    );
  }
}

class _Header extends StatelessWidget {
  const _Header({required this.controller, required this.browse});
  final LibraryController controller;
  final BrowseController browse;

  @override
  Widget build(BuildContext context) {
    final c = controller;
    final b = browse;
    final parts = <String>[
      if (c.state == LoadState.ready)
        if (b.view == LibraryView.tracks)
          '${c.visible.length} / ${c.total} tracks'
              '${c.closestMatch ? ' (closest match)' : ''}'
        else if (!b.loading || b.rows.isNotEmpty)
          '${b.shown} / ${b.total} ${b.view.label.toLowerCase()}'
              '${b.closestMatch ? ' (closest match)' : ''}',
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

/// library.py `_scan_progress_container`: the bar and the status line,
/// under the view bar while a scan runs.
class _ScanBanner extends StatelessWidget {
  const _ScanBanner({required this.controller});
  final LibraryController controller;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Container(
      margin: const EdgeInsets.fromLTRB(14, 0, 14, 8),
      padding: const EdgeInsets.symmetric(vertical: 12, horizontal: 16),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: accent.withValues(alpha: 0.3)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          LinearProgressIndicator(
            value: controller.scanProgress,
            color: accent,
            backgroundColor: Tokens.surface2,
          ),
          const SizedBox(height: 8),
          Text(
            controller.scanStatus,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(color: Tokens.dim, fontSize: 11),
          ),
        ],
      ),
    );
  }
}

/// library.py `_empty_label`, with ENTER PATHS (Settings → Storage).
class _EmptyLibrary extends StatelessWidget {
  const _EmptyLibrary({this.onEnterPaths});

  final VoidCallback? onEnterPaths;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            Icons.library_music_outlined,
            color: accent.withValues(alpha: 0.3),
            size: 80,
          ),
          const SizedBox(height: 12),
          const Text(
            "It's empty in here.",
            style: TextStyle(
              color: Tokens.text,
              fontSize: 20,
              fontWeight: FontWeight.w800,
            ),
          ),
          const SizedBox(height: 12),
          const Text(
            'Your music library is currently empty.\nIndex your folders to start listening.',
            textAlign: TextAlign.center,
            style: TextStyle(color: Tokens.dim, fontSize: 14),
          ),
          if (onEnterPaths != null) ...[
            const SizedBox(height: 20),
            TextButton.icon(
              style: TextButton.styleFrom(foregroundColor: accent),
              icon: const Icon(Icons.settings_rounded, size: 16),
              label: const Text('ENTER PATHS', style: TextStyle(fontSize: 13)),
              onPressed: onEnterPaths,
            ),
          ],
        ],
      ),
    );
  }
}
