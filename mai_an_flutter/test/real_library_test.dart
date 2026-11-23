// Opt-in smoke test against a real library.db written by the Python app:
//   LIBRARY_DB=/path/to/library.db flutter test test/real_library_test.dart
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/db_worker.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/graph/coord_graph.dart';

void main() {
  final path = Platform.environment['LIBRARY_DB'];
  test(
    'loads tracks and graph from a real library',
    () async {
      final sw = Stopwatch()..start();
      final repo = await LibraryRepository.open(path!);
      final tracks = await repo.allTracks();
      final tTracks = sw.elapsedMilliseconds;
      final CoordGraph g;
      try {
        g = await repo.coordGraph();
      } on DbWorkerException catch (e) {
        // ignore: avoid_print
        print('tracks=${tracks.length} ($tTracks ms)  no graph: ${e.message}');
        await repo.close();
        return;
      }
      final tGraph = sw.elapsedMilliseconds - tTracks;
      final byPath = {for (final t in tracks) t.path: t};
      final seed = g.length ~/ 2;
      final ws = Stopwatch()..start();
      final walk = g.walk(seed, length: 10);
      final tWalk = ws.elapsedMicroseconds;
      // ignore: avoid_print
      print(
        'tracks=${tracks.length} ($tTracks ms)  graph=${g.length}x${g.dim} '
        'skipped=${g.skipped} ($tGraph ms)  walk=$tWalk µs',
      );
      final s = byPath[g.paths[seed]]!;
      // ignore: avoid_print
      print('seed: ${s.artist} — ${s.title}');
      for (final i in walk) {
        final t = byPath[g.paths[i]]!;
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
