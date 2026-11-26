import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';

import 'tokens.dart';

/// The app's one switch: an iOS-style toggle with a white thumb that keeps
/// its size, the accent track when on and a neutral raised track when off.
/// Material 3's default switch drew a dark thumb from the seeded scheme.
class AppSwitch extends StatelessWidget {
  const AppSwitch({super.key, required this.value, required this.onChanged});

  final bool value;

  /// Null disables the switch.
  final ValueChanged<bool>? onChanged;

  @override
  Widget build(BuildContext context) => CupertinoSwitch(
    value: value,
    onChanged: onChanged,
    activeTrackColor: Theme.of(context).colorScheme.primary,
    inactiveTrackColor: Tokens.surfaceElevated,
    thumbColor: Tokens.text,
  );
}
