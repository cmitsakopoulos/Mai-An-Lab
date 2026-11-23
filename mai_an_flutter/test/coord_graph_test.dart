import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/graph/coord_graph.dart';

import 'fixtures.dart';

CoordRow row(String path, List<double> v, {String? artist, String? album}) =>
    CoordRow(path: path, coords: f32Blob(v), artist: artist, album: album);

void main() {
  test('decodes float32 blobs and L2-normalises rows', () {
    final g = CoordGraph.fromRows([
      row('a', [3, 4]),
      row('b', [0, 2]),
    ]);
    expect(g.dim, 2);
    expect(g.unit[0], closeTo(0.6, 1e-6));
    expect(g.unit[1], closeTo(0.8, 1e-6));
    expect(g.unit[3], closeTo(1.0, 1e-6));
  });

  test('skips rows whose dimension disagrees', () {
    final g = CoordGraph.fromRows([
      row('a', [1, 0, 0]),
      row('b', [1, 0]), // stale-version blob
      row('c', [0, 1, 0]),
    ]);
    expect(g.paths, ['a', 'c']);
    expect(g.skipped, 1);
  });

  test('zero vector does not produce NaN', () {
    final g = CoordGraph.fromRows([
      row('a', [0, 0]),
      row('b', [1, 0]),
    ]);
    expect(g.similarities(1).any((s) => s.isNaN), isFalse);
  });

  test('nearest ranks by cosine, excluding the seed', () {
    final g = CoordGraph.fromRows([
      row('seed', [1, 0]),
      row('far', [-1, 0]),
      row('close', [1, 0.1]),
      row('mid', [1, 1]),
    ]);
    expect(g.nearest(0, 3).map((i) => g.paths[i]), ['close', 'mid', 'far']);
  });

  test('walk enforces artist/album caps, counting the seed', () {
    final g = CoordGraph.fromRows([
      row('seed', [1, 0], artist: 'A', album: 'X'),
      row('sameAlbum', [1, 0.01], artist: 'A', album: 'X'),
      row('a2', [1, 0.02], artist: 'A', album: 'Y'),
      row('a3', [1, 0.03], artist: 'A', album: 'Z'),
      row('b1', [1, 0.5], artist: 'B', album: 'W'),
    ]);
    final names = g.walk(0, length: 10).map((i) => g.paths[i]).toList();
    // sameAlbum: album cap 1 (seed). a3: artist cap 2 (seed + a2).
    expect(names, ['a2', 'b1']);
  });

  test('walk honours avoid and length', () {
    final g = CoordGraph.fromRows([
      for (var i = 0; i < 20; i++) row('t$i', [1, i / 20], artist: 'ar$i'),
    ]);
    final out = g.walk(0, length: 5, avoid: {1, 2});
    expect(out.length, 5);
    expect(out, isNot(contains(1)));
    expect(out, isNot(contains(2)));
    expect(out.first, 3);
  });
}
