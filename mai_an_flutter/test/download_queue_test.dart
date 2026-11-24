// queue_controller.py's tests (tests/test_download_queue.py), ported, plus
// the workflow paths Flet covers in test_sync_and_batch.py: retries, cancel,
// features stored with the file, the Mac told how each transfer ended, and
// one quiet scan after the queue drains.
import 'dart:async';

import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:maian_native/maian_native.dart';
import 'package:mai_an_lab/core/progress_notify.dart';
import 'package:mai_an_lab/downloads/companion_download.dart';
import 'package:mai_an_lab/downloads/download_queue.dart';

class _Bridge implements NotificationBridge {
  final progress = <List<Object?>>[];
  final finished = <(String, String?)>[];

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
  }) async {
    if (done) {
      finished.add((content, actionTarget));
    } else {
      this.progress.add([progress, total, content]);
    }
  }

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

class _Clock extends Stopwatch {
  _Clock(this.async);
  final FakeAsync async;
  @override
  Duration get elapsed => async.elapsed;
}

class _Host implements DownloadHost {
  final snacks = <String>[];
  final reports = <(String, bool)>[];
  final stored = <(String, Map<String, Object?>)>[];
  var successes = 0;
  var scans = 0;
  var downloads = 0;

  /// What each download call does; default: one file named after the job.
  Future<List<String>> Function(
    DownloadJob job,
    DownloadProgress progress,
    bool Function() isCancelled,
  )?
  onDownload;

  @override
  void snack(String text) => snacks.add(text);
  @override
  Future<String> downloadDir() async => '/music';
  @override
  Future<List<String>> download(
    DownloadJob job,
    String dir, {
    required DownloadProgress progress,
    required bool Function() isCancelled,
  }) {
    downloads++;
    final f = onDownload;
    if (f != null) return f(job, progress, isCancelled);
    return Future.value(['$dir/${job.title}.flac']);
  }

  @override
  Future<void> Function(String path, Map<String, Object?> dsp)?
  get persistFeatures =>
      (path, dsp) async => stored.add((path, dsp));
  @override
  void downloadSucceeded() => successes++;
  @override
  void quietScan() => scans++;
  @override
  void transferFinished(String transferId, {required bool ok}) =>
      reports.add((transferId, ok));
}

Map<String, Object?> _item(int i, {String source = 'qobuz'}) => {
  'id': '$i',
  'media_type': 'track',
  'source': source,
  'ui_title': 'Track $i',
  'ui_subtitle': 'Artist',
  'image': '',
};

Map<String, Object?> _transfer(String id, {Object? dsp}) => {
  'source': 'companion',
  'media_type': 'track',
  'id': id,
  'companion_transfer_id': id,
  'url': 'http://mac/api/v1/tracks/stream?path=%2Fm%2F$id.flac',
  'ui_title': 'T$id',
  'ui_subtitle': 'A',
  'dsp_features': dsp,
  'quality_label': 'FLAC',
};

DownloadQueue _held([_Host? host]) =>
    DownloadQueue(host ?? _Host(), bridge: _Bridge())..holdWorker = true;

void main() {
  group('chip desync regression', () {
    test('rapid enqueues all survive', () {
      final q = _held()
        ..enqueue(_item(1))
        ..enqueue(_item(2))
        ..enqueue(_item(3));
      expect(q.pending.length, 3);
      expect(q.activeCount, 3);
      expect(q.pending.map((j) => j.title), ['Track 1', 'Track 2', 'Track 3']);
    });

    test('every job gets a distinct id', () {
      final jobs = _held().enqueueMany([for (var i = 0; i < 5; i++) _item(i)]);
      expect(jobs.map((j) => j.id).toSet().length, 5);
    });
  });

  group('job model', () {
    test('denormalises display fields', () {
      final job = _held().enqueue(
        _item(1, source: 'deezer'),
        qualityTier: 'hires',
      );
      expect(job.state, JobState.queued);
      expect(job.source, 'deezer');
      expect(job.title, 'Track 1');
      expect(job.qualityLabel, 'HIRES');
      // Deezer has no 24-bit tier; hires resolves to its FLAC value.
      expect(job.metadata['quality'], 2);
    });

    test('hires quality differs per source', () {
      expect(DownloadQueue.qualityInt('qobuz', 'hires'), 4);
      expect(DownloadQueue.qualityInt('deezer', 'hires'), 2);
      expect(DownloadQueue.qualityInt('deezer', 'mp3'), 1);
    });

    test('a Mac transfer shows its real quality', () {
      final job = _held().enqueueMany([
        {..._transfer('a'), 'quality_label': 'AAC 320'},
      ], qualityTier: 'original').single;
      expect(job.qualityLabel, 'AAC 320');
      expect(job.url, startsWith('http://mac/'));
      expect(job.transferId, 'a');
    });

    test('enqueue_many notifies once for the batch', () {
      final q = _held();
      var calls = 0;
      q.addListener(() => calls++);
      q.enqueueMany([for (var i = 0; i < 40; i++) _item(i)]);
      expect(calls, 1);
    });
  });

  group('removal and retry', () {
    test('remove a pending job', () {
      final q = _held();
      final jobs = q.enqueueMany([for (var i = 0; i < 3; i++) _item(i)]);
      q.remove(jobs[1].id);
      expect(q.pending.map((j) => j.id), [jobs[0].id, jobs[2].id]);
      expect(jobs[1].state, JobState.cancelled);
      expect(jobs[1].status, 'Removed');
    });

    test('removing the job in flight requests cancellation', () {
      final host = _Host();
      final q = _held(host);
      final job = q.enqueue(_item(1));
      q.currentJob = q.pending.single;
      q.remove(job.id);
      expect(q.cancelRequested, isTrue);
      expect(host.snacks, ['Cancellation requested…']);
    });

    test('retry re-queues a failed job', () {
      final q = _held();
      final job = q.enqueue(_item(1));
      q.remove(job.id); // pending → history (cancelled)
      job.state = JobState.failed;
      final fresh = q.retry(job.id);
      expect(fresh, isNotNull);
      expect(fresh!.state, JobState.queued);
      expect(fresh.percent, 0.0);
      expect(fresh.id, isNot(job.id));
      expect(q.pending.length, 1);
    });

    test('retry refuses an active job', () {
      final q = _held();
      expect(q.retry(q.enqueue(_item(1)).id), isNull);
    });

    test('clear cancels everything and records it', () {
      final host = _Host();
      final q = _held(host);
      final jobs = q.enqueueMany([for (var i = 0; i < 3; i++) _item(i)]);
      expect(q.clear(), 3);
      expect(q.pending, isEmpty);
      expect(q.cancelRequested, isTrue);
      expect(jobs.every((j) => j.state == JobState.cancelled), isTrue);
      // Cleared jobs stay visible as outcomes.
      expect(q.history.length, 3);
      expect(host.snacks, ['Queue cleared (3 items).']);
    });

    test('history is bounded', () {
      final q = _held();
      for (var i = 0; i < historyLimit + 15; i++) {
        q.remove(q.enqueue(_item(i)).id);
      }
      expect(q.history.length, historyLimit);
    });

    test('clear_finished leaves active jobs alone', () {
      final q = _held();
      q.remove(q.enqueue(_item(1)).id);
      q.enqueue(_item(2));
      q.clearFinished();
      expect(q.history, isEmpty);
      expect(q.activeCount, 1);
    });

    test('a removed Mac transfer is reported as not arrived', () {
      final host = _Host();
      final q = _held(host);
      q.remove(q.enqueueMany([_transfer('t1')]).single.id);
      expect(host.reports, [('t1', false)]);
    });
  });

  group('observers', () {
    test('listeners hear state changes', () {
      final q = _held();
      var calls = 0;
      q.addListener(() => calls++);
      final job = q.enqueue(_item(1));
      q.set(job, status: 'Downloading', percent: 50);
      q.remove(job.id);
      expect(calls, greaterThanOrEqualTo(3));
    });

    test('a failing listener does not break the queue', () {
      final q = _held();
      q.addListener(() => throw StateError('boom'));
      var ok = 0;
      q.addListener(() => ok++);
      q.enqueue(_item(1));
      expect(q.pending.length, 1);
      expect(ok, 1);
    });

    test('active first, then pending, then newest finished', () {
      final q = _held();
      final old = q.enqueue(_item(0));
      q.remove(old.id);
      old.state = JobState.done;
      final newer = q.enqueue(_item(1));
      q.remove(newer.id);
      newer.state = JobState.done;
      q.enqueue(_item(2));
      q.currentJob = q.pending.first;
      q.enqueue(_item(3));
      // currentJob is still in pending here; the worker pops it first.
      expect(q.jobs.first.title, 'Track 2');
      expect(q.jobs.map((j) => j.title).skip(2), [
        'Track 3',
        'Track 1',
        'Track 0',
      ]);
    });
  });

  group('download notification', () {
    Future<_Bridge> runBatch(List<JobState> outcomes) async {
      final bridge = _Bridge();
      final q = DownloadQueue(_Host(), bridge: bridge);
      final states = outcomes.iterator;
      q.workflowOverride = (job) async {
        q.set(job, percent: 50);
        states.moveNext();
        q.set(job, state: states.current, percent: 100);
      };
      q.enqueueMany([for (var i = 0; i < outcomes.length; i++) _item(i)]);
      await q.worker;
      return bridge;
    }

    test('batch progress counts finished jobs', () async {
      final b = await runBatch([JobState.done, JobState.done]);
      // The second job at 50 %: one finished (100) + 50, of 2 × 100. The
      // notifier throttles, so look at what it was asked to show.
      expect(
        b.progress.any((p) => p[1] == 200 && '${p[2]}'.contains('of 2')),
        isTrue,
      );
    });

    test('summary reports done and failed', () async {
      final b = await runBatch([JobState.done, JobState.failed, JobState.done]);
      expect(b.finished.last.$1, '2 downloads finished · 1 failed.');
      // A failed non-Mac job can be retried from the card.
      expect(b.finished.last.$2, 'downloads:retry');
    });

    test('all cancelled removes the notification', () async {
      final b = await runBatch([JobState.cancelled]);
      expect(b.finished.last.$1, '');
    });

    test('a worker crash still finishes', () async {
      final bridge = _Bridge();
      final q = DownloadQueue(_Host(), bridge: bridge);
      q.workflowOverride = (job) async {
        q.set(job, percent: 10);
        throw StateError('boom');
      };
      q.enqueue(_item(1));
      await expectLater(q.worker, throwsStateError);
      expect(bridge.finished.length, 1);
      expect(q.isProcessing, isFalse);
    });
  });

  group('workflow', () {
    test('a Mac transfer lands: features, ack, haptic, one quiet scan', () {
      fakeAsync((async) {
        final host = _Host();
        final q = DownloadQueue(host, bridge: _Bridge());
        host.onDownload = (job, progress, _) async {
          progress('transferring', 40, '0.4/1.0 MB');
          // The Mac's name arrives decomposed (NFD).
          return ['/music/Ά.flac'.replaceAll('Ά', 'Ά')];
        };
        q.enqueueMany([
          _transfer('t1', dsp: {'bpm': 120.0, 'timbre_b64': ''}),
          _transfer('t2'),
        ], qualityTier: 'original');
        async.flushMicrotasks();
        async.elapse(const Duration(seconds: 1));
        expect(q.isIdle, isTrue);
        expect(q.history.map((j) => j.state), [JobState.done, JobState.done]);
        expect(q.history.first.status, 'Finished');
        expect(q.history.first.detail, 'Download complete');
        expect(host.reports, [('t1', true), ('t2', true)]);
        expect(host.stored.single.$1, '/music/Ά.flac'); // stored under NFC
        expect(host.successes, 2);
        expect(host.scans, 0);
        async.elapse(const Duration(seconds: 1));
        expect(host.scans, 1);
      });
    });

    test('three failures end the job, with Python\'s text', () {
      fakeAsync((async) {
        final host = _Host();
        final bridge = _Bridge();
        final q = DownloadQueue(host, bridge: bridge);
        final details = <String>[];
        q.addListener(() {
          final j = q.currentJob;
          if (j != null && j.status == 'Retrying') details.add(j.detail);
        });
        host.onDownload = (_, _, _) async => throw const DownloadError(
          'FileNotFoundError',
          'Track is no longer queued on the Mac',
        );
        q.enqueueMany([_transfer('t1')]);
        async.elapse(const Duration(seconds: 20));
        expect(host.downloads, 3);
        expect(details.toSet(), {
          'Track is no longer queued on the Mac. Retrying in 5s…',
          'Track is no longer queued on the Mac. Retrying in 10s…',
        });
        final job = q.history.single;
        expect(job.state, JobState.failed);
        expect(
          job.detail,
          'Failed after 3 attempts: Track is no longer queued on the Mac',
        );
        expect(host.reports, [('t1', false)]);
        expect(host.scans, 0);
        // Mac transfers are re-offered by the manager, not retried here.
        expect(bridge.finished.single, ('1 failed.', null));
      });
    });

    test('cancel during the retry wait', () {
      fakeAsync((async) {
        final host = _Host();
        final q = DownloadQueue(host, bridge: _Bridge());
        host.onDownload = (_, _, _) async => throw StateError('offline');
        q.enqueue(_item(1));
        async.elapse(const Duration(seconds: 2));
        expect(q.currentJob?.status, 'Retrying');
        q.cancelCurrent();
        async.elapse(const Duration(seconds: 1));
        final job = q.history.single;
        expect(job.state, JobState.cancelled);
        expect(job.detail, 'Aborted by user');
        expect(host.downloads, 1);
      });
    });

    test('progress is throttled to four updates a second', () {
      fakeAsync((async) {
        final host = _Host();
        final q = DownloadQueue(host, bridge: _Bridge(), clock: _Clock(async));
        final seen = <double>[];
        host.onDownload = (job, progress, _) async {
          for (var i = 1; i <= 10; i++) {
            progress('transferring', i, '');
          }
          progress('finished', 100, 'Transfer complete.');
          return ['/music/x.flac'];
        };
        q.addListener(() {
          final j = q.currentJob;
          if (j != null && j.status == 'Transferring') seen.add(j.percent);
        });
        q.enqueue(_item(1));
        async.elapse(const Duration(seconds: 1));
        expect(seen, [1.0]);
      });
    });
  });
}
