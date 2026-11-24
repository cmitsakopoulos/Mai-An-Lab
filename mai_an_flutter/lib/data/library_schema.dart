import 'package:flutter/foundation.dart';
import 'package:sqlite3/sqlite3.dart';

/// `DatabaseManager.GEOMETRY_VERSION`, stored in `PRAGMA user_version`.
/// Bump together with the Python constant.
const kGeometryVersion = 2;

/// What `DatabaseManager(path)` + `initialize()` does to a database when the
/// Flet app opens it (main.py does both on every start): the connection
/// pragmas, then `get_connection()`'s migrations, then `initialize()`'s
/// schema, in that order. The order matters: on a fresh file the migrations
/// run before the tables exist, so some of them do nothing until the next
/// open (e.g. `play_counts.cluster_id` appears on the second start), and the
/// port reproduces that rather than tidying it, so a database looks the same
/// whichever app opened it. `test/db_schema_test.dart` checks the result
/// against Python on fresh and legacy databases
/// (`tool/export_db_goldens.py`).
///
/// One difference: Python commits after each step; this runs every step in
/// one transaction, so a failure leaves the file as it was.
void migrateLibrary(Database db) {
  try {
    db.execute('PRAGMA journal_mode = WAL');
    db.execute('PRAGMA synchronous = NORMAL');
  } on SqliteException {
    db.execute('PRAGMA journal_mode = DELETE');
  }
  db.execute('PRAGMA foreign_keys = ON');
  db.execute('PRAGMA cache_size = -65536'); // 64 MB page cache

  db.execute('BEGIN');
  try {
    // get_connection()
    _migratePlaylists(db);
    _migratePartitions(db);
    _migratePca(db);
    _tryExec(db, 'ALTER TABLE play_counts ADD COLUMN cluster_id INTEGER');
    _migrateEnrichment(db);
    db.execute(_aiResearch);
    _migrateArtistAliases(db);
    db.execute(_artistReleases);
    db.execute(_releaseSyncLog);
    db.execute(_releaseSyncLogIndex);
    // _drop_retired_autoplay_tables
    db.execute('DROP TABLE IF EXISTS genre_graph');
    db.execute('DROP TABLE IF EXISTS autoplay_hop_rejects');
    db.execute('DROP TABLE IF EXISTS autoplay_hidden');
    _tryExec(db, 'ALTER TABLE albums ADD COLUMN genre_bucket TEXT');
    _migrateFeaturesV4ToV5(db);
    // initialize()
    _initialize(db);
    db.execute('COMMIT');
  } catch (_) {
    db.execute('ROLLBACK');
    rethrow;
  }
}

/// Python's `try: execute(...) except: pass` around an ALTER that fails when
/// the column exists or the table doesn't yet.
void _tryExec(Database db, String sql) {
  try {
    db.execute(sql);
  } on SqliteException {
    // already there, or not created yet
  }
}

Set<String> _columns(Database db, String table) => {
  for (final r in db.select('PRAGMA table_info("$table")')) r['name'] as String,
};

void _migratePlaylists(Database db) {
  db.execute(r"""
            CREATE TABLE IF NOT EXISTS playlists (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                color   TEXT
            )
        """);
  _tryExec(db, 'ALTER TABLE playlists ADD COLUMN color TEXT');
  db.execute('''
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL
                                REFERENCES playlists(id) ON DELETE CASCADE,
                track_path  TEXT    NOT NULL,
                order_index INTEGER NOT NULL DEFAULT 0,
                UNIQUE (playlist_id, track_path)
            )
        ''');
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_pt_playlist_id '
    'ON playlist_tracks(playlist_id, order_index)',
  );
}

const _partitionsComposite = '''
                    CREATE TABLE track_partitions (
                        track_path TEXT REFERENCES tracks(path) ON DELETE CASCADE,
                        mood       TEXT,
                        islet_id   INTEGER,
                        PRIMARY KEY (track_path, mood)
                    )
                ''';

void _migratePartitions(Database db) {
  db.execute('''
            CREATE TABLE IF NOT EXISTS track_partitions (
                track_path TEXT REFERENCES tracks(path) ON DELETE CASCADE,
                mood       TEXT,
                islet_id   INTEGER,
                PRIMARY KEY (track_path, mood)
            )
        ''');
  // A legacy single-column-PK table: rebuild once with the composite PK.
  try {
    final pk = [
      for (final c in db.select('PRAGMA table_info(track_partitions)'))
        if ((c['pk'] as int) != 0) c['name'] as String,
    ];
    if (pk.length == 1 && pk.first == 'track_path') {
      db.execute(
        'ALTER TABLE track_partitions RENAME TO track_partitions_legacy',
      );
      db.execute(_partitionsComposite);
      db.execute(
        'INSERT OR IGNORE INTO track_partitions (track_path, mood, islet_id) '
        'SELECT track_path, mood, islet_id FROM track_partitions_legacy '
        'WHERE mood IS NOT NULL',
      );
      db.execute('DROP TABLE track_partitions_legacy');
    }
  } on SqliteException catch (e) {
    debugPrint('track_partitions PK migration skipped: $e');
  }
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_tp_mood ON track_partitions(mood)',
  );
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_tp_islet_id ON track_partitions(islet_id)',
  );
}

/// Coordinates built by an older geometry are dropped (the next graph build
/// repopulates them); on a fresh file only the version is stamped.
void _migratePca(Database db) {
  _tryExec(db, 'ALTER TABLE play_counts ADD COLUMN pca_coords BLOB');
  db.execute('DROP TABLE IF EXISTS pca_space');
  final current = db.select('PRAGMA user_version').first.values.first as int;
  if (current < kGeometryVersion) {
    _tryExec(db, 'UPDATE play_counts SET pca_coords = NULL');
    db.execute('PRAGMA user_version = $kGeometryVersion');
  }
}

void _migrateEnrichment(Database db) {
  db.execute('''
            CREATE TABLE IF NOT EXISTS artist_enrichment (
                artist_name TEXT PRIMARY KEY COLLATE NOCASE,
                mbid        TEXT,
                country     TEXT,
                area        TEXT,
                genres      TEXT,     -- JSON: [{"name": str, "count": int}, ...]
                source      TEXT,     -- e.g. 'musicbrainz'
                score       INTEGER,  -- match confidence (0-100) from the search
                status      TEXT,     -- 'ok' | 'lowconfidence' | 'notfound' | 'retry' | 'error'
                fetched_at  REAL
            )
        ''');
  final cols = _columns(db, 'artist_enrichment');
  if (!cols.contains('provenance')) {
    db.execute('ALTER TABLE artist_enrichment ADD COLUMN provenance TEXT');
  }
  if (!cols.contains('attempts')) {
    db.execute(
      'ALTER TABLE artist_enrichment ADD COLUMN attempts INTEGER DEFAULT 0',
    );
  }
  db.execute('''
            CREATE TABLE IF NOT EXISTS genre_affinity (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                model      TEXT NOT NULL,   -- JSON {"a|b": npmi, ...}, a < b
                updated_at REAL NOT NULL
            )
        ''');
}

const _aiResearch = '''
            CREATE TABLE IF NOT EXISTS artist_ai_research (
                artist_name TEXT PRIMARY KEY COLLATE NOCASE,
                payload     TEXT NOT NULL,   -- JSON, see metadata_ai.research_artist
                state       TEXT NOT NULL DEFAULT 'pending',
                created_at  REAL NOT NULL
            )
        ''';

void _migrateArtistAliases(Database db) {
  db.execute('''
            CREATE TABLE IF NOT EXISTS artist_aliases (
                alias       TEXT PRIMARY KEY COLLATE NOCASE,
                canonical   TEXT NOT NULL COLLATE NOCASE,
                enrichment  TEXT,   -- JSON snapshot of the alias's artist_enrichment row
                created_at  REAL NOT NULL
            )
        ''');
  db.execute('''
            CREATE TABLE IF NOT EXISTS artist_alias_rejections (
                a TEXT NOT NULL, b TEXT NOT NULL,   -- casefolded, a < b
                PRIMARY KEY (a, b)
            )
        ''');
  db.execute('''
            CREATE TABLE IF NOT EXISTS metadata_audit_dismissed (
                artist_name TEXT NOT NULL COLLATE NOCASE,
                rule        TEXT NOT NULL,
                value       TEXT,   -- the country the user said was right
                PRIMARY KEY (artist_name, rule)
            )
        ''');
  // On a fresh file `tracks` doesn't exist yet, and initialize() creates it
  // with the column.
  final cols = _columns(db, 'tracks');
  if (cols.isNotEmpty && !cols.contains('artist_tag')) {
    db.execute('ALTER TABLE tracks ADD COLUMN artist_tag TEXT');
  }
}

const _artistReleases = '''
            CREATE TABLE IF NOT EXISTS artist_releases (
                artist_name TEXT PRIMARY KEY COLLATE NOCASE,
                source      TEXT NOT NULL,
                source_id   TEXT,
                releases    TEXT NOT NULL,
                fetched_at  REAL NOT NULL
            )
        ''';

const _releaseSyncLog = '''
            CREATE TABLE IF NOT EXISTS release_sync_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_date        TEXT NOT NULL,
                started_at       REAL NOT NULL,
                finished_at      REAL,
                artists_checked  INTEGER DEFAULT 0,
                new_found        INTEGER DEFAULT 0,
                trigger_type     TEXT NOT NULL,
                status           TEXT NOT NULL
            )
        ''';

const _releaseSyncLogIndex = '''
            CREATE INDEX IF NOT EXISTS idx_release_sync_log_date
            ON release_sync_log (sync_date, status)
        ''';

/// v4 timbre blobs (88 float32s) lose the 20-float mfcc_delta block at
/// [40, 60) and become v5 (68). Blobs of any other length are left alone.
void _migrateFeaturesV4ToV5(Database db) {
  final existing = {
    for (final r in db.select(
      "SELECT name FROM sqlite_master WHERE type='table' "
      "AND name IN ('play_counts', 'feature_cache')",
    ))
      r['name'] as String,
  };
  for (final table in ['play_counts', 'feature_cache']) {
    if (!existing.contains(table)) continue;
    try {
      final rows = db.select(
        'SELECT track_path, timbre FROM $table '
        'WHERE timbre IS NOT NULL AND features_version = 4',
      );
      for (final r in rows) {
        final blob = r['timbre'];
        if (blob is! Uint8List || blob.length != 88 * 4) continue;
        final v5 = Uint8List(68 * 4)
          ..setRange(0, 40 * 4, blob)
          ..setRange(40 * 4, 68 * 4, blob, 60 * 4);
        db.execute(
          'UPDATE $table SET timbre = ?, features_version = 5 '
          'WHERE track_path = ?',
          [v5, r['track_path']],
        );
      }
    } on SqliteException catch (e) {
      // A play_counts from before the timbre column: nothing to migrate.
      debugPrint('Failed to migrate $table to v5: $e');
    }
  }
}

void _initialize(Database db) {
  db.execute('''
                    CREATE TABLE IF NOT EXISTS artists (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        name        TEXT UNIQUE COLLATE NOCASE NOT NULL,
                        album_count INTEGER DEFAULT 0,
                        track_count INTEGER DEFAULT 0
                    )
                ''');
  db.execute('''
                    CREATE TABLE IF NOT EXISTS albums (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        artist_id    INTEGER NOT NULL,
                        title        TEXT    NOT NULL COLLATE NOCASE,
                        year         TEXT,
                        genre        TEXT,
                        genre_bucket TEXT,
                        track_count  INTEGER DEFAULT 0,
                        FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE,
                        UNIQUE (artist_id, title)
                    )
                ''');
  db.execute('''
                    CREATE TABLE IF NOT EXISTS tracks (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        album_id   INTEGER NOT NULL,
                        title      TEXT,
                        track_num  INTEGER,
                        duration   REAL,
                        path       TEXT UNIQUE NOT NULL,
                        format     TEXT,
                        added_date REAL,
                        bitrate    INTEGER,
                        bpm        REAL DEFAULT 0,
                        energy     REAL DEFAULT 0,
                        brightness REAL DEFAULT 0,
                        artist_tag TEXT,   -- the file's artist tag when an alias redirected it
                        FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE
                    )
                ''');
  db.execute('''
                    CREATE TABLE IF NOT EXISTS play_counts (
                        track_path  TEXT PRIMARY KEY,
                        count       INTEGER DEFAULT 0,
                        last_played INTEGER,
                        bpm         REAL DEFAULT 0,
                        energy      REAL DEFAULT 0,
                        brightness  REAL DEFAULT 0,
                        pca_coords  BLOB
                    )
                ''');
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_albums_artist_id  ON albums(artist_id)',
  );
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_tracks_album_id   ON tracks(album_id)',
  );
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_tracks_added_date ON tracks(added_date)',
  );
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_tracks_title      '
    'ON tracks(title COLLATE NOCASE)',
  );
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_albums_title      '
    'ON albums(title COLLATE NOCASE)',
  );

  createFtsAndTriggers(db);

  // KNN feature columns, then the sound-profile columns (dsp.py's layout).
  for (final table in ['tracks', 'play_counts']) {
    for (final col in ['bpm', 'energy', 'brightness']) {
      _tryExec(db, 'ALTER TABLE $table ADD COLUMN $col REAL DEFAULT 0');
    }
  }
  for (final (col, ddl) in const [
    ('timbre', 'BLOB'),
    ('features_version', 'INTEGER DEFAULT 0'),
    ('rolloff', 'REAL DEFAULT 0'),
    ('beat_strength', 'REAL DEFAULT 0'),
    ('spectral_flatness', 'REAL DEFAULT 0'),
    ('spectral_contrast', 'REAL DEFAULT 0'),
    ('key_index', 'INTEGER DEFAULT 0'),
  ]) {
    _tryExec(db, 'ALTER TABLE play_counts ADD COLUMN $col $ddl');
  }

  db.execute('''
                    CREATE TABLE IF NOT EXISTS track_neighbors (
                        track_path    TEXT NOT NULL,
                        neighbor_path TEXT NOT NULL,
                        weight        REAL NOT NULL,
                        edge_kind     TEXT NOT NULL,
                        PRIMARY KEY (track_path, neighbor_path, edge_kind)
                    )
                ''');
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_neighbors_track '
    'ON track_neighbors(track_path, edge_kind, weight DESC)',
  );

  db.execute('''
                    CREATE TABLE IF NOT EXISTS playback_history (
                        track_path TEXT NOT NULL,
                        played_at  REAL NOT NULL,
                        event      TEXT NOT NULL,
                        seed_path  TEXT
                    )
                ''');
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_history_recent '
    'ON playback_history(played_at DESC)',
  );
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_history_track '
    'ON playback_history(track_path, event)',
  );
  _tryExec(db, 'ALTER TABLE playback_history ADD COLUMN listened_s REAL');
  // Makes the ledger drain idempotent (a replayed line is ignored). Partial,
  // so legacy 'played' rows that repeat within a second can't block it.
  db.execute(
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_history_listen_uniq '
    'ON playback_history(track_path, played_at, event) '
    "WHERE event IN ('completed', 'skipped_early')",
  );

  // A no-op after _migratePartitions; kept because initialize() has it.
  db.execute('''
                    CREATE TABLE IF NOT EXISTS track_partitions (
                        track_path TEXT PRIMARY KEY REFERENCES tracks(path) ON DELETE CASCADE,
                        mood       TEXT,
                        islet_id   INTEGER
                    )
                ''');
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_tp_mood ON track_partitions(mood)',
  );
  db.execute(
    'CREATE INDEX IF NOT EXISTS idx_tp_islet_id ON track_partitions(islet_id)',
  );

  db.execute('DROP TABLE IF EXISTS mood_feedback');
  db.execute('DROP TABLE IF EXISTS mood_profiles');
}

const _triggerNames = [
  'trg_tracks_insert_counts',
  'trg_tracks_delete_counts',
  'trg_tracks_update_counts',
  'trg_tracks_update_fts',
  'trg_albums_insert_counts',
  'trg_albums_delete_counts',
  'trg_albums_update_fts',
  'trg_albums_artist_update_fts',
  'trg_albums_artist_update_counts',
  'trg_artists_update_fts',
];

/// `_create_fts_and_triggers`: the FTS table and the triggers that keep it
/// and the album/artist counts in step with every write. Dropped and
/// recreated on each open, as in Python.
void createFtsAndTriggers(Database db) {
  for (final t in _triggerNames) {
    db.execute('DROP TRIGGER IF EXISTS $t');
  }
  db.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_search USING fts5(
                title, album, artist, tokenize='unicode61'
            )
        ''');
  for (final sql in _triggers) {
    db.execute(sql);
  }
}

const _triggers = [
  '''
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
        ''',
  '''
            CREATE TRIGGER trg_tracks_delete_counts AFTER DELETE ON tracks
            BEGIN
                UPDATE albums SET track_count = track_count - 1 WHERE id = OLD.album_id;
                UPDATE artists SET track_count = track_count - 1 WHERE id = (
                    SELECT artist_id FROM albums WHERE id = OLD.album_id
                );
                DELETE FROM fts_search WHERE rowid = OLD.id;
            END;
        ''',
  '''
            CREATE TRIGGER trg_tracks_update_counts AFTER UPDATE OF album_id ON tracks
            BEGIN
                UPDATE albums SET track_count = track_count - 1 WHERE id = OLD.album_id;
                UPDATE artists SET track_count = track_count - 1 WHERE id = (
                    SELECT artist_id FROM albums WHERE id = OLD.album_id
                );
                UPDATE albums SET track_count = track_count + 1 WHERE id = NEW.album_id;
                UPDATE artists SET track_count = track_count + 1 WHERE id = (
                    SELECT artist_id FROM albums WHERE id = NEW.album_id
                );
            END;
        ''',
  '''
            CREATE TRIGGER trg_tracks_update_fts AFTER UPDATE ON tracks
            BEGIN
                UPDATE fts_search
                SET title = NEW.title,
                    album = (SELECT title FROM albums WHERE id = NEW.album_id),
                    artist = (
                        SELECT ar.name FROM artists ar
                        JOIN albums al ON al.artist_id = ar.id
                        WHERE al.id = NEW.album_id
                    )
                WHERE rowid = NEW.id;
            END;
        ''',
  '''
            CREATE TRIGGER trg_albums_insert_counts AFTER INSERT ON albums
            BEGIN
                UPDATE artists SET album_count = album_count + 1 WHERE id = NEW.artist_id;
            END;
        ''',
  '''
            CREATE TRIGGER trg_albums_delete_counts AFTER DELETE ON albums
            BEGIN
                UPDATE artists SET album_count = album_count - 1 WHERE id = OLD.artist_id;
            END;
        ''',
  '''
            CREATE TRIGGER trg_albums_update_fts AFTER UPDATE OF title ON albums
            BEGIN
                UPDATE fts_search SET album = NEW.title WHERE rowid IN (
                    SELECT id FROM tracks WHERE album_id = NEW.id
                );
            END;
        ''',
  '''
            CREATE TRIGGER trg_albums_artist_update_fts AFTER UPDATE OF artist_id ON albums
            BEGIN
                UPDATE fts_search
                SET artist = (SELECT name FROM artists WHERE id = NEW.artist_id)
                WHERE rowid IN (SELECT id FROM tracks WHERE album_id = NEW.id);
            END;
        ''',
  '''
            CREATE TRIGGER trg_albums_artist_update_counts AFTER UPDATE OF artist_id ON albums
            BEGIN
                UPDATE artists SET
                    album_count = album_count - 1,
                    track_count = track_count - (SELECT COUNT(*) FROM tracks WHERE album_id = OLD.id)
                WHERE id = OLD.artist_id;
                UPDATE artists SET
                    album_count = album_count + 1,
                    track_count = track_count + (SELECT COUNT(*) FROM tracks WHERE album_id = NEW.id)
                WHERE id = NEW.artist_id;
            END;
        ''',
  '''
            CREATE TRIGGER trg_artists_update_fts AFTER UPDATE OF name ON artists
            BEGIN
                UPDATE fts_search SET artist = NEW.name WHERE rowid IN (
                    SELECT t.id FROM tracks t
                    JOIN albums al ON t.album_id = al.id
                    WHERE al.artist_id = NEW.id
                );
            END;
        ''',
];
