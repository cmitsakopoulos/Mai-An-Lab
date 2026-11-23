import 'dart:typed_data';

import 'package:sqlite3/sqlite3.dart';

/// float32 little-endian blob, the layout numpy writes for pca_coords.
Uint8List f32Blob(List<double> v) {
  final b = ByteData(v.length * 4);
  for (var i = 0; i < v.length; i++) {
    b.setFloat32(i * 4, v[i], Endian.little);
  }
  return b.buffer.asUint8List();
}

/// The subset of utils/db_manager.py's schema the Flutter app reads.
void createLibrarySchema(Database db) {
  db.execute('''
    CREATE TABLE artists (id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE COLLATE NOCASE NOT NULL, album_count INTEGER DEFAULT 0,
      track_count INTEGER DEFAULT 0);
    CREATE TABLE albums (id INTEGER PRIMARY KEY AUTOINCREMENT,
      artist_id INTEGER NOT NULL, title TEXT NOT NULL COLLATE NOCASE, year TEXT,
      genre TEXT, genre_bucket TEXT, track_count INTEGER DEFAULT 0,
      UNIQUE (artist_id, title));
    CREATE TABLE tracks (id INTEGER PRIMARY KEY AUTOINCREMENT,
      album_id INTEGER NOT NULL, title TEXT, track_num INTEGER, duration REAL,
      path TEXT UNIQUE NOT NULL, format TEXT, added_date REAL, bitrate INTEGER,
      bpm REAL DEFAULT 0, energy REAL DEFAULT 0, brightness REAL DEFAULT 0,
      artist_tag TEXT);
    CREATE TABLE play_counts (track_path TEXT PRIMARY KEY, count INTEGER DEFAULT 0,
      last_played INTEGER, bpm REAL DEFAULT 0, energy REAL DEFAULT 0,
      brightness REAL DEFAULT 0, pca_coords BLOB);
    PRAGMA user_version = 2;
  ''');
}
