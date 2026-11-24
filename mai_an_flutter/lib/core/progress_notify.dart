import 'package:flutter/foundation.dart';
import 'package:maian_native/maian_native.dart';

/// progress_notify.py: system notifications for long jobs (sync, downloads,
/// later analysis and metadata). Jobs can run for many minutes with the app
/// in the background, where snackbars are invisible. Off Android every call
/// does nothing ([MaianNative] is a no-op there), so callers never guard.

/// What the notifications go through; tests swap in a recorder.
abstract class NotificationBridge {
  Future<void> showProgress({
    required String job,
    required String title,
    required String content,
    required int progress,
    required int total,
    required bool done,
    required bool cancellable,
    String? actionLabel,
    String? actionTarget,
  });

  Future<void> showAlert({
    required String key,
    required String title,
    required String content,
    String navigateTo = '',
    String? actionLabel,
    String? actionTarget,
    bool urgent = false,
    String icon = '',
  });

  Future<void> cancelAlert(String key);

  /// Taps on notification buttons (`cancel`) and notifications (`navigate`).
  Stream<NotificationAction> get actions;

  static NotificationBridge instance = const _NativeBridge();
}

class _NativeBridge implements NotificationBridge {
  const _NativeBridge();

  @override
  Future<void> showProgress({
    required String job,
    required String title,
    required String content,
    required int progress,
    required int total,
    required bool done,
    required bool cancellable,
    String? actionLabel,
    String? actionTarget,
  }) => MaianNative.showProgressNotification(
    job: job,
    title: title,
    content: content,
    progress: progress,
    total: total,
    done: done,
    cancellable: cancellable,
    actionLabel: actionLabel,
    actionTarget: actionTarget,
  );

  @override
  Future<void> showAlert({
    required String key,
    required String title,
    required String content,
    String navigateTo = '',
    String? actionLabel,
    String? actionTarget,
    bool urgent = false,
    String icon = '',
  }) => MaianNative.showAlert(
    key: key,
    icon: icon,
    title: title,
    content: content,
    navigateTo: navigateTo,
    actionLabel: actionLabel ?? '',
    actionTarget: actionTarget ?? '',
    urgent: urgent,
  );

  @override
  Future<void> cancelAlert(String key) => MaianNative.cancelAlert(key);

  @override
  Stream<NotificationAction> get actions => MaianNative.notificationActions;
}

/// A notification button: (label, navigate target), e.g.
/// ("Retry", "downloads:retry").
typedef NotifyAction = (String label, String target);

const _minInterval = Duration(seconds: 1);

/// `ProgressNotifier`: one job's notification, throttled to about one
/// update a second (Android silently drops faster updates), with an
/// optional Cancel button and a summary when the job ends.
class ProgressNotifier {
  ProgressNotifier(
    this.job,
    this.title, {
    void Function()? onCancel,
    NotificationBridge? bridge,
    Stopwatch? clock,
  }) : _bridge = bridge ?? NotificationBridge.instance,
       _clock = clock ?? (Stopwatch()..start()) {
    setCancelHandler(onCancel);
  }

  final String job;
  final String title;
  final NotificationBridge _bridge;
  final Stopwatch _clock;
  void Function()? _onCancel;

  /// The cancel handler per job, as the extension keeps them: a newer run's
  /// notifier replaces an older one's, and finishing the older one leaves
  /// the newer one registered.
  static final _registry = <String, ProgressNotifier>{};
  static final _subscribed = <NotificationBridge>{};

  Duration? _lastAt;
  int _lastPct = -1;
  String _lastContent = '';

  /// Only a notifier that showed something posts a summary, so a run with
  /// nothing to do stays silent.
  bool posted = false;

  void setCancelHandler(void Function()? fn) {
    _onCancel = fn;
    if (fn == null) return;
    _registry[job] = this;
    if (_subscribed.add(_bridge)) {
      _bridge.actions.listen((a) {
        if (a.action != 'cancel') return;
        try {
          _registry[a.job]?._onCancel?.call();
        } catch (e) {
          debugPrint('on_cancel callback for ${a.job} failed: $e');
        }
      });
    }
  }

  /// Whether this notifier's Cancel button reaches it (tests).
  @visibleForTesting
  bool get cancelRegistered => identical(_registry[job], this);

  void _post(
    String content,
    int progress,
    int total, {
    required bool done,
    NotifyAction? action,
  }) {
    if (!done) posted = true;
    // Fire and forget: a notification is never worth failing the job over.
    _bridge
        .showProgress(
          job: job,
          title: title,
          content: content,
          progress: progress,
          total: total,
          done: done,
          // A Cancel button nothing listens to would do nothing on tap.
          cancellable: _onCancel != null,
          actionLabel: action?.$1,
          actionTarget: action?.$2,
        )
        .catchError(
          (Object e) => debugPrint('progress notification ($job) failed: $e'),
        );
  }

  /// Progress; throttled, but the first and the last update always go, and
  /// a changed text or percentage goes once the interval has passed.
  /// [total] <= 0 shows an indeterminate bar.
  void update(int done, int total, String content) {
    final now = _clock.elapsed;
    final pct = total > 0 ? done * 100 ~/ total : -1;
    final first = _lastAt == null;
    final last = total > 0 && done >= total;
    if (!(first || last)) {
      if (now - _lastAt! < _minInterval) return;
      if (pct == _lastPct && content == _lastContent) return;
    }
    _lastAt = now;
    _lastPct = pct;
    _lastContent = content;
    _post(content, done, total, done: false);
  }

  /// An indeterminate bar with a new message, unthrottled.
  void stage(String content) {
    _lastAt = _clock.elapsed;
    _lastPct = -1;
    _lastContent = content;
    _post(content, 0, 0, done: false);
  }

  /// Replace the bar with a short summary, or remove it when [content] is
  /// empty. Nothing happens if nothing was ever shown. [action] adds a
  /// button that opens the app at its target, so it still works after this
  /// run has ended.
  void finish([String content = '', NotifyAction? action]) {
    if (posted) {
      _post(content, 0, 0, done: true, action: content.isEmpty ? null : action);
    }
    if (identical(_registry[job], this)) _registry.remove(job);
  }
}

/// `post_alert`: a one-shot card. [key] names it, so a newer card replaces
/// an older one.
void postAlert(
  String key,
  String title,
  String content, {
  String navigateTo = '',
  NotifyAction? action,
  bool urgent = false,
  String icon = '',
  NotificationBridge? bridge,
}) {
  (bridge ?? NotificationBridge.instance)
      .showAlert(
        key: key,
        title: title,
        content: content,
        navigateTo: navigateTo,
        actionLabel: action?.$1 ?? '',
        actionTarget: action?.$2 ?? '',
        urgent: urgent,
        icon: icon,
      )
      .catchError((Object e) => debugPrint('alert ($key) failed: $e'));
}

/// `cancel_alert`.
void cancelAlert(String key, {NotificationBridge? bridge}) {
  (bridge ?? NotificationBridge.instance)
      .cancelAlert(key)
      .catchError((Object e) => debugPrint('cancel alert ($key) failed: $e'));
}

/// `format_eta`.
String formatEta(double seconds) {
  final s = seconds.truncate();
  if (s < 60) return '~${s}s left';
  if (s < 3600) return '~${s ~/ 60}m ${s % 60}s left';
  return '~${s ~/ 3600}h ${(s % 3600) ~/ 60}m left';
}
