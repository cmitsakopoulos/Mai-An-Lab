import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';
import 'package:permission_handler/permission_handler.dart';

import '../core/app_config.dart';
import '../library/library_dialogs.dart';
import '../library/track_query.dart';
import '../player/queue_store.dart';
import '../scan/library_folder.dart';
import '../ui/tokens.dart';
import 'folder_browser.dart';
import 'settings_page.dart';

InputDecoration _fieldDecoration(BuildContext context, String label) {
  final accent = Theme.of(context).colorScheme.primary;
  OutlineInputBorder border(Color c) => OutlineInputBorder(
    borderRadius: BorderRadius.circular(10),
    borderSide: BorderSide(color: c),
  );
  return InputDecoration(
    labelText: label,
    labelStyle: TextStyle(color: accent, fontSize: 11),
    filled: true,
    fillColor: Tokens.surface2,
    enabledBorder: border(Tokens.border),
    focusedBorder: border(accent),
    border: border(Tokens.border),
    isDense: true,
  );
}

/// A Flet `ft.Dropdown` with a floating label: the options' keys and texts.
class SettingsDropdown extends StatelessWidget {
  const SettingsDropdown({
    super.key,
    required this.label,
    required this.value,
    required this.options,
    required this.onChanged,
  });

  final String label;
  final String? value;
  final List<(String, String)> options;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) {
    // A stored value no option has shows blank, as Flet's dropdown does.
    final known = options.any((o) => o.$1 == value);
    return DropdownButtonFormField<String>(
      // ignore: deprecated_member_use
      value: known ? value : null,
      isExpanded: true,
      dropdownColor: Tokens.surface2,
      decoration: _fieldDecoration(context, label),
      style: const TextStyle(color: Tokens.text, fontSize: 13),
      items: [
        for (final (k, t) in options)
          DropdownMenuItem(value: k, child: Text(t)),
      ],
      onChanged: (v) {
        if (v != null) onChanged(v);
      },
    );
  }
}

/// A switch with its label to the right (settings.py's `ft.Row([switch,
/// text])`).
class SwitchRow extends StatelessWidget {
  const SwitchRow({
    super.key,
    required this.value,
    required this.label,
    required this.onChanged,
    this.trailing,
  });

  final bool value;
  final String label;
  final ValueChanged<bool>? onChanged;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) => Row(
    children: [
      Switch(
        value: value,
        activeThumbColor: Theme.of(context).colorScheme.primary,
        onChanged: onChanged,
      ),
      const SizedBox(width: 10),
      Flexible(
        child: GestureDetector(
          onTap: onChanged == null ? null : () => onChanged!(!value),
          child: Text(
            label,
            style: const TextStyle(color: Tokens.text, fontSize: 12),
          ),
        ),
      ),
      if (trailing != null) ...[const SizedBox(width: 10), trailing!],
    ],
  );
}

// ---------------------------------------------------------------------------
// Storage & Paths

/// settings.py `_build_storage_group`, `_save_paths` and the folder pick.
/// The Default Download Quality card waits for downloads (decision A); its
/// stored value is written back unchanged, as Flet's untouched dropdown does.
class StorageGroup extends StatefulWidget {
  const StorageGroup({super.key, required this.deps});

  final SettingsDeps deps;

  @override
  State<StorageGroup> createState() => _StorageGroupState();
}

class _StorageGroupState extends State<StorageGroup> {
  final _dl = TextEditingController();
  final _lib = TextEditingController();
  String _baseDl = '', _baseLib = '';
  String _quality = '';

  SettingsDeps get d => widget.deps;

  @override
  void initState() {
    super.initState();
    _dl.addListener(_changed);
    _lib.addListener(_changed);
    _quality = d.config.string('general', 'default_quality', '').toLowerCase();
    _refresh();
  }

  @override
  void dispose() {
    _dl.dispose();
    _lib.dispose();
    super.dispose();
  }

  void _changed() => setState(() {});

  /// `refresh`: the resolved folders, and a clean baseline.
  Future<void> _refresh() async {
    final (library, download, _) = await resolveFolders(d.dataDir);
    if (!mounted) return;
    setState(() {
      _dl.text = _baseDl = download;
      _lib.text = _baseLib = library;
    });
  }

  bool get _dirty => _dl.text.trim() != _baseDl || _lib.text.trim() != _baseLib;

  Future<String?> _privateError(String path) async {
    final sp = await storagePathsFor(d.dataDir);
    return sp.isAppPrivate(path)
        ? "That folder is inside the app's private storage, which no files "
              'app can open. Pick a folder in shared storage.'
        : null;
  }

  Future<void> _save() async {
    final dl = _dl.text.trim();
    final lib = _lib.text.trim();
    if (dl.isEmpty || lib.isEmpty) {
      showSettingsSnack(context, 'Paths cannot be empty.');
      return;
    }
    final bad = await _privateError(dl) ?? await _privateError(lib);
    if (!mounted) return;
    if (bad != null) {
      showSettingsSnack(context, bad);
      return;
    }
    await d.config.update({
      'downloads': {'folder': dl},
    });
    await d.config.update({
      'general': {'default_quality': _quality},
    });
    final prefs = Prefs(d.dataDir);
    await prefs.load();
    await prefs.set('folder_path', dl);
    await prefs.set('library_path', lib);
    await prefs.flush();
    await _refresh();
    if (!mounted) return;
    showSettingsSnack(context, 'Storage paths updated.');
    unawaited(d.library.scan());
  }

  /// `_handle_folder_picked`: saved at once, no scan.
  Future<void> _picked(String path, {required bool download}) async {
    final bad = await _privateError(path);
    if (!mounted) return;
    if (bad != null) {
      showSettingsSnack(context, bad);
      return;
    }
    final prefs = Prefs(d.dataDir);
    await prefs.load();
    if (download) {
      await d.config.update({
        'downloads': {'folder': path},
      });
      await prefs.set('folder_path', path);
    } else {
      await prefs.set('library_path', path);
    }
    await prefs.flush();
    await _refresh();
    if (!mounted) return;
    showSettingsSnack(
      context,
      '${download ? 'Download' : 'Library'} folder set: $path',
    );
  }

  Future<void> _browse({required bool download}) async {
    final sp = await storagePathsFor(d.dataDir);
    if (!mounted) return;
    final path = await showFolderBrowser(
      context,
      label: download ? 'Download Folder' : 'Library Folder',
      isPrivate: sp.isAppPrivate,
    );
    if (path != null) await _picked(path, download: download);
  }

  Widget _folderCard({
    required IconData icon,
    required String title,
    required String description,
    required TextEditingController field,
    required String label,
    required bool download,
  }) {
    final accent = Theme.of(context).colorScheme.primary;
    return SettingsCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Icon(icon, color: accent, size: 18),
              const SizedBox(width: 8),
              Text(
                title,
                style: const TextStyle(
                  color: Tokens.text,
                  fontSize: 13,
                  fontWeight: FontWeight.bold,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Dim(description, size: 11),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: TextField(
                  controller: field,
                  style: const TextStyle(color: Tokens.text, fontSize: 13),
                  decoration: _fieldDecoration(context, label),
                ),
              ),
              // Flet's Android folder navigator; the desktop pickers aren't
              // ported (type the path).
              if (Platform.isAndroid) ...[
                const SizedBox(width: 8),
                IconButton(
                  icon: const Icon(Icons.folder_open_rounded),
                  color: accent,
                  tooltip: 'Browse folder',
                  onPressed: () => _browse(download: download),
                ),
              ],
            ],
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return GroupBody(
      onSave: _dirty ? _save : null,
      saveLabel: 'SAVE PATHS',
      saveIcon: Icons.folder_rounded,
      children: [
        SettingsCard(
          child: Row(
            children: [
              Icon(
                Icons.storage_rounded,
                color: Theme.of(context).colorScheme.primary,
                size: 24,
              ),
              const SizedBox(width: 14),
              const Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Storage Locations & Directories',
                      style: TextStyle(
                        color: Tokens.text,
                        fontSize: 15,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                    SizedBox(height: 2),
                    Dim(
                      'Configure filesystem paths for downloading audio files '
                      'and scanning your local music collection.',
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
        _folderCard(
          icon: Icons.download_rounded,
          title: 'Downloads Directory',
          description:
              'Destination folder where downloaded tracks, albums, and '
              'Hi-Res FLAC files are saved.',
          field: _dl,
          label: 'Download Path',
          download: true,
        ),
        _folderCard(
          icon: Icons.library_music_rounded,
          title: 'Local Music Library',
          description:
              'Root folder scanned by Streamrip to index your local audio '
              'files, tags, and album artwork.',
          field: _lib,
          label: 'Library Path',
          download: false,
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Appearance

/// settings.py `_build_color_selector`'s twelve accents.
const kAccentChoices = [
  ('Cyan', '#64D2FF'),
  ('Blue', '#0A84FF'),
  ('Indigo', '#5E5CE6'),
  ('Purple', '#BF5AF2'),
  ('Lavender', '#D0BCFF'),
  ('Pink', '#FF375F'),
  ('Red', '#FF453A'),
  ('Orange', '#FF9F0A'),
  ('Yellow', '#FFD60A'),
  ('Green', '#30D158'),
  ('Emerald', '#25E89B'),
  ('Mint', '#63E6E2'),
];

/// settings.py `_build_appearance_group` and `_save_appearance_settings`.
/// The Jarvis and Acoustic Network switches wait for their tabs (phases 5
/// and 3i); their stored values are written back unchanged.
class AppearanceGroup extends StatefulWidget {
  const AppearanceGroup({super.key, required this.deps});

  final SettingsDeps deps;

  @override
  State<AppearanceGroup> createState() => _AppearanceGroupState();
}

class _AppearanceGroupState extends State<AppearanceGroup> {
  late Map<String, Object?> _state = _read();
  late Map<String, Object?> _baseline = Map.of(_state);

  SettingsDeps get d => widget.deps;

  /// `refresh`'s reads.
  Map<String, Object?> _read() {
    final c = d.config;
    bool landing(String k) => switch (c.value('landing', k)) {
      null => true,
      final Object v => _pyBool(v),
    };
    bool tab(String k, bool fallback) =>
        _toBool(c.value('appearance', 'show_$k'), fallback);
    final rawAccent = c.string('appearance', 'accent_color', '#FFD60A');
    return {
      'startup_page': c.string('general', 'startup_page', 'Library'),
      'default_sort': c.string('general', 'library_sort', 'date'),
      'pill_style': c.string('appearance', 'pill_style', 'category'),
      'most_listened': landing('show_search_history'),
      'library_stats': landing('show_library_stats'),
      'top_artists': landing('show_top_artists'),
      'top_genres': landing('show_top_genres'),
      'new_releases': landing('show_new_releases'),
      'jarvis': tab('jarvis', true),
      'network': tab('network', false),
      'playlists': tab('playlists', true),
      'genres': tab('genres', true),
      'artists': tab('artists', true),
      'albums': tab('albums', false),
      'tracks': tab('tracks', true),
      'accent_color':
          AppConfig.legacyAccents[rawAccent.toUpperCase()] ?? rawAccent,
    };
  }

  static bool _pyBool(Object v) => switch (v) {
    bool b => b,
    String s => s.isNotEmpty,
    num n => n != 0,
    List<Object?> l => l.isNotEmpty,
    Map<Object?, Object?> m => m.isNotEmpty,
    _ => true,
  };

  /// settings.py `refresh._to_bool`.
  static bool _toBool(Object? v, bool fallback) => switch (v) {
    null => fallback,
    bool b => b,
    String s => const {'true', '1', 'yes'}.contains(s.trim().toLowerCase()),
    final Object o => _pyBool(o),
  };

  void _set(String k, Object? v) => setState(() => _state[k] = v);

  bool get _dirty => !mapEquals(_state, _baseline);

  Future<void> _save() async {
    final s = _state;
    await d.config.update({
      'appearance': {
        'accent_color': s['accent_color'],
        'pill_style': s['pill_style'] ?? 'category',
        'show_jarvis': s['jarvis'],
        'show_network': s['network'],
        'show_playlists': s['playlists'],
        'show_genres': s['genres'],
        'show_artists': s['artists'],
        'show_albums': s['albums'],
        'show_tracks': s['tracks'],
      },
      'landing': {
        'show_search_history': s['most_listened'],
        'show_library_stats': s['library_stats'],
        'show_top_artists': s['top_artists'],
        'show_top_genres': s['top_genres'],
        'show_new_releases': s['new_releases'],
      },
      'general': {
        'startup_page': s['startup_page'],
        'default_sort': s['default_sort'],
        // Flet writes only default_sort here, which nothing reads; the
        // library's sort is library_sort (PORT_MAP §7).
        'library_sort': s['default_sort'],
      },
    });
    d.library.setSort(LibrarySort.fromConfig('${s['default_sort']}'));
    if (!mounted) return;
    setState(() {
      _state = _read();
      _baseline = Map.of(_state);
    });
    showSettingsSnack(context, 'Appearance and interface settings saved.');
  }

  Widget _switch(String key, String label, {Widget? trailing}) => SwitchRow(
    value: _state[key] == true,
    label: label,
    onChanged: (v) => _set(key, v),
    trailing: trailing,
  );

  @override
  Widget build(BuildContext context) {
    const divider = Divider(color: Tokens.border, height: 20);
    return GroupBody(
      spacing: 20,
      onSave: _dirty ? _save : null,
      saveLabel: 'SAVE APPEARANCE SETTINGS',
      saveIcon: Icons.palette_rounded,
      children: [
        const Dim('Customize how the app looks and behaves on startup.'),
        SettingsDropdown(
          label: 'Startup Page',
          value: '${_state['startup_page']}',
          options: const [('Search', 'Search'), ('Library', 'Library')],
          onChanged: (v) => _set('startup_page', v),
        ),
        SettingsDropdown(
          label: 'Default Library Sort',
          value: '${_state['default_sort']}',
          options: [for (final s in LibrarySort.values) (s.name, s.label)],
          onChanged: (v) => _set('default_sort', v),
        ),
        SettingsDropdown(
          label: 'Library Pill Styling',
          value: '${_state['pill_style']}',
          options: const [
            ('category', 'Category Tints (Curated Apple Glow)'),
            ('unified', 'Unified Accent (Monochrome Glow)'),
          ],
          onChanged: (v) => _set('pill_style', v),
        ),
        divider,
        const AccentLabel('Landing Page Sections'),
        _switch('new_releases', 'Show New From Your Artists'),
        _switch('most_listened', 'Show Most Listened Tracks'),
        _switch('top_artists', 'Show Most Listened Artists'),
        _switch('top_genres', 'Show Most Listened Genres'),
        _switch('library_stats', 'Show Library Stats'),
        divider,
        const AccentLabel('Library Tabs Visibility'),
        _switch('playlists', 'Playlists'),
        _switch('genres', 'Genres'),
        _switch('artists', 'Artists'),
        _switch('albums', 'Albums'),
        _switch('tracks', 'Tracks'),
        divider,
        const AccentLabel('Accent Color'),
        Wrap(
          spacing: 12,
          runSpacing: 12,
          children: [
            for (final (name, hex) in kAccentChoices)
              _AccentDot(
                name: name,
                hex: hex,
                selected:
                    hex.toLowerCase() ==
                    '${_state['accent_color']}'.toLowerCase(),
                onTap: () => _set('accent_color', hex),
              ),
          ],
        ),
        const SizedBox(height: 50),
      ],
    );
  }
}

class _AccentDot extends StatelessWidget {
  const _AccentDot({
    required this.name,
    required this.hex,
    required this.selected,
    required this.onTap,
  });

  final String name, hex;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => Tooltip(
    message: name,
    child: GestureDetector(
      onTap: onTap,
      child: Container(
        width: 32,
        height: 32,
        decoration: BoxDecoration(
          color: AppConfig.parseAccent(hex),
          shape: BoxShape.circle,
          border: selected
              ? Border.all(color: Tokens.text, width: 2.5)
              : Border.all(color: const Color(0x26FFFFFF)),
        ),
      ),
    ),
  );
}

// ---------------------------------------------------------------------------
// Haptic Feedback

const _levels = [
  ('none', 'None'),
  ('light', 'Light'),
  ('medium', 'Medium'),
  ('heavy', 'Heavy'),
  ('vibrate', 'Vibrate (Long)'),
];

const _selectionLevels = [
  ('none', 'None'),
  ('selection', 'Selection Click'),
  ('light', 'Light'),
  ('medium', 'Medium'),
  ('heavy', 'Heavy'),
];

/// settings.py `_build_haptics_group`: every change is saved at once.
class HapticsGroup extends StatefulWidget {
  const HapticsGroup({super.key, required this.deps});

  final SettingsDeps deps;

  /// The six dropdowns: config key, label, options, default.
  static const rows = [
    ('eq_drag', 'EQ node drag & sliders', _levels, 'light'),
    ('swipe_queue', 'Swipe track to Queue', _levels, 'medium'),
    ('long_press', 'Long press track', _levels, 'heavy'),
    (
      'network_tap',
      'Network node tap & inspect',
      _selectionLevels,
      'selection',
    ),
    ('network_reseed', 'Network reseed graph', _levels, 'medium'),
    ('network_walk', 'Network walk step traversal', _selectionLevels, 'light'),
  ];

  @override
  State<HapticsGroup> createState() => _HapticsGroupState();
}

class _HapticsGroupState extends State<HapticsGroup> {
  SettingsDeps get d => widget.deps;
  late bool _enabled = switch (d.config.value(
    'haptics',
    'haptic_feedback_enabled',
  )) {
    null => true,
    final bool b => b,
    final Object o => '$o'.isNotEmpty,
  };
  late final Map<String, String> _levelsNow = {
    for (final (k, _, _, def) in HapticsGroup.rows)
      k: d.config.string('haptics', '${k}_intensity', def),
  };

  Future<void> _toggle(bool v) async {
    setState(() => _enabled = v);
    await d.config.update({
      'haptics': {'haptic_feedback_enabled': v},
    });
  }

  /// `_on_haptic_intensity_change`: all six written, then a sample.
  Future<void> _level(String key, String v) async {
    setState(() => _levelsNow[key] = v);
    await d.config.update({
      'haptics': {
        for (final (k, _, _, _) in HapticsGroup.rows)
          '${k}_intensity': _levelsNow[k],
      },
    });
    if (v != 'none') unawaited(d.haptics.trigger(v));
  }

  @override
  Widget build(BuildContext context) {
    return GroupBody(
      spacing: 20,
      children: [
        const Dim(
          'Customize tactile vibration effects for user gestures and '
          'actions (Android only).',
        ),
        SettingsCard(
          radius: 10,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const CardTitle(Icons.vibration_rounded, 'Global Control'),
              const SizedBox(height: 10),
              const Dim(
                'Enable or completely disable tactile haptic feedback '
                'vibrations throughout the application.',
              ),
              const SizedBox(height: 10),
              SwitchRow(
                value: _enabled,
                label: 'Enable Haptic Feedback',
                onChanged: _toggle,
              ),
            ],
          ),
        ),
        SettingsCard(
          radius: 10,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const CardTitle(Icons.touch_app_rounded, 'Effect Intensities'),
              const SizedBox(height: 10),
              const Dim(
                'Configure the vibration intensity for each tactile action. '
                "Select 'None' to disable haptics for that action.",
              ),
              for (final (k, label, options, _) in HapticsGroup.rows) ...[
                const SizedBox(height: 15),
                SettingsDropdown(
                  label: label,
                  value: _levelsNow[k],
                  options: options,
                  onChanged: (v) => _level(k, v),
                ),
              ],
            ],
          ),
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Permissions

/// settings.py `_PERMISSION_SPECS` and `_build_permissions_group`: each
/// permission's status, re-read whenever the app comes back to the front.
class PermissionsGroup extends StatefulWidget {
  const PermissionsGroup({super.key});

  static final specs = [
    (
      Permission.notification,
      'Notifications',
      'Required for media controls on the lock screen',
    ),
    (
      Permission.audio,
      'Audio Files',
      'Read access to music files (Android 13+)',
    ),
    (
      Permission.storage,
      'Storage',
      'Read/write external storage (Android ≤12)',
    ),
    (
      Permission.manageExternalStorage,
      'All Files Access',
      'Required to delete or edit songs on Android 11+',
    ),
    (Permission.microphone, 'Microphone', 'Required for Jarvis voice commands'),
  ];

  @override
  State<PermissionsGroup> createState() => _PermissionsGroupState();
}

class _PermissionsGroupState extends State<PermissionsGroup> {
  Map<Permission, PermissionStatus>? _status;
  AppLifecycleListener? _lifecycle;

  @override
  void initState() {
    super.initState();
    if (Platform.isAndroid) {
      _refresh();
      _lifecycle = AppLifecycleListener(onResume: _refresh);
    }
  }

  @override
  void dispose() {
    _lifecycle?.dispose();
    super.dispose();
  }

  Future<void> _refresh() async {
    try {
      final out = {
        for (final (p, _, _) in PermissionsGroup.specs) p: await p.status,
      };
      if (mounted) setState(() => _status = out);
    } catch (e) {
      debugPrint('query permissions failed: $e');
    }
  }

  @override
  Widget build(BuildContext context) {
    if (!Platform.isAndroid) {
      return const GroupBody(
        spacing: 12,
        children: [
          Dim(
            'Permissions are managed by the OS on this platform; this panel '
            'only applies on Android.',
          ),
        ],
      );
    }
    final accent = Theme.of(context).colorScheme.primary;
    return GroupBody(
      spacing: 12,
      children: [
        SettingsCard(
          padding: 12,
          radius: 8,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const CardTitle(
                Icons.info_outline_rounded,
                'How to Grant Permissions on Android',
                size: 13,
              ),
              const SizedBox(height: 6),
              const Dim(
                'Android restricts sensitive permissions for security. To '
                'grant or manage access:\n'
                "1. Tap 'OPEN SYSTEM SETTINGS' below to open the Android App "
                'Info page for Mai An Lab.\n'
                "2. Tap 'Permissions'.\n"
                '3. Select the required permission (e.g. Files and Media, '
                "Microphone) and set it to 'Allow'.",
                size: 11,
              ),
              const SizedBox(height: 8),
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton.icon(
                  icon: const Icon(Icons.open_in_new_rounded, size: 16),
                  label: const Text('OPEN SYSTEM SETTINGS'),
                  onPressed: openAppSettings,
                ),
              ),
            ],
          ),
        ),
        for (final (p, label, desc) in PermissionsGroup.specs)
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: Tokens.border),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        label,
                        style: const TextStyle(
                          color: Tokens.text,
                          fontSize: 14,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    switch (_status?[p]) {
                      null => const Dim('checking…', size: 11),
                      final s => Text(
                        s.name.toUpperCase(),
                        style: TextStyle(
                          color: s.isGranted ? accent : const Color(0xFFFF8866),
                          fontSize: 11,
                        ),
                      ),
                    },
                  ],
                ),
                const SizedBox(height: 4),
                Dim(desc, size: 11),
              ],
            ),
          ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Database Management

/// settings.py `_build_database_management_group`: the maintenance half.
/// The compute pipeline (DSP, PCA) waits for decision B.
class DatabaseGroup extends StatefulWidget {
  const DatabaseGroup({super.key, required this.deps});

  final SettingsDeps deps;

  @override
  State<DatabaseGroup> createState() => _DatabaseGroupState();
}

class _DatabaseGroupState extends State<DatabaseGroup> {
  SettingsDeps get d => widget.deps;
  bool _busy = false;

  /// main.py `open_maintenance_confirmation` / `open_wipe_confirmation`.
  Future<void> _confirmThen(
    String title,
    String message,
    String button,
    Future<String> Function() action,
  ) async {
    final ok = await confirmDelete(
      context,
      title: title,
      message: message,
      confirmLabel: button,
    );
    if (!ok || !mounted) return;
    setState(() => _busy = true);
    final text = await action();
    if (!mounted) return;
    setState(() => _busy = false);
    showSettingsSnack(context, text);
  }

  /// The queue goes with the index (main.py: `audio_engine.clear_queue()`
  /// and the queue files).
  Future<void> _clearQueue() async {
    final player = d.player.value;
    if (player != null) {
      player.stop();
      player.clearQueue();
      await player.settled;
    }
    final store = QueueStore(d.dataDir);
    for (final p in [
      store.queueStatePath,
      store.positionPath,
      for (final n in ['regular', 'shuffle', 'similar'])
        store.partitionPath('queue_$n.json'),
    ]) {
      try {
        await File(p).delete();
      } on FileSystemException {
        // Not there.
      }
    }
  }

  Future<String> _run(
    Future<void> Function() job, {
    required String ok,
    required String failed,
    bool clearsQueue = false,
  }) async {
    final repo = d.library.repository;
    if (repo == null) return '$failed: the library is not open';
    try {
      await job();
      if (clearsQueue) await _clearQueue();
      await d.library.reload();
      return ok;
    } catch (e) {
      return '$failed: $e';
    }
  }

  @override
  Widget build(BuildContext context) {
    final repo = d.library.repository;
    final can = !_busy && (repo?.writable ?? false);
    return GroupBody(
      spacing: 15,
      children: [
        const Text(
          'Database Maintenance',
          style: TextStyle(color: Tokens.dim, fontWeight: FontWeight.bold),
        ),
        const Dim(
          'Wipe or reset specific tables and indexes in the local application '
          'database. Your actual music files will NOT be touched.',
        ),
        Wrap(
          spacing: 10,
          children: [
            TextButton.icon(
              icon: const Icon(Icons.format_list_bulleted_rounded),
              label: const Text('Library Index'),
              onPressed: !can
                  ? null
                  : () => _confirmThen(
                      'Clear Library Index?',
                      'This will clear all indexed music, playlists, and '
                          'metadata from the local database, leaving '
                          'configuration, history, and play counts intact.'
                          '\n\nYour actual music files will NOT be touched.',
                      'Clear Index',
                      () => _run(
                        repo!.clearLibraryIndex,
                        ok: 'Library index cleared successfully.',
                        failed: 'Failed to clear library index',
                        clearsQueue: true,
                      ),
                    ),
            ),
            TextButton.icon(
              icon: const Icon(Icons.graphic_eq_rounded),
              label: const Text('DSP Features'),
              onPressed: !can
                  ? null
                  : () => _confirmThen(
                      'Clear DSP Features?',
                      'This will purge all extracted acoustic DSP features '
                          'and PCA space definitions, forcing a full '
                          'recalculation/re-scan on your next sweep.\n\n'
                          'Your library index will remain intact.',
                      'Clear DSP',
                      () => _run(
                        repo!.clearDspFeatures,
                        ok: 'Acoustic DSP features cleared successfully.',
                        failed: 'Failed to clear DSP features',
                      ),
                    ),
            ),
            TextButton.icon(
              icon: const Icon(Icons.delete_forever, color: Color(0xFFFF4444)),
              label: const Text('Wipe DB'),
              onPressed: !can
                  ? null
                  : () => _confirmThen(
                      'Wipe Library Database?',
                      'This will clear the search index and all metadata '
                          'from the database. \n\nIMPORTANT: Your local music '
                          'files will NOT be touched or deleted.',
                      'Wipe Database',
                      () => _run(
                        repo!.wipeDatabase,
                        ok: 'Library database wiped successfully.',
                        failed: 'Wipe failed',
                        clearsQueue: true,
                      ),
                    ),
            ),
          ],
        ),
        if (_busy) const LinearProgressIndicator(),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Advanced

/// settings.py `_build_advanced_group`: cache maintenance and the raw TOML
/// editor. State export/import comes with phase 4; demo search, the
/// play-count debug button and the fresh-install reset with decision C.
class AdvancedGroup extends StatefulWidget {
  const AdvancedGroup({super.key, required this.deps});

  final SettingsDeps deps;

  @override
  State<AdvancedGroup> createState() => _AdvancedGroupState();
}

class _AdvancedGroupState extends State<AdvancedGroup> {
  final _editor = TextEditingController();
  SettingsDeps get d => widget.deps;

  @override
  void initState() {
    super.initState();
    d.config.readText().then((t) {
      if (mounted) _editor.text = t;
    });
  }

  @override
  void dispose() {
    _editor.dispose();
    super.dispose();
  }

  /// main.py `clear_album_artwork_cache`: Flet's `temp/` (all but
  /// `.nomedia`), and the artwork cache the Kotlin resolver keeps
  /// (`cacheDir/np_art`), which the port uses instead.
  Future<String> _clearAlbumCache() async {
    try {
      final temp = Directory('${d.dataDir}${Platform.pathSeparator}temp');
      if (await temp.exists()) {
        await for (final e in temp.list()) {
          if (e.uri.pathSegments.where((s) => s.isNotEmpty).last ==
              '.nomedia') {
            continue;
          }
          try {
            await e.delete(recursive: true);
          } on FileSystemException {
            // Python skips what it can't remove.
          }
        }
      }
      if (Platform.isAndroid) {
        final art = Directory('${(await getTemporaryDirectory()).path}/np_art');
        if (await art.exists()) await art.delete(recursive: true);
      }
      return 'Album artwork cache cleared.';
    } catch (e) {
      return 'Failed to clear album cache: $e';
    }
  }

  /// main.py `clear_preview_cache`.
  Future<String> _clearPreviewCache() async {
    try {
      final dir = Directory('${d.dataDir}${Platform.pathSeparator}previews');
      if (await dir.exists()) await dir.delete(recursive: true);
      return 'Preview cache cleared.';
    } catch (e) {
      return 'Could not clear cache: $e';
    }
  }

  Future<void> _snackOf(Future<String> Function() job) async {
    final text = await job();
    if (mounted) showSettingsSnack(context, text);
  }

  /// `_save_config`: the text as typed.
  Future<void> _saveConfig() async {
    try {
      await d.config.saveText(_editor.text);
      if (mounted) showSettingsSnack(context, 'Configuration saved.');
    } catch (e) {
      if (mounted) showSettingsSnack(context, 'Save failed: $e');
    }
  }

  @override
  Widget build(BuildContext context) {
    const heading = TextStyle(color: Tokens.dim, fontWeight: FontWeight.bold);
    return GroupBody(
      spacing: 15,
      children: [
        const Text('Cache Maintenance', style: heading),
        Wrap(
          spacing: 10,
          children: [
            TextButton.icon(
              icon: const Icon(Icons.image_rounded),
              label: const Text('Album Cache'),
              onPressed: () => _snackOf(_clearAlbumCache),
            ),
            TextButton.icon(
              icon: const Icon(Icons.music_note_rounded),
              label: const Text('Preview Cache'),
              onPressed: () => _snackOf(_clearPreviewCache),
            ),
          ],
        ),
        const Divider(color: Tokens.border, height: 40),
        const Text('Raw Configuration (TOML)', style: heading),
        const Dim(
          'Directly edit the Streamrip TOML configuration file for advanced '
          'control.',
        ),
        TextField(
          controller: _editor,
          minLines: 15,
          maxLines: 25,
          style: const TextStyle(
            color: Tokens.text,
            fontFamily: 'monospace',
            fontSize: 11,
          ),
          decoration: InputDecoration(
            filled: true,
            fillColor: Tokens.surface,
            contentPadding: const EdgeInsets.all(12),
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(8),
              borderSide: const BorderSide(color: Tokens.border),
            ),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(8),
              borderSide: const BorderSide(color: Tokens.border),
            ),
          ),
        ),
        Center(
          child: OnyxButton(
            label: 'SAVE CONFIG FILE',
            icon: Icons.terminal,
            onTap: _saveConfig,
          ),
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// About

/// settings.py `_build_about_group`.
class AboutGroup extends StatelessWidget {
  const AboutGroup({super.key});

  static const github = 'https://github.com/cmitsakopoulos/Mai-An-Lab';

  static const _features = [
    (
      'High-Fidelity Audio & Streaming',
      'Seamless Qobuz & Deezer integration supporting Hi-Res FLAC and MP3 '
          'streaming, metadata fetching, and track downloading with your '
          'personal accounts.',
      Icons.high_quality_rounded,
    ),
    (
      'DSP & Similarity Engine',
      'Native audio analysis (spectral centroid, RMS energy, rhythm/tempo, '
          'timbre vectors) powering intelligent Auto-Play queues and track '
          'similarity matching.',
      Icons.graphic_eq_rounded,
    ),
    (
      'Jarvis AI Assistant',
      'Natural language voice & text control powered by Google Gemini or '
          'local LLM servers (Ollama, LM Studio) for queue, playback, and '
          'discovery management.',
      Icons.smart_toy_rounded,
    ),
    (
      'Parametric Equalizer & DSP',
      'Interactive 5-band equalizer with real-time curve rendering, custom '
          'preset saving, and audio dynamism enhancement.',
      Icons.tune_rounded,
    ),
    (
      'Metadata Workbench',
      'Advanced tag cleaning, canonical artist normalization, and '
          'MusicBrainz metadata enrichment to optimize library intelligence.',
      Icons.label_important_rounded,
    ),
    (
      'Library & Playback Analytics',
      'Track play history, listener statistics, most listened breakdown, '
          'and local cache management.',
      Icons.analytics_rounded,
    ),
  ];

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    const bold = TextStyle(color: Tokens.text, fontWeight: FontWeight.bold);
    const divider = Divider(color: Tokens.border, height: 30);
    return GroupBody(
      spacing: 12,
      children: [
        Center(
          child: Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: Column(
              children: [
                Text(
                  'Mai An Lab',
                  style: TextStyle(
                    color: accent,
                    fontSize: 28,
                    fontWeight: FontWeight.w900,
                  ),
                ),
                const SizedBox(height: 4),
                const Dim('Version 2.0.0', size: 14),
              ],
            ),
          ),
        ),
        const Text('Summary', style: bold),
        const Dim(
          'Mai An Lab is a modern high-fidelity audio player and streaming '
          'application. Built with Flutter, it pairs audiophile-grade '
          'playback with intelligent DSP similarity search, AI assistant '
          'controls, and comprehensive library tools.',
        ),
        const SizedBox(height: 5),
        const Text('Core Features', style: bold),
        for (final (title, desc, icon) in _features)
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: Tokens.surface2,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: Tokens.border),
            ),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(icon, color: accent, size: 20),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        title,
                        style: const TextStyle(
                          color: Tokens.text,
                          fontSize: 13,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Dim(desc),
                    ],
                  ),
                ),
              ],
            ),
          ),
        divider,
        const Text('Developer', style: bold),
        const Dim('Christophoros Mitsakopoulos', size: 13),
        divider,
        const Text('Credits', style: bold),
        const Dim('• Streamrip by nathom and community'),
        const Dim('• Flutter and its community'),
        const Divider(color: Tokens.border, height: 40),
        Wrap(
          alignment: WrapAlignment.center,
          children: [
            // No browser hand-off yet: the link is copied instead.
            TextButton.icon(
              icon: const Icon(Icons.code_rounded),
              label: const Text('Project GitHub'),
              onPressed: () async {
                await Clipboard.setData(const ClipboardData(text: github));
                if (context.mounted) {
                  showSettingsSnack(context, 'Link copied: $github');
                }
              },
            ),
            TextButton.icon(
              icon: const Icon(Icons.gavel_rounded),
              label: const Text('Licenses'),
              onPressed: () => showLicensePage(
                context: context,
                applicationName: 'Mai An Lab',
                applicationVersion: '2.0.0',
              ),
            ),
            TextButton.icon(
              icon: const Icon(Icons.person_rounded),
              label: const Text('Developer'),
              onPressed: () => showSettingsSnack(
                context,
                'Contact: mitsacopoulos@gmail.com',
              ),
            ),
          ],
        ),
        const SizedBox(height: 20),
        const Center(
          child: Text(
            '2026 Mai An Lab',
            style: TextStyle(
              color: Tokens.dim,
              fontSize: 11,
              fontStyle: FontStyle.italic,
            ),
          ),
        ),
      ],
    );
  }
}
