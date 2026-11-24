import 'package:sqlite3/sqlite3.dart';

import '../core/py_text.dart';

/// One file as the scanner read it: `LibraryScanner._read_tags_batch`'s
/// dict. Sent between isolates, so plain fields only.
class ScannedTrack {
  const ScannedTrack({
    required this.title,
    required this.artist,
    required this.album,
    required this.path,
    required this.format,
    required this.addedDate,
    this.year,
    this.genre,
    this.duration = 0.0,
    this.bitrate = 0,
    this.trackNum,
  });

  final String title;
  final String artist;
  final String album;
  final String? year;
  final String? genre;
  final double duration;
  final String path;
  final String format;
  final double addedDate;

  /// kbit/s; tinytag's float, or the int 0 when it had none. SQLite's
  /// INTEGER affinity stores a whole float as an integer, as from Python.
  final num bitrate;
  final int? trackNum;
}

// The scanner's writes: db_manager.py's methods of the same names, as DB
// isolate jobs. Python's `clear_caches` has no counterpart: its artist and
// album caches only save a SELECT, and the jobs below look each name up
// again, which gives the same ids.

/// `get_path_mtime_map`. A row without `added_date` (older databases) maps
/// to null, which the scanner treats as changed.
Map<String, double?> pathMtimeMapJob(Database db) => {
  for (final r in db.select('SELECT path, added_date FROM tracks'))
    r['path'] as String: (r['added_date'] as num?)?.toDouble(),
};

/// `_load_aliases`: casefolded alias → canonical artist.
Map<String, String> artistAliasesJob(Database db) => {
  for (final r in db.select('SELECT alias, canonical FROM artist_aliases'))
    pyCasefold(r['alias'] as String): r['canonical'] as String,
};

/// `delete_tracks_by_paths`: playlist memberships first (so a later reorder
/// can't collide with orphaned slots), then the tracks, then empty albums
/// and artists, in one transaction.
void Function(Database) deleteTracksByPathsJob(List<String> paths) => (db) {
  if (paths.isEmpty) return;
  _transaction(db, () {
    for (var i = 0; i < paths.length; i += 900) {
      final chunk = paths.sublist(
        i,
        i + 900 > paths.length ? paths.length : i + 900,
      );
      final ph = List.filled(chunk.length, '?').join(',');
      db.execute(
        'DELETE FROM playlist_tracks WHERE track_path IN ($ph)',
        chunk,
      );
      db.execute('DELETE FROM tracks WHERE path IN ($ph)', chunk);
    }
    db.execute('DELETE FROM albums  WHERE track_count <= 0');
    db.execute('DELETE FROM artists WHERE album_count <= 0');
  });
};

/// `prune_orphans`.
void pruneOrphansJob(Database db) => _transaction(db, () {
  db.execute('DELETE FROM albums  WHERE track_count <= 0');
  db.execute('DELETE FROM artists WHERE album_count <= 0');
});

/// `checkpoint`: fold the WAL back into the file.
void checkpointJob(Database db) {
  try {
    db.select('PRAGMA wal_checkpoint(TRUNCATE)');
  } on SqliteException {
    // Python logs and carries on.
  }
}

/// `begin_bulk_import`: drop the per-row insert trigger for a large scan;
/// [endBulkImportJob] rebuilds what it maintained in one pass.
void beginBulkImportJob(Database db) =>
    db.execute('DROP TRIGGER IF EXISTS trg_tracks_insert_counts');

/// `end_bulk_import`: recount albums and artists, rebuild the FTS index,
/// and recreate the trigger (the same SQL text as Python's, so the schema
/// reads the same).
void endBulkImportJob(Database db) => _transaction(db, () {
  db.execute(
    'UPDATE albums SET track_count = '
    '(SELECT COUNT(*) FROM tracks WHERE album_id = albums.id)',
  );
  db.execute(
    'UPDATE artists SET track_count = '
    '(SELECT COUNT(*) FROM tracks t '
    ' JOIN albums al ON t.album_id = al.id '
    ' WHERE al.artist_id = artists.id)',
  );
  db.execute(
    'UPDATE artists SET album_count = '
    '(SELECT COUNT(*) FROM albums WHERE artist_id = artists.id)',
  );
  db.execute('DELETE FROM fts_search');
  db.execute(
    'INSERT INTO fts_search(rowid, title, album, artist) '
    'SELECT t.id, t.title, al.title, ar.name '
    'FROM tracks t '
    'JOIN albums al ON al.id = t.album_id '
    'JOIN artists ar ON ar.id = al.artist_id',
  );
  db.execute('''
                    CREATE TRIGGER trg_tracks_insert_counts AFTER INSERT ON tracks
                    BEGIN
                        UPDATE albums SET track_count = track_count + 1 WHERE id = NEW.album_id;
                        UPDATE artists SET track_count = track_count + 1 WHERE id = (
                            SELECT artist_id FROM albums WHERE id = NEW.album_id
                        );
                        INSERT INTO fts_search(rowid, title, album, artist)
                        SELECT NEW.id, NEW.title, al.title, ar.name
                        FROM albums al JOIN artists ar ON al.artist_id = ar.id WHERE al.id = NEW.album_id;
                    END;
                ''');
});

/// `insert_tracks_batch`: each track's artist and album found or created,
/// then the track inserted (or replaced, by path), in one transaction.
/// [aliases] is [artistAliasesJob]'s map: a merged artist's tracks go to
/// the canonical artist and keep their own tag in `artist_tag`.
void Function(Database) insertTracksBatchJob(
  List<ScannedTrack> tracks,
  Map<String, String> aliases,
) => (db) {
  if (tracks.isEmpty) return;
  final artistIds = <String, int>{};
  final albumIds = <(int, String), int>{};
  final insertArtist = db.prepare(
    'INSERT OR IGNORE INTO artists (name) VALUES (?)',
  );
  final selectArtist = db.prepare('SELECT id FROM artists WHERE name = ?');
  final insertAlbum = db.prepare(
    'INSERT OR IGNORE INTO albums (artist_id, title, year, genre) '
    'VALUES (?, ?, ?, ?)',
  );
  final selectAlbum = db.prepare(
    'SELECT id FROM albums WHERE artist_id = ? AND title = ?',
  );
  final insertTrack = db.prepare('''INSERT OR REPLACE INTO tracks
                           (album_id, title, track_num, duration, path, format, added_date, bitrate, artist_tag)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''');
  try {
    _transaction(db, () {
      for (final t in tracks) {
        // _get_or_create_artist
        var name = t.artist.isNotEmpty ? pyStrip(t.artist) : 'Unknown Artist';
        name = aliases[pyCasefold(name)] ?? name;
        final artistId = artistIds[name] ??= () {
          insertArtist.execute([name]);
          return selectArtist.select([name]).first['id'] as int;
        }();
        // _get_or_create_album
        final title = t.album.isNotEmpty ? pyStrip(t.album) : 'Unknown Album';
        final albumId = albumIds[(artistId, title)] ??= () {
          insertAlbum.execute([artistId, title, t.year, t.genre]);
          return selectAlbum.select([artistId, title]).first['id'] as int;
        }();
        insertTrack.execute([
          albumId,
          t.title,
          t.trackNum,
          t.duration,
          t.path,
          t.format,
          t.addedDate,
          t.bitrate,
          _artistTag(t.artist, aliases),
        ]);
      }
    });
  } finally {
    insertArtist.close();
    selectArtist.close();
    insertAlbum.close();
    selectAlbum.close();
    insertTrack.close();
  }
};

/// `_artist_tag`: the file's own artist tag when an alias redirects it.
String? _artistTag(String raw, Map<String, String> aliases) {
  final name = pyStrip(raw);
  return name.isNotEmpty && aliases.containsKey(pyCasefold(name)) ? name : null;
}

void _transaction(Database db, void Function() body) {
  db.execute('BEGIN');
  try {
    body();
    db.execute('COMMIT');
  } catch (_) {
    db.execute('ROLLBACK');
    rethrow;
  }
}
