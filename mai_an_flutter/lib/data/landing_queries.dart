import 'package:sqlite3/sqlite3.dart';

import '../core/db_worker.dart';

/// The Search tab's landing cards: `db_manager.get_most_played`,
/// `get_most_played_artists`, `get_most_played_genres` and
/// `get_library_counts`, with the same SQL, so the ranks come out in the
/// Flet app's order. As in browse_queries.dart, Python's double-quoted string
/// literals are single-quoted, and `t.*` is narrowed to the columns used.

/// A "Most Listened Tracks" row. [title] and [artist] are raw: the card's
/// fallbacks ("Untitled", "Unknown Artist") differ from the track rows'.
class MostPlayedTrack {
  const MostPlayedTrack({
    required this.path,
    required this.title,
    required this.artist,
    required this.count,
  });

  final String path;
  final String? title;
  final String? artist;
  final int count;
}

/// A "Most Listened Artists" or "Most Listened Genres" row. [secondary] is
/// `tracks_played` for an artist and `albums_played` for a genre.
class MostPlayedGroup {
  const MostPlayedGroup({
    required this.name,
    required this.plays,
    required this.secondary,
  });

  final String? name;
  final int plays;
  final int secondary;
}

/// "Library at a Glance": counted the way the Library tabs list them (an
/// album only when its artist row exists).
class LibraryCounts {
  const LibraryCounts({
    required this.tracks,
    required this.albums,
    required this.artists,
    required this.playlists,
  });

  final int tracks;
  final int albums;
  final int artists;
  final int playlists;
}

/// `get_most_played(limit)`. A row with count 0 is listed, as in Flet.
DbJob<List<MostPlayedTrack>> mostPlayedJob(int limit) =>
    (db) => [
      for (final r in db.select(
        '''
        SELECT t.path, t.title, ar.name AS artist, pc.count
        FROM play_counts pc
        JOIN tracks t ON t.path = pc.track_path
        JOIN albums al ON t.album_id = al.id
        JOIN artists ar ON al.artist_id = ar.id
        ORDER BY pc.count DESC, pc.last_played DESC
        LIMIT ?
      ''',
        [limit],
      ))
        MostPlayedTrack(
          path: r['path'] as String,
          title: r['title'] as String?,
          artist: r['artist'] as String?,
          count: (r['count'] as int?) ?? 0,
        ),
    ];

/// `get_most_played_artists(limit)`: summed track plays per artist.
DbJob<List<MostPlayedGroup>> mostPlayedArtistsJob(int limit) =>
    (db) => [
      for (final r in db.select(
        '''
        SELECT ar.name AS name, SUM(pc.count) AS plays,
               COUNT(DISTINCT pc.track_path) AS tracks_played
        FROM play_counts pc
        JOIN tracks t   ON t.path = pc.track_path
        JOIN albums al  ON al.id  = t.album_id
        JOIN artists ar ON ar.id  = al.artist_id
        WHERE pc.count > 0
        GROUP BY ar.id
        ORDER BY plays DESC, MAX(pc.last_played) DESC
        LIMIT ?
      ''',
        [limit],
      ))
        MostPlayedGroup(
          name: r['name'] as String?,
          plays: (r['plays'] as int?) ?? 0,
          secondary: (r['tracks_played'] as int?) ?? 0,
        ),
    ];

/// `get_most_played_genres(limit)`: the Genres tab's tokens (genre_bucket,
/// else genre; comma-split, junk dropped); a multi-genre album credits its
/// plays to each of its genres.
DbJob<List<MostPlayedGroup>> mostPlayedGenresJob(int limit) =>
    (db) => [
      for (final r in db.select(
        '''
        WITH RECURSIVE album_plays(album_id, plays) AS (
            SELECT t.album_id, SUM(pc.count)
            FROM play_counts pc
            JOIN tracks t ON t.path = pc.track_path
            WHERE pc.count > 0
            GROUP BY t.album_id
        ),
        split_genres(album_id, genre_token, rest) AS (
            SELECT id,
                   TRIM(SUBSTR(COALESCE(NULLIF(genre_bucket, ''), genre, 'Other'), 1, INSTR(COALESCE(NULLIF(genre_bucket, ''), genre, 'Other') || ',', ',') - 1)),
                   SUBSTR(COALESCE(NULLIF(genre_bucket, ''), genre, 'Other') || ',', INSTR(COALESCE(NULLIF(genre_bucket, ''), genre, 'Other') || ',', ',') + 1)
            FROM albums
            WHERE id IN (SELECT album_id FROM album_plays)
              AND ((genre_bucket IS NOT NULL AND genre_bucket != '') OR (genre IS NOT NULL AND genre != ''))
            UNION ALL
            SELECT album_id,
                   TRIM(SUBSTR(rest, 1, INSTR(rest, ',') - 1)),
                   SUBSTR(rest, INSTR(rest, ',') + 1)
            FROM split_genres
            WHERE rest != ''
        )
        SELECT sg.genre_token AS name, SUM(ap.plays) AS plays,
               COUNT(DISTINCT sg.album_id) AS albums_played
        FROM split_genres sg
        JOIN album_plays ap ON ap.album_id = sg.album_id
        WHERE sg.genre_token != ''
          AND LOWER(sg.genre_token) NOT IN ('unknown', 'various', 'divers', 'misc', 'other')
        GROUP BY sg.genre_token
        ORDER BY plays DESC, name COLLATE NOCASE ASC
        LIMIT ?
      ''',
        [limit],
      ))
        MostPlayedGroup(
          name: r['name'] as String?,
          plays: (r['plays'] as int?) ?? 0,
          secondary: (r['albums_played'] as int?) ?? 0,
        ),
    ];

/// `get_library_counts`.
LibraryCounts libraryCountsJob(Database db) {
  final r = db
      .select(
        'SELECT (SELECT COUNT(*) FROM tracks), '
        '(SELECT COUNT(*) FROM albums al JOIN artists ar ON ar.id = al.artist_id), '
        '(SELECT COUNT(*) FROM artists), '
        '(SELECT COUNT(*) FROM playlists)',
      )
      .first;
  return LibraryCounts(
    tracks: r.columnAt(0) as int,
    albums: r.columnAt(1) as int,
    artists: r.columnAt(2) as int,
    playlists: r.columnAt(3) as int,
  );
}
