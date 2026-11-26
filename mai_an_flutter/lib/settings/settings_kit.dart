import 'package:flutter/material.dart';

import '../ui/app_switch.dart';
import '../ui/frost.dart';
import '../ui/tokens.dart';

export '../ui/app_switch.dart';

/// The one visual language for every Settings page: grouped inset sections
/// of uniform rows. A page is a list of [SettingsSection]s; a section is a
/// list of rows ([SettingsTile] and its variants, or [SettingsCustomTile]
/// for panels that are not rows). Dividers, padding and type come from here,
/// never from the page.
abstract final class SettingsStyle {
  static const hPad = 16.0;
  static const vPad = 11.0;
  static const minRowHeight = 50.0;
  static const iconBadge = 30.0;
  static const iconGap = 12.0;

  /// Where a divider starts under a row with a leading icon badge.
  static const iconIndent = hPad + iconBadge + iconGap;

  static const title = TextStyle(
    color: Tokens.text,
    fontSize: 15,
    fontWeight: FontWeight.w500,
    height: 1.25,
  );
  static const subtitle = TextStyle(
    color: Tokens.dim,
    fontSize: 12,
    height: 1.3,
  );
  static const value = TextStyle(color: Tokens.dim, fontSize: 14);
  static const footer = TextStyle(
    color: Tokens.dim,
    fontSize: 12,
    height: 1.35,
  );
  static const divider = Tokens.borderSubtle;
}

/// An uppercase header, a card of rows split by inset hairlines, and an
/// optional footnote.
class SettingsSection extends StatelessWidget {
  const SettingsSection({
    super.key,
    this.header,
    this.headerTrailing,
    this.footer,
    required this.children,
  });

  final String? header;
  final Widget? headerTrailing;
  final String? footer;
  final List<Widget> children;

  static double _indentOf(Widget w) => switch (w) {
    SettingsTile(leading: _?) => SettingsStyle.iconIndent,
    _ => SettingsStyle.hPad,
  };

  @override
  Widget build(BuildContext context) {
    final rows = [for (final c in children) c];
    const r = BorderRadius.all(Radius.circular(Tokens.radiusCard));
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (header != null || headerTrailing != null)
          Padding(
            padding: const EdgeInsets.fromLTRB(4, 0, 4, 8),
            child: Row(
              children: [
                if (header != null)
                  Expanded(
                    child: Text(
                      header!.toUpperCase(),
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.primary,
                        fontSize: 11,
                        fontWeight: FontWeight.w700,
                        letterSpacing: 0.6,
                      ),
                    ),
                  )
                else
                  const Spacer(),
                ?headerTrailing,
              ],
            ),
          ),
        GlassCard(
          padding: EdgeInsets.zero,
          child: ClipRRect(
            borderRadius: r,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                for (final (i, row) in rows.indexed) ...[
                  if (i > 0)
                    Divider(
                      height: 0.5,
                      thickness: 0.5,
                      indent: _indentOf(row),
                      color: SettingsStyle.divider,
                    ),
                  row,
                ],
              ],
            ),
          ),
        ),
        if (footer != null)
          Padding(
            padding: const EdgeInsets.fromLTRB(4, 8, 4, 0),
            child: Text(footer!, style: SettingsStyle.footer),
          ),
      ],
    );
  }
}

/// A rounded-square tinted icon, the leading badge of a row.
class SettingsIcon extends StatelessWidget {
  const SettingsIcon(this.icon, {super.key, this.color});

  final IconData icon;

  /// Null: the accent.
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final c = color ?? Theme.of(context).colorScheme.primary;
    return Container(
      width: SettingsStyle.iconBadge,
      height: SettingsStyle.iconBadge,
      decoration: BoxDecoration(
        color: c.withValues(alpha: 0.18),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Icon(icon, color: c, size: 17),
    );
  }
}

/// One settings row: optional leading badge, title over subtitle, and a
/// trailing control. The named constructors are the standard kinds.
class SettingsTile extends StatelessWidget {
  const SettingsTile({
    super.key,
    required this.title,
    this.subtitle,
    this.leading,
    this.trailing,
    this.onTap,
    this.enabled = true,
    this.titleColor,
  });

  /// A switch; the whole row toggles it.
  factory SettingsTile.toggle({
    Key? key,
    required String title,
    String? subtitle,
    Widget? leading,
    required bool value,
    required ValueChanged<bool>? onChanged,
  }) => SettingsTile(
    key: key,
    title: title,
    subtitle: subtitle,
    leading: leading,
    enabled: onChanged != null,
    onTap: onChanged == null ? null : () => onChanged(!value),
    trailing: AppSwitch(value: value, onChanged: onChanged),
  );

  /// Opens something: a sub-page, a sheet. Shows [value] before the
  /// chevron when given.
  factory SettingsTile.nav({
    Key? key,
    required String title,
    String? subtitle,
    Widget? leading,
    String? value,
    required VoidCallback? onTap,
  }) => SettingsTile(
    key: key,
    title: title,
    subtitle: subtitle,
    leading: leading,
    onTap: onTap,
    enabled: onTap != null,
    trailing: _NavTrailing(value: value),
  );

  /// Does something now. [destructive] colours it red.
  factory SettingsTile.action({
    Key? key,
    required String title,
    String? subtitle,
    Widget? leading,
    bool destructive = false,
    required VoidCallback? onTap,
  }) => SettingsTile(
    key: key,
    title: title,
    subtitle: subtitle,
    leading: leading,
    onTap: onTap,
    enabled: onTap != null,
    titleColor: destructive ? Tokens.accentRed : null,
  );

  final String title;
  final String? subtitle;
  final Widget? leading;
  final Widget? trailing;
  final VoidCallback? onTap;
  final bool enabled;

  /// Null: the text colour; [SettingsTile.action] uses the accent.
  final Color? titleColor;

  @override
  Widget build(BuildContext context) {
    final isAction = trailing == null && onTap != null;
    final color =
        titleColor ??
        (isAction ? Theme.of(context).colorScheme.primary : Tokens.text);
    final row = ConstrainedBox(
      constraints: const BoxConstraints(minHeight: SettingsStyle.minRowHeight),
      child: Padding(
        padding: const EdgeInsets.symmetric(
          horizontal: SettingsStyle.hPad,
          vertical: SettingsStyle.vPad,
        ),
        child: Row(
          children: [
            if (leading != null) ...[
              leading!,
              const SizedBox(width: SettingsStyle.iconGap),
            ],
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    title,
                    style: SettingsStyle.title.copyWith(color: color),
                  ),
                  if (subtitle != null) ...[
                    const SizedBox(height: 2),
                    Text(subtitle!, style: SettingsStyle.subtitle),
                  ],
                ],
              ),
            ),
            if (trailing != null) ...[const SizedBox(width: 12), trailing!],
          ],
        ),
      ),
    );
    return Opacity(
      opacity: enabled ? 1 : 0.45,
      child: onTap == null ? row : InkWell(onTap: onTap, child: row),
    );
  }
}

class _NavTrailing extends StatelessWidget {
  const _NavTrailing({this.value});

  final String? value;

  @override
  Widget build(BuildContext context) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      if (value != null)
        ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 170),
          child: Text(
            value!,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            textAlign: TextAlign.right,
            style: SettingsStyle.value,
          ),
        ),
      const SizedBox(width: 2),
      Icon(
        Icons.chevron_right_rounded,
        color: Tokens.dim.withValues(alpha: 0.6),
        size: 20,
      ),
    ],
  );
}

/// One choice in a [SettingsPickerTile] or [SettingsSegmented].
class SettingsOption<T> {
  const SettingsOption(this.value, this.label, {this.subtitle, this.icon});

  final T value;
  final String label;
  final String? subtitle;
  final IconData? icon;
}

/// A row showing the current choice; tapping opens a sheet of the options.
/// For more than three options, or labels too long for a segmented control.
class SettingsPickerTile<T> extends StatelessWidget {
  const SettingsPickerTile({
    super.key,
    required this.title,
    required this.value,
    required this.options,
    required this.onChanged,
    this.subtitle,
    this.leading,
    this.sheetTitle,
  });

  final String title;
  final String? subtitle;
  final Widget? leading;
  final T value;
  final List<SettingsOption<T>> options;
  final ValueChanged<T>? onChanged;

  /// Null: [title].
  final String? sheetTitle;

  @override
  Widget build(BuildContext context) {
    final current = options.where((o) => o.value == value).firstOrNull;
    return SettingsTile.nav(
      title: title,
      subtitle: subtitle,
      leading: leading,
      value: current?.label ?? '',
      onTap: onChanged == null
          ? null
          : () async {
              final picked = await showSettingsPicker<T>(
                context,
                title: sheetTitle ?? title,
                value: value,
                options: options,
              );
              if (picked != null) onChanged!(picked.value);
            },
    );
  }
}

/// A frosted sheet listing [options] with a check on [value]. Returns the
/// picked option (wrapped, so a nullable `T` can be picked), or null when
/// dismissed.
Future<SettingsOption<T>?> showSettingsPicker<T>(
  BuildContext context, {
  required String title,
  required T value,
  required List<SettingsOption<T>> options,
}) {
  final accent = Theme.of(context).colorScheme.primary;
  return showFrostedSheet<SettingsOption<T>>(
    context: context,
    showDragHandle: true,
    isScrollControlled: options.length > 7,
    builder: (ctx) => SafeArea(
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 4, 20, 12),
              child: Text(
                title,
                style: const TextStyle(
                  color: Tokens.text,
                  fontSize: 16,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
            const Divider(height: 1, color: Tokens.borderSubtle),
            for (final o in options)
              InkWell(
                onTap: () => Navigator.pop(ctx, o),
                child: Padding(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 20,
                    vertical: 13,
                  ),
                  child: Row(
                    children: [
                      if (o.icon != null) ...[
                        Icon(
                          o.icon,
                          size: 20,
                          color: o.value == value ? accent : Tokens.dim,
                        ),
                        const SizedBox(width: 14),
                      ],
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              o.label,
                              style: SettingsStyle.title.copyWith(
                                color: o.value == value ? accent : Tokens.text,
                                fontWeight: o.value == value
                                    ? FontWeight.w600
                                    : FontWeight.w400,
                              ),
                            ),
                            if (o.subtitle != null)
                              Text(o.subtitle!, style: SettingsStyle.subtitle),
                          ],
                        ),
                      ),
                      if (o.value == value)
                        Icon(Icons.check_rounded, color: accent, size: 20),
                    ],
                  ),
                ),
              ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    ),
  );
}

/// A row with a [SettingsSegmented] control: beside the title when
/// [inline] (two or three short labels), else full width under it.
class SettingsSegmentedTile<T> extends StatelessWidget {
  const SettingsSegmentedTile({
    super.key,
    required this.title,
    required this.value,
    required this.options,
    required this.onChanged,
    this.subtitle,
    this.leading,
    this.inline = true,
  });

  final String title;
  final String? subtitle;
  final Widget? leading;
  final T value;
  final List<SettingsOption<T>> options;
  final ValueChanged<T> onChanged;
  final bool inline;

  @override
  Widget build(BuildContext context) {
    final control = SettingsSegmented<T>(
      value: value,
      options: options,
      onChanged: onChanged,
    );
    if (inline) {
      return SettingsTile(
        title: title,
        subtitle: subtitle,
        leading: leading,
        trailing: ConstrainedBox(
          constraints: BoxConstraints(maxWidth: 84.0 * options.length),
          child: control,
        ),
      );
    }
    return SettingsCustomTile(title: title, subtitle: subtitle, child: control);
  }
}

/// A compact value control: a recessed track and a raised thumb that slides
/// to the choice, in neutral greys (it picks a value; it is not navigation,
/// so it does not borrow the tab bars' tinted pill).
class SettingsSegmented<T> extends StatelessWidget {
  const SettingsSegmented({
    super.key,
    required this.value,
    required this.options,
    required this.onChanged,
  });

  final T value;
  final List<SettingsOption<T>> options;
  final ValueChanged<T> onChanged;

  @override
  Widget build(BuildContext context) {
    final n = options.length;
    final index = options.indexWhere((o) => o.value == value);
    return Container(
      height: 32,
      padding: const EdgeInsets.all(2),
      decoration: BoxDecoration(
        color: Tokens.surface2,
        borderRadius: BorderRadius.circular(9),
      ),
      child: Stack(
        children: [
          if (index >= 0)
            AnimatedAlign(
              duration: Tokens.motionBase,
              curve: Tokens.curveIn,
              alignment: Alignment(n < 2 ? -1 : -1 + 2 * index / (n - 1), 0),
              child: FractionallySizedBox(
                widthFactor: 1 / n,
                heightFactor: 1,
                child: DecoratedBox(
                  decoration: BoxDecoration(
                    color: Tokens.surfaceElevated,
                    borderRadius: BorderRadius.circular(7),
                    boxShadow: const [
                      BoxShadow(
                        color: Color(0x40000000),
                        blurRadius: 4,
                        offset: Offset(0, 1),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          Row(
            children: [
              for (final (i, o) in options.indexed)
                Expanded(
                  child: Semantics(
                    button: true,
                    selected: i == index,
                    label: o.label,
                    excludeSemantics: true,
                    child: GestureDetector(
                      behavior: HitTestBehavior.opaque,
                      onTap: () => onChanged(o.value),
                      child: Center(
                        child: Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 6),
                          child: Row(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              if (o.icon != null) ...[
                                Icon(
                                  o.icon,
                                  size: 15,
                                  color: i == index ? Tokens.text : Tokens.dim,
                                ),
                                const SizedBox(width: 5),
                              ],
                              Flexible(
                                child: AnimatedDefaultTextStyle(
                                  duration: Tokens.motionFast,
                                  style: TextStyle(
                                    fontSize: 13,
                                    fontWeight: i == index
                                        ? FontWeight.w600
                                        : FontWeight.w500,
                                    color: i == index
                                        ? Tokens.text
                                        : Tokens.dim,
                                  ),
                                  child: Text(
                                    o.label,
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                  ),
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
            ],
          ),
        ],
      ),
    );
  }
}

/// A slider row: title and the current value on one line, the slider under
/// them, and optional end labels.
class SettingsSliderTile extends StatelessWidget {
  const SettingsSliderTile({
    super.key,
    required this.title,
    required this.value,
    required this.valueLabel,
    required this.onChanged,
    this.onChangeEnd,
    this.min = 0,
    this.max = 1,
    this.divisions,
    this.subtitle,
    this.marks = const [],
  });

  final String title;
  final String? subtitle;
  final double value, min, max;
  final int? divisions;
  final String valueLabel;
  final ValueChanged<double>? onChanged;
  final ValueChanged<double>? onChangeEnd;

  /// Labels spread evenly under the track ("Clear", "Solid").
  final List<String> marks;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Padding(
      padding: const EdgeInsets.fromLTRB(
        SettingsStyle.hPad,
        SettingsStyle.vPad + 2,
        SettingsStyle.hPad,
        SettingsStyle.vPad,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(child: Text(title, style: SettingsStyle.title)),
              Text(
                valueLabel,
                style: TextStyle(
                  color: accent,
                  fontSize: 14,
                  fontWeight: FontWeight.w600,
                  fontFeatures: const [FontFeature.tabularFigures()],
                ),
              ),
            ],
          ),
          if (subtitle != null) ...[
            const SizedBox(height: 2),
            Text(subtitle!, style: SettingsStyle.subtitle),
          ],
          const SizedBox(height: 6),
          SliderTheme(
            data: SliderTheme.of(context).copyWith(
              trackHeight: 4,
              activeTrackColor: accent,
              inactiveTrackColor: Tokens.surfaceElevated,
              thumbColor: Tokens.text,
              thumbShape: const RoundSliderThumbShape(
                enabledThumbRadius: 11,
                elevation: 2,
              ),
              overlayShape: SliderComponentShape.noOverlay,
              trackShape: const RoundedRectSliderTrackShape(),
            ),
            child: SizedBox(
              height: 28,
              child: Slider(
                value: value.clamp(min, max),
                min: min,
                max: max,
                divisions: divisions,
                onChanged: onChanged,
                onChangeEnd: onChangeEnd,
              ),
            ),
          ),
          if (marks.isNotEmpty) ...[
            const SizedBox(height: 4),
            Row(
              mainAxisAlignment: marks.length == 1
                  ? MainAxisAlignment.start
                  : MainAxisAlignment.spaceBetween,
              children: [
                for (final m in marks)
                  Text(
                    m,
                    style: const TextStyle(
                      color: Tokens.textTertiary,
                      fontSize: 11,
                    ),
                  ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}

/// A panel that is not a row (a preview, swatches, a chart) placed in a
/// section with the rows' padding, under an optional row-style title.
class SettingsCustomTile extends StatelessWidget {
  const SettingsCustomTile({
    super.key,
    required this.child,
    this.title,
    this.subtitle,
    this.padding = const EdgeInsets.symmetric(
      horizontal: SettingsStyle.hPad,
      vertical: SettingsStyle.vPad + 2,
    ),
  });

  final Widget child;
  final String? title;
  final String? subtitle;
  final EdgeInsetsGeometry padding;

  @override
  Widget build(BuildContext context) => Padding(
    padding: padding,
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (title != null) Text(title!, style: SettingsStyle.title),
        if (subtitle != null) ...[
          const SizedBox(height: 2),
          Text(subtitle!, style: SettingsStyle.subtitle),
        ],
        if (title != null || subtitle != null) const SizedBox(height: 10),
        child,
      ],
    ),
  );
}

/// A text field with its label stacked above the box and optional help
/// under it; for typed values (credentials, prompts), inside a
/// [SettingsCustomTile]. [obscureToggle] hides the text behind an eye.
class ModernTextField extends StatefulWidget {
  const ModernTextField({
    super.key,
    required this.label,
    required this.controller,
    this.hint,
    this.minLines = 1,
    this.maxLines = 1,
    this.obscureToggle = false,
    this.fieldKey,
    this.help,
  });

  final String label;
  final TextEditingController controller;
  final String? hint;
  final int minLines;
  final int maxLines;
  final bool obscureToggle;
  final Key? fieldKey;

  /// A dim line under the box.
  final String? help;

  @override
  State<ModernTextField> createState() => _ModernTextFieldState();
}

class _ModernTextFieldState extends State<ModernTextField> {
  late bool _obscured = widget.obscureToggle;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    OutlineInputBorder border(Color c, [double width = 1.0]) =>
        OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: BorderSide(color: c, width: width),
        );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Padding(
          padding: const EdgeInsets.only(left: 2, bottom: 6),
          child: Text(
            widget.label,
            style: const TextStyle(
              color: Tokens.dim,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
        TextField(
          key: widget.fieldKey,
          controller: widget.controller,
          obscureText: _obscured,
          minLines: _obscured ? 1 : widget.minLines,
          maxLines: _obscured ? 1 : widget.maxLines,
          style: const TextStyle(
            color: Tokens.text,
            fontSize: 13,
            height: 1.35,
          ),
          decoration: InputDecoration(
            hintText: widget.hint,
            hintStyle: const TextStyle(
              color: Tokens.textTertiary,
              fontSize: 13,
            ),
            filled: true,
            fillColor: Tokens.surface2,
            contentPadding: const EdgeInsets.symmetric(
              horizontal: 14,
              vertical: 12,
            ),
            enabledBorder: border(Tokens.borderSubtle),
            focusedBorder: border(accent, 1.3),
            border: border(Tokens.borderSubtle),
            suffixIcon: widget.obscureToggle
                ? IconButton(
                    icon: Icon(
                      _obscured
                          ? Icons.visibility_rounded
                          : Icons.visibility_off_rounded,
                      size: 18,
                      color: Tokens.dim,
                    ),
                    tooltip: _obscured ? 'Show key' : 'Hide key',
                    onPressed: () => setState(() => _obscured = !_obscured),
                  )
                : null,
          ),
        ),
        if (widget.help != null)
          Padding(
            padding: const EdgeInsets.only(left: 2, top: 6),
            child: Text(widget.help!, style: SettingsStyle.subtitle),
          ),
      ],
    );
  }
}
