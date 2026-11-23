import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/player/queue_entry.dart';
import 'package:mai_an_lab/player/queue_store.dart';

final queue = [
  for (var i = 0; i < 6; i++) QueueEntry({'path': '/m/$i', 'duration': 100.0 + i}),
];

ResumePoint choose(
  List<QueueEntry> q,
  int index,
  double pos,
  double dur,
  Map<String, Object?>? pstate,
  NativeResume? native, {
  required double posMtime,
}) => QueueStore.chooseResume(q, index, pos, dur, pstate, native, posMtime);

void main() {
  // tests/test_resume_restore.py::ChooseResumeTests
  group('chooseResume', () {
    const pstate = {
      'path': '/m/1',
      'current_index': 1,
      'position': 42.0,
      'duration': 101.0,
    };

    test('a newer native record wins over a stale position file', () {
      final r = choose(queue, 0, 0, 0, pstate,
          (path: '/m/4', hint: 4, position: 3.0, at: 2000.0), posMtime: 1000);
      expect((r.index, r.position, r.duration), (4, 3.0, 104.0));
    });

    test('an older native record loses', () {
      final r = choose(queue, 0, 0, 0, pstate,
          (path: '/m/4', hint: 4, position: 3.0, at: 500.0), posMtime: 1000);
      expect((r.index, r.position, r.duration), (1, 42.0, 101.0));
    });

    test('a native track no longer queued is ignored', () {
      final r = choose(queue, 2, 5, 102, null,
          (path: '/gone', hint: 9, position: 3.0, at: 2000.0), posMtime: 0);
      expect((r.index, r.position, r.duration), (2, 5.0, 102.0));
    });

    test('the index hint disambiguates a track queued twice', () {
      final q = [...queue, QueueEntry({'path': '/m/1', 'duration': 101.0})];
      final r = choose(q, 0, 0, 0, null,
          (path: '/m/1', hint: 6, position: 0.0, at: 2000.0), posMtime: 0);
      expect(r.index, 6);
    });

    test('without records the queue file index stands', () {
      final r = choose(queue, 3, 7, 103, null, null, posMtime: 0);
      expect((r.index, r.position, r.duration), (3, 7.0, 103.0));
    });
  });

  group('files', () {
    late Directory dir;
    late QueueStore store;
    setUp(() {
      dir = Directory.systemTemp.createTempSync('mai_an_store');
      store = QueueStore(dir.path);
    });
    tearDown(() => dir.deleteSync(recursive: true));

    Map<String, dynamic> read(String name) =>
        jsonDecode(File('${dir.path}/$name').readAsStringSync());

    // QueueSnapshotTests
    test('an empty queue before any restore keeps the snapshot', () async {
      File(store.queueStatePath).writeAsStringSync(
        '{"queue": [{"path": "/m/1"}], "current_index": 0}',
      );
      await store.saveQueueState(queue: [], index: 0, position: 0, duration: 0);
      expect(File(store.queueStatePath).existsSync(), isTrue);
    });

    test('clearing a queue this process held removes both files', () async {
      await store.saveQueueState(queue: queue, index: 1, position: 2, duration: 3);
      expect(store.held, isTrue);
      await store.saveQueueState(queue: [], index: 0, position: 0, duration: 0);
      expect(File(store.queueStatePath).existsSync(), isFalse);
      expect(File(store.positionPath).existsSync(), isFalse);
    });

    test('writes the Flet JSON shape, and the position file with it', () async {
      await store.saveQueueState(
        queue: queue,
        index: 2,
        position: 12.5,
        duration: 102,
        autoplayHeld: [
          {'path': '/held'},
        ],
      );
      final s = read(QueueStore.queueStateName);
      expect(s.keys, containsAll(['queue', 'current_index', 'position', 'duration', 'autoplay_held']));
      expect(s['queue'][2], {'path': '/m/2', 'duration': 102.0});
      expect(read(QueueStore.positionName), {
        'path': '/m/2',
        'current_index': 2,
        'position': 12.5,
        'duration': 102.0,
      });
    });

    // test_queue_modes.py::test_position_file_wins_over_older_queue_snapshot
    test('the position file wins over an older queue snapshot', () async {
      await store.saveQueueState(queue: queue, index: 0, position: 0, duration: 0);
      await store.savePosition(queue: queue, index: 2, position: 42, duration: 102);
      expect(read(QueueStore.queueStateName)['current_index'], 0);
      final (saved, at) = (await store.loadResume())!;
      expect(saved.queue, hasLength(6));
      expect((at.index, at.position), (2, 42.0));
    });

    test('the native record, written later, wins', () async {
      await store.saveQueueState(queue: queue, index: 0, position: 9, duration: 0);
      final later = DateTime.now().millisecondsSinceEpoch / 1000 + 60;
      File(store.nativeResumePath).writeAsStringSync(jsonEncode({
        'src': pathToUri('/m/5'),
        'index': 5,
        'position_ms': 1500,
        'playing': true,
        'at': later,
      }));
      final (_, at) = (await store.loadResume())!;
      expect((at.index, at.position, at.duration), (5, 1.5, 105.0));
    });

    // test_queue_modes.py::test_sigkill_atomic_queue_state_durability
    test('a torn write leaves the previous file intact', () async {
      await store.saveQueueState(queue: queue, index: 0, position: 0, duration: 0);
      // A killed process leaves only the tmp file behind.
      File('${store.queueStatePath}.tmp').writeAsStringSync('{"queue": [{"pa');
      final saved = await store.readQueueState();
      expect(saved!.queue.first.path, '/m/0');
    });

    test('a Flet-written snapshot loads, unknown keys and all', () async {
      File(store.queueStatePath).writeAsStringSync(jsonEncode({
        'queue': [
          {'path': '/m/a', 'track_title': 'A', 'artist_name': 'X', 'album_title': 'Y', '_queued': true, 'image_url': ''},
        ],
        'current_index': 0,
        'position': 3.25,
        'duration': 200,
        'autoplay_held': [{'path': '/h'}],
      }));
      final s = (await store.readQueueState())!;
      expect(s.queue.single.queued, isTrue);
      expect(s.queue.single.data['image_url'], '');
      expect(s.position, 3.25);
      expect(s.duration, 200.0);
      expect(s.autoplayHeld, [{'path': '/h'}]);
    });

    test('partitions live in .cache and empty deletes them', () async {
      await store.savePartition('queue_regular.json', queue: queue, index: 3, position: 1, duration: 2);
      expect(File('${dir.path}/.cache/queue_regular.json').existsSync(), isTrue);
      final p = (await store.loadPartition('queue_regular.json'))!;
      expect((p.queue.length, p.index), (6, 3));
      await store.savePartition('queue_regular.json', queue: [], index: 0, position: 0, duration: 0);
      expect(await store.loadPartition('queue_regular.json'), isNull);
    });

    test('prefs merge into what is on disk', () async {
      final a = Prefs(dir.path);
      final b = Prefs(dir.path);
      await a.load();
      await b.load();
      await a.set('library_path', '/music');
      await b.set('is_shuffle', true); // b's copy never saw library_path
      await b.flush();
      expect(read('flet_prefs.json'), {'library_path': '/music', 'is_shuffle': true});
    });
  });
}
