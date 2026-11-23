import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/player/play_queue.dart';
import 'package:mai_an_lab/player/queue_entry.dart';

QueueEntry row(String path, [Map<String, Object?> tags = const {}]) =>
    QueueEntry({'path': path, ...tags});

List<String> paths(PlayQueue q) => [for (final t in q.entries) t.path];

PlayQueue queueOf(List<QueueEntry> rows, [int current = 0]) =>
    PlayQueue()
      ..set(rows)
      ..currentIndex = current;

/// Applies [change] to a plain list the way the native playlist does.
void applyNative(List<QueueEntry> native, QueueChange change, PlayQueue q) {
  switch (change) {
    case NoChange():
      break;
    case Emptied():
      native.clear();
    case Reload():
      native
        ..clear()
        ..addAll(q.entries);
    case Splice(:final edits):
      for (final e in edits) {
        expect(e.start + e.deleteCount, lessThanOrEqualTo(native.length));
        native.removeRange(e.start, e.start + e.deleteCount);
        native.insertAll(e.start, e.items);
      }
    case Move(:final from, :final to):
      native.insert(to, native.removeAt(from));
  }
}

void main() {
  // tests/test_queue_layout.py
  group('layout while auto-play is on', () {
    test('up-next block ends at the first unqueued row', () {
      final q = queueOf([
        row('/now'),
        row('/u/1', {kQueued: true}),
        row('/u/2', {kQueued: true}),
        row('/r/1', {kAutoplay: true}),
        row('/lib/1'),
      ]);
      expect(q.upNextEnd(), 3);
    });
  });

  group('EngineQueueAddTests', () {
    late PlayQueue q;
    setUp(() {
      q = queueOf([
        row('/now'),
        row('/r/1', {kAutoplay: true}),
        row('/r/2', {kAutoplay: true}),
        row('/lib/1'),
        row('/lib/2'),
      ]);
    });

    test('add to queue plays before the station, in order', () {
      q.queueLast(row('/a'));
      q.queueLast(row('/b'));
      expect(paths(q), ['/now', '/a', '/b', '/r/1', '/r/2', '/lib/1', '/lib/2']);
      expect(q.entries[1].queued, isTrue);
    });

    test('play next jumps ahead of earlier picks', () {
      q.queueLast(row('/a'));
      q.queueNext(row('/n'));
      expect(paths(q).take(3), ['/now', '/n', '/a']);
    });

    test('add to queue without auto-play goes ahead of the rest', () {
      q = queueOf([row('/now'), row('/album/2'), row('/album/3')]);
      q.queueLast(row('/a'));
      expect(paths(q), ['/now', '/a', '/album/2', '/album/3']);
    });

    test('add to queue under shuffle still appends', () {
      q.shuffle = true;
      q.queueLast(row('/a'));
      expect(q.entries.last.path, '/a');
    });

    test('a block keeps its order after earlier picks', () {
      q.queueLast(row('/a'));
      q.queueLastMany([row('/b/1'), row('/b/2')]);
      expect(paths(q).take(5), ['/now', '/a', '/b/1', '/b/2', '/r/1']);
      expect(q.entries.sublist(1, 4).every((t) => t.queued), isTrue);
    });

    test("the caller's row is not tagged", () {
      final meta = row('/a');
      q.queueLast(meta);
      expect(meta.queued, isFalse);
    });
  });

  // tests/test_queue_divergence.py, entry filtering / index bookkeeping
  test('pathless tracks never enter the queue', () {
    final tracks = [for (var i = 0; i < 4; i++) row('/n/$i')];
    tracks.insert(1, QueueEntry({'track_title': 'no path'}));
    final q = PlayQueue()..set(tracks, 3); // → original /n/2
    expect(q.length, 4);
    expect(q.current!.path, '/n/2');
  });

  test('move tracks the playing row despite a duplicate', () {
    final rows = [for (var i = 0; i < 5; i++) row('/t/$i')];
    rows[0] = row('/t/3'); // equal value earlier in the queue
    final q = queueOf(rows, 3);
    q.move(4, 1);
    expect(q.currentIndex, 4);
  });

  test('scattered removal is one splice of contiguous runs, highest first', () {
    final q = queueOf([for (var i = 0; i < 10; i++) row('/t/$i')], 1);
    final change = q.removeIndices([2, 3, 5, 8, 9]) as Splice;
    expect(
      [for (final e in change.edits) (e.start, e.deleteCount)],
      [(8, 2), (5, 1), (2, 2)],
    );
    expect(paths(q), ['/t/0', '/t/1', '/t/4', '/t/6', '/t/7']);
    expect(q.currentIndex, 1);
  });

  test('removing rows above the playing one keeps it playing', () {
    final q = queueOf([for (var i = 0; i < 6; i++) row('/t/$i')], 4);
    q.removeIndices([0, 2, 4]); // 4 is playing: never removed
    expect(q.current!.path, '/t/4');
    expect(paths(q), ['/t/1', '/t/3', '/t/4', '/t/5']);
  });

  test('replace rows lands the block where the first removed row was', () {
    final q = queueOf([
      row('/now'),
      row('/u', {kQueued: true}),
      row('/r/1', {kAutoplay: true}),
      row('/r/2', {kAutoplay: true}),
      row('/lib'),
    ]);
    q.replaceRows([2, 3], [row('/n/1'), row('/n/2')]);
    expect(paths(q), ['/now', '/u', '/n/1', '/n/2', '/lib']);
  });

  test('removing the last row empties the queue', () {
    final q = queueOf([row('/a')]);
    expect(q.removeAt(0), isA<Emptied>());
    expect(q.isEmpty, isTrue);
  });

  test('removing the playing row shows the row that slid into its place', () {
    final q = queueOf([row('/a'), row('/b'), row('/c')], 1);
    q.removeAt(1);
    expect(q.current!.path, '/c');
    final last = queueOf([row('/a'), row('/b')], 1);
    last.removeAt(1);
    expect(last.current!.path, '/a');
  });

  test('neighbour follows the shuffle order and stops at the ends', () {
    final q = queueOf([for (var i = 0; i < 4; i++) row('/t/$i')], 2)
      ..shuffle = true;
    const order = [3, 2, 0, 1];
    expect(q.neighbour(1, order), 0);
    expect(q.neighbour(-1, order), 3);
    q.currentIndex = 1;
    expect(q.neighbour(1, order), isNull);
    // A stale order (wrong length) falls back to sequential.
    expect(q.neighbour(1, const [0, 1]), 2);
  });

  test('upcoming lists rows from the playing one in play order', () {
    final q = queueOf([for (var i = 0; i < 4; i++) row('/t/$i')], 2);
    expect([for (final (i, _) in q.upcoming(const [])) i], [2, 3]);
    q.shuffle = true;
    expect([for (final (i, _) in q.upcoming(const [3, 2, 0, 1])) i], [2, 0, 1]);
  });

  test('findSrc prefers the duplicate nearest the hint', () {
    final q = queueOf([row('/a'), row('/x'), row('/b'), row('/c'), row('/x')]);
    expect(q.findSrc(pathToUri('/x'), 3), 4);
    expect(q.findSrc(pathToUri('/x'), 1), 1);
    expect(q.findSrc(pathToUri('/none'), 1), isNull);
  });

  // tests/test_queue_divergence.py::test_random_mutation_storm_keeps_both_sides_identical
  test('random mutation storm: applied edits keep the native list identical', () {
    for (var seed = 0; seed < 25; seed++) {
      final rng = Random(seed);
      var fresh = 0;
      QueueEntry track() => row('/r/${fresh++}.flac');
      final q = queueOf([for (var i = 0; i < 8; i++) track()], 2);
      final native = List.of(q.entries);
      for (var step = 0; step < 40; step++) {
        final n = q.length;
        final cur = q.currentIndex;
        final playing = q.current;
        final op = rng.nextInt(7);
        QueueChange change = const NoChange();
        if (op == 0) {
          change = q.queueNext(track());
        } else if (op == 1) {
          change = q.queueLast(track());
        } else if (op == 2) {
          change = q.insertAfterCurrent([
            for (var i = 0; i < 1 + rng.nextInt(5); i++) track(),
          ]);
        } else if (op == 3 && n > 2) {
          final k = 1 + rng.nextInt(min(5, n - 1));
          final pick = (List.generate(n, (i) => i)..shuffle(rng)).take(k);
          change = q.removeIndices(pick);
        } else if (op == 4 && n > 2) {
          change = q.move(rng.nextInt(n), rng.nextInt(n));
        } else if (op == 5 && n > 0) {
          q.currentIndex = rng.nextInt(n);
        } else if (op == 6 && n > 2 && rng.nextDouble() < 0.3) {
          final idx = rng.nextInt(n);
          if (idx != cur) change = q.removeAt(idx);
        }
        applyNative(native, change, q);
        expect(
          [for (final t in native) t.path],
          paths(q),
          reason: 'seed $seed step $step',
        );
        expect(q.currentIndex, inInclusiveRange(0, max(0, q.length - 1)));
        if (op != 5) {
          expect(identical(q.current, playing), isTrue,
              reason: 'the playing row moved, seed $seed step $step');
        }
      }
    }
  });
}
