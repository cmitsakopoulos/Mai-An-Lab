import 'package:flutter/material.dart';

import 'size_reporter.dart';
import 'tokens.dart';

/// The top inset occupied by the enclosing [PageHeader] (status bar + title
/// row + any `bottom` rows). Scrollables under a header pad their top by
/// [HeaderInsets.topOf] so content starts below the chrome but can still
/// scroll behind it.
class HeaderInsets extends InheritedWidget {
  const HeaderInsets({super.key, required this.top, required super.child});

  final double top;

  static double topOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<HeaderInsets>()?.top ??
      MediaQuery.paddingOf(context).top;

  @override
  bool updateShouldNotify(HeaderInsets old) => old.top != top;
}

/// The one top bar used by every primary tab (Jarvis, Search, Library).
///
/// Always floats over [child] behind [Tokens.headerScrim]: a fixed-height
/// title row (title, optional inline [caption], trailing [actions]) and
/// optional stacked [bottom] rows (search bar, view pills, banners). The
/// header measures itself and publishes its height through [HeaderInsets],
/// so screens never hard-code chrome heights.
class PageHeader extends StatefulWidget {
  const PageHeader({
    super.key,
    required this.title,
    this.leading,
    this.caption,
    this.actions = const [],
    this.bottom = const [],
    required this.child,
  });

  /// Height of the title row (excluding status bar and [bottom]).
  static const titleRowHeight = 52.0;

  final String title;

  /// Before the title; a [HeaderBackButton] on pushed pages.
  final Widget? leading;

  /// Short secondary text shown on the title's baseline (e.g. a count).
  final String? caption;

  /// Trailing controls; use [HeaderAction] (or [HeaderAction.style] for
  /// other buttons) so sizes and spacing match across tabs. Settings last.
  final List<Widget> actions;

  /// Rows stacked under the title, inside one [BackdropGroup] so grouped
  /// glass surfaces share a single blur pass.
  final List<Widget> bottom;

  /// The page content. Read [HeaderInsets.topOf] for its top padding.
  final Widget child;

  @override
  State<PageHeader> createState() => _PageHeaderState();
}

class _PageHeaderState extends State<PageHeader> {
  double? _measured;

  void _onSize(Size size) {
    if (size.height != _measured) setState(() => _measured = size.height);
  }

  @override
  Widget build(BuildContext context) {
    final hPad = Tokens.pagePad(context);
    final safeTop = MediaQuery.paddingOf(context).top;
    final top =
        _measured ?? safeTop + PageHeader.titleRowHeight + kPageHeaderGap;

    return Stack(
      fit: StackFit.expand,
      children: [
        HeaderInsets(top: top, child: widget.child),
        Positioned(
          top: 0,
          left: 0,
          right: 0,
          child: SizeReporter(
            onSize: _onSize,
            child: DecoratedBox(
              decoration: const BoxDecoration(gradient: Tokens.headerScrim),
              child: Padding(
                padding: EdgeInsets.only(top: safeTop),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    SizedBox(
                      height: PageHeader.titleRowHeight,
                      child: Padding(
                        // 44px targets with 22px icons: an 11px inner inset,
                        // so the last icon's edge lines up with the gutter.
                        padding: EdgeInsets.fromLTRB(
                          widget.leading == null ? hPad : hPad - 11,
                          0,
                          hPad - 11,
                          0,
                        ),
                        child: Row(
                          children: [
                            ?widget.leading,
                            Expanded(
                              child: _Title(
                                title: widget.title,
                                caption: widget.caption,
                              ),
                            ),
                            ...widget.actions,
                          ],
                        ),
                      ),
                    ),
                    if (widget.bottom.isNotEmpty)
                      BackdropGroup(
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: widget.bottom,
                        ),
                      ),
                    const SizedBox(height: kPageHeaderGap),
                  ],
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

/// Space under the last header row before the scrim fades out.
const kPageHeaderGap = 4.0;

class _Title extends StatelessWidget {
  const _Title({required this.title, this.caption});
  final String title;
  final String? caption;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.baseline,
      textBaseline: TextBaseline.alphabetic,
      children: [
        Flexible(
          flex: 3,
          child: Text(
            title,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              color: Tokens.text,
              fontSize: 24,
              fontWeight: FontWeight.w800,
              letterSpacing: -0.4,
            ),
          ),
        ),
        if (caption case final c?) ...[
          const SizedBox(width: 8),
          Flexible(
            flex: 2,
            child: Text(
              c,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(
                color: Tokens.dim,
                fontSize: 12,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
        ],
      ],
    );
  }
}

/// A [PageHeader] action: 22px icon in a 44px target, dim unless [accent].
class HeaderAction extends StatelessWidget {
  const HeaderAction({
    super.key,
    required this.icon,
    required this.tooltip,
    required this.onPressed,
    this.accent = false,
    this.color,
  });

  final IconData icon;
  final String tooltip;
  final VoidCallback? onPressed;

  /// Tint with the theme accent (an active toggle or the screen's primary
  /// action).
  final bool accent;

  /// Explicit colour; overrides [accent].
  final Color? color;

  static const iconSize = 22.0;

  /// Button style for non-[IconButton] actions (e.g. a `PopupMenuButton`).
  static final style = IconButton.styleFrom(
    fixedSize: const Size.square(44),
    minimumSize: const Size.square(44),
    padding: EdgeInsets.zero,
  );

  @override
  Widget build(BuildContext context) {
    return IconButton(
      icon: Icon(icon, size: iconSize),
      color:
          color ??
          (accent ? Theme.of(context).colorScheme.primary : Tokens.dim),
      tooltip: tooltip,
      style: style,
      onPressed: onPressed,
    );
  }
}

/// The [PageHeader.leading] back chevron for pushed pages, in the accent.
class HeaderBackButton extends StatelessWidget {
  const HeaderBackButton({super.key, required this.onPressed});
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) => IconButton(
    icon: const Icon(Icons.arrow_back_ios_new_rounded, size: 18),
    color: Theme.of(context).colorScheme.primary,
    tooltip: 'Back',
    style: HeaderAction.style,
    onPressed: onPressed,
  );
}
