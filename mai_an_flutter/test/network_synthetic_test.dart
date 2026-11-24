// The Acoustic Network view against Python's output (PORT_MAP §0 rule 3):
// build_metadata_edges, the view's queries, and library.py's own graph
// builder and re-walk merge on a synthetic library.
//
//   ~/miniconda3/bin/python tool/export_network_goldens.py --synthetic-only
//   flutter test test/network_synthetic_test.dart
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/py_num.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/network_controller.dart';
import 'package:mai_an_lab/library/network_layout.dart';
import 'package:sqlite3/sqlite3.dart';

final golden =
    jsonDecode(File('test/goldens/network_synthetic.json').readAsStringSync())
        as Map<String, dynamic>;

/// Python's dumps of the view's rows and graph, for comparison.
List<Object?> pcaRow(NetworkTrackRow r) => [
  r.path,
  (r.coords ?? const []).take(2).toList(),
  r.playCount,
  r.title,
  r.duration,
  r.artist,
  r.album,
  r.genre,
];

List<Object?> neighborRow(NeighborRow n) => [
  n.path,
  n.weight,
  n.edgeKind,
  n.title,
  n.artist,
  n.album,
];

Map<String, Object?> layoutState(NetworkGraph g) => {
  'dims': [g.width, g.height, kNetPad],
  'proj': [g.focalX, g.focalY, g.scale],
  'nodes': [
    for (final n in g.nodes)
      {
        'path': n.path,
        'px': n.px,
        'py': n.py,
        'radius': n.radius,
        'color': colorHex(n.color),
        'is_seed': n.isSeed,
        'walk_step': n.walkStep,
        'title': n.title,
        'artist': n.artist,
        'album': n.album,
        'genre': n.genre,
        'play_count': n.playCount,
        'duration': n.duration,
      },
  ],
  'edges': [
    for (final e in g.edges) [e.src, e.dst, e.weight, e.kind.name],
  ],
  'selected': g.selected,
  'walk_seq': g.walkSeq,
  'walk_seed': g.walkSeedPath,
  'local': g.localPaths.toList()..sort(),
};

/// A JSON number may come back as an int (Python's 20.0 is written 20.0,
/// but 0 stays 0): compare numbers as doubles.
Object? norm(Object? v) => switch (v) {
  final int i => i.toDouble(),
  final double d => d,
  final List<Object?> l => [for (final x in l) norm(x)],
  final Map<String, Object?> m => {
    for (final e in m.entries) e.key: norm(e.value),
  },
  _ => v,
};

/// Python's builder leaves `_net_proj` and `_net_dims` set even when it
/// draws nothing; the port returns no graph then.
bool pyEmpty(Map<String, dynamic> layout) => (layout['nodes'] as List).isEmpty;

Uint8List blob(List<dynamic> xy) {
  final f = Float32List(8)..fillRange(0, 8, 0.25);
  for (var i = 0; i < xy.length; i++) {
    f[i] = (xy[i] as num).toDouble();
  }
  return f.buffer.asUint8List();
}

/// tool/export_network_goldens.py `_setup`, from the exported library.
List<(String, List<Object?>)> setup() {
  final out = <(String, List<Object?>)>[];
  var t = 1700000000;
  for (final (ai0, a) in (golden['library'] as List).indexed) {
    final ai = ai0 + 1;
    final [artist, album, genre, tracks] = a as List;
    out.add(('INSERT INTO artists (id, name) VALUES (?, ?)', [ai, artist]));
    out.add((
      'INSERT INTO albums (id, artist_id, title, genre) VALUES (?, ?, ?, ?)',
      [ai, ai, album, genre],
    ));
    for (final (n0, tr) in (tracks as List).indexed) {
      final n = n0 + 1;
      final [title, path, coords, pc] = tr as List;
      t++;
      final added = artist == 'Delta' ? 1700000000 : t;
      out.add((
        'INSERT INTO tracks (album_id, title, track_num, duration, path, '
            "format, added_date) VALUES (?, ?, ?, ?, ?, 'FLAC', ?)",
        [ai, title, n, 180.5 + n, path, added],
      ));
      if (pc != null) {
        out.add((
          'INSERT INTO play_counts (track_path, count, pca_coords) '
              'VALUES (?, ?, ?)',
          [path, pc, if (coords == null) null else blob(coords as List)],
        ));
      }
    }
  }
  return out;
}

void main() {
  late Directory dir;
  late String path;
  late LibraryRepository repo;

  test('pyHypot is math.hypot', () {
    for (final c in (golden['hypot'] as List).cast<List<dynamic>>()) {
      final x = (c[0] as num).toDouble(), y = (c[1] as num).toDouble();
      expect(pyHypot(x, y), (c[2] as num).toDouble(), reason: '$x, $y');
    }
  });

  group('on the synthetic library', () {
    setUpAll(() async {
      dir = Directory.systemTemp.createTempSync('network');
      path = '${dir.path}/library.db';
      // The second open adds play_counts.cluster_id, as in Flet.
      await (await LibraryRepository.open(path, writable: true)).close();
      repo = await LibraryRepository.open(path, writable: true);
      expect(await repo.hasPcaCoords(), golden['has_pca_empty']);
      await repo.debugExecute(setup());
    });
    tearDownAll(() async {
      await repo.close();
      dir.deleteSync(recursive: true);
    });

    List<List<Object?>> edges() {
      final db = sqlite3.open(path, mode: OpenMode.readOnly);
      try {
        return [
          for (final r in db.select(
            'SELECT track_path, neighbor_path, weight, edge_kind '
            'FROM track_neighbors ORDER BY track_path, neighbor_path, edge_kind',
          ))
            [for (var i = 0; i < 4; i++) r.columnAt(i)],
        ];
      } finally {
        db.close();
      }
    }

    test(
      'build_metadata_edges, then get_neighbors and the coordinates',
      () async {
        final k3 = golden['edges_k3'] as Map<String, dynamic>;
        expect(await repo.buildMetadataEdges(3), (k3['count'] as List)[0]);
        expect(norm(edges()), norm(k3['rows']));
        final k30 = golden['edges_k30'] as Map<String, dynamic>;
        expect(await repo.buildMetadataEdges(), (k30['count'] as List)[0]);
        expect(norm(edges()), norm(k30['rows']));
        await repo.debugExecute([
          for (final e in (golden['extra_edges'] as List).cast<List<dynamic>>())
            ('INSERT OR REPLACE INTO track_neighbors VALUES (?, ?, ?, ?)', e),
        ]);
        await repo.buildMetadataEdges();
        expect(norm(edges()), norm(golden['edges_final']));

        expect(await repo.hasPcaCoords(), golden['has_pca']);
        for (final MapEntry(:key, :value)
            in (golden['neighbors'] as Map<String, dynamic>).entries) {
          final [p, k] = key.split('|');
          final got = await repo.networkNeighbors(p, int.parse(k));
          expect(
            norm([for (final n in got) neighborRow(n)]),
            norm(value),
            reason: key,
          );
        }
        for (final c
            in (golden['pca_for_paths'] as List).cast<Map<String, dynamic>>()) {
          final got = await repo.pcaCoordsForPaths(
            (c['paths'] as List).cast<String>(),
          );
          expect(
            norm([for (final r in got) pcaRow(r)]),
            norm(c['rows']),
            reason: '${c['paths']}',
          );
        }
      },
    );

    test('the view builds and re-walks the graph as library.py does', () async {
      for (final s
          in (golden['scenarios'] as List).cast<Map<String, dynamic>>()) {
        final name = s['name'] as String;
        final page = (s['page'] as List).cast<num>();
        final (w, h) = netCanvasSize(page[0].toDouble(), page[1].toDouble());
        final given = (s['walk'] as List).cast<String>();
        final b = await buildNetwork(
          repo,
          seedPath: s['seed'] as String?,
          nowPlaying: s['now_playing'] as String,
          selected: s['selected_in'] as String?,
          density: s['k'] as int,
          live: false,
          walk: (_) async => given,
          width: w,
          height: h,
        );
        expect(b.current, s['current_path'], reason: name);
        expect(b.walkSeed, s['walk_seed'], reason: name);
        expect(
          norm([for (final n in b.neighbors) neighborRow(n)]),
          norm(s['neighbors']),
          reason: name,
        );
        expect(b.needed, s['needed'], reason: name);
        expect(
          norm([for (final r in b.rows) pcaRow(r)]),
          norm(s['pca_rows']),
          reason: name,
        );
        expect('${b.rows.length} TRACKS', s['stats'], reason: name);
        final layout = s['layout'] as Map<String, dynamic>;
        if (pyEmpty(layout)) {
          expect(b.graph, isNull, reason: name);
          continue;
        }
        final g = b.graph!;
        expect(norm(layoutState(g)), norm(layout), reason: name);
        for (final (i, r)
            in (s['rewalks'] as List).cast<Map<String, dynamic>>().indexed) {
          final seq = (r['seq'] as List).cast<String>();
          final missing = [
            for (final p in seq)
              if (!g.byPath.containsKey(p)) p,
          ];
          expect(missing, r['missing'], reason: '$name, re-walk $i');
          final rows = missing.isEmpty
              ? <NetworkTrackRow>[]
              : await repo.pcaCoordsForPaths(missing);
          expect(
            norm([for (final x in rows) pcaRow(x)]),
            norm(r['new_rows']),
            reason: '$name, re-walk $i',
          );
          g.applyWalk(r['seed'] as String, seq, rows);
          expect(
            norm(layoutState(g)),
            norm(r['layout']),
            reason: '$name, re-walk $i',
          );
        }
      }
    });
  });
}
