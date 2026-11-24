import 'package:sqlite3/sqlite3.dart';

import '../core/db_worker.dart';
import 'library_repository.dart';

/// The Albums, Artists and Genres tabs' queries: `db_manager.get_all_albums`,
/// `get_all_artists`, `get_all_genres`, `get_artists_by_genre`,
/// `get_albums_by_artist`, `get_tracks_by_album` and `get_tracks_by_genre`,
/// with the same SQL and sort maps, so the rows come out in the Flet app's
/// order (ties included). Search runs in memory (LibraryController), so the
/// search halves of those methods aren't here.
///
/// One change to the SQL: Python's double-quoted string literals ("Other",
/// ",") are single-quoted. SQLite reads them the same way, but builds without
/// SQLITE_DQS reject the double-quoted form.

class AlbumRow {
  const AlbumRow({
    required this.id,
    required this.title,
    required this.artist,
    required this.trackCount,
  });

  final int id;

  /// Raw `albums.title` / `artists.name`: the expansion queries match on
  /// them, as Flet's do.
  final String? title;
  final String? artist;
  final int? trackCount;

  /// library.py `_album_row`: `album or "Unknown Album"`,
  /// `artist or "Unknown Artist"`.
  String get displayTitle => _or(title, 'Unknown Album');
  String get displayArtist => _or(artist, 'Unknown Artist');
}

class ArtistRow {
  const ArtistRow({
    required this.id,
    required this.name,
    required this.albumCount,
    required this.trackCount,
  });

  final int id;
  final String? name;
  final int? albumCount;
  final int? trackCount;

  /// library.py `_artist_row`: `name or "Unknown Artist"`.
  String get displayName => _or(name, 'Unknown Artist');
}

class GenreRow {
  const GenreRow({
    required this.name,
    required this.artistCount,
    required this.albumCount,
    required this.trackCount,
  });

  /// A token of `albums.genre_bucket` (else `albums.genre`), never empty.
  final String name;
  final int artistCount;
  final int albumCount;
  final int trackCount;
}

// ── Sort maps (db_manager's, keys and defaults as there) ────────────────────

const _albumOrder = {
  'date': 'latest_added DESC',
  'artist': 'ar.name COLLATE NOCASE ASC',
  'album': 'al.title COLLATE NOCASE ASC',
  'track': 'al.title COLLATE NOCASE ASC',
};
const _artistOrder = {
  'name': 'name COLLATE NOCASE ASC',
  'artist': 'name COLLATE NOCASE ASC',
  'tracks': 'track_count DESC',
  'albums': 'album_count DESC',
  'track': 'name COLLATE NOCASE ASC',
};
const _genreOrder = {
  'name': 'name COLLATE NOCASE ASC',
  'tracks': 'track_count DESC, name ASC',
  'albums': 'album_count DESC, name ASC',
  'artists': 'artist_count DESC, name ASC',
};

// ── Jobs (run on the DB isolate; each captures only strings) ────────────────

const _albumBase = '''
  SELECT al.id, al.title AS album, al.year, al.genre, ar.name AS artist, al.track_count,
         (SELECT MAX(added_date) FROM tracks WHERE album_id = al.id) AS latest_added
  FROM albums al JOIN artists ar ON al.artist_id = ar.id
''';

AlbumRow _album(Row r) => AlbumRow(
  id: r['id'] as int,
  title: r['album'] as String?,
  artist: r['artist'] as String?,
  trackCount: r['track_count'] as int?,
);

ArtistRow _artist(Row r) => ArtistRow(
  id: r['id'] as int,
  name: r['name'] as String?,
  albumCount: r['album_count'] as int?,
  trackCount: r['track_count'] as int?,
);

/// `get_all_albums(sort_mode=…)`.
DbJob<List<AlbumRow>> allAlbumsJob(String sortMode) {
  final order = _albumOrder[sortMode] ?? 'latest_added DESC';
  return (db) => [
    for (final r in db.select('$_albumBase ORDER BY $order')) _album(r),
  ];
}

/// `get_all_artists(sort_mode=…)`.
DbJob<List<ArtistRow>> allArtistsJob(String sortMode) {
  final order = _artistOrder[sortMode] ?? 'name COLLATE NOCASE ASC';
  return (db) => [
    for (final r in db.select(
      'SELECT id, name, album_count, track_count FROM artists ORDER BY $order',
    ))
      _artist(r),
  ];
}

/// `get_all_genres(sort_mode=…)`: the album genre strings split on commas,
/// placeholders dropped, counted per token.
DbJob<List<GenreRow>> allGenresJob(String sortMode) {
  final order = _genreOrder[sortMode] ?? 'name COLLATE NOCASE ASC';
  return (db) => [
    for (final r in db.select('''
      WITH RECURSIVE split_genres(album_id, artist_id, genre_token, rest, track_count) AS (
          SELECT id, artist_id,
                 TRIM(SUBSTR(COALESCE(NULLIF(genre_bucket, ''), genre, 'Other'), 1, INSTR(COALESCE(NULLIF(genre_bucket, ''), genre, 'Other') || ',', ',') - 1)) AS genre_token,
                 SUBSTR(COALESCE(NULLIF(genre_bucket, ''), genre, 'Other') || ',', INSTR(COALESCE(NULLIF(genre_bucket, ''), genre, 'Other') || ',', ',') + 1) AS rest,
                 track_count
          FROM albums
          WHERE (genre_bucket IS NOT NULL AND genre_bucket != '') OR (genre IS NOT NULL AND genre != '')
          UNION ALL
          SELECT album_id, artist_id,
                 TRIM(SUBSTR(rest, 1, INSTR(rest, ',') - 1)) AS genre_token,
                 SUBSTR(rest, INSTR(rest, ',') + 1) AS rest,
                 track_count
          FROM split_genres
          WHERE rest != ''
      )
      SELECT genre_token AS name,
             COUNT(DISTINCT artist_id) AS artist_count,
             COUNT(DISTINCT album_id) AS album_count,
             SUM(track_count) AS track_count
      FROM split_genres
      WHERE genre_token != '' AND LOWER(genre_token) NOT IN ('unknown', 'various', 'divers', 'misc', 'other')
      GROUP BY genre_token
      ORDER BY $order
    '''))
      GenreRow(
        name: r['name'] as String,
        artistCount: r['artist_count'] as int? ?? 0,
        albumCount: r['album_count'] as int? ?? 0,
        trackCount: r['track_count'] as int? ?? 0,
      ),
  ];
}

/// `get_artists_by_genre(genre)` (the expansion uses the default sort).
DbJob<List<ArtistRow>> artistsByGenreJob(String genre) =>
    (db) => [
      for (final r in db.select(
        '''
        SELECT ar.id, ar.name,
               COUNT(DISTINCT al.id) AS album_count,
               SUM(al.track_count) AS track_count
        FROM artists ar
        JOIN albums al ON al.artist_id = ar.id
        WHERE (',' || COALESCE(al.genre_bucket, al.genre, '') || ',') LIKE ?
           OR (al.genre LIKE ?)
        GROUP BY ar.id, ar.name
        ORDER BY ar.name COLLATE NOCASE ASC
      ''',
        ['%,$genre,%', '%$genre%'],
      ))
        _artist(r),
    ];

/// `get_albums_by_artist(artist, genre=…)`.
DbJob<List<AlbumRow>> albumsByArtistJob(String? artist, String? genre) {
  final params = <Object?>[artist];
  var genreClause = '';
  if (genre != null && genre.isNotEmpty) {
    genreClause =
        "AND ((',' || COALESCE(al.genre_bucket, al.genre, '') || ',') LIKE ? "
        'OR al.genre LIKE ?)';
    params.addAll(['%,$genre,%', '%$genre%']);
  }
  return (db) => [
    for (final r in db.select(
      '$_albumBase WHERE ar.name = ? $genreClause ORDER BY al.title ASC',
      params,
    ))
      _album(r),
  ];
}

const _trackColumns = '''
  SELECT t.id, t.path, t.title, t.duration, t.format, t.track_num,
         t.added_date, t.album_id, al.artist_id,
         al.title AS album, ar.name AS artist
  FROM tracks t JOIN albums al ON t.album_id = al.id JOIN artists ar ON al.artist_id = ar.id
''';

/// `get_tracks_by_album(album, artist)`: matched by title and artist name.
DbJob<List<Track>> tracksByAlbumJob(String? album, String? artist) =>
    (db) => [
      for (final r in db.select(
        '$_trackColumns WHERE al.title = ? AND ar.name = ? '
        'ORDER BY t.track_num ASC',
        [album, artist],
      ))
        trackFromRow(r),
    ];

/// `get_tracks_by_genre(genre)`: newest first.
DbJob<List<Track>> tracksByGenreJob(String genre) =>
    (db) => [
      for (final r in db.select(
        '''$_trackColumns
        WHERE (',' || COALESCE(al.genre_bucket, al.genre, '') || ',') LIKE ?
           OR (al.genre LIKE ?)
        ORDER BY t.added_date DESC''',
        ['%,$genre,%', '%$genre%'],
      ))
        trackFromRow(r),
    ];

String _or(String? v, String fallback) =>
    (v == null || v.isEmpty) ? fallback : v;
