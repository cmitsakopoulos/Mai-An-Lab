import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/stations/station.dart';

import 'fixtures.dart';

StationRow row(String path, List<double> v, {String? artist, String? album}) =>
    StationRow(path: path, coords: f32Blob(v), artist: artist, album: album);

StationSource build(List<StationRow> rows) =>
    StationSource.build(StationData(rows: rows));

void main() {
  test('decodes float32 blobs and L2-normalises rows', () {
    final g = build([
      row('a', [3, 4]),
      row('b', [0, 2]),
    ]);
    expect(g.dim, 2);
    expect(g.xUnit[0], closeTo(0.6, 1e-6));
    expect(g.xUnit[1], closeTo(0.8, 1e-6));
    expect(g.xUnit[3], closeTo(1.0, 1e-6));
  });

  test('skips rows whose dimension disagrees', () {
    final g = build([
      row('a', [1, 0, 0]),
      row('b', [1, 0]), // stale-version blob
      row('c', [0, 1, 0]),
    ]);
    expect(g.paths, ['a', 'c']);
    expect(g.skipped, 1);
  });

  test('zero vector does not produce NaN', () {
    final g = build([
      row('a', [0, 0]),
      row('b', [1, 0]),
    ]);
    expect(g.xUnit.any((v) => v.isNaN), isFalse);
  });

  test('a seed without coordinates gives an empty station', () {
    final g = build([
      row('a', [1, 0], artist: 'A'),
      row('b', [0, 1], artist: 'B'),
    ]);
    final s = g.open('missing');
    expect(s.isEmpty, isTrue);
    expect(s.take(5), isEmpty);
  });

  test('untagged library: never the seed, never twice', () {
    final g = build([
      row('a1', [1, 0, 0], artist: 'A', album: 'x'),
      row('a2', [0.9, 0.1, 0], artist: 'A', album: 'x'),
      row('a3', [0.9, 0, 0.1], artist: 'A', album: 'y'),
      row('b1', [0.8, 0.2, 0], artist: 'B', album: 'z'),
      row('c1', [-1, 0, 0], artist: 'C', album: 'w'),
    ]);
    final walk = g.walk('a1', length: 10);
    expect(walk, isNotEmpty);
    expect(walk, isNot(contains('a1')));
    expect(walk.toSet().length, walk.length);
  });
}
