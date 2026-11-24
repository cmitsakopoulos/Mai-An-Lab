import 'package:sqlite3/sqlite3.dart';

import '../core/db_worker.dart';
import '../core/py_text.dart';
import 'library_repository.dart';

/// db_manager's playlist methods (`get_all_playlists`,
/// `get_tracks_in_playlist`, `create_playlist`, `add_track_to_playlist`,
/// `remove_track_from_playlist`, `move_playlist_track`, `update_playlist`,
/// `delete_playlist`), with its SQL, golden-tested against Python's output
/// (tool/export_playlist_goldens.py, test/playlists_test.dart).
///
/// Search runs in memory (BrowseController), as for the other views, so the
/// `LIKE` half of `get_all_playlists` isn't here.

class PlaylistRow {
  const PlaylistRow({
    required this.id,
    required this.name,
    required this.created,
    required this.color,
    required this.trackCount,
  });

  final int id;
  final String? name;
  final int? created;

  /// `#RRGGBB` from the playlist editor, or null (the default orange).
  final String? color;

  /// `COUNT(pt.track_path)`: playlist_tracks rows, including any whose track
  /// has gone from the library (the Flet count includes them too).
  final int trackCount;

  /// library.py `_playlist_row`: `name or "Untitled Playlist"`.
  String get displayName =>
      (name == null || name!.isEmpty) ? 'Untitled Playlist' : name!;
}

/// db_manager's order: `name` (A–Z, NOCASE) or anything else (newest
/// first).
DbJob<List<PlaylistRow>> allPlaylistsJob(String sortMode) => (db) {
  final order = sortMode == 'name'
      ? 'p.name COLLATE NOCASE ASC'
      : 'p.created DESC';
  return [
    for (final r in db.select('''
            SELECT p.id, p.name, p.created, p.color, COUNT(pt.track_path) AS track_count
            FROM playlists p
            LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
            GROUP BY p.id
            ORDER BY $order
        '''))
      PlaylistRow(
        id: r['id'] as int,
        name: r['name'] as String?,
        created: r['created'] as int?,
        color: r['color'] as String?,
        trackCount: r['track_count'] as int,
      ),
  ];
};

/// `get_tracks_in_playlist`: INNER joins, so a row whose track is gone
/// doesn't show.
DbJob<List<Track>> tracksInPlaylistJob(int playlistId) =>
    (db) => [
      for (final r in db.select(
        '''
            SELECT t.id, t.path, t.title, t.duration, t.format, t.track_num,
                   t.added_date, t.bitrate, t.album_id, al.artist_id,
                   al.title AS album, ar.name AS artist
            FROM playlist_tracks pt
            JOIN tracks t  ON t.path    = pt.track_path
            JOIN albums al ON al.id     = t.album_id
            JOIN artists ar ON ar.id    = al.artist_id
            WHERE pt.playlist_id = ?
            ORDER BY pt.order_index ASC
        ''',
        [playlistId],
      ))
        trackFromRow(r),
    ];

/// `create_playlist`: the name stripped. Throws on a name clash (NOCASE
/// UNIQUE), with SQLite's message, as Python's IntegrityError has.
DbJob<int> createPlaylistJob(String name) => (db) {
  db.execute('INSERT INTO playlists (name) VALUES (?)', [pyStrip(name)]);
  return db.lastInsertRowId;
};

/// `add_track_to_playlist` for each path in turn (Flet's loop), in one
/// transaction: each goes at the next order_index; one already there is
/// ignored.
DbJob<void> addTracksToPlaylistJob(int playlistId, List<String> paths) =>
    (db) => _transaction(db, () {
      for (final p in paths) {
        final next =
            db.select(
                  'SELECT COALESCE(MAX(order_index), -1) + 1 AS next_idx '
                  'FROM playlist_tracks WHERE playlist_id = ?',
                  [playlistId],
                ).first['next_idx']
                as int;
        db.execute(
          'INSERT OR IGNORE INTO playlist_tracks '
          '(playlist_id, track_path, order_index) VALUES (?, ?, ?)',
          [playlistId, p, next],
        );
      }
    });

DbJob<void> removeTrackFromPlaylistJob(int playlistId, String path) =>
    (db) => db.execute(
      'DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_path = ?',
      [playlistId, path],
    );

/// `move_playlist_track`: [from] and [to] are positions in the playlist as
/// shown. Rows whose track is gone are dropped first, then the indices are
/// clamped and every row renumbered.
DbJob<void> movePlaylistTrackJob(int playlistId, int from, int to) =>
    (db) => _transaction(db, () {
      db.execute(
        'DELETE FROM playlist_tracks '
        'WHERE playlist_id = ? '
        'AND track_path NOT IN (SELECT path FROM tracks)',
        [playlistId],
      );
      final paths = [
        for (final r in db.select(
          'SELECT track_path FROM playlist_tracks '
          'WHERE playlist_id = ? ORDER BY order_index ASC',
          [playlistId],
        ))
          r['track_path'] as String,
      ];
      if (paths.isEmpty) return;
      final f = from.clamp(0, paths.length - 1);
      final t = to.clamp(0, paths.length);
      final item = paths.removeAt(f);
      paths.insert(t > paths.length ? paths.length : t, item);
      for (var i = 0; i < paths.length; i++) {
        db.execute(
          'UPDATE playlist_tracks SET order_index = ? '
          'WHERE playlist_id = ? AND track_path = ?',
          [i, playlistId, paths[i]],
        );
      }
    });

/// `update_playlist`: an empty or null [name] or [color] is left as it is.
/// The name is not stripped (Flet's editor doesn't). Throws on a name clash.
DbJob<void> updatePlaylistJob(int playlistId, {String? name, String? color}) =>
    (db) => _transaction(db, () {
      if (name != null && name.isNotEmpty) {
        db.execute('UPDATE playlists SET name = ? WHERE id = ?', [
          name,
          playlistId,
        ]);
      }
      if (color != null && color.isNotEmpty) {
        db.execute('UPDATE playlists SET color = ? WHERE id = ?', [
          color,
          playlistId,
        ]);
      }
    });

/// `delete_playlist`: its playlist_tracks rows go by ON DELETE CASCADE
/// (the connection has foreign keys on, as Python's does).
DbJob<void> deletePlaylistJob(int playlistId) =>
    (db) => db.execute('DELETE FROM playlists WHERE id = ?', [playlistId]);

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
