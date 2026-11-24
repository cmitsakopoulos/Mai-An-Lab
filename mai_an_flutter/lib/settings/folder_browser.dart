import 'dart:io';

import 'package:flutter/material.dart';

import '../core/py_text.dart';
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

/// `os.path.basename` (a trailing slash ignored, as the browsers show it).
String _base(String path) {
  final p = path.endsWith('/') && path.length > 1
      ? path.substring(0, path.length - 1)
      : path;
  return p.substring(p.lastIndexOf('/') + 1);
}

/// `os.path.dirname`.
String _parent(String path) {
  final i = path.lastIndexOf('/');
  return i <= 0 ? '/' : path.substring(0, i);
}

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
        ].where((n) => !n.startsWith('.')).toList()..sort(pyStrCompare);
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

/// settings.py `_browse_android_state_bundle`: the same navigator for state
/// bundles. Export picks a folder ("Export here"); import lists each
/// folder's .zip files above its sub-folders. Resolves with the folder or
/// the bundle picked, or null. [appStorage] adds the app's data dir.
Future<String?> showBundleBrowser(
  BuildContext context, {
  required bool import,
  String? appStorage,
}) => showModalBottomSheet<String>(
  context: context,
  backgroundColor: Tokens.surface,
  isScrollControlled: true,
  useSafeArea: true,
  builder: (_) => _BundleBrowser(import: import, appStorage: appStorage),
);

class _BundleBrowser extends StatefulWidget {
  const _BundleBrowser({required this.import, this.appStorage});

  final bool import;
  final String? appStorage;

  @override
  State<_BundleBrowser> createState() => _BundleBrowserState();
}

/// `f"{size/1024:.0f} KB"`: Python rounds half to even.
String bundleSizeLabel(int bytes) {
  final whole = bytes ~/ 1024, rest = bytes % 1024;
  final kb = rest > 512 || (rest == 512 && whole.isOdd) ? whole + 1 : whole;
  return '$kb KB';
}

class _BundleBrowserState extends State<_BundleBrowser> {
  /// Null on the bookmarks.
  String? _dir;
  List<String> _dirs = const [];
  List<(String, int)> _zips = const [];
  bool _denied = false;

  List<(String, String)> get _bookmarks => [
    ('/storage/emulated/0/Download', 'Downloads'),
    ('/storage/emulated/0', 'Internal Storage'),
    ('/sdcard', 'SD Card'),
    ('/storage/emulated/0/Music', 'Music'),
    if (widget.appStorage case final a? when a.isNotEmpty) (a, 'App Storage'),
  ];

  void _go(String? dir) {
    final dirs = <String>[];
    final zips = <(String, int)>[];
    var denied = false;
    if (dir != null) {
      try {
        final entries = Directory(dir).listSync(followLinks: true)
          ..sort((a, b) => pyStrCompare(_base(a.path), _base(b.path)));
        for (final e in entries) {
          final name = _base(e.path);
          if (e is Directory && !name.startsWith('.')) dirs.add(name);
          if (widget.import &&
              e is File &&
              name.toLowerCase().endsWith('.zip')) {
            zips.add((name, e.lengthSync()));
          }
        }
      } on FileSystemException {
        denied = true;
      }
    }
    setState(() {
      _dir = dir;
      _dirs = dirs;
      _zips = zips;
      _denied = denied;
    });
  }

  void _up() {
    final cur = _dir;
    if (cur == null) return;
    final parent = _parent(cur);
    _go(parent == cur ? null : parent);
  }

  String _join(String dir, String name) => dir == '/' ? '/$name' : '$dir/$name';

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final dir = _dir;
    final import = widget.import;
    const small = TextStyle(color: Tokens.dim, fontSize: 10);
    const italic = TextStyle(
      color: Tokens.dim,
      fontSize: 12,
      fontStyle: FontStyle.italic,
    );
    final rows = <Widget>[];
    String title, subtitle;
    if (dir == null) {
      title = import ? 'Import State Bundle' : 'Export State Bundle';
      subtitle = import ? 'Pick a .zip bundle' : 'Pick a destination folder';
      for (final (path, name) in _bookmarks) {
        final exists = Directory(path).existsSync();
        rows.add(
          ListTile(
            leading: Icon(
              Icons.folder_rounded,
              color: exists ? accent : Tokens.dim,
              size: 20,
            ),
            title: Text(
              name,
              style: TextStyle(
                color: exists ? Tokens.text : Tokens.dim,
                fontSize: 13,
              ),
            ),
            subtitle: Text(
              path,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: small,
            ),
            onTap: () => _go(path),
          ),
        );
      }
    } else {
      title = _base(dir).isEmpty ? dir : _base(dir);
      subtitle = dir;
      if (!import) {
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
              icon: const Icon(Icons.ios_share_rounded),
              label: const Text(
                'Export here',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
              onPressed: () => Navigator.pop(context, dir),
            ),
          ),
        );
      }
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
      } else {
        for (final (name, size) in _zips) {
          rows.add(
            ListTile(
              leading: Icon(Icons.archive_rounded, color: accent, size: 18),
              title: Text(
                name,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: Tokens.text, fontSize: 13),
              ),
              subtitle: Text(bundleSizeLabel(size), style: small),
              onTap: () => Navigator.pop(context, _join(dir, name)),
            ),
          );
        }
        for (final name in _dirs) {
          rows.add(
            ListTile(
              leading: Icon(Icons.folder_outlined, color: accent, size: 18),
              title: Text(
                name,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: Tokens.text, fontSize: 13),
              ),
              onTap: () => _go(_join(dir, name)),
            ),
          );
        }
        if (_dirs.isEmpty && _zips.isEmpty) {
          rows.add(
            Text(
              import ? '(no .zip bundles or sub-folders)' : '(no sub-folders)',
              style: italic,
            ),
          );
        }
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
                      title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Tokens.text,
                        fontWeight: FontWeight.w700,
                        fontSize: 14,
                      ),
                    ),
                    Text(
                      subtitle,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: small.copyWith(fontStyle: FontStyle.italic),
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
