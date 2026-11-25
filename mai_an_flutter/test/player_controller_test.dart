import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/player/playback_backend.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/player/queue_entry.dart';
import 'package:mai_an_lab/player/queue_store.dart';

import 'fake_backend.dart';

Track track(int i, {String dir = '/music'}) => Track(
  id: i,
  path: '$dir/song$i.mp3',
  title: 'Song $i',
  artist: 'Artist $i',
  album: 'Album $i',
  duration: 100.0 + i,
  format: 'mp3',
);

void main() {
  late Directory dir;
  late FakeBackend backend;
  late PlayerController c;
  final library = [for (var i = 1; i <= 3; i++) track(i)];

  PlayerController make() => PlayerController(
    backend: backend,
    store: QueueStore(dir.path),
    prefs: Prefs(dir.path),
    libraryTracks: () async => library,
  );

  setUp(() async {
    dir = Directory.systemTemp.createTempSync('mai_an_player');
    backend = FakeBackend();
    c = make();
    await c.init();
  });

  tearDown(() async {
    await c.flush();
    c.dispose();
    dir.deleteSync(recursive: true);
  });

  List<String> qPaths() => [for (final t in c.queue.entries) t.path];
  List<String> nativePaths() => [for (final t in backend.rows) t.path];

  Future<void> expectInStep() async {
    await c.settled;
    expect(nativePaths(), qPaths());
    expect(backend.currentIndex, c.queue.currentIndex);
  }

  // tests/test_queue_modes.py::test_regular_sequential_play
  test('a tap plays within the full list, and the queue is saved', () async {
    await c.playTrack(library[1], context: library);
    await expectInStep();
    expect(c.queue.length, 3);
    expect(c.queue.currentIndex, 1);
    expect(c.nowPlaying!.path, '/music/song2.mp3');
    expect(backend.playing, isTrue);
    await c.saveQueue();
    final s = jsonDecode(
      File('${dir.path}/queue_state.json').readAsStringSync(),
    );
    expect((s['queue'] as List).length, 3);
    expect(s['current_index'], 1);
  });

  test('a tap on a queued track in the library just jumps to it', () async {
    await c.playTrack(library[0], context: library);
    await c.settled;
    backend.ops.clear();
    await c.playTrack(library[2], context: [library[2]], generalView: true);
    await expectInStep();
    expect(backend.ops, ['skip']);
    expect(c.queue.currentIndex, 2);
  });

  test('a track missing from the view is reported, not played', () async {
    final said = <String>[];
    c.messages.listen(said.add);
    await c.playTrack(track(9), context: library);
    await Future<void>.delayed(Duration.zero);
    expect(said, ['Track not found in current view.']);
    expect(c.queue.isEmpty, isTrue);
  });

  // test_shuffle_from_entire_library
  test('shuffle plays from the entire library', () async {
    await c.toggleShuffle();
    await c.playTrack(library[2], context: [library[2]]);
    await expectInStep();
    expect(c.queue.length, 3);
    expect(c.nowPlaying!.path, '/music/song3.mp3');
    expect(backend.shuffleOn, isTrue);
  });

  // test_aggressive_mode_switching_and_surviving
  test(
    'shuffle on then off returns the regular queue at the same track',
    () async {
      final album = [track(7, dir: '/a'), library[0], track(8, dir: '/a')];
      await c.playTrack(library[0], context: album);
      await c.settled;
      await c.toggleShuffle();
      await expectInStep();
      expect(c.shuffle, isTrue);
      expect(qPaths(), library.map((t) => t.path)); // the whole library
      await c.flush();
      expect(
        File('${dir.path}/.cache/queue_regular.json').existsSync(),
        isTrue,
      );
      await c.toggleShuffle();
      await expectInStep();
      expect(c.shuffle, isFalse);
      expect(qPaths(), album.map((t) => t.path));
      expect(c.nowPlaying!.path, library[0].path);
      await c.flush();
      final prefs = jsonDecode(
        File('${dir.path}/flet_prefs.json').readAsStringSync(),
      );
      expect(prefs['is_shuffle'], isFalse);
    },
  );

  test('shuffle keeps the playing track playing where it was', () async {
    await c.playTrack(library[1], context: library);
    await c.settled;
    c.seek(const Duration(seconds: 30));
    await c.toggleShuffle();
    await c.settled;
    expect(backend.position, const Duration(seconds: 30));
    expect(backend.playing, isTrue);
  });

  test('the player advancing by itself moves now playing', () async {
    await c.playTrack(library[0], context: library);
    await c.settled;
    backend.advance();
    expect(c.queue.currentIndex, 1);
    expect(c.nowPlaying!.path, library[1].path);
  });

  test('an index from before a pending edit is not adopted', () async {
    await c.playTrack(library[0], context: library);
    await c.settled;
    backend.latency = const Duration(milliseconds: 20);
    c.queueNext(track(5));
    backend.currentIndex = 2; // stale event while the splice is in flight
    backend.emit();
    expect(c.queue.currentIndex, 0);
    await expectInStep();
  });

  test('the notification\'s Next/Previous take the app\'s path', () async {
    await c.playTrack(library[0], context: library);
    await c.settled;
    // audio_service calls the handler's skipToNext/skipToPrevious.
    await backend.skipToNext();
    await expectInStep();
    expect(c.queue.currentIndex, 1);
    expect(backend.ops.last, 'skip');

    // In the background [position] isn't ticked; Previous reads the
    // player's and restarts the track past 3 s.
    c.setForeground(false);
    backend.position = const Duration(seconds: 40);
    await backend.skipToPrevious();
    await c.settled;
    expect(c.queue.currentIndex, 1);
    expect(backend.position, Duration.zero);
    await backend.skipToPrevious();
    await expectInStep();
    expect(c.queue.currentIndex, 0);

    // Next at the end of the queue ends it, as in the app.
    c.playTrackAt(2);
    await c.settled;
    await backend.skipToNext();
    await c.settled;
    expect(c.stopped, isTrue);
  });

  test('next, previous, repeat', () async {
    await c.playTrack(library[0], context: library);
    c.next();
    await expectInStep();
    expect(c.queue.currentIndex, 1);
    c.previous();
    await expectInStep();
    expect(c.queue.currentIndex, 0);

    // Past 3 s, previous restarts the track.
    c.playTrackAt(2);
    await c.settled;
    c.seek(const Duration(seconds: 10));
    c.previous();
    await c.settled;
    expect(c.queue.currentIndex, 2);
    expect(backend.position, Duration.zero);

    // End of the queue: stop; with repeat-all, wrap.
    c.next();
    await c.settled;
    expect(c.stopped, isTrue);
    expect(backend.ops.last, 'stop');
    c.toggle();
    await c.settled;
    c.playTrackAt(2);
    c.cycleRepeat(); // one
    c.cycleRepeat(); // all
    expect(c.repeat, Repeat.all);
    await c.settled;
    expect(backend.repeat, Repeat.all);
    c.next();
    await expectInStep();
    expect(c.queue.currentIndex, 0);
  });

  test('removing the only row stops and forgets the saved queue', () async {
    await c.playTrack(library[0], context: [library[0]]);
    await c.saveQueue();
    c.removeRow(c.queue.entries.single);
    await c.settled;
    await c.saveQueue();
    expect(backend.ops.last, 'stop');
    expect(File('${dir.path}/queue_state.json').existsSync(), isFalse);
  });

  test('a failed edit is repaired with a full push', () async {
    await c.playTrack(library[0], context: library);
    await c.settled;
    backend.failNextSplice = true;
    c.queueLast(track(5));
    await c.settled;
    await expectInStep();
    expect(backend.ops.sublist(backend.ops.length - 2), [
      'splice',
      'set_playlist',
    ]);
  });

  test('restore loads the saved queue paused at the saved offset', () async {
    // Real files, so the restore preloads instead of waiting for play.
    final real = [
      for (var i = 0; i < 3; i++)
        (File('${dir.path}/t$i.mp3')..writeAsStringSync('x')).path,
    ];
    File('${dir.path}/queue_state.json').writeAsStringSync(
      jsonEncode({
        'queue': [
          for (final p in real) {'path': p, 'track_title': p},
        ],
        'current_index': 0,
        'position': 0.0,
        'duration': 0.0,
      }),
    );
    File('${dir.path}/queue_pos.json').writeAsStringSync(
      jsonEncode({
        'path': real[2],
        'current_index': 2,
        'position': 42.0,
        'duration': 200.0,
      }),
    );
    File(
      '${dir.path}/flet_prefs.json',
    ).writeAsStringSync('{"repeat_mode": "one"}');
    await c.settled;
    c.dispose();
    backend = FakeBackend();
    c = make();
    await c.init();
    await expectInStep();
    expect(c.nowPlaying!.path, real[2]);
    expect(c.position.value, const Duration(seconds: 42));
    expect(c.duration, const Duration(seconds: 200));
    expect(backend.playing, isFalse);
    expect(backend.position, const Duration(seconds: 42));
    expect(c.repeat, Repeat.one);
    expect(backend.ledgerPath, '${dir.path}/play_ledger.jsonl');
    expect(backend.resumePath, '${dir.path}/native_resume.json');
  });

  test('a restored track that is gone loads on first play instead', () async {
    File('${dir.path}/queue_state.json').writeAsStringSync(
      jsonEncode({
        'queue': [
          {'path': '/gone/a.flac'},
        ],
        'current_index': 0,
        'position': 12.0,
        'duration': 0,
      }),
    );
    await c.settled;
    c.dispose();
    backend = FakeBackend();
    c = make();
    await c.init();
    await c.settled;
    expect(backend.rows, isEmpty);
    expect(c.nowPlaying!.path, '/gone/a.flac');
    c.toggle();
    await expectInStep();
    expect(backend.playing, isTrue);
    expect(backend.position, const Duration(seconds: 12));
  });

  // test_queue_divergence.py::test_random_mutation_storm_keeps_both_sides_identical,
  // through the controller and a slow player.
  test('random mutation storm keeps queue and player identical', () async {
    for (var seed = 0; seed < 15; seed++) {
      final rng = Random(seed);
      var fresh = 100;
      Track t() => track(fresh++, dir: '/r');
      await c.playTrack(
        library[0],
        context: [for (var i = 0; i < 8; i++) t()]..insert(0, library[0]),
      );
      await c.settled;
      for (var step = 0; step < 40; step++) {
        backend.latency = Duration(microseconds: rng.nextInt(3) * 500);
        final n = c.queue.length;
        switch (rng.nextInt(6)) {
          case 0:
            c.queueNext(t());
          case 1:
            c.queueLast(t());
          case 2:
            c.queueLastMany([for (var i = 0; i < 1 + rng.nextInt(4); i++) t()]);
          case 3 when n > 2:
            final row = c.queue.entries[rng.nextInt(n)];
            if (!identical(row, c.queue.current)) c.removeRow(row);
          case 4 when n > 2:
            c.moveRow(rng.nextInt(n), rng.nextInt(n));
          case 5 when n > 0:
            c.playTrackAt(rng.nextInt(n));
          default:
        }
        if (rng.nextDouble() < 0.4) {
          await Future<void>.delayed(
            Duration(microseconds: rng.nextInt(3) * 700),
          );
        }
      }
      await c.settled;
      expect(nativePaths(), qPaths(), reason: 'seed $seed');
      expect(backend.currentIndex, c.queue.currentIndex, reason: 'seed $seed');
      expect(backend.idAt(backend.currentIndex), c.queue.current!.src);
    }
  });

  // library.py `_enqueue_network_tracks` (the selection bar).
  test('selection Play goes in after the playing row and plays', () async {
    await c.playTrack(library[0], context: library);
    await c.settled;
    final picks = [track(8), track(9)];
    expect(c.playBlock(picks, replace: true), 2);
    await expectInStep();
    expect(qPaths(), [
      '/music/song1.mp3',
      '/music/song8.mp3',
      '/music/song9.mp3',
      '/music/song2.mp3',
      '/music/song3.mp3',
    ]);
    expect(c.nowPlaying!.path, '/music/song8.mp3');
  });

  test('selection Play turns shuffle order off, keeping the queue', () async {
    await c.playTrack(library[0], context: library);
    await c.toggleShuffle();
    await c.settled;
    final before = qPaths().length;
    c.playBlock([track(8)], replace: true);
    await expectInStep();
    expect(c.shuffle, isFalse);
    expect(qPaths().length, before + 1);
    expect(c.nowPlaying!.path, '/music/song8.mp3');
  });

  test('selection Add to Queue: after earlier picks, no jump', () async {
    await c.playTrack(library[0], context: library);
    c.queueLast(track(7));
    c.playBlock([track(8), track(9)], replace: false);
    await expectInStep();
    expect(qPaths().sublist(0, 4), [
      '/music/song1.mp3',
      '/music/song7.mp3',
      '/music/song8.mp3',
      '/music/song9.mp3',
    ]);
    expect(c.nowPlaying!.path, '/music/song1.mp3');
  });

  test('selection Play on an empty queue plays the block', () async {
    c.playBlock([track(8), track(9)], replace: true);
    await expectInStep();
    expect(qPaths(), ['/music/song8.mp3', '/music/song9.mp3']);
    expect(backend.playing, isTrue);
  });

  test('selection Play after stop loads the queue at the block', () async {
    await c.playTrack(library[0], context: library);
    await c.settled;
    c.stop();
    c.playBlock([track(8)], replace: true);
    await expectInStep();
    expect(c.nowPlaying!.path, '/music/song8.mp3');
    expect(c.stopped, isFalse);
  });

  test('deleting a track stops it if playing and drops every row', () async {
    await c.playTrack(library[1], context: library);
    c.queueLast(library[2]);
    await c.settled;
    c.removePath('/music/song2.mp3');
    await c.settled;
    expect(backend.playing, isFalse);
    expect(backend.ops, contains('stop'));
    expect(qPaths(), [
      '/music/song1.mp3',
      '/music/song3.mp3',
      '/music/song3.mp3',
    ]);
    c.removePath('/music/song3.mp3');
    await c.settled;
    expect(qPaths(), ['/music/song1.mp3']);
  });

  test('QueueEntry ids are the Flet engine ids', () async {
    await c.playTrack(library[0], context: library);
    await c.settled;
    expect(backend.idAt(0), pathToUri('/music/song1.mp3'));
  });
}
