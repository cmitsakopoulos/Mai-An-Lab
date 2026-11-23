import 'dart:math' as math;
import 'dart:typed_data';

/// The acoustic coordinate graph: the Dart counterpart of
/// `track_graph.load_live_coordinate_graph`.
///
/// Holds each analysed track's persisted Zr coordinates (`play_counts.pca_coords`,
/// float32 blobs written by the Python DSP pipeline), L2-normalised into one
/// flat row-major [Float32List] so cosine similarity is a plain dot product.
/// Pure Dart with no Flutter imports, so it is built on the DB isolate and is
/// unit-testable without a device.
class CoordGraph {
  CoordGraph._({
    required this.paths,
    required this.dim,
    required this.unit,
    required this.artistKeys,
    required this.albumKeys,
    required this.skipped,
  }) : indexOf = {for (var i = 0; i < paths.length; i++) paths[i]: i};

  final List<String> paths;
  final Map<String, int> indexOf;
  final int dim;

  /// `length * dim` floats; row i is track i's unit vector.
  final Float32List unit;
  final List<String> artistKeys;
  final List<String> albumKeys;

  /// Rows dropped because their blob was empty or had a different dimension.
  final int skipped;

  int get length => paths.length;

  /// Builds the graph from raw rows. Every row's [CoordRow.coords] holds the
  /// blob bytes. The dimension is taken from the first valid blob and rows
  /// that disagree are skipped, matching the Python side, which ignores
  /// stale-version coordinates.
  factory CoordGraph.fromRows(List<CoordRow> rows) {
    var dim = 0;
    for (final r in rows) {
      if (r.coords.lengthInBytes >= 4 && r.coords.lengthInBytes % 4 == 0) {
        dim = r.coords.lengthInBytes ~/ 4;
        break;
      }
    }
    final kept = <CoordRow>[
      for (final r in rows)
        if (dim > 0 && r.coords.lengthInBytes == dim * 4) r,
    ];
    final unit = Float32List(kept.length * dim);
    for (var i = 0; i < kept.length; i++) {
      final bytes = ByteData.sublistView(kept[i].coords);
      final base = i * dim;
      var norm = 0.0;
      for (var d = 0; d < dim; d++) {
        // numpy writes native-endian float32; every target we ship is LE.
        final v = bytes.getFloat32(d * 4, Endian.little);
        unit[base + d] = v;
        norm += v * v;
      }
      final inv = norm > 1e-18 ? 1.0 / math.sqrt(norm) : 0.0;
      for (var d = 0; d < dim; d++) {
        unit[base + d] *= inv;
      }
    }
    return CoordGraph._(
      paths: [for (final r in kept) r.path],
      dim: dim,
      unit: unit,
      artistKeys: [for (final r in kept) _key(r.artist)],
      albumKeys: [for (final r in kept) _key(r.album)],
      skipped: rows.length - kept.length,
    );
  }

  /// Cosine similarity of every track to [seed]. O(N·D).
  Float32List similarities(int seed) {
    final out = Float32List(length);
    final s = seed * dim;
    for (var i = 0; i < length; i++) {
      final b = i * dim;
      var dot = 0.0;
      for (var d = 0; d < dim; d++) {
        dot += unit[s + d] * unit[b + d];
      }
      out[i] = dot;
    }
    return out;
  }

  /// Indices of the [k] nearest tracks to [seed], best first, seed excluded.
  List<int> nearest(int seed, int k) {
    final sims = similarities(seed);
    final order = List<int>.generate(length, (i) => i)
      ..remove(seed)
      ..sort((a, b) => sims[b].compareTo(sims[a]));
    return order.take(k).toList();
  }

  /// A one-shot similarity queue from [seed] with per-artist and per-album
  /// repeat caps. The seed counts towards the caps, since it is the track
  /// already playing.
  ///
  /// SIMPLIFIED STAND-IN for `track_graph.walk`, which drives
  /// `artist_graph.Drift`: there, artists are ranked by genre knowledge and the
  /// acoustics only choose the track. That port comes later; this keeps the
  /// same signature and caps so the UI and its tests are already wired.
  List<int> walk(
    int seed, {
    int length = 10,
    int maxPerArtist = 2,
    int maxPerAlbum = 1,
    Set<int> avoid = const {},
  }) {
    final sims = similarities(seed);
    final order = List<int>.generate(this.length, (i) => i)
      ..sort((a, b) => sims[b].compareTo(sims[a]));
    final perArtist = <String, int>{artistKeys[seed]: 1};
    final perAlbum = <String, int>{albumKeys[seed]: 1};
    final out = <int>[];
    for (final i in order) {
      if (out.length >= length) break;
      if (i == seed || avoid.contains(i)) continue;
      final a = artistKeys[i], al = albumKeys[i];
      if (a.isNotEmpty && (perArtist[a] ?? 0) >= maxPerArtist) continue;
      if (al.isNotEmpty && (perAlbum[al] ?? 0) >= maxPerAlbum) continue;
      out.add(i);
      if (a.isNotEmpty) perArtist[a] = (perArtist[a] ?? 0) + 1;
      if (al.isNotEmpty) perAlbum[al] = (perAlbum[al] ?? 0) + 1;
    }
    return out;
  }

  // Python decomposes collab credits ("A & B") via metadata_enrich.credit_keys;
  // until that is ported a credit is one key.
  static String _key(String? s) => (s ?? '').trim().toLowerCase();
}

class CoordRow {
  const CoordRow({
    required this.path,
    required this.coords,
    this.artist,
    this.album,
  });
  final String path;
  final Uint8List coords;
  final String? artist;
  final String? album;
}
