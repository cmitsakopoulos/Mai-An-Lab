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
  static const amber = Color(0xFFFF9F0A);
  static const accentGreen = Color(0xFF30D158);
  static const accentRed = Color(0xFFFF453A);

  static const libTrack = Color(0xFF30D158);

  static const radiusCard = 14.0;
  static const radiusPill = 20.0;
  static const radiusThumb = 8.0;

  /// [accentColor]: config `appearance.accent_color` (AppConfig). Widgets
  /// read it as `Theme.of(context).colorScheme.primary`.
  static ThemeData theme({Color accentColor = accent}) {
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
      scaffoldBackgroundColor: bg,
      appBarTheme: const AppBarTheme(backgroundColor: bg, elevation: 0),
      dividerColor: border,
      sliderTheme: SliderThemeData(
        activeTrackColor: accentColor,
        inactiveTrackColor: surface2,
        thumbColor: text,
        overlayShape: SliderComponentShape.noOverlay,
      ),
      snackBarTheme: const SnackBarThemeData(
        backgroundColor: surface2,
        contentTextStyle: TextStyle(color: text),
        behavior: SnackBarBehavior.floating,
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

/// ui/widgets.py fmt_time: m:ss, minutes unbounded.
String fmtTime(Duration d) {
  final s = d.inSeconds < 0 ? 0 : d.inSeconds;
  return '${s ~/ 60}:${(s % 60).toString().padLeft(2, '0')}';
}
