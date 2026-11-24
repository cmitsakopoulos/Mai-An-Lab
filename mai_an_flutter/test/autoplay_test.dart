// AutoPlay over the real PlayerController and a fake native player. Ported
// from the Flet app's tests/test_autoplay.py (same scenarios, same expected
// queues); the station is a fixed ranking, as there.
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/player/autoplay.dart';
import 'package:mai_an_lab/player/playback_backend.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/player/queue_store.dart';
import 'package:mai_an_lab/stations/station.dart';

import 'fake_backend.dart';

Track track(String path) => Track(
  id: path.hashCode,
  path: path,
  title: path,
  artist: 'A${path.substring(path.length - 1)}',
  album: 'B',
  duration: 200,
  format: 'mp3',
);

class FakeStation implements StationHandle {
  FakeStation(List<String> ranking, Set<String> avoid)
    : _ranking = List.of(ranking),
      used = {...avoid};
  final List<String> _ranking;
  final Set<String> used;
  final rejected = <List<String>>[];
  final accepted = <String>[];

  @override
  String? get flavour => 'Rock';

  @override
  List<String> take(int n) {
    final out = _ranking.where((p) => !used.contains(p)).take(n).toList();
    used.addAll(out);
    return out;
  }

  @override
  void reject(Iterable<String> paths) {
    rejected.add(paths.toList());
    used.addAll(paths);
  }

  @override
  void exclude(Iterable<String> paths) => used.addAll(paths);

  @override
  void accept(String path) => accepted.add(path);
}

class FakeLibrary extends ChangeNotifier implements AutoPlayLibrary {
  List<String> nextRanking = ranking;
  bool analysed = true;
  final stations = <FakeStation>[];
  final opens = <(String, Map<String, Object>)>[];
  List<String> recent = const [];

  @override
  bool get stationsAvailable => true;

  @override
  Future<StationHandle?> openStation(
    String seed, {
    required Set<String> avoid,
    required bool focused,
    required int maxPerArtist,
    required int maxPerAlbum,
    required int window,
  }) async {
    opens.add((
      seed,
      {
        'avoid': avoid,
        'focused': focused,
        'max_per_artist': maxPerArtist,
        'max_per_album': maxPerAlbum,
        'window': window,
      },
    ));
    final s = FakeStation(nextRanking, avoid);
    stations.add(s);
    return s;
  }

  @override
  Future<Map<String, Track>> tracksBrief(List<String> paths) async => {
    for (final p in paths) p: track(p),
  };

  @override
  Future<bool> hasCurrentFeatures(String path) async => analysed;

  @override
  Future<List<String>> recentPaths(int limit) async => recent;
}

final ranking = [for (var i = 1; i < 30; i++) '/r/$i'];
final libAbc = [
  for (final p in ['/lib/a', '/lib/b', '/lib/c']) track(p),
];

void main() {
  late Directory dir;
  late FakeBackend backend;
  late FakeLibrary lib;
  late PlayerController c;
  late AutoPlay ap;
  late List<String> said;

  Future<PlayerController> make() async {
    final p = PlayerController(
      backend: backend,
      store: QueueStore(dir.path),
      prefs: Prefs(dir.path),
      libraryTracks: () async => libAbc,
    );
    p.autoplay = AutoPlay(player: p, library: lib);
    p.messages.listen(said.add);
    await p.init();
    return p;
  }

  setUp(() async {
    dir = Directory.systemTemp.createTempSync('mai_an_autoplay');
    backend = FakeBackend();
    lib = FakeLibrary();
    said = [];
    c = await make();
    ap = c.autoplay!;
    await c.playTrack(libAbc[0], context: libAbc);
    await pumpEventQueue();
  });

  tearDown(() async {
    await c.flush();
    c.dispose();
    dir.deleteSync(recursive: true);
  });

  Future<void> drain() async {
    await pumpEventQueue();
    await c.settled;
    await pumpEventQueue();
  }

  List<String> paths() => [for (final t in c.queue.entries) t.path];
  List<String> nativePaths() => [for (final t in backend.rows) t.path];
  List<String> held() => [for (final t in ap.held) t.path];

  /// Rows after the playing one: only picks and recommendations, ever.
  List<String> ahead() {
    final q = c.queue.entries.skip(c.queue.currentIndex + 1).toList();
    expect(q.every((t) => t.queued || t.autoplay), isTrue, reason: '$q');
    return [for (final t in q) t.path];
  }

  Future<void> enable() async {
    ap.setMode(true);
    await drain();
  }

  Future<void> advance(int i) async {
    c.playTrackAt(i);
    await drain();
  }

  test('enable sets the queue aside and queues a buffer', () async {
    await enable();
    expect(paths(), ['/lib/a', '/r/1', '/r/2', '/r/3', '/r/4']);
    expect(nativePaths(), paths());
    expect(held(), ['/lib/b', '/lib/c']);
    expect(c.continuesAtEnd, isTrue);
    expect(lib.opens.first.$1, '/lib/a');
    expect(lib.opens.first.$2['focused'], isTrue);
  });

  test('disable drops the buffer and puts the queue back', () async {
    await enable();
    ap.setMode(false);
    await drain();
    expect(paths(), ['/lib/a', '/lib/b', '/lib/c']);
    expect(nativePaths(), paths());
    expect(ap.held, isEmpty);
    expect(c.continuesAtEnd, isFalse);
    expect(ap.station, isNull);
  });

  test('toggle off mid-fill discards the stale block', () async {
    ap.setMode(true);
    ap.setMode(false);
    await drain();
    expect(paths(), ['/lib/a', '/lib/b', '/lib/c']);
  });

  test("the listener's picks play first and stay", () async {
    c.queueLast(track('/u/1'));
    c.queueLast(track('/u/2'));
    await enable();
    expect(ahead(), ['/u/1', '/u/2', '/r/1', '/r/2', '/r/3', '/r/4']);
    ap.setMode(false);
    await drain();
    expect(paths(), ['/lib/a', '/u/1', '/u/2', '/lib/b', '/lib/c']);
  });

  test(
    'playing through picks and buffer never reaches the old queue',
    () async {
      c.queueLast(track('/u/1'));
      await enable();
      for (var i = 0; i < 12; i++) {
        await advance(c.queue.currentIndex + 1);
        ahead();
      }
      expect(lib.opens, hasLength(1)); // one station throughout
      expect(paths().skip(1).any((p) => p.startsWith('/lib/')), isFalse);
      expect(nativePaths(), paths());
    },
  );

  test('a new queue starts a station and is set aside', () async {
    await enable();
    final xs = [for (var i = 1; i <= 3; i++) track('/x/$i')];
    await c.playTrack(xs[0], context: xs);
    await drain();
    expect(ap.anchor, '/x/1');
    expect(ahead(), hasLength(4));
    expect(held(), ['/x/2', '/x/3']);
  });

  test('refills continue the same station', () async {
    await enable();
    await advance(2); // /r/2 playing
    expect(lib.opens, hasLength(1));
    expect(ahead(), ['/r/3', '/r/4', '/r/5', '/r/6']);
  });

  test('restart: held queue and mode persist, and resume tops up', () async {
    await enable();
    c.removeIndices([2, 3, 4]); // only /r/1 left pending
    await c.flush();
    final saved = jsonDecode(
      File('${dir.path}/queue_state.json').readAsStringSync(),
    );
    expect(
      [for (final r in saved['autoplay_held'] as List) (r as Map)['path']],
      ['/lib/b', '/lib/c'],
    );
    c.dispose();

    backend = FakeBackend();
    c = await make();
    ap = c.autoplay!;
    await drain();
    expect(ap.enabled, isTrue);
    expect(ap.anchor, '/lib/a');
    expect(held(), ['/lib/b', '/lib/c']);
    expect(ap.bufferPaths(), hasLength(4));
    expect(ap.bufferPaths().first, '/r/1');
  });

  test('background prefill tops up to the background target', () async {
    await enable();
    c.setForeground(false);
    await drain();
    expect(ap.bufferPaths(), hasLength(AutoPlay.backgroundTarget));
    c.setForeground(true);
  });

  test('choosing a pending recommendation keeps the station', () async {
    await enable();
    c.playTrackAt(2);
    ap.onUserChose('/r/2');
    await drain();
    expect(ap.anchor, '/lib/a');
    expect(lib.opens, hasLength(1));
  });

  test('a library tap starts a new station', () async {
    await enable();
    final abcd = [...libAbc, track('/lib/d')];
    lib.nextRanking = ['/x/1', '/x/2', '/x/3', '/x/4'];
    await c.playTrack(abcd[2], context: abcd);
    await drain();
    expect(ap.anchor, '/lib/c');
    expect(ahead(), ['/x/1', '/x/2', '/x/3', '/x/4']);
    expect(held(), ['/lib/d']);
    expect(nativePaths(), paths());
  });

  test('a new station avoids history and the playing track', () async {
    ap.onTrackChanged('/lib/z'); // heard earlier
    await enable();
    final avoid = lib.opens.first.$2['avoid'] as Set<String>;
    expect(avoid, containsAll(['/lib/z', '/lib/a']));
  });

  test('a played-out station starts the genre over', () async {
    lib.nextRanking = ranking.sublist(0, 8);
    await enable();
    said.clear();
    for (var i = 1; i < 8; i++) {
      await advance(i);
    }
    expect(said, hasLength(1));
    expect(said.single, contains('Starting over'));
    expect(said.single, contains('Rock'));
    final next = ahead();
    expect(next, isNotEmpty);
    // The last few heard are kept out of the restart.
    final recent = paths().sublist(3, c.queue.currentIndex + 1);
    expect(next.toSet().intersection(recent.toSet()), isEmpty);
  });

  test('nothing left even starting over: the queue ends quietly', () async {
    lib.nextRanking = ranking.sublist(0, 2);
    await enable();
    said.clear();
    await advance(1);
    await advance(2);
    expect(ahead(), isEmpty);
    expect(said, isEmpty);
    c.next(); // end of the queue: asks auto-play, which has nothing
    await drain();
    expect(c.stopped, isTrue);
  });

  test('each negative steers; two replace the buffer once', () async {
    await enable();
    await advance(1); // /r/1 playing
    final st = lib.stations.first;
    backend.listenEvents.add((path: '/r/1', listenedS: 4, durationS: 200));
    expect(st.rejected, [
      ['/r/1'],
    ]);
    c.removeRow(c.queue.entries[2]); // the listener removes /r/2
    expect(st.rejected, [
      ['/r/1'],
      ['/r/2'],
    ]);
    await drain();
    expect(ap.bufferPaths(), ['/r/5', '/r/6', '/r/7', '/r/8']);
    expect(nativePaths(), paths());
  });

  test('one removal steers the station and tops up', () async {
    await enable();
    c.removeRow(c.queue.entries[2]); // /r/2
    await drain();
    expect(lib.stations.first.rejected, [
      ['/r/2'],
    ]);
    expect(ap.bufferPaths(), ['/r/1', '/r/3', '/r/4', '/r/5']);
  });

  test(
    'a listen through pulls the station; library skips are not negatives',
    () async {
      await enable();
      backend.listenEvents.add((path: '/r/1', listenedS: 120, durationS: 200));
      backend.listenEvents.add((path: '/lib/b', listenedS: 3, durationS: 200));
      expect(lib.stations.first.accepted, ['/r/1']);
      expect(lib.stations.first.rejected, isEmpty);
    },
  );

  test('station shape is passed through', () async {
    await enable();
    final kw = lib.opens.first.$2;
    expect(
      [kw['max_per_artist'], kw['max_per_album'], kw['window']],
      [AutoPlay.artistCap, AutoPlay.albumCap, AutoPlay.capWindow],
    );
  });

  test('a played-out queue appends a block and plays its first', () async {
    await c.playTrack(libAbc[0], context: [libAbc[0]]);
    await drain();
    ap.enabled = true; // on, but nothing queued yet
    backend
      ..processing = Processing.completed
      ..playing = false
      ..emit();
    await drain();
    expect(paths(), ['/lib/a', '/r/1', '/r/2', '/r/3', '/r/4']);
    expect(c.queue.currentIndex, 1);
    expect(backend.currentIndex, 1);
    expect(backend.playing, isTrue);
  });

  test('no station: the queue comes back and the next song retries', () async {
    lib
      ..analysed = false
      ..nextRanking = const [];
    await enable();
    expect(paths(), ['/lib/a', '/lib/b', '/lib/c']);
    expect(said.single, contains("isn't analysed"));
    ap.onTrackChanged('/lib/a'); // the same song again doesn't retry
    await drain();
    expect(lib.opens, hasLength(1));
    lib.nextRanking = ranking;
    await advance(1);
    expect(ap.anchor, '/lib/b');
    expect(ahead(), ['/r/1', '/r/2', '/r/3', '/r/4']);
    expect(held(), ['/lib/c']);
    expect(said, hasLength(1));
  });

  test('shuffle and auto-play exclude each other', () async {
    await c.toggleShuffle();
    await drain();
    expect(c.shuffle, isTrue);
    await enable();
    expect(c.shuffle, isFalse);
    expect(backend.shuffleOn, isFalse);
    await c.toggleShuffle();
    await drain();
    expect(ap.enabled, isFalse);
    expect(c.shuffle, isTrue);
  });

  test('Start radio plays the track and starts a station from it', () async {
    ap.startRadio(track('/lib/c'));
    await drain();
    expect(ap.enabled, isTrue);
    expect(c.currentPath, '/lib/c');
    expect(ap.anchor, '/lib/c');
    expect(ahead(), ['/r/1', '/r/2', '/r/3', '/r/4']);
  });
}
