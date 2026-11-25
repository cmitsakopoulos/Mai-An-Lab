import 'package:flutter/material.dart';

import 'tokens.dart';

/// Vibrant, distinct colour per audio format tag so formats are scannable at a
/// glance and bleed colour cleanly through frosted glass pills.
Color formatBadgeColor(String badge) {
  final key = badge.trim().toUpperCase();
  return switch (key) {
    'FLAC' => const Color(0xFF32D2F2), // crisp lossless cyan
    'MP3' => const Color(0xFFFF9F0A), // warm amber
    'WAV' || 'AIFF' || 'AIF' || 'PCM' => const Color(0xFF5E5CE6), // indigo
    'ALAC' || 'M4A' || 'AAC' || 'MP4' => const Color(0xFFFF375F), // coral rose
    'OGG' || 'OPUS' || 'OGA' => const Color(0xFF30D158), // emerald
    'DSD' || 'DSF' || 'DFF' => const Color(0xFFBF5AF2), // royal violet
    'WV' || 'APE' || 'WMA' || 'MPC' => const Color(0xFF64D2FF), // sky
    _ => _fallbackColor(key),
  };
}

const _fallbackPalette = [
  Color(0xFF32D2F2),
  Color(0xFF30D158),
  Color(0xFFFF9F0A),
  Color(0xFFFF375F),
  Color(0xFFBF5AF2),
  Color(0xFF5E5CE6),
  Color(0xFFFFD60A),
];

Color _fallbackColor(String key) {
  if (key.isEmpty) return Tokens.dim;
  var hash = 0;
  for (final code in key.codeUnits) {
    hash = (hash * 31 + code) & 0x7fffffff;
  }
  return _fallbackPalette[hash % _fallbackPalette.length];
}

/// Colour-coded pill badge for a track's audio format (FLAC, MP3, ALAC, ...).
class FormatBadge extends StatelessWidget {
  const FormatBadge({
    super.key,
    required this.label,
    this.highlighted = false,
    this.compact = false,
  });

  final String label;
  final bool highlighted;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    final color = formatBadgeColor(label);
    return Container(
      width: compact ? null : 42,
      height: compact ? null : 21,
      padding: compact
          ? const EdgeInsets.symmetric(horizontal: 6, vertical: 1.5)
          : null,
      alignment: compact ? null : Alignment.center,
      decoration: BoxDecoration(
        color: color.withValues(alpha: highlighted ? 0.32 : 0.20),
        border: Border.all(
          color: color.withValues(alpha: highlighted ? 0.85 : 0.45),
          width: highlighted ? 1.0 : 0.8,
        ),
        borderRadius: BorderRadius.circular(compact ? 4 : 5),
      ),
      child: Text(
        label,
        maxLines: 1,
        overflow: TextOverflow.clip,
        style: TextStyle(
          fontSize: 9,
          fontWeight: FontWeight.w800,
          letterSpacing: 0.3,
          color: color,
        ),
      ),
    );
  }
}
