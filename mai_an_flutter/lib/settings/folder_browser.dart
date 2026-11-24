import 'dart:io';

import 'package:flutter/material.dart';

import '../ui/tokens.dart';

/// settings.py `_browse_android_paths`: a folder navigator over shared
/// storage. Starts on the bookmarks; each folder offers "Use …" (disabled
/// for app-private ones) and its sub-folders, hidden ones left out. Resolves
/// with the folder picked, or null.
Future<String?> showFolderBrowser(
  BuildContext context, {
  required String label,
  required bool Function(String) isPrivate,
}) => showModalBottomSheet<String>(
  context: context,
  backgroundColor: Tokens.surface,
  isScrollControlled: true,
  useSafeArea: true,
  builder: (_) => _FolderBrowser(label: label, isPrivate: isPrivate),
);

const _bookmarks = [
  ('/storage/emulated/0', 'Internal Storage'),
  ('/sdcard', 'SD Card'),
  ('/storage/emulated/0/Music', 'Music'),
  ('/storage/emulated/0/Download', 'Downloads'),
];

class _FolderBrowser extends StatefulWidget {
  const _FolderBrowser({required this.label, required this.isPrivate});

  final String label;
  final bool Function(String) isPrivate;

  @override
  State<_FolderBrowser> createState() => _FolderBrowserState();
}

class _FolderBrowserState extends State<_FolderBrowser> {
  /// Null on the bookmarks.
  String? _dir;
  List<String> _entries = const [];
  bool _denied = false;

  void _go(String? dir) {
    var entries = <String>[];
    var denied = false;
    if (dir != null) {
      try {
        entries = [
          for (final e in Directory(dir).listSync(followLinks: true))
            if (e is Directory) _base(e.path),
        ].where((n) => !n.startsWith('.')).toList()..sort(_pyCompare);
      } on FileSystemException {
        denied = true;
      }
    }
    setState(() {
      _dir = dir;
      _entries = entries;
      _denied = denied;
    });
  }

  /// Python's `sorted` on str: by code point.
  static int _pyCompare(String a, String b) {
    final ra = a.runes.toList(), rb = b.runes.toList();
    for (var i = 0; i < ra.length && i < rb.length; i++) {
      if (ra[i] != rb[i]) return ra[i] - rb[i];
    }
    return ra.length - rb.length;
  }

  static String _base(String path) {
    final p = path.endsWith('/') && path.length > 1
        ? path.substring(0, path.length - 1)
        : path;
    return p.substring(p.lastIndexOf('/') + 1);
  }

  static String _parent(String path) {
    final i = path.lastIndexOf('/');
    return i <= 0 ? '/' : path.substring(0, i);
  }

  void _up() {
    final cur = _dir;
    if (cur == null) return;
    final parent = _parent(cur);
    _go(parent == cur ? null : parent);
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final dir = _dir;
    final name = dir == null ? '' : (_base(dir).isEmpty ? dir : _base(dir));
    final rows = <Widget>[];
    if (dir == null) {
      for (final (path, title) in _bookmarks) {
        final exists = Directory(path).existsSync();
        rows.add(
          ListTile(
            leading: Icon(
              Icons.folder_rounded,
              color: exists ? accent : Tokens.dim,
              size: 20,
            ),
            title: Text(
              title,
              style: TextStyle(
                color: exists ? Tokens.text : Tokens.dim,
                fontSize: 13,
              ),
            ),
            subtitle: Text(
              path,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: Tokens.dim, fontSize: 10),
            ),
            onTap: () => _go(path),
          ),
        );
      }
    } else {
      final private = widget.isPrivate(dir);
      rows.add(
        Padding(
          padding: const EdgeInsets.only(bottom: 6),
          child: FilledButton.icon(
            style: FilledButton.styleFrom(
              backgroundColor: accent,
              foregroundColor: Tokens.bg,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(8),
              ),
            ),
            icon: Icon(private ? Icons.block_rounded : Icons.check_rounded),
            label: Text(
              private ? 'App-private folder: not usable' : 'Use "$name"',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
            onPressed: private ? null : () => Navigator.pop(context, dir),
          ),
        ),
      );
      if (_denied) {
        rows.add(
          const Text(
            'Permission denied',
            style: TextStyle(
              color: Color(0xFFFF5555),
              fontSize: 12,
              fontStyle: FontStyle.italic,
            ),
          ),
        );
      }
      for (final e in _entries) {
        rows.add(
          ListTile(
            leading: Icon(Icons.folder_outlined, color: accent, size: 18),
            title: Text(
              e,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: Tokens.text, fontSize: 13),
            ),
            onTap: () => _go(dir == '/' ? '/$e' : '$dir/$e'),
          ),
        );
      }
      if (_entries.isEmpty && !_denied) {
        rows.add(
          const Text(
            '(no sub-folders)',
            style: TextStyle(
              color: Tokens.dim,
              fontSize: 12,
              fontStyle: FontStyle.italic,
            ),
          ),
        );
      }
    }
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 40),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              IconButton(
                icon: const Icon(Icons.arrow_back_rounded),
                color: accent,
                tooltip: 'Up',
                onPressed: _up,
              ),
              const SizedBox(width: 4),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      dir == null ? 'Select ${widget.label}' : name,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Tokens.text,
                        fontWeight: FontWeight.w700,
                        fontSize: 14,
                      ),
                    ),
                    Text(
                      dir ?? 'Choose a starting location',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Tokens.dim,
                        fontSize: 10,
                        fontStyle: FontStyle.italic,
                      ),
                    ),
                  ],
                ),
              ),
              TextButton(
                onPressed: () => Navigator.pop(context),
                child: const Text('Cancel'),
              ),
            ],
          ),
          const Divider(color: Tokens.border),
          SizedBox(height: 320, child: ListView(children: rows)),
        ],
      ),
    );
  }
}
