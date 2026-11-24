// The Acoustic Network view on a real library against Python's output
// (PORT_MAP §0 rule 3): build_metadata_edges, and for fixed seeds at each
// density the neighbours, the walk, library.py's layout and a re-walk.
// Personal data, so the goldens sit beside the gitignored bundles:
//
//   ~/miniconda3/bin/python tool/export_station_goldens.py   # copies library.db
//   ~/miniconda3/bin/python tool/export_network_goldens.py   # writes network.json
//   flutter test test/network_real_golden_test.dart
//
// NETWORK_GOLDEN=/path/to/network.json picks a specific export. Skipped
// when there is none.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/network_controller.dart';
import 'package:mai_an_lab/library/network_layout.dart';
import 'package:mai_an_lab/stations/station.dart';
import 'package:sqlite3/sqlite3.dart';

import 'network_synthetic_test.dart'
    show layoutState, neighborRow, norm, pcaRow;

File? _golden() {
  final env = Platform.environment['NETWORK_GOLDEN'];
  if (env != null) return File(env);
  final dir = Directory('../tools/state_backups/goldens');
  if (!dir.existsSync()) return null;
  final found = [
    for (final d in dir.listSync().whereType<Directory>())
      if (File('${d.path}/network.json').existsSync())
        File('${d.path}/network.json'),
  ]..sort((a, b) => b.path.compareTo(a.path));
  return found.firstOrNull;
}

void main() {
  final file = _golden();
  if (file == null) {
    test('network goldens', () {}, skip: 'no exported goldens');
    return;
  }
  final golden = jsonDecode(file.readAsStringSync()) as Map<String, dynamic>;
  final dbPath = '${file.parent.path}/${golden['db']}';

  test('build_metadata_edges writes the artist tier Python writes', () async {
    final dir = Directory.systemTemp.createTempSync('network_real');
    addTearDown(() => dir.deleteSync(recursive: true));
    final copy = '${dir.path}/library.db';
    File(dbPath).copySync(copy);
    final repo = await LibraryRepository.open(copy, writable: true);
    final want = golden['metadata_edges'] as Map<String, dynamic>;
    expect(await repo.buildMetadataEdges(), (want['count'] as List)[0]);
    await repo.close();
    final db = sqlite3.open(copy, mode: OpenMode.readOnly);
    addTearDown(db.close);
    final got = [
      for (final r in db.select(
        "SELECT track_path, neighbor_path FROM track_neighbors WHERE edge_kind = 'artist' "
        'ORDER BY track_path, neighbor_path, edge_kind',
      ))
        [r.columnAt(0), r.columnAt(1)],
    ];
    expect(got, want['rows']);
  });

  group('the view on fixed seeds', () {
    late LibraryRepository repo;
    late StationSource stations;
    setUpAll(() async {
      repo = await LibraryRepository.open(dbPath);
      stations = await repo.stations();
    });
    tearDownAll(() => repo.close());

    test('neighbours, walk, layout and re-walk match', () async {
      final (w, h) = netCanvasSize(360, 640);
      for (final c in (golden['cases'] as List).cast<Map<String, dynamic>>()) {
        final reason = '${c['seed']} at ${c['k']}';
        final b = await buildNetwork(
          repo,
          seedPath: c['seed'] as String,
          nowPlaying: '',
          selected: null,
          density: c['k'] as int,
          live: false,
          walk: (seed) async => stations.walk(seed, length: 10),
          width: w,
          height: h,
        );
        expect(
          norm([for (final n in b.neighbors) neighborRow(n)]),
          norm(c['neighbors']),
          reason: reason,
        );
        expect(b.walk, c['walk'], reason: reason);
        expect(
          norm([for (final r in b.rows) pcaRow(r)]),
          norm(c['pca_rows']),
          reason: reason,
        );
        final g = b.graph!;
        expect(norm(layoutState(g)), norm(c['layout']), reason: reason);
        for (final r in (c['rewalks'] as List).cast<Map<String, dynamic>>()) {
          final from = r['seed'] as String;
          final seq = stations.walk(from, length: 15);
          expect(seq, r['seq'], reason: '$reason, re-walk from $from');
          final missing = [
            for (final p in seq)
              if (!g.byPath.containsKey(p)) p,
          ];
          final rows = missing.isEmpty
              ? <NetworkTrackRow>[]
              : await repo.pcaCoordsForPaths(missing);
          g.applyWalk(from, seq, rows);
          expect(
            norm(layoutState(g)),
            norm(r['layout']),
            reason: '$reason, re-walk from $from',
          );
        }
      }
    });
  });
}
