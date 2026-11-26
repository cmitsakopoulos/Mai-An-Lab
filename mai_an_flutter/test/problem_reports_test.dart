import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/sync/problem_reports.dart';

/// The phone's half of problem reports: what it remembers until the Mac is
/// reachable. Sending is covered against the real Mac in
/// mac_companion_interop_test.dart.
void main() {
  late Directory tmp;
  setUp(() => tmp = Directory.systemTemp.createTempSync('reports_'));
  tearDown(() => tmp.deleteSync(recursive: true));

  test('the log keeps the newest lines, stamped', () {
    final r = ProblemReporter(maxLog: 3);
    for (var i = 0; i < 5; i++) {
      r.log('line $i');
    }
    expect(r.recentLog, hasLength(3));
    expect(r.recentLog.first, matches(RegExp(r'^\d\d:\d\d:\d\d line 2$')));
  });

  test('the same error counts up; reports survive a restart', () async {
    final f = File('${tmp.path}/pending.json');
    final r = ProblemReporter(file: f)..log('before the crash');
    final stack = StackTrace.current;
    r.recordError(StateError('boom'), stack);
    r.recordError(StateError('boom'), stack);
    expect(r.pending, hasLength(1));
    expect(r.pending.single['count'], 2);
    expect(r.pending.single['logs'], [endsWith('before the crash')]);
    expect(await r.report('It froze'), isFalse); // no Mac
    await Future<void>.delayed(const Duration(milliseconds: 50));
    final again = ProblemReporter(file: f);
    expect(again.pending, hasLength(2));
    expect(again.pendingErrors, 1);
    expect(again.pending.last['text'], 'It froze');
  });

  test('install: debugPrint and Flutter errors are captured', () {
    final r = ProblemReporter();
    final print0 = debugPrint;
    final flutter0 = FlutterError.onError;
    addTearDown(() {
      debugPrint = print0;
      FlutterError.onError = flutter0;
    });
    FlutterError.onError = (_) {};
    r.install();
    debugPrint('hello from the app');
    expect(r.recentLog.last, endsWith('hello from the app'));
    FlutterError.reportError(
      FlutterErrorDetails(exception: Exception('layout broke')),
    );
    FlutterError.reportError(
      FlutterErrorDetails(
        exception: Exception('offline image'),
        library: 'image resource service',
      ),
    );
    expect(r.pending, hasLength(1));
    expect(r.pending.single['error'], contains('layout broke'));
  });
}
