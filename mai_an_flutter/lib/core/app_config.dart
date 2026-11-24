import 'dart:io';
import 'dart:ui';

import 'package:flutter/foundation.dart';
import 'package:toml/toml.dart';

import '../library/track_query.dart';
import '../player/dsp.dart';
import 'haptics.dart';

/// The parts of the Flet app's config.toml the port reads so far. Read-only:
/// utils/config.py owns the file (and its migrations) until the settings that
/// write it are ported.
class AppConfig {
  const AppConfig({
    this.librarySort = LibrarySort.date,
    this.accent,
    this.dsp = const DspConfig(),
    this.haptics = const HapticsConfig(),
    this.libraryTabs = const LibraryTabs(),
    this.downloadsFolder = '',
    this.startupPage = 'Library',
    this.landing = const LandingSections(),
  });

  /// `general.library_sort` (Flet library.py: the initial sort).
  final LibrarySort librarySort;

  /// `appearance.accent_color`, legacy values mapped as in ui/tokens.py.
  final Color? accent;

  /// `[dsp]`: EQ preset and Dynamism, applied on every track change.
  final DspConfig dsp;

  /// `[haptics]`: on/off and the intensity per action.
  final HapticsConfig haptics;

  /// `appearance.show_*` and `pill_style`: the Library tab's view bar.
  final LibraryTabs libraryTabs;

  /// `[downloads] folder`, unresolved (storage_paths.dart resolves it; the
  /// library folder falls back to it).
  final String downloadsFolder;

  /// `general.startup_page`: "Jarvis", "Search", "Library" or "Settings"
  /// (main.py `_build_ui`, default "Library").
  final String startupPage;

  /// `[landing]`: which Search landing cards show.
  final LandingSections landing;

  /// Where utils/config.py puts it. The Flet app points HOME at the data dir
  /// first (main.py), so `Path.home()` is `<data>`:
  ///   Android: <data>/.config/streamrip/config.toml
  ///   macOS:   <data>/Library/Application Support/streamrip/config.toml
  static String pathFor(String dataDir) {
    final sep = Platform.pathSeparator;
    return Platform.isMacOS
        ? '$dataDir${sep}Library${sep}Application Support${sep}streamrip${sep}config.toml'
        : '$dataDir$sep.config${sep}streamrip${sep}config.toml';
  }

  static Future<AppConfig> load(String dataDir) async {
    try {
      final text = await File(pathFor(dataDir)).readAsString();
      return parse(text);
    } on FileSystemException {
      return const AppConfig();
    } catch (e) {
      debugPrint('AppConfig: unreadable config.toml: $e');
      return const AppConfig();
    }
  }

  static AppConfig parse(String toml) {
    final doc = TomlDocument.parse(toml).toMap();
    final general = doc['general'];
    final appearance = doc['appearance'];
    final sort = general is Map ? general['library_sort'] : null;
    final rawAccent = appearance is Map ? appearance['accent_color'] : null;
    return AppConfig(
      librarySort: LibrarySort.fromConfig(sort is String ? sort : null),
      accent: rawAccent is String ? parseAccent(rawAccent) : null,
      dsp: DspConfig.fromToml(doc['dsp']),
      haptics: HapticsConfig.fromToml(doc['haptics']),
      libraryTabs: LibraryTabs.fromToml(appearance),
      downloadsFolder: switch (doc['downloads']) {
        {'folder': final Object f?} => '$f',
        _ => '',
      },
      startupPage: switch (general) {
        {'startup_page': final Object p?} => '$p',
        _ => 'Library',
      },
      landing: LandingSections.fromToml(doc['landing']),
    );
  }

  /// ui/tokens.py LEGACY_ACCENT_MAP.
  static const legacyAccents = {
    '#FFD600': '#FFD60A',
    '#FFD700': '#FFD60A',
    '#00BFFF': '#64D2FF',
    '#2979FF': '#0A84FF',
    '#9B59B6': '#BF5AF2',
    '#B39DDB': '#D0BCFF',
    '#E91E63': '#FF375F',
    '#E74C3C': '#FF453A',
    '#DC143C': '#FF453A',
    '#E67E22': '#FF9F0A',
    '#2ECC71': '#30D158',
    '#00FF7F': '#25E89B',
    '#69F0AE': '#63E6E2',
    '#78909C': '#5E5CE6',
  };

  static Color? parseAccent(String raw) {
    final hex = legacyAccents[raw.toUpperCase()] ?? raw;
    final m = RegExp(r'^#?([0-9a-fA-F]{6})$').firstMatch(hex.trim());
    if (m == null) return null;
    return Color(0xFF000000 | int.parse(m[1]!, radix: 16));
  }
}

/// search.py `refresh_setup_state`'s `sections`: each `[landing]` key read
/// with Python's `bool()`, defaulting to shown. `show_search_history` is the
/// historical key of the Most Listened Tracks card.
class LandingSections {
  const LandingSections({
    this.newReleases = true,
    this.mostListened = true,
    this.topArtists = true,
    this.topGenres = true,
    this.stats = true,
  });

  /// New From Your Artists (phase 6; read so the setting carries over).
  final bool newReleases;
  final bool mostListened;
  final bool topArtists;
  final bool topGenres;
  final bool stats;

  static LandingSections fromToml(Object? landing) {
    final l = landing is Map ? landing : const <String, Object?>{};
    bool on(String key) => switch (l[key]) {
      null => true,
      bool b => b,
      String s => s.isNotEmpty,
      num n => n != 0,
      List<Object?> v => v.isNotEmpty,
      Map<Object?, Object?> v => v.isNotEmpty,
      _ => true,
    };
    return LandingSections(
      newReleases: on('show_new_releases'),
      mostListened: on('show_search_history'),
      topArtists: on('show_top_artists'),
      topGenres: on('show_top_genres'),
      stats: on('show_library_stats'),
    );
  }
}

/// The Library tab's view bar as config.toml sets it (library.py
/// `_visible_view_modes`, `_update_view_tabs`).
class LibraryTabs {
  const LibraryTabs({
    this.visible = const [
      'playlists', 'genres', 'artists', 'tracks', //
    ],
    this.unified = false,
  });

  /// Shown views, in bar order (network, playlists, genres, artists, albums,
  /// tracks); never empty.
  final List<String> visible;

  /// `pill_style = "unified"`: every pill in the accent colour instead of its
  /// category colour.
  final bool unified;

  static const order = [
    'network', 'playlists', 'genres', 'artists', 'albums', 'tracks', //
  ];

  /// The defaults of `_visible_view_modes`.
  static const _defaults = {
    'network': false,
    'playlists': true,
    'genres': true,
    'artists': true,
    'albums': false,
    'tracks': true,
  };

  static LibraryTabs fromToml(Object? appearance) {
    final a = appearance is Map ? appearance : const <String, Object?>{};
    final visible = [
      for (final m in order)
        if (_toBool(a['show_$m'], _defaults[m]!)) m,
    ];
    return LibraryTabs(
      visible: visible.isEmpty ? const ['tracks'] : visible,
      unified: '${a['pill_style'] ?? 'category'}'.toLowerCase() == 'unified',
    );
  }

  /// library.py `_to_bool`.
  static bool _toBool(Object? v, bool fallback) => switch (v) {
    null => fallback,
    bool b => b,
    String s => const {'true', '1', 'yes'}.contains(s.trim().toLowerCase()),
    num n => n != 0,
    _ => true,
  };
}
