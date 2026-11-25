import 'dart:async';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:material_color_utilities/material_color_utilities.dart';

import '../../ui/tokens.dart';

/// The most appealing colour of a cover (material_color_utilities'
/// quantizer + scorer on a 48 px decode), cached per path. Null if the file
/// is missing or undecodable.
class ArtPalette {
  ArtPalette._();

  static final _cache = <String, Color?>{};
  static const _max = 64;

  static Future<Color?> of(String path) async {
    if (_cache.containsKey(path)) return _cache[path];
    Color? out;
    try {
      final bytes = await File(path).readAsBytes();
      final codec = await ui.instantiateImageCodec(bytes, targetWidth: 48);
      final frame = await codec.getNextFrame();
      final data = await frame.image.toByteData();
      frame.image.dispose();
      codec.dispose();
      if (data != null) {
        final px = <int>[];
        for (var i = 0; i + 3 < data.lengthInBytes; i += 4) {
          final r = data.getUint8(i), g = data.getUint8(i + 1);
          final b = data.getUint8(i + 2), a = data.getUint8(i + 3);
          if (a < 128) continue;
          px.add((0xFF << 24) | (r << 16) | (g << 8) | b);
        }
        if (px.isNotEmpty) {
          final result = await QuantizerCelebi().quantize(px, 16);
          final ranked = Score.score(result.colorToCount, desired: 1);
          if (ranked.isNotEmpty) out = Color(ranked.first);
        }
      }
    } on Object {
      out = null;
    }
    if (_cache.length >= _max) _cache.remove(_cache.keys.first);
    return _cache[path] = out;
  }
}

/// Builds with the cover's colour ([fallback] until/unless it is found).
/// Keeps the last colour while the next cover loads so transitions never flash.
class ArtTint extends StatefulWidget {
  const ArtTint({
    super.key,
    required this.path,
    required this.fallback,
    required this.builder,
  });

  final String? path;
  final Color fallback;
  final Widget Function(BuildContext context, Color color) builder;

  @override
  State<ArtTint> createState() => _ArtTintState();
}

class _ArtTintState extends State<ArtTint> {
  Color? _color;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(ArtTint old) {
    super.didUpdateWidget(old);
    if (old.path != widget.path) _load();
  }

  Future<void> _load() async {
    final p = widget.path;
    if (p == null || p.isEmpty) {
      if (mounted) setState(() => _color = null);
      return;
    }
    final c = await ArtPalette.of(p);
    if (mounted && widget.path == p) setState(() => _color = c);
  }

  @override
  Widget build(BuildContext context) {
    final target = _color ?? widget.fallback;
    return TweenAnimationBuilder<Color?>(
      tween: ColorTween(end: target),
      duration: const Duration(milliseconds: 500),
      builder: (context, c, _) => widget.builder(context, c ?? target),
    );
  }
}

/// Full-bleed heavily blurred album-art backdrop with a dark gradient scrim,
/// giving Now Playing Apple Music-style depth without a harsh radial glow.
class BlurredArtBackdrop extends StatelessWidget {
  const BlurredArtBackdrop({
    super.key,
    required this.path,
    required this.tint,
  });

  final String? path;
  final Color tint;

  @override
  Widget build(BuildContext context) {
    final p = path;
    final hasArt = p != null && p.isNotEmpty;
    return IgnorePointer(
      child: RepaintBoundary(
        child: Stack(
          fit: StackFit.expand,
          children: [
            const ColoredBox(color: Tokens.bg),
            if (hasArt)
              ImageFiltered(
                imageFilter: ui.ImageFilter.blur(
                  sigmaX: 48,
                  sigmaY: 48,
                  tileMode: TileMode.mirror,
                ),
                child: Image.file(
                  File(p),
                  fit: BoxFit.cover,
                  cacheWidth: 64,
                  filterQuality: FilterQuality.low,
                  gaplessPlayback: true,
                  errorBuilder: (_, _, _) => const SizedBox.shrink(),
                ),
              )
            else
              DecoratedBox(
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.topCenter,
                    end: Alignment.bottomCenter,
                    colors: [
                      tint.withValues(alpha: 0.22),
                      Tokens.bg,
                    ],
                  ),
                ),
              ),
            DecoratedBox(
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topCenter,
                  end: Alignment.bottomCenter,
                  stops: const [0.0, 0.48, 1.0],
                  colors: [
                    const Color(0x88000000),
                    Color.alphaBlend(
                      tint.withValues(alpha: 0.08),
                      const Color(0xB8000000),
                    ),
                    const Color(0xEC000000),
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
