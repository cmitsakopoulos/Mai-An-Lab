import 'package:flutter/foundation.dart';
import 'package:toml/toml.dart';

import '../core/app_config.dart';
import '../core/config_file.dart';

/// config.toml for the running app: what it says now ([config], and [raw]
/// for the Settings rows AppConfig doesn't model), and the writes Settings
/// makes (`update_config_params`), one at a time, in order.
class ConfigController extends ChangeNotifier {
  ConfigController(this.store);

  final ConfigStore store;

  AppConfig config = const AppConfig();

  /// The parsed file.
  Map<String, Object?> raw = const {};

  Future<void> _writes = Future.value();

  /// main.py's start: `load_config`, which creates the file from the
  /// template when it is missing (`ensure_config_exists`).
  static Future<ConfigController> load(ConfigStore store) async {
    final c = ConfigController(store);
    await c.reload();
    return c;
  }

  /// Read the file again (after a write, or the raw editor's save).
  Future<void> reload() async {
    try {
      final text = await store.read();
      raw = TomlDocument.parse(text).toMap();
      config = AppConfig.parse(text);
    } catch (e) {
      debugPrint('ConfigController: unreadable config.toml: $e');
    }
    notifyListeners();
  }

  /// `update_config_params(params)`; false when the file could not be
  /// written (Flet logs it; callers show what Flet shows).
  Future<bool> update(Map<String, Map<String, Object?>> params) {
    final done = _writes.then((_) => store.update(params));
    _writes = done.then((_) => reload());
    return done.then((ok) => _writes.then((_) => ok));
  }

  /// The file's text, for Settings → Advanced.
  Future<String> readText() => store.read();

  /// Settings → Advanced's SAVE CONFIG FILE.
  Future<void> saveText(String text) {
    final done = _writes.then((_) => store.writeRaw(text));
    _writes = done.then((_) => reload());
    return _writes;
  }

  /// `cfg.get(section, {}).get(key, fallback)`.
  Object? value(String section, String key) => switch (raw[section]) {
    final Map<Object?, Object?> t => t[key],
    _ => null,
  };

  String string(String section, String key, String fallback) =>
      switch (value(section, key)) {
        null => fallback,
        final Object v => '$v',
      };
}
