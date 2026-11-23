// Opt-in smoke test against a real library.db written by the Python app:
//   LIBRARY_DB=/path/to/library.db flutter test test/real_library_test.dart
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/db_worker.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/stations/station.dart';

void main() {
  final path = Platform.environment['LIBRARY_DB'];
  test(
    'loads tracks and stations from a real library',
    () async {
      final sw = Stopwatch()..start();
      final repo = await LibraryRepository.open(path!);
      final tracks = await repo.allTracks();
      final tTracks = sw.elapsedMilliseconds;
      final StationSource g;
      try {
        g = await repo.stations();
      } on DbWorkerException catch (e) {
        // ignore: avoid_print
        print(
          'tracks=${tracks.length} ($tTracks ms)  no stations: ${e.message}',
        );
        await repo.close();
        return;
      }
      final tGraph = sw.elapsedMilliseconds - tTracks;
      final byPath = {for (final t in tracks) t.path: t};
      final seed = g.paths[g.length ~/ 2];
      final ws = Stopwatch()..start();
      final walk = g.walk(seed, length: 10);
      final tWalk = ws.elapsedMicroseconds;
      // ignore: avoid_print
      print(
        'tracks=${tracks.length} ($tTracks ms)  graph=${g.length}x${g.dim} '
        'artists=${g.graph.length} tags=${g.graph.tags.length} '
        'skipped=${g.skipped} ($tGraph ms)  walk=$tWalk µs',
      );
      final s = byPath[seed]!;
      // ignore: avoid_print
      print('seed: ${s.artist} — ${s.title}');
      for (final p in walk) {
        final t = byPath[p]!;
        // ignore: avoid_print
        print('  ${t.artist} — ${t.title}');
      }
      expect(tracks, isNotEmpty);
      expect(walk, isNotEmpty);
      await repo.close();
    },
    skip: path == null ? 'set LIBRARY_DB to run' : false,
  );
}
