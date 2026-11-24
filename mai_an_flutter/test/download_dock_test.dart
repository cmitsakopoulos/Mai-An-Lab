// download_dock.py in the shell: the pill while jobs are active, the sheet
// with every job, and the `downloads` notification target.
import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:maian_native/maian_native.dart';
import 'package:mai_an_lab/app_shell.dart';
import 'package:mai_an_lab/core/haptics.dart';
import 'package:mai_an_lab/core/notification_router.dart';
import 'package:mai_an_lab/core/progress_notify.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/downloads/companion_download.dart';
import 'package:mai_an_lab/downloads/download_queue.dart';
import 'package:mai_an_lab/library/browse_controller.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/search/landing_controller.dart';
import 'package:mai_an_lab/ui/tokens.dart';

class _Bridge implements NotificationBridge {
  @override
  Stream<NotificationAction> get actions => const Stream.empty();
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
  }) async {}
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
  }) async {}
  @override
  Future<void> cancelAlert(String key) async {}
}

class _Host implements DownloadHost {
  @override
  void snack(String text) {}
  @override
  Future<String> downloadDir() async => '/music';
  @override
  Future<List<String>> download(
    DownloadJob job,
    String dir, {
    required DownloadProgress progress,
    required bool Function() isCancelled,
  }) async => ['$dir/x.flac'];
  @override
  Future<void> Function(String, Map<String, Object?>)? get persistFeatures =>
      null;
  @override
  void downloadSucceeded() {}
  @override
  void quietScan() {}
  @override
  void transferFinished(String transferId, {required bool ok}) {}
}

Map<String, Object?> _transfer(String id) => {
  'source': 'companion',
  'id': id,
  'companion_transfer_id': id,
  'url': 'http://mac/api/v1/tracks/stream?path=%2Fm%2F$id.flac',
  'ui_title': 'Song $id',
  'ui_subtitle': 'Artist',
  'quality_label': 'FLAC',
};

void main() {
  late Directory dir;
  setUp(() => dir = Directory.systemTemp.createTempSync('mai_an_dock'));
  tearDown(() => dir.deleteSync(recursive: true));

  testWidgets('pill, sheet and the downloads notification target', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(360 * 3, 760 * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);

    final path = '${dir.path}/library.db';
    final lib = LibraryController(
      () => LibraryRepository.open(path, writable: true),
    );
    await tester.runAsync(lib.load);
    final queue = DownloadQueue(_Host(), bridge: _Bridge());
    final gates = <Completer<void>>[];
    queue.workflowOverride = (job) async {
      final g = Completer<void>();
      gates.add(g);
      queue.set(job, status: 'Transferring', percent: 40, detail: '0.4/1.0 MB');
      await g.future;
      queue.set(job, state: JobState.done, status: 'Finished', percent: 100);
    };
    final router = NotificationRouter();
    await tester.pumpWidget(
      MaterialApp(
        theme: Tokens.theme(),
        home: AppShell(
          library: lib,
          browse: BrowseController(lib, views: const [LibraryView.tracks]),
          landing: LandingController(lib),
          player: ValueNotifier<PlayerController?>(null),
          haptics: Haptics(const HapticsConfig(enabled: false)),
          router: router,
          downloads: queue,
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('Waiting to start…'), findsNothing);

    queue.enqueueMany([_transfer('1'), _transfer('2')]);
    await tester.pump();
    expect(find.text('2 queued'), findsOneWidget);
    expect(find.text('Waiting to start…'), findsOneWidget);

    // The worker starts on the next turn (a zero timer).
    await tester.pump(const Duration(milliseconds: 1));
    await tester.pump();
    expect(find.text('2 downloading'), findsOneWidget);
    expect(find.text('Transferring · Song 1'), findsOneWidget);

    await tester.tap(find.text('2 downloading'));
    await tester.pumpAndSettle();
    expect(find.text('Downloads'), findsOneWidget);
    expect(find.text('2 in progress'), findsOneWidget);
    expect(find.text('COMPANION'), findsNWidgets(2));
    expect(find.text('Transferring · 0.4/1.0 MB'), findsOneWidget);
    expect(find.text('Queued'), findsOneWidget);

    gates.single.complete();
    await tester.pump(const Duration(milliseconds: 500));
    gates.last.complete();
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pumpAndSettle();
    expect(find.text('2 finished'), findsOneWidget);
    expect(queue.isIdle, isTrue);

    // Dismissing a finished job.
    await tester.tap(find.byTooltip('Dismiss').first);
    await tester.pumpAndSettle();
    expect(find.text('1 finished'), findsOneWidget);
    Navigator.of(tester.element(find.text('Downloads'))).pop();
    await tester.pumpAndSettle();
    // Idle: the pill is gone.
    expect(find.textContaining('downloading'), findsNothing);

    // The summary card's Retry: Mac transfers are never retried from here.
    router.route('downloads:retry');
    await tester.pumpAndSettle();
    expect(find.text('Downloads'), findsOneWidget);
    expect(
      find.text(
        'Nothing to retry. The failed downloads are no longer in the list.',
      ),
      findsOneWidget,
    );
    await tester.runAsync(() => lib.repository!.close());
  });
}
