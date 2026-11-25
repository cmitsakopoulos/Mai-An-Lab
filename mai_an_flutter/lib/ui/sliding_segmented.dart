import 'package:flutter/material.dart';

import 'frost.dart';
import 'tokens.dart';

class Segment<T> {
  const Segment({
    required this.value,
    required this.label,
    this.icon,
    this.color,
  });

  final T value;
  final String label;
  final IconData? icon;

  /// Null: the control's [SlidingSegmented.accent].
  final Color? color;
}

/// One capsule, equal-width segments, and a single pill that slides (and
/// recolours) to the selected one. Shared by the Library view bar, the
/// Search mode bar, and the floating bottom tab bar.
class SlidingSegmented<T> extends StatelessWidget {
  const SlidingSegmented({
    super.key,
    required this.segments,
    required this.selected,
    required this.onSelect,
    required this.accent,
    this.height = 40,
    this.fontSize = 11,
    this.hPad = 4,
    this.iconSize = 15,
    this.iconOnly = false,
    this.frosted = true,
    this.grouped = false,
  });

  final List<Segment<T>> segments;
  final T selected;
  final ValueChanged<T> onSelect;
  final Color accent;
  final double height, fontSize, hPad, iconSize;
  final bool iconOnly;
  final bool frosted;
  final bool grouped;

  @override
  Widget build(BuildContext context) {
    final n = segments.length;
    final index = segments.indexWhere((s) => s.value == selected);
    final shown = index >= 0 && n > 0;
    final pillColor = shown ? segments[index].color ?? accent : accent;
    final content = SizedBox(
      height: height - 6,
      child: Stack(
        children: [
          AnimatedAlign(
            duration: Tokens.motionBase,
            curve: Tokens.curveIn,
            alignment: Alignment(
              n < 2 || !shown ? -1 : -1 + 2 * index / (n - 1),
              0,
            ),
            child: FractionallySizedBox(
              widthFactor: n == 0 ? 1 : 1 / n,
              heightFactor: 1,
              child: AnimatedOpacity(
                duration: Tokens.motionFast,
                opacity: shown ? 1 : 0,
                child: TweenAnimationBuilder<Color?>(
                  tween: ColorTween(end: pillColor),
                  duration: Tokens.motionBase,
                  builder: (context, c, _) => DecoratedBox(
                    decoration: BoxDecoration(
                      color: c!.withValues(alpha: 0.22),
                      border: Border.all(
                        color: c.withValues(alpha: 0.36),
                        width: 0.75,
                      ),
                      borderRadius: BorderRadius.circular(
                        Tokens.radiusPill - 3,
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
          Row(
            children: [
              for (final s in segments)
                Expanded(
                  child: _Cell(
                    label: s.label,
                    icon: s.icon,
                    active: s.value == selected,
                    color: s.color ?? accent,
                    fontSize: fontSize,
                    iconSize: iconSize,
                    hPad: hPad,
                    iconOnly: iconOnly,
                    onTap: () => onSelect(s.value),
                  ),
                ),
            ],
          ),
        ],
      ),
    );

    if (frosted) {
      return GlassSurface(
        radius: Tokens.radiusPill,
        padding: const EdgeInsets.all(3),
        grouped: grouped,
        child: content,
      );
    }
    return Container(
      height: height,
      padding: const EdgeInsets.all(3),
      decoration: cardDecoration(
        radius: Tokens.radiusPill,
        color: Tokens.surface,
      ),
      child: content,
    );
  }
}

class _Cell extends StatelessWidget {
  const _Cell({
    required this.label,
    required this.icon,
    required this.active,
    required this.color,
    required this.fontSize,
    required this.iconSize,
    required this.hPad,
    required this.iconOnly,
    required this.onTap,
  });

  final String label;
  final IconData? icon;
  final bool active;
  final Color color;
  final double fontSize, iconSize, hPad;
  final bool iconOnly;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final child = GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: onTap,
      child: Padding(
        padding: EdgeInsets.symmetric(horizontal: iconOnly ? 2 : hPad),
        child: Center(
          child: iconOnly && icon != null
              ? TweenAnimationBuilder<Color?>(
                  tween: ColorTween(end: active ? color : Tokens.dim),
                  duration: Tokens.motionBase,
                  builder: (_, c, _) => Icon(icon, size: iconSize, color: c),
                )
              : Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (icon != null) ...[
                      TweenAnimationBuilder<Color?>(
                        tween: ColorTween(end: active ? color : Tokens.dim),
                        duration: Tokens.motionBase,
                        builder: (_, c, _) =>
                            Icon(icon, size: iconSize, color: c),
                      ),
                      const SizedBox(width: 4),
                    ],
                    Flexible(
                      child: AnimatedDefaultTextStyle(
                        duration: Tokens.motionBase,
                        style: TextStyle(
                          fontSize: fontSize,
                          fontWeight: active
                              ? FontWeight.w600
                              : FontWeight.w500,
                          color: active
                              ? (icon != null ? Tokens.text : color)
                              : Tokens.dim,
                        ),
                        child: Text(
                          label,
                          maxLines: 1,
                          softWrap: false,
                          overflow: TextOverflow.fade,
                        ),
                      ),
                    ),
                  ],
                ),
        ),
      ),
    );

    if (!iconOnly) return child;
    return Semantics(
      label: label,
      selected: active,
      button: true,
      child: Tooltip(message: label, child: child),
    );
  }
}
