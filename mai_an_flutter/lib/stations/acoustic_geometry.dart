import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/foundation.dart' show visibleForTesting;

/// The acoustic geometry: track_graph.py `build_acoustic_edges`' maths and
/// pca_engine.py's redundancy cleaver, without the database (graph_build.dart
/// reads the rows and stores the result).
///
/// Parity. Everything up to the SVD is numpy's float32 arithmetic, op for
/// op (numpy reduces a float32 matrix down axis 0 as one sequential float32
/// sum per column; checked bit for bit), so the matrix that enters the SVD
/// is Python's. numpy then runs LAPACK's float32 SVD; the port takes the
/// eigenvectors of ZᵀZ in float64 (cyclic Jacobi), which agrees with it to
/// float32 precision: on the real 1,216-track library the coordinates are
/// within 2.4e-4 (largest 11.6) and every cosine within 1.3e-6. LAPACK's
/// per-component sign is arbitrary (it differs between numpy builds); the
/// port fixes it the way scikit-learn's `svd_flip` does (each component's
/// largest loading positive). Cosine and distances, all the stations use,
/// don't depend on it; the Network view's map can come out mirrored next to
/// a Python-built one (PORT_MAP §7).

/// `dsp.GRAPH_EMBED_DIMS`: the v5 timbre BLOB, 68 float32.
const kGraphEmbedDims = 68;

/// `track_graph._SCALAR_ORDER` and `pca_engine._RAW_FEATURES` (the same
/// list): the scalar descriptors after the timbre block.
const kScalarOrder = [
  'bpm',
  'brightness',
  'energy',
  'rolloff',
  'beat_strength',
  'spectral_flatness',
  'spectral_contrast',
];

/// One `get_tracks_with_features` row: the path, the timbre BLOB and the
/// seven scalars in [kScalarOrder] (bpm raw, not log2; COALESCEd to 0).
class FeatureInput {
  const FeatureInput(this.path, this.timbre, this.scalars);
  final String path;
  final Uint8List? timbre;
  final List<double> scalars;
}

/// What a build produced.
class AcousticGeometry {
  const AcousticGeometry({
    required this.paths,
    required this.coords,
    required this.dims,
    required this.redundant,
    required this.eigenvalues,
  });

  /// The projected tracks, in row order.
  final List<String> paths;

  /// `Zr`, row-major float32: `coords[i * dims + c]`.
  final Float32List coords;
  final int dims;

  /// The scalars the covariance analysis cleaved.
  final Set<String> redundant;

  /// `S² / (N - 1)`, descending (the Kaiser criterion's values).
  final List<double> eigenvalues;

  Float32List row(int i) =>
      Float32List.sublistView(coords, i * dims, (i + 1) * dims);
}

// ── float32 arithmetic ─────────────────────────────────────────────────────

final _f32 = Float32List(1);

/// Round to float32. One float64 op then this rounding is exactly the
/// float32 op for +, -, *, / and sqrt (53 ≥ 2·24 + 2).
double f32(double x) {
  _f32[0] = x;
  return _f32[0];
}

/// numpy's float32 `X.mean(axis=0)` and `X.std(axis=0)` for a row-major
/// N×D matrix of float32 values.
(Float32List mean, Float32List std) _meanStd(Float32List x, int n, int d) {
  final mean = Float32List(d);
  for (var i = 0; i < n; i++) {
    for (var j = 0; j < d; j++) {
      mean[j] = mean[j] + x[i * d + j];
    }
  }
  for (var j = 0; j < d; j++) {
    mean[j] = mean[j] / n;
  }
  final acc = Float32List(d);
  for (var i = 0; i < n; i++) {
    for (var j = 0; j < d; j++) {
      final dev = f32(x[i * d + j] - mean[j]);
      acc[j] = acc[j] + f32(dev * dev);
    }
  }
  final std = Float32List(d);
  for (var j = 0; j < d; j++) {
    std[j] = math.sqrt(f32(acc[j] / n));
  }
  return (mean, std);
}

// ── pca_engine.py ──────────────────────────────────────────────────────────

/// `_redundant_features_from`: centrality-based cleaving over
/// [kScalarOrder] of a Pearson matrix (NaN counts as 0); equal
/// centralities keep the lower index.
Set<String> redundantFromCorrelation(
  List<List<double>> corr, {
  double threshold = 0.70,
}) {
  final d = kScalarOrder.length;
  double c(int i, int j) {
    final v = corr[i][j];
    return v.isNaN ? 0 : v;
  }

  final mates = [
    for (var i = 0; i < d; i++)
      [
        for (var j = 0; j < d; j++)
          if (i != j && c(i, j).abs() >= threshold) j,
      ],
  ];
  final centrality = [
    for (var i = 0; i < d; i++)
      mates[i].isEmpty
          ? 0.0
          : mates[i].fold<double>(0, (s, j) => s + c(i, j).abs()) /
                mates[i].length,
  ];
  // sorted(key=(centrality, -i), reverse=True).
  final order = List.generate(d, (i) => i)
    ..sort((a, b) {
      final byCentrality = centrality[b].compareTo(centrality[a]);
      return byCentrality != 0 ? byCentrality : a - b;
    });
  final redundant = <String>{};
  final kept = <int>{};
  for (final i in order) {
    if (mates[i].isEmpty || redundant.contains(kScalarOrder[i])) continue;
    kept.add(i);
    for (final j in mates[i]) {
      if (kept.contains(j) || redundant.contains(kScalarOrder[j])) continue;
      redundant.add(kScalarOrder[j]);
    }
  }
  return redundant;
}

/// `redundant_raw_features`: the scalars collinear (|r| ≥ [threshold]) with
/// a more central one in this library. Empty below 50 rows.
///
/// The Pearson matrix is made exactly symmetric, (C + Cᵀ) / 2, before the
/// cleaving. In a two-scalar cluster both scalars' centrality is the same
/// |r|, but np.corrcoef divides it by the two deviations in opposite orders,
/// so numpy's two entries can differ in the last bit, and that bit (it
/// varies between BLAS builds) picks which scalar stays. Symmetric, it is a
/// tie, and the lower index stays. tool/export_acoustic_goldens.py pins
/// Python's cleaver the same way (PORT_MAP §7).
Set<String> redundantRawFeatures(
  List<FeatureInput> rows, {
  double threshold = 0.70,
}) {
  final n = rows.length, d = kScalarOrder.length;
  if (n < 50) return {};
  final x = Float32List(n * d);
  for (var i = 0; i < n; i++) {
    for (var j = 0; j < d; j++) {
      x[i * d + j] = rows[i].scalars[j];
    }
  }
  final (mean, std) = _meanStd(x, n, d);
  for (var j = 0; j < d; j++) {
    if (std[j] == 0) std[j] = 1;
  }
  // X_scaled (float32), then np.corrcoef in float64.
  final cols = [for (var j = 0; j < d; j++) Float64List(n)];
  for (var i = 0; i < n; i++) {
    for (var j = 0; j < d; j++) {
      cols[j][i] = f32(f32(x[i * d + j] - mean[j]) / std[j]);
    }
  }
  for (final col in cols) {
    var s = 0.0;
    for (final v in col) {
      s += v;
    }
    final avg = s / n;
    for (var i = 0; i < n; i++) {
      col[i] -= avg;
    }
  }
  final cov = List.generate(d, (_) => List<double>.filled(d, 0));
  for (var a = 0; a < d; a++) {
    for (var b = a; b < d; b++) {
      var s = 0.0;
      for (var i = 0; i < n; i++) {
        s += cols[a][i] * cols[b][i];
      }
      cov[a][b] = cov[b][a] = s * (1 / (n - 1));
    }
  }
  final sd = [for (var j = 0; j < d; j++) math.sqrt(cov[j][j])];
  final raw = List.generate(
    d,
    (a) => List<double>.generate(d, (b) {
      // c /= stddev[:, None]; c /= stddev[None, :]; clip to [-1, 1].
      final v = cov[a][b] / sd[a] / sd[b];
      return v.isNaN ? v : v.clamp(-1.0, 1.0);
    }),
  );
  final corr = List.generate(
    d,
    (a) => List<double>.generate(d, (b) => (raw[a][b] + raw[b][a]) / 2),
  );
  return redundantFromCorrelation(corr, threshold: threshold);
}

// ── track_graph.py ─────────────────────────────────────────────────────────

/// `dsp.unpack_graph_embedding`: the 68 float32 of a v5 BLOB, or null for a
/// missing or malformed one.
Float32List? unpackGraphEmbedding(Uint8List? blob) {
  if (blob == null || blob.length != kGraphEmbedDims * 4) return null;
  final bd = ByteData.sublistView(blob);
  return Float32List.fromList([
    for (var i = 0; i < kGraphEmbedDims; i++)
      bd.getFloat32(i * 4, Endian.little),
  ]);
}

double _log2(double x) => math.log(x) / math.ln2;

/// `build_acoustic_edges`' maths: null when fewer than two tracks have a
/// usable embedding (Python returns 0 and writes nothing).
AcousticGeometry? buildAcousticGeometry(
  List<FeatureInput> rows, {
  bool zScore = true,
  double scalarWeight = 1.5,
  @visibleForTesting void Function(Float64List z, int n, int d)? onMatrix,
}) {
  if (rows.length < 2) return null;
  final redundant = redundantRawFeatures(rows);
  final surviving = [
    for (var j = 0; j < kScalarOrder.length; j++)
      if (!redundant.contains(kScalarOrder[j])) j,
  ];
  final d = kGraphEmbedDims + surviving.length;
  final paths = <String>[];
  final values = <double>[];
  for (final r in rows) {
    final v = unpackGraphEmbedding(r.timbre);
    if (v == null) continue;
    paths.add(r.path);
    values.addAll(v);
    for (final j in surviving) {
      // `_all_scalars`: bpm as log2(max(bpm, 1)).
      values.add(j == 0 ? _log2(math.max(r.scalars[0], 1.0)) : r.scalars[j]);
    }
  }
  final n = paths.length;
  if (n < 2) return null;
  final x = Float32List.fromList(values);

  // Z = (X - mu) / sd in float32; sd < 1e-8 counts as 1. Then the scalars
  // are weighted.
  final Float32List mean, sd;
  if (zScore) {
    (mean, sd) = _meanStd(x, n, d);
    for (var j = 0; j < d; j++) {
      if (sd[j] < 1e-8) sd[j] = 1;
    }
  } else {
    (mean, _) = _meanStd(x, n, d);
    sd = Float32List(d)..fillRange(0, d, 1);
  }
  final weighted = scalarWeight != 1.0 && d > kGraphEmbedDims;
  final z = Float64List(n * d);
  for (var i = 0; i < n; i++) {
    for (var j = 0; j < d; j++) {
      var v = f32(f32(x[i * d + j] - mean[j]) / sd[j]);
      if (weighted && j >= kGraphEmbedDims) v = f32(v * scalarWeight);
      z[i * d + j] = v;
    }
  }

  onMatrix?.call(z, n, d);

  // The Kaiser-truncated SVD: the right singular vectors are the
  // eigenvectors of ZᵀZ, the squared singular values its eigenvalues.
  final gram = Float64List(d * d);
  for (var i = 0; i < n; i++) {
    final o = i * d;
    for (var a = 0; a < d; a++) {
      final za = z[o + a];
      if (za == 0) continue;
      for (var b = a; b < d; b++) {
        gram[a * d + b] += za * z[o + b];
      }
    }
  }
  for (var a = 0; a < d; a++) {
    for (var b = 0; b < a; b++) {
      gram[a * d + b] = gram[b * d + a];
    }
  }
  final (eig, vec) = _symmetricEigen(gram, d);
  final rank = math.min(n, d); // Vt's rows
  final eigenvalues = [
    for (var c = 0; c < rank; c++) math.max(eig[c], 0.0) / (n - 1),
  ];
  var kaiser = eigenvalues.where((e) => e > 1.0).length;
  kaiser = math.max(3, math.min(kaiser, rank));
  final keep = math.min(kaiser, rank);

  // svd_flip: each kept component's largest-magnitude loading positive.
  final v = Float64List(d * keep);
  for (var c = 0; c < keep; c++) {
    var arg = 0;
    for (var r = 1; r < d; r++) {
      if (vec[r * d + c].abs() > vec[arg * d + c].abs()) arg = r;
    }
    final sign = vec[arg * d + c] < 0 ? -1.0 : 1.0;
    for (var r = 0; r < d; r++) {
      v[r * keep + c] = sign * vec[r * d + c];
    }
  }
  final coords = Float32List(n * keep);
  for (var i = 0; i < n; i++) {
    for (var c = 0; c < keep; c++) {
      var s = 0.0;
      for (var r = 0; r < d; r++) {
        s += z[i * d + r] * v[r * keep + c];
      }
      coords[i * keep + c] = s;
    }
  }
  return AcousticGeometry(
    paths: paths,
    coords: coords,
    dims: keep,
    redundant: redundant,
    eigenvalues: eigenvalues,
  );
}

/// Eigen-decomposition of the symmetric d×d matrix [a] (row-major; it is
/// overwritten) by cyclic Jacobi rotations: eigenvalues descending, and the
/// eigenvectors as the columns of a row-major d×d matrix in the same order.
(List<double>, Float64List) _symmetricEigen(Float64List a, int d) {
  final v = Float64List(d * d);
  for (var i = 0; i < d; i++) {
    v[i * d + i] = 1;
  }
  for (var sweep = 0; sweep < 100; sweep++) {
    var off = 0.0, diag = 0.0;
    for (var p = 0; p < d; p++) {
      diag += a[p * d + p] * a[p * d + p];
      for (var q = p + 1; q < d; q++) {
        off += a[p * d + q] * a[p * d + q];
      }
    }
    if (off == 0 || off <= 1e-30 * diag) break;
    for (var p = 0; p < d - 1; p++) {
      for (var q = p + 1; q < d; q++) {
        final apq = a[p * d + q];
        if (apq == 0) continue;
        final app = a[p * d + p], aqq = a[q * d + q];
        // Below rounding next to both diagonal entries even when scaled
        // by 100: zero it (Numerical Recipes' `jacobi`).
        final g = 100 * apq.abs();
        if (sweep > 3 &&
            app.abs() + g == app.abs() &&
            aqq.abs() + g == aqq.abs()) {
          a[p * d + q] = a[q * d + p] = 0;
          continue;
        }
        final theta = (aqq - app) / (2 * apq);
        final t =
            (theta >= 0 ? 1.0 : -1.0) /
            (theta.abs() + math.sqrt(theta * theta + 1));
        final c = 1 / math.sqrt(t * t + 1), s = t * c;
        for (var k = 0; k < d; k++) {
          final akp = a[k * d + p], akq = a[k * d + q];
          a[k * d + p] = c * akp - s * akq;
          a[k * d + q] = s * akp + c * akq;
        }
        for (var k = 0; k < d; k++) {
          final apk = a[p * d + k], aqk = a[q * d + k];
          a[p * d + k] = c * apk - s * aqk;
          a[q * d + k] = s * apk + c * aqk;
        }
        for (var k = 0; k < d; k++) {
          final vkp = v[k * d + p], vkq = v[k * d + q];
          v[k * d + p] = c * vkp - s * vkq;
          v[k * d + q] = s * vkp + c * vkq;
        }
      }
    }
  }
  final order = List.generate(d, (i) => i)
    ..sort((x, y) => a[y * d + y].compareTo(a[x * d + x]));
  final values = [for (final i in order) a[i * d + i]];
  final vectors = Float64List(d * d);
  for (var c = 0; c < d; c++) {
    final src = order[c];
    for (var r = 0; r < d; r++) {
      vectors[r * d + c] = v[r * d + src];
    }
  }
  return (values, vectors);
}
