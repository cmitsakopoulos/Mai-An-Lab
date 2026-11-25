import 'dart:async';

import 'package:flutter/material.dart';

import 'frost.dart';
import 'tokens.dart';

/// Top-floating frosted glass toast (Dynamic-Island style) that replaces boxy
/// bottom Material SnackBars. Floats cleanly below the status bar so it never
/// collides with the floating bottom glass dock or save pills, wraps up to 2
/// lines cleanly, and can be swiped up to dismiss.

OverlayEntry? _activeEntry;
GlobalKey<_TopToastState>? _activeKey;

/// Dismisses any currently visible top toast immediately.
void hideAppToast() {
  _activeKey = null;
  _activeEntry?.remove();
  _activeEntry = null;
}

/// Shows a top-floating frosted glass toast using the nearest [Overlay].
void showAppToast(
  BuildContext context,
  String message, {
  IconData? icon,
  Color? color,
  String? actionLabel,
  VoidCallback? onAction,
  Duration? duration,
}) {
  final overlay = Overlay.maybeOf(context, rootOverlay: true);
  if (overlay == null) return;
  showAppToastOn(
    overlay,
    message,
    icon: icon,
    color: color,
    actionLabel: actionLabel,
    onAction: onAction,
    duration: duration,
  );
}

/// Shows a top-floating frosted glass toast directly on [overlay] (used from
/// top-level callbacks in `main.dart` that hold a navigator key).
void showAppToastOn(
  OverlayState overlay,
  String message, {
  IconData? icon,
  Color? color,
  String? actionLabel,
  VoidCallback? onAction,
  Duration? duration,
}) {
  if (!overlay.mounted) return;
  hideAppToast();

  final key = GlobalKey<_TopToastState>();
  _activeKey = key;

  final ttl =
      duration ??
      (actionLabel != null
          ? const Duration(milliseconds: 4500)
          : const Duration(milliseconds: 3200));

  final entry = OverlayEntry(
    builder: (context) => _TopToast(
      key: key,
      message: message,
      icon: icon,
      color: color,
      actionLabel: actionLabel,
      onAction: onAction,
      duration: ttl,
      onDismissed: () {
        if (_activeKey == key) hideAppToast();
      },
    ),
  );

  _activeEntry = entry;
  overlay.insert(entry);
}

class _TopToast extends StatefulWidget {
  const _TopToast({
    super.key,
    required this.message,
    required this.duration,
    required this.onDismissed,
    this.icon,
    this.color,
    this.actionLabel,
    this.onAction,
  });

  final String message;
  final Duration duration;
  final IconData? icon;
  final Color? color;
  final String? actionLabel;
  final VoidCallback? onAction;
  final VoidCallback onDismissed;

  @override
  State<_TopToast> createState() => _TopToastState();
}

class _TopToastState extends State<_TopToast>
    with SingleTickerProviderStateMixin {
  late final AnimationController _anim = AnimationController(
    vsync: this,
    duration: Tokens.motionBase,
    reverseDuration: Tokens.motionFast,
  )..forward();

  late final Animation<Offset> _slide = Tween(
    begin: const Offset(0, -1.1),
    end: Offset.zero,
  ).animate(CurvedAnimation(parent: _anim, curve: Curves.easeOutCubic));

  late final Animation<double> _fade = CurvedAnimation(
    parent: _anim,
    curve: Curves.easeOut,
  );

  Timer? _timer;
  bool _dismissing = false;

  @override
  void initState() {
    super.initState();
    _timer = Timer(widget.duration, dismiss);
  }

  Future<void> dismiss() async {
    _timer?.cancel();
    _timer = null;
    if (_dismissing || !mounted) return;
    _dismissing = true;
    try {
      await _anim.reverse();
    } finally {
      widget.onDismissed();
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    _anim.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final topPad = MediaQuery.paddingOf(context).top;
    final accent = widget.color ?? Theme.of(context).colorScheme.primary;
    final hPad = Tokens.pagePad(context);

    return Positioned(
      top: topPad + 8,
      left: hPad,
      right: hPad,
      child: IgnorePointer(
        ignoring: _dismissing,
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 440),
            child: SlideTransition(
              position: _slide,
              child: FadeTransition(
                opacity: _fade,
                child: Dismissible(
                  key: const ValueKey('app_top_toast'),
                  direction: DismissDirection.up,
                  onDismissed: (_) => widget.onDismissed(),
                  child: Material(
                    type: MaterialType.transparency,
                    child: GlassSurface(
                      radius: 20,
                      grouped: false,
                      padding: const EdgeInsets.symmetric(
                        horizontal: 14,
                        vertical: 10,
                      ),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Container(
                            width: 26,
                            height: 26,
                            decoration: BoxDecoration(
                              color: accent.withValues(alpha: 0.16),
                              borderRadius: BorderRadius.circular(8),
                            ),
                            child: Icon(
                              widget.icon ?? Icons.info_outline_rounded,
                              color: accent,
                              size: 16,
                            ),
                          ),
                          const SizedBox(width: 10),
                          Flexible(
                            child: Text(
                              widget.message,
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                color: Tokens.text,
                                fontSize: 13,
                                fontWeight: FontWeight.w500,
                                height: 1.28,
                              ),
                            ),
                          ),
                          if (widget.actionLabel != null &&
                              widget.onAction != null) ...[
                            const SizedBox(width: 10),
                            InkWell(
                              borderRadius: BorderRadius.circular(10),
                              onTap: () {
                                widget.onAction!();
                                dismiss();
                              },
                              child: Container(
                                padding: const EdgeInsets.symmetric(
                                  horizontal: 10,
                                  vertical: 5,
                                ),
                                decoration: BoxDecoration(
                                  color: accent.withValues(alpha: 0.18),
                                  borderRadius: BorderRadius.circular(10),
                                ),
                                child: Text(
                                  widget.actionLabel!,
                                  style: TextStyle(
                                    color: accent,
                                    fontSize: 12,
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                              ),
                            ),
                          ],
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
