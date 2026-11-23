// Stations on a real library against the Python app's output (PORT_MAP §0
// rule 3). The goldens and the database they were computed from are personal
// data, so they live beside the gitignored state bundles:
//
//   ~/miniconda3/bin/python tool/export_station_goldens.py   # writes them
//   flutter test test/station_real_golden_test.dart
//
// STATION_GOLDEN=/path/to/stations.json picks a specific export. Skipped when
// there is none.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/stations/station.dart';

File? _golden() {
  final env = Platform.environment['STATION_GOLDEN'];
  if (env != null) return File(env);
  final dir = Directory('../tools/state_backups/goldens');
  if (!dir.existsSync()) return null;
  final found = [
    for (final d in dir.listSync().whereType<Directory>())
      if (File('${d.path}/stations.json').existsSync())
        File('${d.path}/stations.json'),
  ]..sort((a, b) => b.path.compareTo(a.path));
  return found.firstOrNull;
}

void main() {
  final file = _golden();
  if (file == null) {
    test('station goldens', () {}, skip: 'no exported goldens');
    return;
  }
  final golden = jsonDecode(file.readAsStringSync()) as Map<String, dynamic>;
  late StationSource src;
  late LibraryRepository repo;

  setUpAll(() async {
    repo = await LibraryRepository.open('${file.parent.path}/${golden['db']}');
    src = await repo.stations();
  });
  tearDownAll(() => repo.close());

  test('artist graph matches', () {
    final g = src.graph;
    expect(src.length, golden['paths']);
    expect(g.hasTags, golden['has_tags']);
    expect(g.tags, golden['tags']);
    final units = golden['units'] as List;
    expect(g.units, [for (final u in units) u['name']]);
    for (var u = 0; u < units.length; u++) {
      final want = units[u] as Map;
      expect(g.culture[u], want['culture'], reason: g.units[u]);
      expect(g.inferred[u], want['inferred'], reason: g.units[u]);
      expect(g.namesake[u], want['namesake'], reason: g.units[u]);
      expect(g.tracksOf[u].length, want['tracks'], reason: g.units[u]);
    }
    (golden['rows'] as Map).forEach((u, row) {
      final got = g.row(int.parse(u as String));
      for (var v = 0; v < got.length; v++) {
        expect(
          got[v],
          closeTo((row as List)[v] as num, 1e-5),
          reason: '${g.units[int.parse(u)]} ~ ${g.units[v]}',
        );
      }
    });
  });

  for (final c in golden['cases'] as List) {
    final seed = c['seed'] as String;
    test('Play Similar walk: ${seed.split('/').last}', () {
      expect(src.walk(seed, length: 20), c['walk']);
    });
    test('auto-play station: ${seed.split('/').last}', () {
      final st = src.open(
        seed,
        avoid: [seed],
        focused: true,
        maxPerArtist: 5,
        maxPerAlbum: 1,
        window: 24,
      );
      for (final s in c['station'] as List) {
        switch (s['op']) {
          case 'take':
            expect(st.take(s['n'] as int), s['out']);
            expect(st.flavour, s['flavour']);
          case 'accept':
            st.accept(s['path'] as String);
          case 'reject':
            st.reject([for (final p in s['paths'] as List) p as String]);
        }
      }
    });
  }
}
