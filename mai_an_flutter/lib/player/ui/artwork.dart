import 'dart:io';

import 'package:flutter/material.dart';

import '../../ui/tokens.dart';

/// Cover art from a local file or an `http(s)` URL (stream previews, new
/// releases), cross-fading when it changes, with the Flet placeholder (an
/// icon on SURFACE2) underneath while there is none.
class Artwork extends StatelessWidget {
  const Artwork({
    super.key,
    required this.path,
    required this.size,
    required this.radius,
    required this.placeholder,
    this.placeholderSize,
    this.cacheWidth,
  });

  /// Local file path or `http://` / `https://` URL.
  final String? path;

  /// Null to fill the parent.
  final double? size;
  final double radius;
  final IconData placeholder;
  final double? placeholderSize;

  /// Decode width in physical pixels (the art is at most 512 px anyway).
  final int? cacheWidth;

  static bool isNetworkUrl(String value) {
    final lower = value.toLowerCase();
    return lower.startsWith('https://') || lower.startsWith('http://');
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final p = path;
    final base = Container(
      width: size,
      height: size,
      color: Tokens.surface2,
      alignment: Alignment.center,
      child: Icon(placeholder, color: accent, size: placeholderSize),
    );
    Widget imageFor(String src) {
      if (isNetworkUrl(src)) {
        return Image.network(
          src,
          key: ValueKey(src),
          fit: BoxFit.cover,
          cacheWidth: cacheWidth,
          gaplessPlayback: true,
          errorBuilder: (_, _, _) => const SizedBox.shrink(),
        );
      }
      return Image.file(
        File(src),
        key: ValueKey(src),
        fit: BoxFit.cover,
        cacheWidth: cacheWidth,
        gaplessPlayback: true,
        errorBuilder: (_, _, _) => const SizedBox.shrink(),
      );
    }

    return ClipRRect(
      borderRadius: BorderRadius.circular(radius),
      child: SizedBox(
        width: size,
        height: size,
        child: Stack(
          fit: StackFit.expand,
          children: [
            base,
            AnimatedSwitcher(
              duration: const Duration(milliseconds: 250),
              child: p == null || p.isEmpty
                  ? const SizedBox.shrink()
                  : imageFor(p),
            ),
          ],
        ),
      ),
    );
  }
}
