import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';
import 'package:permission_handler/permission_handler.dart';

import '../analysis/library_analysis.dart';
import '../core/app_config.dart';
import '../core/app_version.dart';
import '../library/browse_controller.dart';
import '../library/browse_view.dart';
import '../library/library_dialogs.dart';
import '../library/track_query.dart';
import '../player/queue_store.dart';
import '../scan/library_folder.dart';
import '../data/state_export.dart' show defaultBundleDir;
import '../sync/state_bundles.dart' show kSnapshotName;
import '../ui/frost.dart';
import '../ui/tokens.dart';
import 'folder_browser.dart';
import 'settings_kit.dart';
import 'settings_page.dart';

/// Modern Apple-style grouped inset section with an uppercase accent header
/// above a clean [GlassCard]. Shared by Appearance, AI Assistant, Audio & DSP.
class LegacySettingsSection extends StatelessWidget {
  const LegacySettingsSection({
    super.key,
    required this.label,
    required this.child,
    this.padding = const EdgeInsets.all(16),
    this.trailing,
  });

  final String label;
  final Widget child;
  final EdgeInsetsGeometry padding;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Padding(
        padding: const EdgeInsets.only(left: 4, right: 4, bottom: 8),
        child: Row(
          children: [
            AccentLabel(label, size: 11),
            if (trailing != null) ...[const Spacer(), trailing!],
          ],
        ),
      ),
      GlassCard(padding: padding, child: child),
    ],
  );
}

/// Modern right-aligned switch row for grouped inset settings cards.
class InsetSwitchRow extends StatelessWidget {
  const InsetSwitchRow({
    super.key,
    required this.label,
    required this.value,
    required this.onChanged,
    this.subtitle,
  });

  final String label;
  final String? subtitle;
  final bool value;
  final ValueChanged<bool>? onChanged;

  @override
  Widget build(BuildContext context) => InkWell(
    onTap: onChanged == null ? null : () => onChanged!(!value),
    borderRadius: BorderRadius.circular(10),
    child: Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  label,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 14,
                    fontWeight: FontWeight.w500,
                  ),
                ),
                if (subtitle != null) ...[
                  const SizedBox(height: 2),
                  Dim(subtitle!, size: 11),
                ],
              ],
            ),
          ),
          const SizedBox(width: 12),
          AppSwitch(value: value, onChanged: onChanged),
        ],
      ),
    ),
  );
}

/// Tappable picker row showing a label on the left and the current value +
/// chevron on the right (opens a frosted bottom sheet).
class SettingsPickerRow extends StatelessWidget {
  const SettingsPickerRow({
    super.key,
    required this.label,
    required this.valueText,
    required this.onTap,
    this.subtitle,
  });

  final String label;
  final String valueText;
  final String? subtitle;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return InkWell(
      borderRadius: BorderRadius.circular(10),
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    label,
                    style: const TextStyle(
                      color: Tokens.text,
                      fontSize: 14,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                  if (subtitle != null) ...[
                    const SizedBox(height: 2),
                    Dim(subtitle!, size: 11),
                  ],
                ],
              ),
            ),
            const SizedBox(width: 12),
            ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 165),
              child: Text(
                valueText,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                textAlign: TextAlign.right,
                style: TextStyle(
                  color: accent,
                  fontSize: 13,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
            const SizedBox(width: 4),
            const Icon(
              Icons.chevron_right_rounded,
              color: Tokens.dim,
              size: 18,
            ),
          ],
        ),
      ),
    );
  }
}

/// Clean stacked-label text field (label sits cleanly above the rounded box
/// instead of colliding with an OutlineInputBorder floating label).
class ModernTextField extends StatefulWidget {
  const ModernTextField({
    super.key,
    required this.label,
    required this.controller,
    this.hint,
    this.minLines = 1,
    this.maxLines = 1,
    this.obscureToggle = false,
    this.fieldKey,
  });

  final String label;
  final TextEditingController controller;
  final String? hint;
  final int minLines;
  final int maxLines;
  final bool obscureToggle;
  final Key? fieldKey;

  @override
  State<ModernTextField> createState() => _ModernTextFieldState();
}

class _ModernTextFieldState extends State<ModernTextField> {
  late bool _obscured = widget.obscureToggle;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    OutlineInputBorder border(Color c, [double width = 1.0]) =>
        OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: BorderSide(color: c, width: width),
        );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Padding(
          padding: const EdgeInsets.only(left: 2, bottom: 6),
          child: Text(
            widget.label,
            style: const TextStyle(
              color: Tokens.dim,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
        TextField(
          key: widget.fieldKey,
          controller: widget.controller,
          obscureText: _obscured,
          minLines: _obscured ? 1 : widget.minLines,
          maxLines: _obscured ? 1 : widget.maxLines,
          style: const TextStyle(
            color: Tokens.text,
            fontSize: 13,
            height: 1.35,
          ),
          decoration: InputDecoration(
            hintText: widget.hint,
            hintStyle: const TextStyle(
              color: Tokens.textTertiary,
              fontSize: 13,
            ),
            filled: true,
            fillColor: Tokens.surface2,
            contentPadding: const EdgeInsets.symmetric(
              horizontal: 14,
              vertical: 12,
            ),
            enabledBorder: border(Tokens.borderSubtle),
            focusedBorder: border(accent, 1.3),
            border: border(Tokens.borderSubtle),
            suffixIcon: widget.obscureToggle
                ? IconButton(
                    icon: Icon(
                      _obscured
                          ? Icons.visibility_rounded
                          : Icons.visibility_off_rounded,
                      size: 18,
                      color: Tokens.dim,
                    ),
                    tooltip: _obscured ? 'Show key' : 'Hide key',
                    onPressed: () => setState(() => _obscured = !_obscured),
                  )
                : null,
          ),
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Storage & Paths

/// settings.py `_build_storage_group`, `_save_paths` and the folder pick.
class StorageGroup extends StatefulWidget {
  const StorageGroup({super.key, required this.deps});

  final SettingsDeps deps;

  @override
  State<StorageGroup> createState() => _StorageGroupState();
}

/// The default-quality tiers: '' asks on every download.
const kDefaultQualityOptions = [
  SettingsOption('', 'Ask every time'),
  SettingsOption('mp3', 'High', subtitle: 'MP3 / AAC 320kbps'),
  SettingsOption('cd', 'CD Quality', subtitle: '16-bit FLAC'),
  SettingsOption('hires', 'Hi-Res', subtitle: '24-bit FLAC (Deezer: 16-bit)'),
];

class _StorageGroupState extends State<StorageGroup> {
  String _dl = '', _lib = '';
  String _quality = '';

  SettingsDeps get d => widget.deps;

  @override
  void initState() {
    super.initState();
    _quality = _storedQuality;
    _refresh();
  }

  String get _storedQuality =>
      d.config.string('general', 'default_quality', '').toLowerCase();

  /// `refresh`: the resolved folders.
  Future<void> _refresh() async {
    final (library, download, _) = await resolveFolders(d.dataDir);
    if (!mounted) return;
    setState(() {
      _dl = download;
      _lib = library;
      _quality = _storedQuality;
    });
  }

  Future<String?> _privateError(String path) async {
    final sp = await storagePathsFor(d.dataDir);
    return sp.isAppPrivate(path)
        ? "That folder is inside the app's private storage, which no files "
              'app can open. Pick a folder in shared storage.'
        : null;
  }

  Future<void> _setQuality(String q) async {
    setState(() => _quality = q);
    await d.config.update({
      'general': {'default_quality': q},
    });
  }

  /// `_handle_folder_picked`: saved at once. A new library folder is
  /// scanned (the old Save button did that).
  Future<void> _picked(String path, {required bool download}) async {
    path = path.trim();
    if (path.isEmpty) {
      showSettingsSnack(context, 'Paths cannot be empty.');
      return;
    }
    if (path == (download ? _dl : _lib)) return;
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
    if (!download) unawaited(d.library.scan());
  }

  /// Android: Flet's folder navigator. Elsewhere (no navigator ported):
  /// type the path.
  Future<void> _browse({required bool download}) async {
    final label = download ? 'Download Folder' : 'Library Folder';
    final String? path;
    if (Platform.isAndroid) {
      final sp = await storagePathsFor(d.dataDir);
      if (!mounted) return;
      path = await showFolderBrowser(
        context,
        label: label,
        isPrivate: sp.isAppPrivate,
      );
    } else {
      path = await _typePath(label, download ? _dl : _lib);
    }
    if (path != null && mounted) await _picked(path, download: download);
  }

  Future<String?> _typePath(String label, String current) async {
    final field = TextEditingController(text: current);
    final path = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: Tokens.surface,
        title: Text(label),
        content: SizedBox(
          width: 420,
          child: ModernTextField(label: 'Path', controller: field),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, field.text),
            child: const Text('Use Folder'),
          ),
        ],
      ),
    );
    field.dispose();
    return path;
  }

  @override
  Widget build(BuildContext context) {
    return GroupBody(
      spacing: 26,
      children: [
        SettingsSection(
          header: 'Folders',
          footer:
              'Downloads land in the download folder. The library folder is '
              'scanned to index your local audio files, tags and artwork.',
          children: [
            SettingsTile.nav(
              leading: const SettingsIcon(Icons.download_rounded),
              title: 'Downloads Directory',
              subtitle: _dl.isEmpty ? null : _dl,
              onTap: () => _browse(download: true),
            ),
            SettingsTile.nav(
              leading: const SettingsIcon(Icons.library_music_rounded),
              title: 'Local Music Library',
              subtitle: _lib.isEmpty ? null : _lib,
              onTap: () => _browse(download: false),
            ),
          ],
        ),
        SettingsSection(
          header: 'Downloads',
          footer:
              'Pick a tier to skip the quality sheet on every download. '
              '"Ask every time" restores the prompt.',
          children: [
            SettingsPickerTile<String>(
              leading: const SettingsIcon(Icons.high_quality_rounded),
              title: 'Default Download Quality',
              sheetTitle: 'Default Quality',
              value: _quality,
              options: kDefaultQualityOptions,
              onChanged: _setQuality,
            ),
          ],
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
/// The Jarvis switch waits for its tab (phase 5); its stored value is
/// written back unchanged.
class AppearanceGroup extends StatefulWidget {
  const AppearanceGroup({super.key, required this.deps});

  final SettingsDeps deps;

  @override
  State<AppearanceGroup> createState() => _AppearanceGroupState();
}

class _AppearanceGroupState extends State<AppearanceGroup> {
  late final Map<String, Object?> _state = _read();
  Future<void> _writeChain = Future<void>.value();

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
      'glass_opacity': AppConfig.parseGlassOpacity(
        c.value('appearance', 'glass_opacity'),
      ),
      'most_listened': landing('show_search_history'),
      'library_stats': landing('show_library_stats'),
      'top_artists': landing('show_top_artists'),
      'top_genres': landing('show_top_genres'),
      'new_releases': landing('show_new_releases'),
      'jarvis': tab('jarvis', true),
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

  /// Updates local state immediately and persists to config.toml without
  /// requiring a separate Save button.
  void _set(String k, Object? v, {bool persist = true}) {
    setState(() => _state[k] = v);
    if (persist) _persist();
  }

  void _persist() {
    final s = Map<String, Object?>.of(_state);
    final opacity =
        ((s['glass_opacity'] as double?) ?? Tokens.defaultGlassOpacity);
    final roundedOpacity = double.parse(opacity.toStringAsFixed(2));
    d.library.setSort(LibrarySort.fromConfig('${s['default_sort']}'));
    _writeChain = _writeChain.then(
      (_) => d.config.update({
        'appearance': {
          'accent_color': s['accent_color'],
          'pill_style': s['pill_style'] ?? 'category',
          'glass_opacity': roundedOpacity,
          'show_jarvis': s['jarvis'],
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
          'library_sort': s['default_sort'],
        },
      }),
    );
  }

  @override
  Widget build(BuildContext context) {
    final accent =
        AppConfig.parseAccent('${_state['accent_color']}') ??
        Theme.of(context).colorScheme.primary;
    final opacity =
        (_state['glass_opacity'] as double?) ?? Tokens.defaultGlassOpacity;
    final pct = (opacity * 100).round();
    final unified = _state['pill_style'] == 'unified';

    SettingsTile toggle(String key, String title, {Widget? leading}) =>
        SettingsTile.toggle(
          title: title,
          leading: leading,
          value: _state[key] == true,
          onChanged: (v) => _set(key, v),
        );

    return GroupBody(
      spacing: 26,
      children: [
        SettingsSection(
          header: 'Glass & Color',
          children: [
            SettingsCustomTile(
              child: _GlassPreview(opacity: opacity, accent: accent),
            ),
            SettingsSliderTile(
              title: 'Glass Opacity',
              value: opacity,
              min: Tokens.minGlassOpacity,
              max: Tokens.maxGlassOpacity,
              valueLabel: opacity >= 0.99
                  ? 'Solid'
                  : pct == 0
                  ? 'Clear'
                  : '$pct%',
              marks: const ['Clear', 'Balanced', 'Solid'],
              onChanged: (v) => _set('glass_opacity', v, persist: false),
              onChangeEnd: (v) => _set('glass_opacity', v),
            ),
            SettingsCustomTile(
              title: 'Accent Color',
              child: Wrap(
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
            ),
            SettingsSegmentedTile<String>(
              title: 'Category Colors',
              subtitle: unified
                  ? 'Artists, albums, tracks and more all use the accent.'
                  : 'Each library category keeps its own colour.',
              inline: false,
              value: unified ? 'unified' : 'category',
              options: const [
                SettingsOption('category', 'Multi-Color'),
                SettingsOption('unified', 'Unified Accent'),
              ],
              onChanged: (v) => _set('pill_style', v),
            ),
          ],
        ),
        SettingsSection(
          header: 'Navigation',
          children: [
            SettingsSegmentedTile<String>(
              title: 'Startup Page',
              value: '${_state['startup_page']}',
              options: const [
                SettingsOption('Library', 'Library'),
                SettingsOption('Search', 'Search'),
              ],
              onChanged: (v) => _set('startup_page', v),
            ),
            SettingsPickerTile<LibrarySort>(
              title: 'Default Library Sort',
              value: LibrarySort.fromConfig('${_state['default_sort']}'),
              options: [
                for (final s in LibrarySort.values) SettingsOption(s, s.label),
              ],
              onChanged: (s) => _set('default_sort', s.name),
            ),
          ],
        ),
        SettingsSection(
          header: 'Library Views',
          footer: 'Hidden views leave the Library view bar.',
          children: [
            for (final (key, view, label) in const [
              ('tracks', LibraryView.tracks, 'Tracks'),
              ('albums', LibraryView.albums, 'Albums'),
              ('artists', LibraryView.artists, 'Artists'),
              ('genres', LibraryView.genres, 'Genres'),
              ('playlists', LibraryView.playlists, 'Playlists'),
            ])
              toggle(
                key,
                label,
                leading: SettingsIcon(
                  ViewBar.icon(view),
                  color: unified ? accent : (ViewBar.color(view) ?? accent),
                ),
              ),
          ],
        ),
        SettingsSection(
          header: 'Search Landing',
          footer: 'What the Search tab shows before you type.',
          children: [
            toggle('new_releases', 'New From Your Artists'),
            toggle('most_listened', 'Most Listened Tracks'),
            toggle('top_artists', 'Most Listened Artists'),
            toggle('top_genres', 'Most Listened Genres'),
            toggle('library_stats', 'Library Stats'),
          ],
        ),
        SettingsSection(
          header: 'Assistant',
          children: [toggle('jarvis', 'Enable Jarvis Tab')],
        ),
      ],
    );
  }
}

class _GlassPreview extends StatelessWidget {
  const _GlassPreview({required this.opacity, required this.accent});

  final double opacity;
  final Color accent;

  @override
  Widget build(BuildContext context) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(14),
      child: SizedBox(
        height: 92,
        child: Stack(
          fit: StackFit.expand,
          children: [
            // Vibrant mock rows behind the glass pill so transparency reads clearly.
            DecoratedBox(
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: [
                    accent.withValues(alpha: 0.45),
                    Tokens.libArtist.withValues(alpha: 0.45),
                    Tokens.libAlbum.withValues(alpha: 0.45),
                  ],
                ),
              ),
              child: Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 14,
                  vertical: 10,
                ),
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    for (final c in [Tokens.libTrack, Tokens.libPlaylist])
                      Row(
                        children: [
                          Container(
                            width: 28,
                            height: 28,
                            decoration: BoxDecoration(
                              color: c,
                              borderRadius: BorderRadius.circular(6),
                            ),
                          ),
                          const SizedBox(width: 10),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Container(
                                  height: 8,
                                  width: 120,
                                  decoration: BoxDecoration(
                                    color: Colors.white.withValues(alpha: 0.75),
                                    borderRadius: BorderRadius.circular(4),
                                  ),
                                ),
                                const SizedBox(height: 5),
                                Container(
                                  height: 6,
                                  width: 72,
                                  decoration: BoxDecoration(
                                    color: Colors.white.withValues(alpha: 0.4),
                                    borderRadius: BorderRadius.circular(3),
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ],
                      ),
                  ],
                ),
              ),
            ),
            // Live glass mini-pill floating over the mock content.
            Center(
              child: GlassSurface(
                opacity: opacity,
                radius: 18,
                margin: const EdgeInsets.symmetric(horizontal: 14),
                padding: const EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 10,
                ),
                child: Row(
                  children: [
                    Container(
                      width: 28,
                      height: 28,
                      decoration: BoxDecoration(
                        color: accent.withValues(alpha: 0.25),
                        borderRadius: BorderRadius.circular(7),
                      ),
                      child: Icon(
                        Icons.music_note_rounded,
                        color: accent,
                        size: 16,
                      ),
                    ),
                    const SizedBox(width: 10),
                    const Expanded(
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            'Now Playing Preview',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              color: Tokens.text,
                              fontSize: 12,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          Text(
                            'Floating Glass Pill',
                            maxLines: 1,
                            style: TextStyle(color: Tokens.dim, fontSize: 10),
                          ),
                        ],
                      ),
                    ),
                    Icon(Icons.play_arrow_rounded, color: accent, size: 22),
                    const SizedBox(width: 4),
                    const Icon(
                      Icons.skip_next_rounded,
                      color: Tokens.dim,
                      size: 18,
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
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
      child: AnimatedContainer(
        duration: Tokens.motionFast,
        width: 34,
        height: 34,
        decoration: BoxDecoration(
          color: AppConfig.parseAccent(hex),
          shape: BoxShape.circle,
          border: selected
              ? Border.all(color: Tokens.text, width: 2.5)
              : Border.all(color: const Color(0x26FFFFFF)),
        ),
        child: selected
            ? const Icon(Icons.check_rounded, size: 18, color: Tokens.bg)
            : null,
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
      spacing: 26,
      children: [
        SettingsSection(
          footer: 'Vibration for gestures and actions. Android only.',
          children: [
            SettingsTile.toggle(
              title: 'Enable Haptic Feedback',
              leading: const SettingsIcon(
                Icons.vibration_rounded,
                color: Tokens.libGenre,
              ),
              value: _enabled,
              onChanged: _toggle,
            ),
          ],
        ),
        SettingsSection(
          header: 'Intensity',
          footer: 'Each choice plays a sample. None turns that one off.',
          children: [
            for (final (k, label, options, _) in HapticsGroup.rows)
              SettingsPickerTile<String>(
                title: label,
                value: _levelsNow[k] ?? '',
                options: [for (final (v, t) in options) SettingsOption(v, t)],
                onChanged: (v) => _level(k, v),
              ),
          ],
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

  static String _statusLabel(PermissionStatus s) => switch (s) {
    PermissionStatus.granted => 'Allowed',
    PermissionStatus.limited => 'Limited',
    PermissionStatus.provisional => 'Provisional',
    PermissionStatus.restricted => 'Restricted',
    PermissionStatus.permanentlyDenied => 'Blocked',
    PermissionStatus.denied => 'Not Allowed',
  };

  @override
  Widget build(BuildContext context) {
    if (!Platform.isAndroid) {
      return const GroupBody(
        children: [
          SettingsSection(
            children: [
              SettingsTile(
                title: 'Managed by the system',
                subtitle:
                    'Permissions are granted in the OS on this platform; '
                    'this page only applies on Android.',
              ),
            ],
          ),
        ],
      );
    }
    return GroupBody(
      spacing: 26,
      children: [
        SettingsSection(
          header: 'Access',
          footer:
              'Tap a permission to open Android’s App Info, then '
              'Permissions, and set it to Allow.',
          children: [
            for (final (p, label, desc) in PermissionsGroup.specs)
              SettingsTile(
                title: label,
                subtitle: desc,
                onTap: openAppSettings,
                trailing: _StatusPill(
                  label: switch (_status?[p]) {
                    null => 'Checking…',
                    final s => _statusLabel(s),
                  },
                  ok: _status?[p]?.isGranted ?? false,
                  pending: _status?[p] == null,
                ),
              ),
          ],
        ),
        SettingsSection(
          children: [
            SettingsTile.action(
              title: 'Open System Settings',
              leading: const SettingsIcon(
                Icons.open_in_new_rounded,
                color: Tokens.accentGreen,
              ),
              onTap: openAppSettings,
            ),
          ],
        ),
      ],
    );
  }
}

class _StatusPill extends StatelessWidget {
  const _StatusPill({
    required this.label,
    required this.ok,
    required this.pending,
  });

  final String label;
  final bool ok, pending;

  @override
  Widget build(BuildContext context) {
    final c = pending
        ? Tokens.dim
        : ok
        ? Tokens.accentGreen
        : Tokens.amber;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: c.withValues(alpha: 0.16),
        borderRadius: BorderRadius.circular(Tokens.radiusPill),
      ),
      child: Text(
        label,
        style: TextStyle(color: c, fontSize: 12, fontWeight: FontWeight.w600),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Database Management

/// settings.py `_build_database_management_group`: the maintenance
/// buttons and the compute pipeline (on-phone DSP compute, a full
/// recompute, and Recompute PCA over the features already stored).
class DatabaseGroup extends StatefulWidget {
  const DatabaseGroup({super.key, required this.deps});

  final SettingsDeps deps;

  @override
  State<DatabaseGroup> createState() => _DatabaseGroupState();
}

class _DatabaseGroupState extends State<DatabaseGroup> {
  SettingsDeps get d => widget.deps;

  /// A maintenance job, or a PCA recompute, is running.
  bool _busy = false, _pcaBusy = false;

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

  /// `_do_recompute_pca`: `build_acoustic_edges` alone.
  Future<void> _recomputePca() async {
    final messenger = ScaffoldMessenger.of(context);
    final accent = Theme.of(context).colorScheme.primary;
    showSnackOn(
      messenger,
      'Recomputing PCA space…',
      icon: Icons.scatter_plot_rounded,
    );
    setState(() => _pcaBusy = true);
    try {
      await d.library.rebuildSimilarityGraph(metadata: false);
      showSnackOn(
        messenger,
        'PCA space rebuilt.',
        icon: Icons.check_circle,
        color: accent,
      );
    } catch (e) {
      showSnackOn(
        messenger,
        'PCA recompute failed: $e',
        color: const Color(0xFFFF4444),
      );
    } finally {
      if (mounted) setState(() => _pcaBusy = false);
    }
  }

  Future<void> _confirmRecomputeAllDsp() async {
    final ok = await confirmDelete(
      context,
      title: 'Recompute All DSP Features?',
      message:
          'This will purge all existing DSP features and recompute every track '
          'in your library from scratch. Depending on library size, this may '
          'take a while.\n\nYour audio files and play counts will NOT be '
          'touched.',
      confirmLabel: 'Recompute All',
    );
    if (!ok || !mounted) return;
    _computeDsp(DspEntry.recomputeAll);
  }

  /// settings.py `_do_compute_dsp` / `_do_recompute_all_dsp`.
  void _computeDsp(DspEntry entry) {
    final messenger = ScaffoldMessenger.of(context);
    final accent = Theme.of(context).colorScheme.primary;
    d.library.analysis
        .run(
          entry,
          notice: (n) => switch (n.kind) {
            DspNoticeKind.info => showSnackOn(
              messenger,
              n.text,
              icon:
                  entry == DspEntry.recomputeAll && n.text.startsWith('Purging')
                  ? Icons.replay_rounded
                  : Icons.graphic_eq_rounded,
            ),
            DspNoticeKind.success => showSnackOn(
              messenger,
              n.text,
              icon: Icons.check_circle,
              color: accent,
            ),
            DspNoticeKind.error => showSnackOn(
              messenger,
              n.text,
              color: const Color(0xFFFF4444),
            ),
          },
        )
        .ignore();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: d.library.analysis,
    builder: (context, _) => _build(context),
  );

  Widget _build(BuildContext context) {
    final repo = d.library.repository;
    final analysis = d.library.analysis;
    final can =
        !_busy && !_pcaBusy && !analysis.running && (repo?.writable ?? false);
    VoidCallback? when(VoidCallback f) => can ? f : null;
    return GroupBody(
      spacing: 26,
      children: [
        SettingsSection(
          header: 'Database Maintenance',
          footer:
              'Wipe or reset specific tables and indexes in the local '
              'application database. Your actual music files will NOT be '
              'touched.',
          children: [
            SettingsTile.action(
              leading: const SettingsIcon(Icons.format_list_bulleted_rounded),
              title: 'Library Index',
              subtitle: 'Clear indexed music, playlists and metadata',
              destructive: true,
              onTap: when(
                () => _confirmThen(
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
            ),
            SettingsTile.action(
              leading: const SettingsIcon(Icons.graphic_eq_rounded),
              title: 'DSP Features',
              subtitle: 'Purge acoustic features and the PCA space',
              destructive: true,
              onTap: when(
                () => _confirmThen(
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
            ),
            SettingsTile.action(
              leading: const SettingsIcon(
                Icons.delete_forever_rounded,
                color: Tokens.accentRed,
              ),
              title: 'Wipe DB',
              subtitle: 'Clear the search index and all metadata',
              destructive: true,
              onTap: when(
                () => _confirmThen(
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
            ),
            if (_busy)
              const SettingsCustomTile(child: LinearProgressIndicator()),
          ],
        ),
        SettingsSection(
          header: 'Compute Pipeline',
          footer:
              'Run the DSP / graph / PCA pipeline manually instead of going '
              'through Jarvis.',
          children: [
            SettingsTile.action(
              leading: const SettingsIcon(Icons.play_arrow_rounded),
              title: 'Commence/Continue DSP Compute',
              onTap: when(() => _computeDsp(DspEntry.settings)),
            ),
            SettingsTile.action(
              leading: const SettingsIcon(Icons.replay_rounded),
              title: 'Recompute All DSP Features',
              onTap: when(_confirmRecomputeAllDsp),
            ),
            SettingsTile.action(
              leading: const SettingsIcon(Icons.scatter_plot_rounded),
              title: 'Recompute PCA',
              onTap: when(_recomputePca),
            ),
            if (analysis.running) ...[
              SettingsCustomTile(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    LinearProgressIndicator(value: analysis.progress),
                    if (analysis.status.isNotEmpty) ...[
                      const SizedBox(height: 8),
                      Text(analysis.status, style: SettingsStyle.subtitle),
                    ],
                  ],
                ),
              ),
              SettingsTile.action(title: 'Cancel', onTap: analysis.cancel),
            ] else if (_pcaBusy)
              const SettingsCustomTile(child: LinearProgressIndicator()),
          ],
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Advanced

/// settings.py `_build_advanced_group`: state bundle export and import,
/// cache maintenance and the raw TOML editor. Demo search, the play-count
/// debug button and the fresh-install reset wait for decision C.
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

  /// `_on_export_state_click`: on Android the bundle browser picks the
  /// folder; elsewhere ~/Downloads (Flet's fallback when no folder is
  /// picked; the port has no native picker).
  Future<void> _exportClick() async {
    if (defaultTargetPlatform != TargetPlatform.android) {
      return _export(defaultBundleDir());
    }
    final dir = await showBundleBrowser(
      context,
      import: false,
      appStorage: d.dataDir,
    );
    if (dir != null) await _export(dir);
  }

  /// `_on_import_state_click`.
  Future<void> _importClick() async {
    if (defaultTargetPlatform != TargetPlatform.android) {
      showSnackOn(
        ScaffoldMessenger.of(context),
        'Desktop import: drop a bundle into ~/Downloads and use Android.',
        color: _red,
      );
      return;
    }
    final zip = await showBundleBrowser(
      context,
      import: true,
      appStorage: d.dataDir,
    );
    if (zip != null) await _import(zip);
  }

  static const _red = Color(0xFFFF4444);

  /// `_do_export_state`.
  Future<void> _export(String outDir) async {
    final bundles = d.bundles;
    if (bundles == null) return;
    final m = ScaffoldMessenger.of(context);
    final accent = Theme.of(context).colorScheme.primary;
    showSnackOn(m, 'Exporting state...', icon: Icons.ios_share_rounded);
    try {
      final out = await bundles.export(outDir: outDir);
      showSnackOn(
        m,
        'Exported to $out',
        icon: Icons.check_circle,
        color: accent,
      );
    } catch (e) {
      debugPrint('state export failed: $e');
      showSnackOn(m, 'Export failed: $e', color: _red);
    }
  }

  /// `_do_import_state`. Flet closes the database and asks for a relaunch;
  /// the port reopens the library and re-reads config.toml (§7).
  Future<void> _import(String zipPath) async {
    final bundles = d.bundles;
    if (bundles == null) return;
    final m = ScaffoldMessenger.of(context);
    final accent = Theme.of(context).colorScheme.primary;
    showSnackOn(m, 'Importing state...', icon: Icons.file_download_rounded);
    try {
      final result = await bundles.import(zipPath);
      final replaced = result.replaced.keys.join(', ');
      showSnackOn(
        m,
        'Imported ${replaced.isEmpty ? 'nothing' : replaced}. '
        'Library reloaded.',
        icon: Icons.check_circle,
        color: accent,
      );
      final text = await d.config.readText();
      if (mounted) _editor.text = text;
    } catch (e) {
      debugPrint('state import failed: $e');
      showSnackOn(m, 'Import failed: $e', color: _red);
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
    return GroupBody(
      spacing: 26,
      children: [
        if (d.bundles != null)
          SettingsSection(
            header: 'Backup & Migration',
            footer:
                'On every startup the app writes a snapshot ($kSnapshotName) '
                'to your library folder, so the Mac companion picks up your '
                'state on its own. Export or import here for a point-in-time '
                'backup.',
            children: [
              SettingsTile.nav(
                title: 'Export State',
                leading: const SettingsIcon(Icons.ios_share_rounded),
                onTap: _exportClick,
              ),
              SettingsTile.nav(
                title: 'Import State',
                leading: const SettingsIcon(Icons.file_download_rounded),
                onTap: _importClick,
              ),
            ],
          ),
        SettingsSection(
          header: 'Cache',
          children: [
            SettingsTile.action(
              title: 'Clear Album Artwork Cache',
              leading: const SettingsIcon(
                Icons.image_rounded,
                color: Tokens.libAlbum,
              ),
              onTap: () => _snackOf(_clearAlbumCache),
            ),
            SettingsTile.action(
              title: 'Clear Preview Cache',
              leading: const SettingsIcon(
                Icons.music_note_rounded,
                color: Tokens.libTrack,
              ),
              onTap: () => _snackOf(_clearPreviewCache),
            ),
          ],
        ),
        SettingsSection(
          header: 'Raw Configuration',
          footer: 'Edits config.toml directly; saved exactly as typed.',
          children: [
            SettingsCustomTile(
              child: TextField(
                controller: _editor,
                minLines: 15,
                maxLines: 25,
                style: const TextStyle(
                  color: Tokens.text,
                  fontFamily: 'monospace',
                  fontSize: 11,
                  height: 1.4,
                ),
                decoration: InputDecoration(
                  filled: true,
                  fillColor: Tokens.surface2,
                  contentPadding: const EdgeInsets.all(12),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(10),
                    borderSide: BorderSide.none,
                  ),
                  focusedBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(10),
                    borderSide: BorderSide(
                      color: Theme.of(context).colorScheme.primary,
                    ),
                  ),
                ),
              ),
            ),
            SettingsTile.action(
              title: 'Save Configuration',
              leading: const SettingsIcon(Icons.save_rounded),
              onTap: _saveConfig,
            ),
          ],
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
    return GroupBody(
      spacing: 26,
      children: [
        Padding(
          padding: const EdgeInsets.only(top: 8),
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
              const Text('Version $kAppVersion', style: SettingsStyle.value),
              const SizedBox(height: 14),
              const Text(
                'A high-fidelity audio player and streaming app. Built with '
                'Flutter, it pairs audiophile-grade playback with DSP '
                'similarity search, an AI assistant and full library tools.',
                textAlign: TextAlign.center,
                style: SettingsStyle.footer,
              ),
            ],
          ),
        ),
        SettingsSection(
          header: 'Core Features',
          children: [
            for (final (title, desc, icon) in _features)
              SettingsTile(
                title: title,
                subtitle: desc,
                leading: SettingsIcon(icon),
              ),
          ],
        ),
        SettingsSection(
          header: 'Project',
          children: [
            SettingsTile.nav(
              title: 'Project GitHub',
              value: 'Copy link',
              leading: const SettingsIcon(
                Icons.code_rounded,
                color: Tokens.libArtist,
              ),
              onTap: () async {
                await Clipboard.setData(const ClipboardData(text: github));
                if (context.mounted) {
                  showSettingsSnack(context, 'Link copied: $github');
                }
              },
            ),
            SettingsTile.nav(
              title: 'Licenses',
              leading: const SettingsIcon(
                Icons.gavel_rounded,
                color: Tokens.dim,
              ),
              onTap: () => showLicensePage(
                context: context,
                applicationName: 'Mai An Lab',
                applicationVersion: kAppVersion,
              ),
            ),
            SettingsTile.nav(
              title: 'Developer',
              value: 'Christophoros Mitsakopoulos',
              leading: const SettingsIcon(
                Icons.person_rounded,
                color: Tokens.libAlbum,
              ),
              onTap: () => showSettingsSnack(
                context,
                'Contact: mitsacopoulos@gmail.com',
              ),
            ),
          ],
        ),
        const SettingsSection(
          header: 'Credits',
          footer: '2026 Mai An Lab',
          children: [
            SettingsTile(title: 'Streamrip', subtitle: 'nathom and community'),
            SettingsTile(
              title: 'Flutter',
              subtitle: 'Flutter and its community',
            ),
          ],
        ),
      ],
    );
  }
}
