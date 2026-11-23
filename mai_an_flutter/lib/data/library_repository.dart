import 'dart:typed_data';

import 'package:sqlite3/sqlite3.dart';

import '../core/db_worker.dart';
import '../graph/coord_graph.dart';

class Track {
  const Track({
    required this.id,
    required this.path,
    required this.title,
    required this.artist,
    required this.album,
    required this.duration,
    required this.format,
    this.trackNum,
    this.addedDate,
  });

  final int id;
  final String path;
  final String title;
  final String artist;
  final String album;
  final double duration;
  final String format;
  final int? trackNum;

  /// Unix seconds (Python `time.time()`), null for rows that predate the column.
  final double? addedDate;

  String get durationLabel {
    final s = duration.round();
    return '${s ~/ 60}:${(s % 60).toString().padLeft(2, '0')}';
  }
}

/// Read access to the library database that the Python app writes
/// (`library.db`, schema owned by `utils/db_manager.py`). It opens the file
/// READ-ONLY, so the Flutter app can't corrupt or migrate the database while
/// both apps exist side by side.
class LibraryRepository {
  LibraryRepository._(this._db);

  final DbWorker _db;

  static Future<LibraryRepository> open(String path) async =>
      LibraryRepository._(await DbWorker.open(path));

  Future<List<Track>> allTracks() => _db.run(_selectAllTracks);

  Future<CoordGraph> coordGraph() => _db.run(_buildCoordGraph);

  Future<void> close() => _db.close();
}

// ── Jobs (run on the DB isolate) ────────────────────────────────────────────

/// Same rows as `db_manager.get_all_tracks` (INNER joins: a track whose album
/// or artist row is missing doesn't show in the Flet app either). Sorting
/// happens in memory; see track_query.dart.
List<Track> _selectAllTracks(Database db) {
  final rows = db.select('''
    SELECT t.id, t.path, t.title, t.duration, t.format, t.track_num,
           t.added_date,
           ar.name  AS artist,
           al.title AS album
    FROM tracks t
    JOIN albums  al ON t.album_id = al.id
    JOIN artists ar ON al.artist_id = ar.id
  ''');
  return [
    for (final r in rows)
      Track(
        id: r['id'] as int,
        path: r['path'] as String,
        title: (r['title'] as String?) ?? _basename(r['path'] as String),
        artist: (r['artist'] as String?) ?? 'Unknown artist',
        album: (r['album'] as String?) ?? 'Unknown album',
        duration: (r['duration'] as num?)?.toDouble() ?? 0,
        format: (r['format'] as String?) ?? '',
        trackNum: r['track_num'] as int?,
        addedDate: (r['added_date'] as num?)?.toDouble(),
      ),
  ];
}

/// `DatabaseManager.GEOMETRY_VERSION`, stored in `PRAGMA user_version`.
/// Bump together with the Python constant.
const kGeometryVersion = 2;

/// The database's coordinates were built by an older geometry pipeline. The
/// Flet app NULLs them on its next open and rebuilds; until then nothing may
/// walk them.
class StaleGeometryException implements Exception {
  StaleGeometryException(this.found);
  final int found;
  @override
  String toString() =>
      'Acoustic coordinates are from geometry v$found (current: '
      'v$kGeometryVersion). Open the library in the Flet app and re-run '
      'analysis to rebuild them.';
}

/// Mirrors `db_manager.get_tracks_pca_coords` + `load_live_coordinate_graph`,
/// including `_migrate_pca`'s rule that coordinates from an older
/// GEOMETRY_VERSION are invalid.
CoordGraph _buildCoordGraph(Database db) {
  final version = db.select('PRAGMA user_version').first.values.first as int;
  if (version < kGeometryVersion) throw StaleGeometryException(version);
  final rows = db.select('''
    SELECT pc.track_path AS path, pc.pca_coords,
           ar.name  AS artist,
           al.title AS album
    FROM play_counts pc
    INNER JOIN tracks  t  ON t.path = pc.track_path
    LEFT  JOIN albums  al ON al.id  = t.album_id
    LEFT  JOIN artists ar ON ar.id  = al.artist_id
    WHERE pc.pca_coords IS NOT NULL
  ''');
  return CoordGraph.fromRows([
    for (final r in rows)
      CoordRow(
        path: r['path'] as String,
        coords: r['pca_coords'] as Uint8List,
        artist: r['artist'] as String?,
        album: r['album'] as String?,
      ),
  ]);
}

String _basename(String path) => path.split(RegExp(r'[/\\]')).last;
