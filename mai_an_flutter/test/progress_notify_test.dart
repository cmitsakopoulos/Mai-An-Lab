// progress_notify.py's tests (tests/test_progress_notify.py and the
// notifier parts of test_notification_scenarios.py), on a recording bridge.
import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:maian_native/maian_native.dart';
import 'package:mai_an_lab/core/notification_router.dart';
import 'package:mai_an_lab/core/progress_notify.dart';

class _Bridge implements NotificationBridge {
  final calls = <Map<String, Object?>>[];
  final _actions = StreamController<NotificationAction>.broadcast(sync: true);

  void tap(String job, String action) =>
      _actions.add((job: job, action: action));

  @override
  Stream<NotificationAction> get actions => _actions.stream;

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
  }) async => calls.add({
    'job': job,
    'title': title,
    'content': content,
    'progress': progress,
    'total': total,
    'done': done,
    'cancellable': cancellable,
    'action_label': ?actionLabel,
    'action_target': ?actionTarget,
  });

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
  }) async => calls.add({
    'alert': true,
    'key': key,
    'title': title,
    'content': content,
    'navigate_to': navigateTo,
    'action_label': actionLabel,
    'action_target': actionTarget,
    'urgent': urgent,
    'icon': icon,
  });

  @override
  Future<void> cancelAlert(String key) async =>
      calls.add({'cancel_alert': key});
}

/// A clock the test moves by hand.
class _Clock extends Stopwatch {
  Duration now = Duration.zero;
  @override
  Duration get elapsed => now;
}

void main() {
  late _Bridge b;
  setUp(() => b = _Bridge());

  ProgressNotifier n([
    String job = 'dsp',
    void Function()? onCancel,
    _Clock? c,
  ]) => ProgressNotifier(
    job,
    'Analysing',
    onCancel: onCancel,
    bridge: b,
    clock: c,
  );

  test('a burst is throttled, but the first and last post', () {
    final p = n();
    for (var i = 1; i <= 500; i++) {
      p.update(i, 500, '$i');
    }
    expect([for (final c in b.calls) c['progress']], [1, 500]);
    expect(
      b.calls.every((c) => c['job'] == 'dsp' && c['done'] == false),
      isTrue,
    );
  });

  test('finish is silent when nothing was shown', () {
    n().finish('done');
    expect(b.calls, isEmpty);
  });

  test('finish after progress posts the summary', () {
    n()
      ..update(1, 2, 'x')
      ..finish('All done');
    expect(b.calls.last['done'], true);
    expect(b.calls.last['content'], 'All done');
  });

  test('a Cancel button only when something listens', () {
    n('download').update(1, 2, 'x');
    n('dsp', () {}).update(1, 2, 'x');
    expect(
      [for (final c in b.calls) (c['job'], c['cancellable'])],
      [('download', false), ('dsp', true)],
    );
  });

  test("finish leaves a newer run's cancel handler", () {
    final old = n('dsp', () {});
    final young = n('dsp', () {});
    old.finish();
    expect(young.cancelRegistered, isTrue);
    young.finish();
    expect(young.cancelRegistered, isFalse);
  });

  test('the Cancel button reaches the newest handler of its job only', () {
    final seen = <String>[];
    final old = n('download', () => seen.add('old'));
    final young = n('download', () => seen.add('young'));
    n('sync', () => seen.add('sync'));
    b.tap('download', 'cancel');
    b.tap('download', 'other');
    expect(seen, ['young']);
    young.finish('Done');
    b.tap('download', 'cancel');
    expect(seen, ['young']);
    old.finish();
  });

  test('a changed text posts once the interval has passed, same percent', () {
    final c = _Clock();
    final p = n('dsp', null, c)..update(1, 1000, 'Track 1 - Starting');
    p.update(2, 1000, 'Track 2'); // within the second: dropped
    c.now = const Duration(milliseconds: 1001);
    p.update(2, 1000, 'Track 2 - 2.5 MB/s');
    c.now = const Duration(milliseconds: 2500);
    p.update(2, 1000, 'Track 2 - 2.5 MB/s'); // nothing changed: dropped
    p.update(3, 1000, 'Track 3 - 3.1 MB/s');
    expect(
      [for (final x in b.calls) x['content']],
      ['Track 1 - Starting', 'Track 2 - 2.5 MB/s', 'Track 3 - 3.1 MB/s'],
    );
  });

  test('stage is unthrottled and indeterminate', () {
    n()
      ..update(1, 3, 'a')
      ..stage('Building the graph');
    expect(b.calls.last, containsPair('total', 0));
    expect(b.calls.last['content'], 'Building the graph');
  });

  test('jobs keep their own notifications', () {
    n('download').update(50, 100, 'Song A.flac');
    n('sync').update(10, 50, 'Offloading batch 1');
    expect(
      {for (final c in b.calls) c['job']: c['content']},
      {'download': 'Song A.flac', 'sync': 'Offloading batch 1'},
    );
  });

  test('a summary with a Retry button; an empty one drops it', () {
    n()
      ..update(1, 2, 'x')
      ..finish('2 failed.', ('Retry', 'downloads:retry'));
    expect(
      (b.calls.last['action_label'], b.calls.last['action_target']),
      ('Retry', 'downloads:retry'),
    );
    n('x')
      ..update(1, 2, 'x')
      ..finish('', ('Retry', 'x'));
    expect(b.calls.last.containsKey('action_label'), isFalse);
  });

  test('post and cancel an alert', () {
    postAlert(
      'scan',
      'T',
      'C',
      navigateTo: 'library',
      action: ('Analyse', 'library:analyse'),
      icon: 'dsp',
      bridge: b,
    );
    cancelAlert('scan', bridge: b);
    expect(b.calls[0]['key'], 'scan');
    expect(b.calls[0]['action_target'], 'library:analyse');
    expect(b.calls[0]['urgent'], false);
    expect(b.calls[1], {'cancel_alert': 'scan'});
  });

  test('format_eta', () {
    expect(formatEta(59.9), '~59s left');
    expect(formatEta(61), '~1m 1s left');
    expect(formatEta(3725), '~1h 2m left');
  });

  test('router: screen and action, held until the screen registers', () {
    final r = NotificationRouter();
    final got = <String>[];
    r.route('sync:transfers'); // a cold-start tap, before the screen exists
    r.register('library', (a) => got.add('library:$a'));
    r.route('library');
    r.route('library:analyse');
    r.route(null);
    r.route('');
    expect(got, ['library:', 'library:analyse']);
    r.register('sync', (a) => got.add('sync:$a'));
    expect(got.last, 'sync:transfers');
    r.unregister('sync');
    r.route('sync');
    expect(got, hasLength(3));
  });
}
