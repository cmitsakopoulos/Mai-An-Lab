import 'package:flutter/material.dart';

/// Port of StreamripApp/ui/tokens.py (Apple HIG dark palette).
abstract final class Tokens {
  static const bg = Color(0xFF000000);
  static const surface = Color(0xFF1C1C1E);
  static const surface2 = Color(0xFF2C2C2E);
  static const surfaceElevated = Color(0xFF3A3A3C);

  /// Default accent. The Python app reads it from config `appearance.accent_color`.
  static const accent = Color(0xFFFFD60A);
  static const text = Color(0xFFFFFFFF);
  static const dim = Color(0xFF8E8E93);
  static const textTertiary = Color(0xFF636366);
  static const border = Color(0xFF38383A);

  /// BORDER_SUBTLE: translucent 10% white card edge.
  static const borderSubtle = Color(0x1AFFFFFF);

  /// Apple-style dark glass chrome tokens (used with BackdropFilter).
  static const defaultGlassOpacity = 0.46;
  static const minGlassOpacity = 0.0;
  static const maxGlassOpacity = 1.0;
  static const glassFill = Color(0x751C1C1E);
  static const glassFillDeep = Color(0x9E161618);
  static const glassBorder = Color(0x28FFFFFF);
  static const glassHighlight = Color(0x16FFFFFF);

  /// The one scrim behind every floating top header ([PageHeader]): solid
  /// under the status bar and title, fading out below the last header row.
  static const headerScrim = LinearGradient(
    begin: Alignment.topCenter,
    end: Alignment.bottomCenter,
    stops: [0.0, 0.6, 1.0],
    colors: [bg, Color(0xCC000000), Color(0x00000000)],
  );

  /// Responsive horizontal gutter for Android phones (narrow vs standard).
  static double pagePad(BuildContext context) =>
      MediaQuery.sizeOf(context).width <= 360 ? s12 : s16;

  /// Horizontal inset for floating glass pills.
  static double dockPad(BuildContext context) =>
      MediaQuery.sizeOf(context).width <= 360 ? 10.0 : 14.0;
  static const amber = Color(0xFFFF9F0A);
  static const accentGreen = Color(0xFF30D158);
  static const accentRed = Color(0xFFFF453A);

  // LIB_*_COLOR: the library views' category colours.
  static const libArtist = Color(0xFFBF5AF2);
  static const libAlbum = Color(0xFF0A84FF);
  static const libTrack = Color(0xFF30D158);
  static const libPlaylist = Color(0xFFFF9F0A);
  static const libGenre = Color(0xFFFF375F);

  // QOBUZ_COLOR / DEEZER_COLOR: streaming source accents (ui/tokens.py).
  static const qobuz = Color(0xFFFF9F0A);
  static const deezer = Color(0xFFBF5AF2);

  static const radiusCard = 14.0;
  static const radiusPill = 20.0;
  static const radiusThumb = 8.0;

  // Motion: one vocabulary so timings feel consistent across screens.
  static const motionFast = Duration(milliseconds: 160);
  static const motionBase = Duration(milliseconds: 220);
  static const motionSlow = Duration(milliseconds: 320);
  static const curveIn = Curves.easeOutCubic;
  static const curveOut = Curves.easeInCubic;

  // Spacing scale (4-pt grid).
  static const s4 = 4.0;
  static const s8 = 8.0;
  static const s12 = 12.0;
  static const s16 = 16.0;
  static const s24 = 24.0;

  /// [accentColor]: config `appearance.accent_color` (AppConfig). Widgets
  /// read it as `Theme.of(context).colorScheme.primary`.
  /// [unifiedCategories]: config `appearance.pill_style == 'unified'`. When
  /// true, [CategoryPalette] maps Artists, Albums, Tracks, Playlists, and
  /// Genres to [accentColor] instead of per-category colours.
  static ThemeData theme({
    Color accentColor = accent,
    bool unifiedCategories = false,
  }) {
    final scheme =
        ColorScheme.fromSeed(
          seedColor: accentColor,
          brightness: Brightness.dark,
        ).copyWith(
          primary: accentColor,
          surface: surface,
          onSurface: text,
          outline: border,
        );
    return ThemeData(
      colorScheme: scheme,
      extensions: [
        CategoryPalette.resolve(
          accent: accentColor,
          unified: unifiedCategories,
        ),
      ],
      scaffoldBackgroundColor: bg,
      appBarTheme: const AppBarTheme(
        backgroundColor: bg,
        elevation: 0,
        scrolledUnderElevation: 0,
      ),
      // Predictive back on Android (manifest opts in); fade-forwards elsewhere.
      pageTransitionsTheme: const PageTransitionsTheme(
        builders: {
          TargetPlatform.android: PredictiveBackPageTransitionsBuilder(),
          TargetPlatform.iOS: CupertinoPageTransitionsBuilder(),
          TargetPlatform.macOS: FadeForwardsPageTransitionsBuilder(),
        },
      ),
      bottomSheetTheme: const BottomSheetThemeData(
        backgroundColor: surface,
        surfaceTintColor: Colors.transparent,
        clipBehavior: Clip.antiAlias,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
        ),
      ),
      dialogTheme: DialogThemeData(
        backgroundColor: surface,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(radiusCard + 4),
        ),
      ),
      popupMenuTheme: PopupMenuThemeData(
        color: surfaceElevated,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(radiusCard),
        ),
      ),
      dividerColor: border,
      // A fallback for any Material [Switch]; settings use AppSwitch. Without
      // it, M3 draws the off thumb in `outline` (near-black here) and the on
      // thumb in the seeded `onPrimary`.
      switchTheme: SwitchThemeData(
        thumbColor: const WidgetStatePropertyAll(text),
        trackColor: WidgetStateProperty.resolveWith(
          (s) =>
              s.contains(WidgetState.selected) ? accentColor : surfaceElevated,
        ),
        trackOutlineColor: const WidgetStatePropertyAll(Colors.transparent),
      ),
      sliderTheme: SliderThemeData(
        activeTrackColor: accentColor,
        inactiveTrackColor: surface2,
        thumbColor: text,
        overlayShape: SliderComponentShape.noOverlay,
      ),
      snackBarTheme: SnackBarThemeData(
        backgroundColor: surface2,
        contentTextStyle: const TextStyle(color: text),
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(radiusCard),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: surface2,
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(radiusPill),
          borderSide: BorderSide.none,
        ),
        hintStyle: const TextStyle(color: dim),
      ),
    );
  }
}

/// Theme-aware category accents for Library, Search, and dialogs.
///
/// In Multi-Color mode ([unified] == false), each entity type keeps its own
/// distinct accent ([Tokens.libArtist], [Tokens.libAlbum], etc.). In Unified
/// Accent mode ([unified] == true), every category resolves to the central
/// theme accent, with [atDepth] providing subtle opacity stepping for nested
/// browse rows so hierarchy stays readable.
@immutable
class CategoryPalette extends ThemeExtension<CategoryPalette> {
  const CategoryPalette({
    required this.unified,
    required this.artist,
    required this.album,
    required this.track,
    required this.playlist,
    required this.genre,
  });

  factory CategoryPalette.resolve({
    required Color accent,
    required bool unified,
  }) => unified
      ? CategoryPalette(
          unified: true,
          artist: accent,
          album: accent,
          track: accent,
          playlist: accent,
          genre: accent,
        )
      : const CategoryPalette(
          unified: false,
          artist: Tokens.libArtist,
          album: Tokens.libAlbum,
          track: Tokens.libTrack,
          playlist: Tokens.libPlaylist,
          genre: Tokens.libGenre,
        );

  final bool unified;
  final Color artist;
  final Color album;
  final Color track;
  final Color playlist;
  final Color genre;

  static CategoryPalette of(BuildContext context) {
    final theme = Theme.of(context);
    return theme.extension<CategoryPalette>() ??
        CategoryPalette.resolve(
          accent: theme.colorScheme.primary,
          unified: false,
        );
  }

  /// Subtle tonal stepping for nested hierarchy rows in unified mode so
  /// Genre → Artist → Album trees retain visual depth.
  Color atDepth(Color color, int depth) {
    if (!unified || depth <= 0) return color;
    final alpha = depth == 1 ? 0.78 : 0.60;
    return color.withValues(alpha: alpha);
  }

  @override
  CategoryPalette copyWith({
    bool? unified,
    Color? artist,
    Color? album,
    Color? track,
    Color? playlist,
    Color? genre,
  }) => CategoryPalette(
    unified: unified ?? this.unified,
    artist: artist ?? this.artist,
    album: album ?? this.album,
    track: track ?? this.track,
    playlist: playlist ?? this.playlist,
    genre: genre ?? this.genre,
  );

  @override
  CategoryPalette lerp(CategoryPalette? other, double t) {
    if (other is! CategoryPalette) return this;
    return CategoryPalette(
      unified: t < 0.5 ? unified : other.unified,
      artist: Color.lerp(artist, other.artist, t) ?? artist,
      album: Color.lerp(album, other.album, t) ?? album,
      track: Color.lerp(track, other.track, t) ?? track,
      playlist: Color.lerp(playlist, other.playlist, t) ?? playlist,
      genre: Color.lerp(genre, other.genre, t) ?? genre,
    );
  }
}

/// ui/widgets.py fmt_time: m:ss, minutes unbounded.
String fmtTime(Duration d) {
  final s = d.inSeconds < 0 ? 0 : d.inSeconds;
  return '${s ~/ 60}:${(s % 60).toString().padLeft(2, '0')}';
}
