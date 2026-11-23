import 'package:flutter/services.dart';

/// The `[haptics]` table of config.toml (Flet Settings → Haptics).
class HapticsConfig {
  const HapticsConfig({this.enabled = true, this.intensities = const {}});

  /// `haptic_feedback_enabled`, default on.
  final bool enabled;

  /// `<action>_intensity` values as stored, keyed by action.
  final Map<String, String> intensities;

  static HapticsConfig fromToml(Object? table) {
    if (table is! Map) return const HapticsConfig();
    final on = table['haptic_feedback_enabled'];
    final levels = <String, String>{};
    table.forEach((k, v) {
      final key = '$k';
      if (key.endsWith('_intensity') && v is String) {
        levels[key.substring(0, key.length - '_intensity'.length)] = v;
      }
    });
    return HapticsConfig(enabled: on is bool ? on : true, intensities: levels);
  }

  /// main.py `_trigger_haptic_async` defaults.
  static const defaults = {
    'eq_drag': 'light',
    'swipe_queue': 'medium',
    'swipe_back': 'light',
    'long_press': 'heavy',
    'network_tap': 'selection',
    'network_reseed': 'medium',
    'network_walk': 'light',
  };

  static const _levels = {
    'light',
    'medium',
    'heavy',
    'selection',
    'vibrate',
    'none',
  };

  /// The intensity [action] resolves to, or null for none. An intensity
  /// name passed as the action is used as is.
  String? intensityFor(String action) {
    if (!enabled) return null;
    final level =
        intensities[action] ??
        (_levels.contains(action) ? action : (defaults[action] ?? 'light'));
    return (level.isEmpty || level == 'none') ? null : level;
  }
}

/// `app.trigger_haptic(action)`: one tap of the configured intensity.
class Haptics {
  Haptics(this.config);

  HapticsConfig config;

  Future<void> trigger(String action) async {
    switch (config.intensityFor(action)) {
      case 'light':
        await HapticFeedback.lightImpact();
      case 'medium':
        await HapticFeedback.mediumImpact();
      case 'heavy':
        await HapticFeedback.heavyImpact();
      case 'selection':
        await HapticFeedback.selectionClick();
      case 'vibrate':
        await HapticFeedback.vibrate();
    }
  }
}
