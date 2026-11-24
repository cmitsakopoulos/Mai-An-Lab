import 'dart:typed_data';

import 'package:sqlite3/sqlite3.dart';

import '../core/db_worker.dart';

/// The Acoustic Network view's queries: db_manager's `has_pca_coords`,
/// `get_neighbors`, `get_tracks_pca_coords_for_paths`, library.py's "any
/// analysed track" lookup, and `track_graph.build_metadata_edges` with
/// `replace_neighbors_bulk`. Same SQL, golden-tested against Python's output
/// (tool/export_network_goldens.py, test/network_synthetic_test.dart).

/// One `track_neighbors` edge out of a track, with the neighbour's tags
/// (null when the neighbour is no longer in the library).
class NeighborRow {
  const NeighborRow({
    required this.path,
    required this.weight,
    required this.edgeKind,
    this.title,
    this.artist,
    this.album,
  });

  final String path;
  final double weight;
  final String edgeKind;
  final String? title;
  final String? artist;
  final String? album;
}

/// `get_tracks_pca_coords_for_paths`' row. [coords] is the blob as float32
/// values (null when it doesn't decode, as numpy's `frombuffer` refuses a
/// length that isn't a multiple of 4).
class NetworkTrackRow {
  const NetworkTrackRow({
    required this.path,
    required this.coords,
    required this.playCount,
    this.title,
    this.duration,
    this.artist,
    this.album,
    this.genre,
  });

  final String path;
  final List<double>? coords;
  final int? playCount;
  final String? title;
  final double? duration;
  final String? artist;
  final String? album;
  final String? genre;

  /// The view's test for "has coordinates" (`r.get("pca_coords")`): a
  /// non-empty list. Two values are needed to place a node.
  bool get placeable => coords != null && coords!.length >= 2;
}

/// `track_graph.DEFAULT_K_METADATA`.
const kMetadataEdgesPerTrack = 30;

/// `track_graph.KIND_ARTIST`.
const kEdgeKindArtist = 'artist';

/// `has_pca_coords`: some library track has coordinates.
bool hasPcaCoordsJob(Database db) => db.select('''
            SELECT 1
            FROM play_counts pc
            INNER JOIN tracks t ON t.path = pc.track_path
            WHERE pc.pca_coords IS NOT NULL LIMIT 1
        ''').isNotEmpty;

/// library.py `load_library`: with no seed and nothing playing, the graph
/// starts from "any single path that has PCA coords".
String anyPcaPathJob(Database db) {
  final rows = db.select('''
                            SELECT pc.track_path
                            FROM play_counts pc
                            INNER JOIN tracks t ON t.path = pc.track_path
                            WHERE pc.pca_coords IS NOT NULL LIMIT 1
                            ''');
  return rows.isEmpty ? '' : rows.first.values.first as String;
}

/// `get_neighbors(track_path, k, edge_kind)`: the [k] heaviest edges, either
/// across every tier (`edgeKind == null`) or restricted to one tier.
DbJob<List<NeighborRow>> neighborsJob(String path, int k, {String? edgeKind}) =>
    (db) {
      final hasKind = edgeKind != null && edgeKind.isNotEmpty;
      final sql = hasKind
          ? '''
                SELECT n.neighbor_path AS path, n.weight, n.edge_kind,
                       t.title, ar.name AS artist, al.title AS album
                FROM track_neighbors n
                LEFT JOIN tracks  t  ON t.path     = n.neighbor_path
                LEFT JOIN albums  al ON al.id      = t.album_id
                LEFT JOIN artists ar ON ar.id      = al.artist_id
                WHERE n.track_path = ? AND n.edge_kind = ?
                ORDER BY n.weight DESC
                LIMIT ?
            '''
          : '''
                SELECT n.neighbor_path AS path, n.weight, n.edge_kind,
                       t.title, ar.name AS artist, al.title AS album
                FROM track_neighbors n
                LEFT JOIN tracks  t  ON t.path     = n.neighbor_path
                LEFT JOIN albums  al ON al.id      = t.album_id
                LEFT JOIN artists ar ON ar.id      = al.artist_id
                WHERE n.track_path = ?
                ORDER BY n.weight DESC
                LIMIT ?
            ''';
      final args = hasKind ? [path, edgeKind, k] : [path, k];
      return [
        for (final r in db.select(sql, args))
          NeighborRow(
            path: r['path'] as String,
            weight: (r['weight'] as num).toDouble(),
            edgeKind: r['edge_kind'] as String,
            title: r['title'] as String?,
            artist: r['artist'] as String?,
            album: r['album'] as String?,
          ),
      ];
    };

/// `get_tracks_pca_coords_for_paths`: coordinates and tags for [paths]
/// (duplicates allowed, as Python passes them), analysed tracks only.
DbJob<List<NetworkTrackRow>> pcaCoordsForPathsJob(List<String> paths) => (db) {
  if (paths.isEmpty) return const [];
  final rows = db.select('''
            SELECT pc.track_path AS path, pc.pca_coords, pc.cluster_id, pc.bpm, pc.energy,
                   pc.count AS play_count,
                   t.title, t.duration,
                   ar.name  AS artist,
                   al.title AS album,
                   al.genre AS genre
            FROM play_counts pc
            INNER JOIN tracks  t  ON t.path  = pc.track_path
            LEFT JOIN albums  al ON al.id   = t.album_id
            LEFT JOIN artists ar ON ar.id   = al.artist_id
            WHERE pc.track_path IN (${List.filled(paths.length, '?').join(',')})
              AND pc.pca_coords IS NOT NULL
        ''', paths);
  return [
    for (final r in rows)
      NetworkTrackRow(
        path: r['path'] as String,
        coords: _floats(r['pca_coords']),
        playCount: r['play_count'] as int?,
        title: r['title'] as String?,
        duration: (r['duration'] as num?)?.toDouble(),
        artist: r['artist'] as String?,
        album: r['album'] as String?,
        genre: r['genre'] as String?,
      ),
  ];
};

List<double>? _floats(Object? blob) {
  if (blob is! Uint8List || blob.lengthInBytes % 4 != 0) return null;
  final bytes = ByteData.sublistView(blob);
  // numpy writes native-endian float32; every target we ship is LE.
  return [
    for (var i = 0; i < blob.lengthInBytes; i += 4)
      bytes.getFloat32(i, Endian.little),
  ];
}

/// `build_metadata_edges`: every track's same-artist tracks, newest added
/// first, at most [k] per track, weight 1.0; the whole artist tier replaced
/// in one transaction (`replace_neighbors_bulk`). Returns the edge count
/// (Python's `(artist_edges, 0)`).
DbJob<int> buildMetadataEdgesJob([int k = kMetadataEdgesPerTrack]) => (db) {
  final byArtist = <int, List<String>>{};
  for (final r in db.select('''
        SELECT ar.id AS artist_id, t.path, t.added_date
        FROM tracks t
        JOIN albums  al ON al.id = t.album_id
        JOIN artists ar ON ar.id = al.artist_id
        ORDER BY ar.id, t.added_date DESC, t.path
    ''')) {
    (byArtist[r['artist_id'] as int] ??= []).add(r['path'] as String);
  }
  final edges = <(String, String)>[];
  for (final paths in byArtist.values) {
    if (paths.length < 2) continue;
    for (final src in paths) {
      final others = [
        for (final p in paths)
          if (p != src) p,
      ];
      for (final dst in others.take(k)) {
        edges.add((src, dst));
      }
    }
  }
  db.execute('BEGIN IMMEDIATE');
  try {
    db.execute('DELETE FROM track_neighbors WHERE edge_kind = ?', [
      kEdgeKindArtist,
    ]);
    final insert = db.prepare(
      'INSERT OR REPLACE INTO track_neighbors '
      '(track_path, neighbor_path, weight, edge_kind) '
      'VALUES (?, ?, ?, ?)',
    );
    try {
      for (final (src, dst) in edges) {
        // Self-loops are dropped (never produced here: dst != src).
        insert.execute([src, dst, 1.0, kEdgeKindArtist]);
      }
    } finally {
      insert.close();
    }
    db.execute('COMMIT');
  } catch (_) {
    db.execute('ROLLBACK');
    rethrow;
  }
  return edges.length;
};
