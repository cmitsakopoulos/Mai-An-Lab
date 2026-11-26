import 'dart:ui' show ImageFilter;

import 'package:flutter/material.dart';

import 'tokens.dart';

/// Provides the user-configured glass opacity (`0.15` ultra-clear to `1.0`
/// solid / non-glass) to every [GlassSurface] and [showFrostedSheet] in the
/// tree. Placed above the Navigator so modal sheets pick it up too.
class GlassScope extends InheritedWidget {
  const GlassScope({super.key, required this.opacity, required super.child});

  final double opacity;

  static double opacityOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<GlassScope>()?.opacity ??
      Tokens.defaultGlassOpacity;

  @override
  bool updateShouldNotify(GlassScope old) => old.opacity != opacity;
}

/// Live insets occupied by the shell's floating bottom chrome (tab bar,
/// mini player, download pill). Scrollable tabs read [ShellInsets.bottomOf]
/// so content scrolls behind the floating glass pills while the last item
/// can still clear them.
class ShellInsets extends InheritedWidget {
  const ShellInsets({super.key, required this.bottom, required super.child});

  final double bottom;

  static double bottomOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<ShellInsets>()?.bottom ?? 0;

  @override
  bool updateShouldNotify(ShellInsets old) => old.bottom != bottom;
}

/// Real Apple-style frosted glass surface for persistent floating chrome
/// (mini player, tab bar, floating Library controls, download pill, sheets).
///
/// Reads [GlassScope.opacityOf] unless [opacity] is passed explicitly.
/// At `opacity >= 0.99`, `BackdropFilter` is skipped automatically so "Solid"
/// mode pays zero GPU blur cost.
class GlassSurface extends StatelessWidget {
  const GlassSurface({
    super.key,
    required this.child,
    this.radius = Tokens.radiusPill,
    this.borderRadius,
    this.padding,
    this.margin,
    this.opacity,
    this.tintColor = Tokens.surface,
    this.borderColor,
    this.sigma,
    this.grouped = false,
    this.elevated = true,
  });

  final Widget child;
  final double radius;
  final BorderRadius? borderRadius;
  final EdgeInsetsGeometry? padding;
  final EdgeInsetsGeometry? margin;

  /// Optional override (used by the Appearance live preview).
  final double? opacity;
  final Color tintColor;
  final Color? borderColor;

  /// Optional blur override. When null, blur scales smoothly with opacity:
  /// lighter/clearer at low opacity (~10), frosted in the middle (~22),
  /// tapering cleanly to 0 at solid (1.0).
  final double? sigma;
  final bool grouped;
  final bool elevated;

  /// Perceptual blur curve so the slider feels continuous from crystal-clear
  /// all the way to solid:
  /// - 0.00 (crystal clear): ~1.5px micro-refraction (content & colors read
  ///   almost completely sharp through the pill)
  /// - 0.46 (balanced): ~13px blur (classic frosted Apple glass)
  /// - 0.85 (smoked): ~22px blur (dense frosted)
  /// - 0.99+ (solid): 0px (BackdropFilter skipped)
  static double _effectiveSigma(double op) {
    if (op >= 0.99) return 0;
    final t =
        ((op - Tokens.minGlassOpacity) /
                (Tokens.maxGlassOpacity - Tokens.minGlassOpacity))
            .clamp(0.0, 1.0);
    return 1.5 + t * 24.5;
  }

  @override
  Widget build(BuildContext context) {
    final r = borderRadius ?? BorderRadius.circular(radius);
    final op = (opacity ?? GlassScope.opacityOf(context)).clamp(
      Tokens.minGlassOpacity,
      Tokens.maxGlassOpacity,
    );
    final isSolid = op >= 0.99;

    // Blend a subtle specular sheen directly into the fill stops because
    // Flutter's BoxDecoration ignores `color` whenever `gradient` is set.
    // Keep sheen delicate at very low opacity so 0% feels truly crystal clear.
    final baseFill = tintColor.withValues(alpha: op);
    final sheenScale = op < 0.10 ? (0.4 + op * 6.0) : 1.0;
    final topSheen = Colors.white.withValues(
      alpha: (1 - op) * 0.10 * sheenScale,
    );
    final bottomSheen = Colors.white.withValues(
      alpha: (1 - op) * 0.035 * sheenScale,
    );

    final resolvedBorder =
        borderColor ??
        (isSolid
            ? Tokens.border
            : Colors.white.withValues(alpha: 0.12 + (1 - op) * 0.14));

    final inner = DecoratedBox(
      decoration: BoxDecoration(
        color: isSolid ? tintColor : null,
        borderRadius: r,
        gradient: isSolid
            ? null
            : LinearGradient(
                begin: Alignment.topCenter,
                end: Alignment.bottomCenter,
                stops: const [0, 0.38, 1],
                colors: [
                  Color.alphaBlend(topSheen, baseFill),
                  baseFill,
                  Color.alphaBlend(bottomSheen, baseFill),
                ],
              ),
        border: Border.all(color: resolvedBorder, width: 0.75),
      ),
      child: padding != null ? Padding(padding: padding!, child: child) : child,
    );

    final Widget body;
    if (isSolid) {
      body = ClipRRect(borderRadius: r, child: inner);
    } else {
      final blur = sigma ?? _effectiveSigma(op);
      final filter = ImageFilter.blur(sigmaX: blur, sigmaY: blur);
      body = ClipRRect(
        borderRadius: r,
        child: grouped
            ? BackdropFilter.grouped(filter: filter, child: inner)
            : BackdropFilter(filter: filter, child: inner),
      );
    }

    final surface = RepaintBoundary(
      child: elevated
          ? DecoratedBox(
              decoration: BoxDecoration(
                borderRadius: r,
                boxShadow: const [
                  BoxShadow(
                    blurRadius: 24,
                    spreadRadius: -4,
                    offset: Offset(0, 8),
                    color: Color(0x66000000),
                  ),
                ],
              ),
              child: body,
            )
          : body,
    );

    return margin != null ? Padding(padding: margin!, child: surface) : surface;
  }
}

/// Clean elevated card decoration for surfaces inside scrolling lists
/// (no backdrop blur, zero extra GPU pass).
BoxDecoration cardDecoration({
  double radius = Tokens.radiusCard,
  Color color = Tokens.surface,
  Color border = Tokens.borderSubtle,
}) => BoxDecoration(
  color: color,
  borderRadius: BorderRadius.circular(radius),
  border: Border.all(color: border, width: 0.75),
);

/// An elevated card for scrollable content (Settings, Search, etc.);
/// tappable when [onTap] is given.
class GlassCard extends StatelessWidget {
  const GlassCard({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(16),
    this.margin,
    this.radius = Tokens.radiusCard,
    this.flat = Tokens.surface,
    this.flatBorder = Tokens.borderSubtle,
    this.onTap,
  });

  final Widget child;
  final EdgeInsetsGeometry padding;
  final EdgeInsetsGeometry? margin;
  final double radius;
  final Color flat, flatBorder;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final r = BorderRadius.circular(radius);
    Widget body = Padding(padding: padding, child: child);
    // Always a Material: ink from rows inside the card otherwise paints on
    // the page beneath the card's opaque fill and never shows.
    body = Material(
      type: MaterialType.transparency,
      child: onTap != null
          ? InkWell(borderRadius: r, onTap: onTap, child: body)
          : body,
    );
    return Container(
      margin: margin,
      decoration: cardDecoration(
        radius: radius,
        color: flat,
        border: flatBorder,
      ),
      child: onTap != null ? ClipRRect(borderRadius: r, child: body) : body,
    );
  }
}

/// A modal bottom sheet backed by frosted glass (or solid when glass opacity
/// is at 100%).
Future<T?> showFrostedSheet<T>({
  required BuildContext context,
  required WidgetBuilder builder,
  bool isScrollControlled = false,
  bool useSafeArea = false,
  bool showDragHandle = false,
  Color backgroundColor = Tokens.surface,
  ShapeBorder? shape,
}) {
  final radius = switch (shape) {
    RoundedRectangleBorder(borderRadius: final BorderRadius b) => b,
    _ => const BorderRadius.vertical(top: Radius.circular(22)),
  };
  final op = GlassScope.opacityOf(
    context,
  ).clamp(Tokens.minGlassOpacity, Tokens.maxGlassOpacity);
  final isSolid = op >= 0.99;
  return showModalBottomSheet<T>(
    context: context,
    isScrollControlled: isScrollControlled,
    useSafeArea: useSafeArea,
    backgroundColor: Colors.transparent,
    elevation: 0,
    builder: (ctx) => _BlurSheet(
      radius: radius,
      tint: isSolid
          ? backgroundColor
          : backgroundColor.withValues(alpha: (op + 0.12).clamp(0.12, 0.94)),
      sigma: isSolid ? 0 : GlassSurface._effectiveSigma(op) + 4,
      solid: isSolid,
      dragHandle: showDragHandle,
      child: builder(ctx),
    ),
  );
}

class _BlurSheet extends StatelessWidget {
  const _BlurSheet({
    required this.radius,
    required this.tint,
    required this.sigma,
    required this.solid,
    required this.dragHandle,
    required this.child,
  });

  final BorderRadius radius;
  final Color tint;
  final double sigma;
  final bool solid;
  final bool dragHandle;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final innerBody = SafeArea(
      top: false,
      child: dragHandle
          ? Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  width: 36,
                  height: 4,
                  margin: const EdgeInsets.only(top: 10, bottom: 6),
                  decoration: BoxDecoration(
                    color: Tokens.textTertiary,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
                Flexible(child: child),
              ],
            )
          : child,
    );
    final content = DecoratedBox(
      decoration: BoxDecoration(
        color: tint,
        borderRadius: radius,
        border: const Border(top: BorderSide(color: Color(0x33FFFFFF))),
      ),
      child: innerBody,
    );
    return RepaintBoundary(
      child: ClipRRect(
        borderRadius: radius,
        child: solid
            ? content
            : BackdropFilter(
                filter: ImageFilter.blur(sigmaX: sigma, sigmaY: sigma),
                child: content,
              ),
      ),
    );
  }
}
