import 'package:flutter/material.dart';

import 'frost.dart';
import 'tokens.dart';

/// Unified Apple-style frosted-glass capsule search bar shared across Search,
/// Library, and Settings.
///
/// Handles its own clear-button visibility via [controller] and subtle focus
/// feedback (brighter glass rim + leading icon) without requiring parent
/// rebuilds on every keystroke.
class GlassSearchBar extends StatefulWidget {
  const GlassSearchBar({
    super.key,
    required this.controller,
    required this.hintText,
    this.onChanged,
    this.onSubmitted,
    this.onClear,
    this.loading = false,
    this.textInputAction = TextInputAction.done,
    this.grouped = false,
    this.elevated = true,
    this.margin,
    this.height = 46.0,
    this.focusNode,
    this.clearTooltip = 'Clear search',
  });

  final TextEditingController controller;
  final String hintText;
  final ValueChanged<String>? onChanged;
  final ValueChanged<String>? onSubmitted;

  /// Called when the circular clear button is tapped. If omitted, clears
  /// [controller] and notifies [onChanged] with `''`.
  final VoidCallback? onClear;

  /// Shows a compact accent spinner alongside/before the clear button.
  final bool loading;

  final TextInputAction textInputAction;

  /// Forwarded to [GlassSurface.grouped] for screens using a [BackdropGroup].
  final bool grouped;

  /// Forwarded to [GlassSurface.elevated].
  final bool elevated;

  final EdgeInsetsGeometry? margin;
  final double height;
  final FocusNode? focusNode;
  final String clearTooltip;

  @override
  State<GlassSearchBar> createState() => _GlassSearchBarState();
}

class _GlassSearchBarState extends State<GlassSearchBar> {
  FocusNode? _ownedFocus;
  bool _focused = false;

  FocusNode get _focusNode => widget.focusNode ?? _ownedFocus!;

  @override
  void initState() {
    super.initState();
    if (widget.focusNode == null) {
      _ownedFocus = FocusNode();
    }
    _focused = _focusNode.hasFocus;
    _focusNode.addListener(_onFocusChange);
  }

  @override
  void didUpdateWidget(GlassSearchBar oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.focusNode != widget.focusNode) {
      (oldWidget.focusNode ?? _ownedFocus)?.removeListener(_onFocusChange);
      if (widget.focusNode == null && _ownedFocus == null) {
        _ownedFocus = FocusNode();
      }
      _focused = _focusNode.hasFocus;
      _focusNode.addListener(_onFocusChange);
    }
  }

  void _onFocusChange() {
    final now = _focusNode.hasFocus;
    if (now != _focused) {
      setState(() => _focused = now);
    }
  }

  @override
  void dispose() {
    _focusNode.removeListener(_onFocusChange);
    _ownedFocus?.dispose();
    super.dispose();
  }

  void _handleClear() {
    if (widget.onClear != null) {
      widget.onClear!();
      return;
    }
    widget.controller.clear();
    widget.onChanged?.call('');
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final op = GlassScope.opacityOf(context);
    final isSolid = op >= 0.99;

    final focusedBorder = isSolid
        ? Colors.white.withValues(alpha: 0.24)
        : Colors.white.withValues(alpha: 0.32);

    return GlassSurface(
      radius: 999,
      grouped: widget.grouped,
      elevated: widget.elevated,
      margin: widget.margin,
      borderColor: _focused ? focusedBorder : null,
      padding: const EdgeInsets.only(left: 14, right: 6),
      child: SizedBox(
        height: widget.height,
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            TweenAnimationBuilder<Color?>(
              duration: Tokens.motionFast,
              tween: ColorTween(
                end: _focused
                    ? Tokens.text.withValues(alpha: 0.90)
                    : Tokens.dim,
              ),
              builder: (_, color, _) =>
                  Icon(Icons.search_rounded, color: color, size: 19),
            ),
            const SizedBox(width: 9),
            Expanded(
              child: TextField(
                controller: widget.controller,
                focusNode: _focusNode,
                textInputAction: widget.textInputAction,
                cursorColor: accent,
                onChanged: widget.onChanged,
                onSubmitted: widget.onSubmitted,
                style: const TextStyle(
                  color: Tokens.text,
                  fontSize: 15,
                  height: 1.2,
                ),
                decoration: InputDecoration(
                  hintText: widget.hintText,
                  hintStyle: TextStyle(
                    color: Tokens.dim.withValues(alpha: 0.9),
                    fontSize: 15,
                    height: 1.2,
                  ),
                  filled: false,
                  border: InputBorder.none,
                  enabledBorder: InputBorder.none,
                  focusedBorder: InputBorder.none,
                  isDense: true,
                  contentPadding: const EdgeInsets.symmetric(vertical: 10),
                ),
              ),
            ),
            if (widget.loading)
              Padding(
                padding: const EdgeInsets.only(left: 4, right: 6),
                child: SizedBox(
                  width: 15,
                  height: 15,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    color: accent,
                  ),
                ),
              ),
            ValueListenableBuilder<TextEditingValue>(
              valueListenable: widget.controller,
              builder: (context, value, _) {
                final hasText = value.text.isNotEmpty;
                return AnimatedSwitcher(
                  duration: Tokens.motionFast,
                  switchInCurve: Tokens.curveIn,
                  switchOutCurve: Tokens.curveOut,
                  transitionBuilder: (child, anim) => FadeTransition(
                    opacity: anim,
                    child: ScaleTransition(scale: anim, child: child),
                  ),
                  child: hasText
                      ? IconButton(
                          key: const ValueKey('clear'),
                          tooltip: widget.clearTooltip,
                          onPressed: _handleClear,
                          constraints: const BoxConstraints.tightFor(
                            width: 34,
                            height: 34,
                          ),
                          padding: EdgeInsets.zero,
                          splashRadius: 16,
                          icon: Container(
                            width: 19,
                            height: 19,
                            decoration: BoxDecoration(
                              color: Colors.white.withValues(alpha: 0.18),
                              shape: BoxShape.circle,
                            ),
                            alignment: Alignment.center,
                            child: const Icon(
                              Icons.close_rounded,
                              size: 13,
                              color: Tokens.text,
                            ),
                          ),
                        )
                      : const SizedBox(
                          key: ValueKey('empty'),
                          width: 6,
                          height: 34,
                        ),
                );
              },
            ),
          ],
        ),
      ),
    );
  }
}
