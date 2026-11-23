import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

/// A tap or button on one of the app's own notifications.
/// `job == 'navigate'` carries a route target in [action].
typedef NotificationAction = ({String job, String action});

/// Dart side of FletAudioServicePlugin.kt. Same channel name and method names
/// as the Flet extension, because it is the same Kotlin.
///
/// Every call is safe off Android (returns null / does nothing), so desktop
/// tests and `flutter run -d macos` don't need a fake.
abstract final class MaianNative {
  static const _channel = MethodChannel('com.flet.flet_audio_service/decode');

  static bool get _android =>
      !kIsWeb && defaultTargetPlatform == TargetPlatform.android;

  static final _actions = StreamController<NotificationAction>.broadcast();
  static bool _listening = false;

  /// Notification taps and buttons. Subscribing installs the channel handler;
  /// call [takePendingNavigate] once afterwards for a cold-start tap.
  static Stream<NotificationAction> get notificationActions {
    if (!_listening && _android) {
      _listening = true;
      _channel.setMethodCallHandler((call) async {
        if (call.method == 'notificationAction') {
          final a = (call.arguments as Map?) ?? const {};
          _actions.add((
            job: a['job']?.toString() ?? '',
            action: a['action']?.toString() ?? '',
          ));
        }
        return null;
      });
    }
    return _actions.stream;
  }

  /// A notification tap that launched the app before Dart was listening.
  static Future<String?> takePendingNavigate() => _invoke('takePendingNavigate');

  /// Embedded art (sidecar as fallback), downscaled to a JPEG in the app
  /// cache, cached per [key]. Null when the file has none.
  static Future<String?> resolveArtwork({
    required String path,
    String? sidecar,
    required String key,
  }) => _invoke('resolveArtwork', {
    'path': path,
    'sidecar': sidecar,
    'key': key,
  });

  /// Drops audio_service's foreground state and wakelock, which its own
  /// stop() can't do (see releasePlaybackService in the Kotlin).
  static Future<void> releasePlaybackService() =>
      _invoke<Object?>('releasePlaybackService');

  static Future<void> showAlert({
    required String key,
    String icon = '',
    required String title,
    required String content,
    String navigateTo = '',
    String? actionLabel,
    String? actionTarget,
    bool urgent = false,
  }) => _invoke<Object?>('showAlertNotification', {
    'key': key,
    'icon': icon,
    'title': title,
    'content': content,
    'navigateTo': navigateTo,
    'actionLabel': actionLabel,
    'actionTarget': actionTarget,
    'urgent': urgent,
  });

  static Future<void> cancelAlert(String key) =>
      _invoke<Object?>('cancelAlertNotification', {'key': key});

  static Future<T?> _invoke<T>(String method, [Object? args]) async {
    if (!_android) return null;
    try {
      return await _channel.invokeMethod<T>(method, args);
    } catch (e) {
      debugPrint('MaianNative.$method failed: $e');
      return null;
    }
  }
}
