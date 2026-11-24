// The acoustic build against Python's output (PORT_MAP §0 rule 3):
// track_graph.build_acoustic_edges, pca_engine.redundant_raw_features and
// genre_similarity.build_npmi_model on synthetic libraries.
//
//   ~/miniconda3/bin/python tool/export_acoustic_goldens.py --synthetic-only
//   flutter test test/acoustic_build_test.dart
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/py_json.dart';
import 'package:mai_an_lab/core/py_num.dart';
import 'package:mai_an_lab/data/graph_build.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/stations/acoustic_geometry.dart';
import 'package:mai_an_lab/stations/genre_npmi.dart';
import 'package:mai_an_lab/sync/companion_client.dart' show FeatureRow;
import 'package:sqlite3/sqlite3.dart';

final golden =
    jsonDecode(File('test/goldens/acoustic_synthetic.json').readAsStringSync())
        as Map<String, dynamic>;

Uint8List _hex(String h) => Uint8List.fromList([
  for (var i = 0; i < h.length; i += 2)
    int.parse(h.substring(i, i + 2), radix: 16),
]);

Object? _arg(Object? v) => switch (v) {
  {'blob': final String h} => _hex(h),
  _ => v,
};

/// The export's `fnv1a64` over float32 little-endian bytes.
String fnv1a64(Uint8List bytes) {
  var h = BigInt.parse('cbf29ce484222325', radix: 16);
  final prime = BigInt.parse('100000001b3', radix: 16);
  final mask = (BigInt.one << 64) - BigInt.one;
  for (final b in bytes) {
    h = ((h ^ BigInt.from(b)) * prime) & mask;
  }
  return h.toRadixString(16).padLeft(16, '0');
}

Uint8List f32Bytes(Float64List z) =>
    Float32List.fromList(z).buffer.asUint8List();

/// A fresh database migrated as the app migrates it (twice, as a second
/// start does), with [setup] applied.
Future<(Database, Directory)> openScenario(List<dynamic> setup) async {
  final dir = Directory.systemTemp.createTempSync('acoustic');
  final path = '${dir.path}/library.db';
  await (await LibraryRepository.open(path, writable: true)).close();
  await (await LibraryRepository.open(path, writable: true)).close();
  final db = sqlite3.open(path);
  for (final s in setup.cast<List<dynamic>>()) {
    db.execute(s[0] as String, [for (final a in s[1] as List) _arg(a)]);
  }
  return (db, dir);
}

Map<String, List<double>> storedCoords(Database db) => {
  for (final r in db.select(
    'SELECT track_path, pca_coords FROM play_counts '
    'WHERE pca_coords IS NOT NULL ORDER BY track_path',
  ))
    r['track_path'] as String: Float32List.sublistView(
      r['pca_coords'] as Uint8List,
    ).toList(),
};

double _cos(List<double> a, List<double> b) {
  var dot = 0.0, na = 0.0, nb = 0.0;
  for (var i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return dot / math.sqrt(na * nb);
}

/// Compares the port's coordinates with Python's: the same tracks and
/// dimensions; each axis equal up to its sign (LAPACK's is arbitrary)
/// within float32 SVD precision; every cosine within [cosTol]. Returns the
/// largest coordinate difference.
double expectCoords(
  Map<String, List<double>> got,
  Map<String, dynamic> want, {
  required Set<String> built,
  double cosTol = 1e-5,
  int cosineSample = 400,
}) {
  expect(got.keys.toList(), want.keys.toList());
  final paths = [
    for (final p in got.keys)
      if (built.contains(p)) p,
  ];
  for (final p in got.keys.where((p) => !built.contains(p))) {
    // Rows the build skipped keep what they had.
    expect(got[p], [for (final v in want[p] as List) (v as num).toDouble()]);
  }
  if (paths.isEmpty) return 0;
  final dims = got[paths.first]!.length;
  final py = {
    for (final p in paths)
      p: [for (final v in want[p] as List) (v as num).toDouble()],
  };
  expect(py[paths.first]!.length, dims);
  var scale = 0.0;
  for (final p in paths) {
    for (final v in py[p]!) {
      scale = math.max(scale, v.abs());
    }
  }
  final signs = [
    for (var c = 0; c < dims; c++)
      paths.fold<double>(0, (s, p) => s + got[p]![c] * py[p]![c]) < 0
          ? -1.0
          : 1.0,
  ];
  var worst = 0.0;
  for (final p in paths) {
    for (var c = 0; c < dims; c++) {
      worst = math.max(worst, (got[p]![c] * signs[c] - py[p]![c]).abs());
    }
  }
  expect(worst, lessThanOrEqualTo(2e-4 * math.max(scale, 1) + 1e-5));
  final step = math.max(1, paths.length ~/ cosineSample);
  for (var i = 0; i < paths.length; i += step) {
    for (var j = i + 1; j < paths.length; j += step) {
      final a = paths[i], b = paths[j];
      expect(
        (_cos(got[a]!, got[b]!) - _cos(py[a]!, py[b]!)).abs(),
        lessThanOrEqualTo(cosTol),
        reason: '$a · $b',
      );
    }
  }
  return worst;
}

/// Runs one exported scenario through the port and checks it.
Future<void> checkScenario(
  Map<String, dynamic> want,
  Database db, {
  double cosTol = 1e-5,
}) async {
  final kwargs = want['kwargs'] as Map<String, dynamic>;
  final zScore = kwargs['z_score'] as bool? ?? true;
  final weight = (kwargs['scalar_weight'] as num?)?.toDouble() ?? 1.5;
  final rows = tracksWithFeatures(db, kFeaturesVersion);
  expect(
    redundantRawFeatures(rows).toList()..sort(),
    want['redundant'],
    reason: 'redundant_raw_features',
  );

  Float64List? z;
  var zn = 0, zd = 0;
  final g = buildAcousticGeometry(
    rows,
    zScore: zScore,
    scalarWeight: weight,
    onMatrix: (m, n, d) => (z, zn, zd) = (m, n, d),
  );
  if (want['z_shape'] == null) {
    expect(g, isNull);
  } else {
    // numpy's float32 Z, bit for bit.
    expect([zn, zd], want['z_shape']);
    expect(fnv1a64(f32Bytes(z!)), want['z_fnv'], reason: 'Z matrix bytes');
    double eig(Object? s) => (s! as num).toDouble() * (s as num) / (zn - 1);
    final f32 = [for (final s in want['singular_values'] as List) eig(s)];
    final f64 = [for (final s in want['singular_values_f64'] as List) eig(s)];
    expect(g!.eigenvalues.length, f64.length);
    for (var i = 0; i < f64.length; i++) {
      // The float64 SVD of the same matrix, to rounding; numpy's float32
      // one to its precision (about 1e-4 of the largest).
      expect(
        (g.eigenvalues[i] - f64[i]).abs(),
        lessThanOrEqualTo(1e-9 * f64.first + 1e-12),
        reason: 'eigenvalue $i (float64)',
      );
      expect(
        (g.eigenvalues[i] - f32[i]).abs(),
        lessThanOrEqualTo(2e-4 * f32.first + 1e-6),
        reason: 'eigenvalue $i (float32)',
      );
    }
  }

  final res = buildAcousticEdgesJob(zScore: zScore, scalarWeight: weight)(db);
  expect(res.tracks, want['returned']);
  expectCoords(
    storedCoords(db),
    want['coords'] as Map<String, dynamic>,
    built: {...?g?.paths},
    cosTol: cosTol,
  );
  final model = db.select('SELECT model FROM genre_affinity WHERE id = 1');
  expect(
    model.isEmpty ? null : model.first['model'],
    want['model'],
    reason: 'genre_affinity.model',
  );
}

void main() {
  final setups = golden['setups'] as Map<String, dynamic>;
  final scenarios = golden['scenarios'] as Map<String, dynamic>;
  for (final MapEntry(key: name, value: s) in scenarios.entries) {
    final want = s as Map<String, dynamic>;
    test('$name: as build_acoustic_edges', () async {
      final (db, dir) = await openScenario(
        setups[want['setup']] as List<dynamic>,
      );
      addTearDown(() {
        db.close();
        dir.deleteSync(recursive: true);
      });
      await checkScenario(want, db);
    });
  }

  group('LibraryController', () {
    /// A scenario's database, closed, for the controller to open.
    Future<(String, Directory)> scenarioDb(String setup) async {
      final (db, dir) = await openScenario(setups[setup] as List<dynamic>);
      db.close();
      return ('${dir.path}/library.db', dir);
    }

    int count(String path, String sql) {
      final db = sqlite3.open(path, mode: OpenMode.readOnly);
      try {
        return db.select(sql).first.values.first as int;
      } finally {
        db.close();
      }
    }

    Future<StationsReady> loaded(String path) async {
      final c = LibraryController(
        () => LibraryRepository.open(path, writable: true),
      );
      await c.load();
      return StationsReady(c);
    }

    test('the readiness check builds a graph that is missing, once', () async {
      final (path, dir) = await scenarioDb('small');
      addTearDown(() => dir.deleteSync(recursive: true));
      // Any coordinates at all count as built (graph_status' coord_tracks
      // counts play_counts rows): the stale row's keep it quiet.
      final s0 = await loaded(path);
      expect(await s0.c.ensureGraphBuilt(delay: Duration.zero), isFalse);
      await s0.close();
      final db = sqlite3.open(path);
      db.execute('UPDATE play_counts SET pca_coords = NULL');
      db.close();

      final s = await loaded(path);
      addTearDown(s.close);
      expect(s.c.stationsAvailable, isFalse);
      expect(await s.c.ensureGraphBuilt(delay: Duration.zero), isTrue);
      await s.graphChange();
      expect(s.c.graph?.length, 30);
      expect(
        count(
          path,
          'SELECT COUNT(*) FROM play_counts WHERE pca_coords IS NOT NULL',
        ),
        30,
      );
      expect(
        count(
          path,
          "SELECT COUNT(*) FROM track_neighbors WHERE edge_kind = 'artist'",
        ),
        greaterThan(0),
      );
      // Coordinates exist now: nothing to do.
      expect(await s.c.ensureGraphBuilt(delay: Duration.zero), isFalse);
    });

    test(
      'the readiness check leaves a library with one analysed track',
      () async {
        final (path, dir) = await scenarioDb('three');
        addTearDown(() => dir.deleteSync(recursive: true));
        // Two of the three analysed tracks lose their features.
        final db = sqlite3.open(path);
        db.execute(
          'UPDATE play_counts SET features_version = 4 WHERE rowid IN '
          '(SELECT rowid FROM play_counts LIMIT 2)',
        );
        db.close();
        final s = await loaded(path);
        addTearDown(s.close);
        expect(await s.c.ensureGraphBuilt(delay: Duration.zero), isFalse);
        expect(
          count(
            path,
            'SELECT COUNT(*) FROM play_counts WHERE pca_coords IS NOT NULL',
          ),
          0,
        );
      },
    );

    test('Recompute PCA leaves the artist tier alone', () async {
      final (path, dir) = await scenarioDb('small');
      addTearDown(() => dir.deleteSync(recursive: true));
      final s = await loaded(path);
      addTearDown(s.close);
      final gen = s.c.dataGeneration;
      final res = await s.c.rebuildSimilarityGraph(metadata: false);
      expect(res.tracks, 30);
      expect(s.c.dataGeneration, gen + 1);
      expect(count(path, 'SELECT COUNT(*) FROM track_neighbors'), 0);
      await s.graphChange();
      expect(s.c.graph?.length, 30);
    });

    test('the offload\'s work list and its writes', () async {
      final (path, dir) = await scenarioDb('library');
      addTearDown(() => dir.deleteSync(recursive: true));
      final s = await loaded(path);
      addTearDown(s.close);
      final missing = await s.c.tracksMissingFeatures();
      // get_tracks_missing_features: the stale, versionless, malformed and
      // timbre-less rows, and the track never analysed (Python's list).
      expect(missing, [
        '/m/extra/0.flac',
        '/m/extra/1.flac',
        '/m/extra/2.flac',
        '/m/extra/5.flac',
        '/m/extra/unplayed.flac',
      ]);
      await s.c.storeFeatureRows([
        FeatureRow(
          path: '/m/extra/unplayed.flac',
          bpm: 99,
          energy: 0.25,
          brightness: 0.5,
          rolloff: 0.5,
          beatStrength: 0.5,
          spectralFlatness: null,
          spectralContrast: 0.5,
          keyIndex: 7,
          timbre: Uint8List(272),
          featuresVersion: 5,
        ),
        FeatureRow(
          path: '/m/extra/0.flac',
          bpm: 98,
          energy: 0.5,
          brightness: 0.5,
          rolloff: 0.5,
          beatStrength: 0.5,
          spectralFlatness: 0.5,
          spectralContrast: 0.5,
          keyIndex: 1,
          timbre: Uint8List(0),
          featuresVersion: 5,
        ),
      ]);
      expect(await s.c.tracksMissingFeatures(), [
        '/m/extra/1.flac',
        '/m/extra/2.flac',
        '/m/extra/5.flac',
      ]);
      final db = sqlite3.open(path, mode: OpenMode.readOnly);
      addTearDown(db.close);
      final r = db.select(
        "SELECT count, bpm, spectral_flatness, key_index, length(timbre) AS n, "
        "features_version, pca_coords IS NOT NULL AS has FROM play_counts "
        "WHERE track_path IN ('/m/extra/unplayed.flac', '/m/extra/0.flac') "
        'ORDER BY track_path',
      );
      expect(
        [for (final x in r) x.values],
        [
          // An existing row keeps its count and coordinates; an empty timbre
          // is an empty BLOB (client.py `_row`), which counts as analysed.
          [0, 98.0, 0.5, 1, 0, 5, 1],
          [0, 99.0, null, 7, 272, 5, 0],
        ],
      );
    });
  });

  group('pca_engine._redundant_features_from', () {
    List<List<double>> corr(Map<(int, int), double> r) => List.generate(
      7,
      (i) =>
          List.generate(7, (j) => i == j ? 1.0 : (r[(i, j)] ?? r[(j, i)] ?? 0)),
    );
    test('keeps the most central of a cluster', () {
      // energy (2) correlates with bpm (0) and brightness (1); they barely
      // with each other.
      expect(
        redundantFromCorrelation(
          corr({(0, 2): 0.9, (1, 2): 0.9, (0, 1): 0.75}),
        ),
        {'bpm', 'brightness'},
      );
      // Centrality is the mean |r| to a feature's mates, not its mate
      // count: bpm (0.9 to its one mate) outranks energy (0.85 to two),
      // so bpm stays and takes energy with it; brightness's only mate is
      // gone.
      expect(
        redundantFromCorrelation(corr({(0, 2): 0.9, (1, 2): 0.8, (0, 1): 0.5})),
        {'energy'},
      );
    });
    test('a two-feature tie keeps the lower index', () {
      expect(redundantFromCorrelation(corr({(3, 5): 0.91})), {
        'spectral_flatness',
      });
      // Not a tie: the more central one stays, whatever its index.
      final c = corr({(3, 5): 0.91});
      c[5][3] = 0.91 + 1e-16;
      expect(redundantFromCorrelation(c), {'rolloff'});
    });
    test('NaN is no correlation; below the threshold keeps both', () {
      final c = corr({(0, 1): 0.69, (2, 3): -0.7});
      c[4][6] = c[6][4] = double.nan;
      expect(redundantFromCorrelation(c), {'rolloff'});
    });
  });

  group('genre_similarity.build_npmi_model', () {
    // tests/test_genre_metadata_integrity.py TestNpmiEvidenceGating.
    test('a single-artist pair is not certainty', () {
      final corpus = [
        {'countryrock', 'swamprock'},
        {'hiphop'},
        {'metal'},
        {'jazz'},
      ];
      expect(
        buildNpmiModel(
          corpus,
          minSupport: 1,
          shrinkageK: 0,
        )['countryrock|swamprock'],
        1.0,
      );
      expect(buildNpmiModel(corpus), isNot(contains('countryrock|swamprock')));
    });
    test('repeated evidence survives, shrunk', () {
      final m = buildNpmiModel([
        for (var i = 0; i < 10; i++) {'hiphop', 'trap'},
        {'jazz'},
        {'metal'},
      ]);
      expect(m['hiphop|trap'], allOf(greaterThan(0.5), lessThan(1.0)));
    });
    test('no saturated pairs under default gating', () {
      final m = buildNpmiModel([
        {'a', 'b'},
        {'a', 'b'},
        {'c', 'd'},
        {'c', 'd'},
        {'e'},
      ]);
      expect(m.values.every((v) => v < 0.999), isTrue);
    });
    test('pairs ordered by code point, as sorted()', () {
      // U+FF21 sorts before U+1D400 in UTF-16 units, after it by code point.
      final m = buildNpmiModel([
        for (var i = 0; i < 3; i++) {'\u{1D400}', 'Ａ'},
        {'x'},
      ]);
      expect(m.keys, ['Ａ|\u{1D400}']);
    });
  });

  group('Python helpers', () {
    test('round(x, 4)', () {
      // Python: round(0.00005, 4), round(0.12345, 4), round(2.675, 2) ...
      expect(pyRound(0.00005, 4), 0.0001);
      expect(pyRound(0.12345, 4), 0.1235);
      expect(pyRound(0.12355, 4), 0.1235);
      expect(pyRound(2.675, 2), 2.67);
      expect(pyRound(0.5, 0), 0.0);
      expect(pyRound(-1.00005, 4), -1.0001);
    });
    test('json.dumps', () {
      expect(pyJsonDumps(<String, double>{}), '{}');
      expect(
        pyJsonDumps({
          'a\x7f': 1.0,
          '\u03bb\u{1F600}': [null, true, 1e-5, 12345678901234567.0, 3],
          'q"\\\n': 0.1,
        }),
        r'{"a\u007f": 1.0, "\u03bb\ud83d\ude00": [null, true, 1e-05, '
        r'1.2345678901234568e+16, 3], "q\"\\\n": 0.1}',
      );
    });
  });
}

/// Waits for the controller's next station-graph change (the rebuild after
/// a reload runs behind it).
class StationsReady {
  StationsReady(this.c);
  final LibraryController c;

  /// The database closed before the folder is deleted (dispose alone
  /// doesn't wait, and SQLite removes its -wal and -shm files on close).
  Future<void> close() async {
    await c.repository?.close();
    c.dispose();
  }

  Future<void> graphChange() async {
    final before = c.graph;
    for (var i = 0; i < 200 && identical(c.graph, before); i++) {
      await Future<void>.delayed(const Duration(milliseconds: 10));
    }
  }
}
