import 'package:sqlite3/sqlite3.dart';

/// Settings → Database Management's three buttons, as main.py runs them:
/// foreign keys off, the deletes in one transaction, keys back on, then
/// VACUUM (whose failure is ignored). Tables Python guards with a bare
/// `try` (optional in old files) are skipped when absent.

/// main.py `clear_library_index`: the index, playlists and edges go;
/// play counts, history and features stay.
void clearLibraryIndexJob(Database db) => _maintain(
  db,
  [
    'playlist_tracks',
    'playlists',
    'track_neighbors',
    'tracks',
    'albums',
    'artists',
  ],
  optional: ['fts_search'],
);

/// main.py `wipe_database`: everything the library knows, play history
/// included. Settings and the music files are untouched.
void wipeDatabaseJob(Database db) => _maintain(
  db,
  [
    'playlist_tracks',
    'playlists',
    'track_partitions',
    'playback_history',
    'track_neighbors',
    'play_counts',
    'tracks',
    'albums',
    'artists',
  ],
  optional: ['artist_enrichment', 'genre_affinity', 'fts_search'],
);

/// main.py `clear_dsp_features`: every track's analysis and coordinates,
/// and the acoustic edges; no foreign-key toggle and no VACUUM there.
void clearDspFeaturesJob(Database db) {
  db.execute('BEGIN');
  try {
    db.execute(
      'UPDATE play_counts SET timbre = NULL, features_version = 0, '
      'pca_coords = NULL',
    );
    db.execute("DELETE FROM track_neighbors WHERE edge_kind = 'acoustic'");
    db.execute('COMMIT');
  } catch (_) {
    db.execute('ROLLBACK');
    rethrow;
  }
}

void _maintain(
  Database db,
  List<String> tables, {
  List<String> optional = const [],
}) {
  db.execute('PRAGMA foreign_keys = OFF');
  try {
    db.execute('BEGIN');
    try {
      for (final t in tables) {
        db.execute('DELETE FROM $t');
      }
      for (final t in optional) {
        try {
          db.execute('DELETE FROM $t');
        } on SqliteException {
          // Absent in this file: Python's bare `except: pass`.
        }
      }
      db.execute('COMMIT');
    } catch (_) {
      db.execute('ROLLBACK');
      rethrow;
    }
  } finally {
    db.execute('PRAGMA foreign_keys = ON');
  }
  try {
    db.execute('VACUUM');
  } on SqliteException {
    // Python ignores it too.
  }
}
