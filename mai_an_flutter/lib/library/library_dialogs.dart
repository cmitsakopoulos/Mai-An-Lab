import 'package:flutter/material.dart';

import '../core/py_text.dart';
import '../data/library_repository.dart';
import '../ui/tokens.dart';
import 'browse_view.dart' show playlistColor;

/// The Library tab's dialogs: library.py `_create_playlist_dialog` and the
/// Add to Playlist sheet's own "New Playlist", ui/player/dialogs.py
/// `PlaylistEditorDialog`, and main.py's `confirm_delete_track` /
/// `confirm_delete_playlist`. Texts and colours are Flet's.

const _danger = Color(0xFFFF2222);

/// library.py `LIB_PLAYLIST_COLOR` as the editor stores it.
const kPlaylistColorHex = '#FF9F0A';

/// The editor's nine colours (dialogs.py).
const kPlaylistColors = [
  '#FF5555', '#55FF55', '#5555FF', '#FFFF55', '#FF55FF', //
  '#55FFFF', '#FFFFFF', '#FF8C00', '#8A2BE2',
];

/// A playlist name. [onSubmit] runs with the stripped name, never empty; it
/// returns an error to show under the field (the dialog stays open), or null
/// to close. With [closeFirst] (the Playlists tab's dialog) it closes before
/// [onSubmit] runs, as Flet's does, and an error goes to [onError].
Future<void> showPlaylistNameDialog(
  BuildContext context, {
  required Future<String?> Function(String name) onSubmit,
  bool closeFirst = false,
  bool withHint = false,
  void Function(String error)? onError,
}) => showDialog<void>(
  context: context,
  builder: (dialog) => _NameDialog(
    onSubmit: onSubmit,
    closeFirst: closeFirst,
    withHint: withHint,
    onError: onError,
  ),
);

class _NameDialog extends StatefulWidget {
  const _NameDialog({
    required this.onSubmit,
    required this.closeFirst,
    required this.withHint,
    this.onError,
  });

  final Future<String?> Function(String name) onSubmit;
  final bool closeFirst;
  final bool withHint;
  final void Function(String error)? onError;

  @override
  State<_NameDialog> createState() => _NameDialogState();
}

class _NameDialogState extends State<_NameDialog> {
  final _field = TextEditingController();
  String? _error;
  bool _busy = false;

  @override
  void dispose() {
    _field.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final name = pyStrip(_field.text);
    if (name.isEmpty || _busy) return;
    if (widget.closeFirst) {
      Navigator.pop(context);
      final err = await widget.onSubmit(name);
      if (err != null) widget.onError?.call(err);
      return;
    }
    setState(() => _busy = true);
    final err = await widget.onSubmit(name);
    if (!mounted) return;
    if (err == null) {
      Navigator.pop(context);
    } else {
      setState(() {
        _busy = false;
        _error = err;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return AlertDialog(
      backgroundColor: Tokens.surface,
      title: const Text('New Playlist'),
      content: TextField(
        controller: _field,
        autofocus: true,
        onSubmitted: (_) => _submit(),
        decoration: InputDecoration(
          labelText: 'Playlist Name',
          hintText: widget.withHint ? 'ex. Mai An' : null,
          errorText: _error,
          errorMaxLines: 3,
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(6),
            borderSide: const BorderSide(color: Tokens.border),
          ),
          focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(6),
            borderSide: const BorderSide(color: Tokens.libPlaylist),
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('Cancel'),
        ),
        if (widget.closeFirst)
          TextButton(onPressed: _submit, child: const Text('Create'))
        else
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: accent,
              foregroundColor: Tokens.bg,
            ),
            onPressed: _busy ? null : _submit,
            child: const Text('Create'),
          ),
      ],
    );
  }
}

/// dialogs.py `PlaylistEditorDialog`: name and accent colour. [onSave] gets
/// the name as typed (Flet doesn't strip it; an empty one is left alone)
/// and the colour picked, which starts at the playlist's own or the default
/// orange.
Future<void> showPlaylistEditor(
  BuildContext context, {
  required PlaylistRow playlist,
  required void Function(String name, String color) onSave,
}) => showDialog<void>(
  context: context,
  builder: (dialog) => _EditorDialog(playlist: playlist, onSave: onSave),
);

class _EditorDialog extends StatefulWidget {
  const _EditorDialog({required this.playlist, required this.onSave});
  final PlaylistRow playlist;
  final void Function(String name, String color) onSave;

  @override
  State<_EditorDialog> createState() => _EditorDialogState();
}

class _EditorDialogState extends State<_EditorDialog> {
  late final _name = TextEditingController(text: widget.playlist.displayName);
  late String _color = widget.playlist.color ?? kPlaylistColorHex;

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
    backgroundColor: Tokens.surface,
    title: const Text('Customize Playlist'),
    content: Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        TextField(
          controller: _name,
          decoration: InputDecoration(
            labelText: 'Playlist Name',
            fillColor: Tokens.surface,
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(6),
              borderSide: const BorderSide(color: Tokens.border),
            ),
            focusedBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(6),
              borderSide: const BorderSide(color: Tokens.libPlaylist),
            ),
          ),
        ),
        const SizedBox(height: 15),
        const Text(
          'Accent Color',
          style: TextStyle(color: Tokens.dim, fontSize: 12),
        ),
        const SizedBox(height: 15),
        Wrap(
          spacing: 10,
          runSpacing: 10,
          children: [
            for (final c in kPlaylistColors)
              GestureDetector(
                onTap: () => setState(() => _color = c),
                child: Container(
                  key: ValueKey('color $c'),
                  width: 24,
                  height: 24,
                  decoration: BoxDecoration(
                    color: playlistColor(c),
                    shape: BoxShape.circle,
                    border: Border.all(
                      width: 2,
                      color: c == _color ? Tokens.text : Colors.transparent,
                    ),
                  ),
                ),
              ),
          ],
        ),
      ],
    ),
    actions: [
      TextButton(
        onPressed: () => Navigator.pop(context),
        child: const Text('Cancel'),
      ),
      FilledButton(
        style: FilledButton.styleFrom(
          backgroundColor: Tokens.libPlaylist,
          foregroundColor: Tokens.bg,
        ),
        onPressed: () {
          Navigator.pop(context);
          widget.onSave(_name.text, _color);
        },
        child: const Text('Save'),
      ),
    ],
  );
}

/// main.py's destructive confirmations: Cancel, and a red Delete. Resolves
/// true when Delete was pressed. Not modal, as in Flet: a tap outside
/// cancels.
Future<bool> confirmDelete(
  BuildContext context, {
  required String title,
  required String message,
  String confirmLabel = 'Delete',
}) async =>
    await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        backgroundColor: Tokens.surface,
        title: Text(title, style: const TextStyle(color: Tokens.text)),
        content: Text(
          message,
          style: const TextStyle(color: Tokens.dim, fontSize: 13),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialog, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: _danger,
              foregroundColor: Tokens.text,
            ),
            onPressed: () => Navigator.pop(dialog, true),
            child: Text(confirmLabel),
          ),
        ],
      ),
    ) ??
    false;

/// library.py `_open_add_to_playlist_sheet`: "New Playlist", then every
/// playlist by name. Resolves with the playlist picked, or `(null, true)`
/// for New Playlist; null when dismissed.
Future<(PlaylistRow?, bool)?> showAddToPlaylistSheet(
  BuildContext context,
  List<PlaylistRow> playlists,
) {
  final accent = Theme.of(context).colorScheme.primary;
  return showModalBottomSheet<(PlaylistRow?, bool)>(
    context: context,
    backgroundColor: Tokens.surface,
    builder: (sheet) => SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'Add to Playlist',
              style: TextStyle(
                color: Tokens.text,
                fontWeight: FontWeight.w700,
                fontSize: 14,
              ),
            ),
            const SizedBox(height: 8),
            SizedBox(
              height: 300,
              child: ListView(
                children: [
                  ListTile(
                    leading: Icon(Icons.add_rounded, color: accent),
                    title: Text(
                      'New Playlist',
                      style: TextStyle(
                        color: accent,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    onTap: () => Navigator.pop(sheet, (null, true)),
                  ),
                  const Divider(color: Tokens.border),
                  for (final p in playlists)
                    ListTile(
                      leading: const Icon(
                        Icons.queue_music_rounded,
                        color: Tokens.libPlaylist,
                      ),
                      title: Text(
                        p.displayName,
                        style: const TextStyle(color: Tokens.text),
                      ),
                      subtitle: Text(
                        '${p.trackCount} tracks',
                        style: const TextStyle(color: Tokens.dim, fontSize: 10),
                      ),
                      onTap: () => Navigator.pop(sheet, (p, false)),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    ),
  );
}
