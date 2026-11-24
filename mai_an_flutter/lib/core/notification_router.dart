import 'package:flutter/foundation.dart';

/// main.py `_on_notification_navigate`: a notification (or its button) was
/// tapped; its target is "screen" or "screen:action". Screens register as
/// they are ported (sync, downloads, workbench, library); a tap that
/// arrives before its screen has registered (a cold start) is held until it
/// does.
class NotificationRouter {
  final _handlers = <String, void Function(String action)>{};
  String? _pending;

  void register(String screen, void Function(String action) handler) {
    _handlers[screen] = handler;
    final p = _pending;
    if (p != null && _split(p).$1 == screen) {
      _pending = null;
      route(p);
    }
  }

  void unregister(String screen) => _handlers.remove(screen);

  void route(String? target) {
    if (target == null || target.isEmpty) return;
    final (screen, action) = _split(target);
    final h = _handlers[screen];
    if (h == null) {
      _pending = target;
      return;
    }
    try {
      h(action);
    } catch (e) {
      debugPrint('notification navigation to $target failed: $e');
    }
  }

  static (String, String) _split(String target) {
    final i = target.indexOf(':');
    return i < 0
        ? (target, '')
        : (target.substring(0, i), target.substring(i + 1));
  }
}
