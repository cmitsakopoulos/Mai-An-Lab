import 'dart:async';
import 'dart:io' show Platform;

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

  /// The real OS, not `defaultTargetPlatform`, which widget tests report
  /// as Android: there the channel has no messenger.
  static bool get _android => !kIsWeb && Platform.isAndroid;

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
  static Future<String?> takePendingNavigate() =>
      _invoke('takePendingNavigate');

  /// Embedded art (sidecar as fallback), downscaled to a JPEG in the app
  /// cache, cached per [key]. Null when the file has none.
  static Future<String?> resolveArtwork({
    required String path,
    String? sidecar,
    required String key,
  }) =>
      _invoke('resolveArtwork', {'path': path, 'sidecar': sidecar, 'key': key});

  /// Drops audio_service's foreground state and wakelock, which its own
  /// stop() can't do (see releasePlaybackService in the Kotlin).
  static Future<void> releasePlaybackService() =>
      _invoke<Object?>('releasePlaybackService');

  /// One progress notification per [job]. While any job is running the
  /// Kotlin holds LibraryTaskService in the foreground (the job survives the
  /// screen going off) and shows the progress there; [done] ends the job and
  /// leaves a summary, or removes it when [content] is empty.
  static Future<void> showProgressNotification({
    required String job,
    required String title,
    required String content,
    required int progress,
    required int total,
    required bool done,
    required bool cancellable,
    String? actionLabel,
    String? actionTarget,
  }) => _invoke<Object?>('showProgressNotification', {
    'job': job,
    'title': title,
    'content': content,
    'progress': progress,
    'total': total,
    'done': done,
    'cancellable': cancellable,
    'actionLabel': actionLabel,
    'actionTarget': actionTarget,
  });

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

  /// MediaCodec decode of [path] to raw int16 LE mono PCM (22050 Hz, up to
  /// 120 s from the middle) in the app cache: (output file, sample rate).
  /// Throws on failure, unlike the other calls (the analyser counts it);
  /// null off Android.
  static Future<({String path, int sampleRate})?> decodePcm(String path) async {
    if (!_android) return null;
    final r = await _channel.invokeMapMethod<String, Object?>('decodePcm', {
      'path': path,
    });
    final out = r?['output_path']?.toString() ?? '';
    final sr = r?['sample_rate'];
    if (out.isEmpty || sr is! int || sr <= 0) {
      throw PlatformException(
        code: 'DECODE_FAILED',
        message: 'decoder returned no PCM for $path',
      );
    }
    return (path: out, sampleRate: sr);
  }

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
