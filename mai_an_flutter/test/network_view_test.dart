// The Acoustic Network view's behaviour (library.py's network half): the
// build, the readout, density and steps, reseeding, the two modes,
// following playback, the list's actions, and the screen.
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/haptics.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/browse_controller.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/library/library_screen.dart';
import 'package:mai_an_lab/library/network_controller.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/player/queue_store.dart';
import 'package:mai_an_lab/ui/tokens.dart';

import 'fake_backend.dart';

Uint8List _blob(double x, double y) {
  final f = Float32List(8)..fillRange(0, 8, 0.3);
  f[0] = x;
  f[1] = y;
  return f.buffer.asUint8List();
}

/// Two artists, three analysed tracks each, one unanalysed track.
Future<String> _library(Directory dir, {bool coords = true}) async {
  final path = '${dir.path}/library.db';
  final repo = await LibraryRepository.open(path, writable: true);
  final rows = <(String, List<Object?>)>[
    ("INSERT INTO artists (id, name) VALUES (1, 'Ana'), (2, 'Bo')", const []),
    (
      'INSERT INTO albums (id, artist_id, title, genre) VALUES '
          "(1, 1, 'First', 'Rock'), (2, 2, 'Second', 'Jazz')",
      const [],
    ),
  ];
  const tracks = [
    (1, 'Alpha', '/m/a1.flac', 0.0, 0.0),
    (1, 'Beta', '/m/a2.flac', 0.2, 0.1),
    (1, 'Gamma', '/m/a3.flac', -0.1, 0.3),
    (2, 'Delta', '/m/b1.flac', 0.6, -0.2),
    (2, 'Epsilon', '/m/b2.flac', 0.7, 0.0),
    (2, 'Zeta', '/m/b3.flac', 0.5, 0.4),
  ];
  for (final (i, (album, title, p, x, y)) in tracks.indexed) {
    rows.add((
      'INSERT INTO tracks (album_id, title, track_num, duration, path, '
          'added_date) VALUES (?, ?, ?, 200, ?, ?)',
      [album, title, i + 1, p, 1700000000 + i],
    ));
    rows.add((
      'INSERT INTO play_counts (track_path, count, pca_coords, timbre, '
          'features_version) VALUES (?, ?, ?, ?, 5)',
      [p, i, if (coords) _blob(x, y) else null, Uint8List(4)],
    ));
  }
  rows.add((
    'INSERT INTO tracks (album_id, title, track_num, duration, path) '
        "VALUES (2, 'Raw', 9, 100, '/m/raw.flac')",
    const [],
  ));
  await repo.debugExecute(rows);
  await repo.buildMetadataEdges();
  await repo.close();
  return path;
}

void main() {
  late Directory dir;
  setUp(() => dir = Directory.systemTemp.createTempSync('mai_an_network'));
  tearDown(() => dir.deleteSync(recursive: true));

  final quiet = Haptics(const HapticsConfig(enabled: false));

  Future<(LibraryController, NetworkController)> open(
    String path, {
    ValueNotifier<PlayerController?>? player,
  }) async {
    final lib = LibraryController(
      () => LibraryRepository.open(path, writable: true),
    );
    await lib.load();
    await lib.stations();
    final net = NetworkController(
      library: lib,
      player: player ?? ValueNotifier(null),
      haptics: quiet,
    )..dims = (336, 268);
    return (lib, net);
  }

  test(
    'builds on the first analysed track, its artist as neighbours',
    () async {
      final (_, net) = await open(await _library(dir));
      await net.load();
      expect(net.status, NetStatus.ready);
      final g = net.graph!;
      expect(net.trackCount, greaterThanOrEqualTo(3));
      expect(g.nodes.first.isSeed, isTrue);
      final seed = g.nodes.first.path;
      expect(net.selected, seed);
      final artistOfSeed = seed.contains('/a') ? 'a' : 'b';
      for (final e in g.edges.where((e) => e.kind.name == 'local')) {
        expect(e.src, seed);
        expect(e.dst, contains('/$artistOfSeed'));
      }
      // The walk starts at the selection, so the walk button is hidden.
      expect(g.walkSeedPath, seed);
      expect(net.canWalkFromSelection, isFalse);

      // Selecting only inspects.
      final other = g.localPaths.firstWhere((p) => p != seed);
      net.select(other);
      expect(identical(net.graph, g), isTrue);
      expect(net.canWalkFromSelection, isTrue);

      // Walking from it merges a new walk into the same graph.
      net.walkFromSelection();
      await pumpEventQueue();
      expect(identical(net.graph, g), isTrue);
      expect(g.walkSeedPath, other);
      expect(net.canWalkFromSelection, isFalse);

      // Explore from here rebuilds on it.
      net.reseedSelection();
      await pumpEventQueue(times: 50);
      expect(net.graph!.nodes.first.path, other);
      expect(net.seedPath, other);

      // Density 12 rebuilds; a shorter walk shortens the list in place.
      net.setDensity(12);
      await pumpEventQueue(times: 50);
      expect(net.density, 12);
      net.setWalkLength(5);
      await pumpEventQueue(times: 50);
      expect(net.graph!.walkSeq.length, lessThanOrEqualTo(5));
      net.dispose();
    },
  );

  test('no coordinates: the setup card', () async {
    final (_, net) = await open(await _library(dir, coords: false));
    await net.load();
    expect(net.status, NetStatus.noCoords);
    expect(net.trackCount, 0);
    net.dispose();
  });

  group('with a player', () {
    late FakeBackend backend;
    late PlayerController player;
    late ValueNotifier<PlayerController?> holder;
    late LibraryController lib;
    late NetworkController net;

    setUp(() async {
      final path = await _library(dir);
      backend = FakeBackend();
      player = PlayerController(
        backend: backend,
        store: QueueStore(dir.path),
        prefs: Prefs(dir.path),
        libraryTracks: () async => const [],
      );
      await player.init();
      holder = ValueNotifier(player);
      (lib, net) = await open(path, player: holder);
    });
    tearDown(() async {
      net.dispose();
      await player.flush();
      player.dispose();
    });

    Future<void> play(String path) async {
      final all = lib.allTracks..sort((a, b) => a.path.compareTo(b.path));
      final t = all.firstWhere((t) => t.path == path);
      await player.playTrack(t, context: all);
      await pumpEventQueue(times: 50);
    }

    test('explore: the playing track is the seed and the focal node; '
        'Play and Next queue the walk', () async {
      await play('/m/b2.flac');
      await net.load();
      final g = net.graph!;
      expect(g.nodes.first.path, '/m/b2.flac');
      // The focal node maps to the centre (the declutter may nudge it <= 12 px).
      expect((g.nodes.first.px - 336 / 2).abs(), lessThanOrEqualTo(12));
      final walk = g.walkSeq;
      expect(walk, isNotEmpty);

      net.addAll();
      // The listener's picks go right after the playing row.
      final q = [for (final r in player.queue.entries) r.path];
      final at = player.queue.currentIndex + 1;
      expect(q.sublist(at, at + walk.length), walk);
      expect(player.currentPath, '/m/b2.flac');

      net.playAll();
      await pumpEventQueue(times: 50);
      expect(player.currentPath, walk.first);
    });

    test('live: the list is the queue ahead and follows the track', () async {
      await play('/m/a1.flac');
      net.attach();
      await net.load();
      net.toggleMode();
      await pumpEventQueue(times: 50);
      expect(net.mode, NetMode.live);
      expect(net.selected, '/m/a1.flac');
      final ahead = net.queueAhead().take(net.walkLength).toList();
      expect(ahead, isNotEmpty);
      expect(net.graph!.walkSeq, [
        for (final p in ahead)
          if (net.graph!.byPath.containsKey(p)) p,
      ]);

      // A track on the graph: re-anchor there (after 200 ms).
      player.playTrackAt(player.queue.currentIndex + 1);
      await pumpEventQueue(times: 50);
      await Future<void>.delayed(const Duration(milliseconds: 260));
      await pumpEventQueue(times: 50);
      expect(net.selected, player.currentPath);

      // Explore pins the graph: playback no longer moves the selection.
      net.toggleMode();
      await pumpEventQueue(times: 50);
      final pinned = net.selected;
      player.playTrackAt(player.queue.currentIndex + 1);
      await Future<void>.delayed(const Duration(milliseconds: 400));
      await pumpEventQueue(times: 50);
      expect(net.selected, pinned);
      net.detach();
    });

    test('row actions: play from here, play next, walk here', () async {
      await play('/m/a1.flac');
      await net.load();
      final g = net.graph!;
      final nodes = g.walkNodes;
      expect(nodes.length, greaterThan(1));
      net.playNext(nodes.last);
      expect(
        player.queue.entries[player.queue.currentIndex + 1].path,
        nodes.last.path,
      );

      net.playFrom(1, nodes[1]);
      await pumpEventQueue(times: 50);
      expect(player.currentPath, nodes[1].path);

      net.toggleMode(); // live
      await pumpEventQueue(times: 50);
      net.walkHere(nodes.first.path);
      expect(net.mode, NetMode.explore);
      expect(net.selected, nodes.first.path);
    });
  });

  testWidgets('no coordinates: Compute DSP Features starts the analysis', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(360 * 3, 760 * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    final path = (await tester.runAsync(() => _library(dir, coords: false)))!;
    final lib = LibraryController(
      () => LibraryRepository.open(path, writable: true),
      pcmDecoder: () async => null,
    );
    await tester.runAsync(lib.load);
    final browse = BrowseController(
      lib,
      views: const [LibraryView.network, LibraryView.tracks],
    );
    await tester.pumpWidget(
      MaterialApp(
        theme: Tokens.theme(),
        home: LibraryScreen(
          controller: lib,
          browse: browse,
          player: ValueNotifier(null),
          haptics: quiet,
        ),
      ),
    );
    Future<void> settle() async {
      for (var i = 0; i < 8; i++) {
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 30)),
        );
        await tester.pump(const Duration(milliseconds: 100));
      }
    }

    await tester.tap(find.text('Network'));
    await settle();
    expect(find.text('Acoustic Network Graph'), findsOneWidget);
    // No decoder on this host: the Flet error, as a snackbar.
    await tester.tap(find.text('Compute DSP Features'));
    await settle();
    expect(
      find.text('Audio service not ready — native analyser unavailable.'),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
    lib.dispose();
  });

  testWidgets('the screen: bar, graph, readout, chips, list, collapse', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(360 * 3, 760 * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    final path = (await tester.runAsync(() => _library(dir)))!;
    final lib = LibraryController(
      () => LibraryRepository.open(path, writable: true),
    );
    // load() returns once the station graphs are built.
    await tester.runAsync(lib.load);
    final browse = BrowseController(
      lib,
      views: const [LibraryView.network, LibraryView.tracks],
    );
    await tester.pumpWidget(
      MaterialApp(
        theme: Tokens.theme(),
        home: LibraryScreen(
          controller: lib,
          browse: browse,
          player: ValueNotifier(null),
          haptics: quiet,
        ),
      ),
    );
    Future<void> settle() async {
      for (var i = 0; i < 12; i++) {
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 30)),
        );
        await tester.pump(const Duration(milliseconds: 100));
      }
    }

    await tester.tap(find.text('Network'));
    await settle();
    expect(find.text('GRAPH WALK'), findsOneWidget);
    expect(find.text('Explore'), findsOneWidget);
    expect(find.text('Density: 24'), findsOneWidget);
    expect(find.textContaining('· seed'), findsOneWidget);
    expect(find.byTooltip('Sort'), findsNothing);
    expect(find.textContaining('tracks'), findsWidgets);
    expect(tester.takeException(), isNull);

    // The mode chip: Now Playing, nothing queued (no player).
    await tester.tap(find.text('Explore'));
    await settle();
    expect(find.text('Now Playing'), findsOneWidget);
    expect(find.text('UP NEXT'), findsOneWidget);
    expect(find.text('Nothing queued ahead of this track.'), findsOneWidget);

    // Back to explore; collapse the graph, then bring it back.
    await tester.tap(find.text('Now Playing'));
    await settle();
    expect(find.text('Density: 24'), findsOneWidget);
    await tester.tap(find.byIcon(Icons.keyboard_arrow_down_rounded));
    await settle();
    expect(find.text('Density: 24'), findsNothing);
    await tester.tap(find.byIcon(Icons.keyboard_arrow_up_rounded));
    await settle();
    expect(find.text('Density: 24'), findsOneWidget);

    // Density from the chip's menu.
    await tester.tap(find.text('Density: 24'));
    await settle();
    await tester.tap(find.text('Density: 12 tracks'));
    await settle();
    expect(find.text('Density: 12'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
  });
}
