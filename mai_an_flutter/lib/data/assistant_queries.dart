import 'dart:convert';
import 'dart:math' as math;

import 'package:sqlite3/sqlite3.dart';

import '../core/db_worker.dart';
import '../core/py_re.dart';
import '../core/py_text.dart';

/// Database queries for `AssistantRunner` and `llm_tools.execute_tool`
/// (phase 5, stages c2 and d):
/// `db_manager.search_tracks_simple`, `get_all_tracks` (raw dict rows, with
/// search), `get_track_full`, `get_most_played` (full rows), `get_total_tracks`,
/// `get_all_artists`, `get_all_albums`, `get_albums_by_artist`,
/// `get_tracks_by_album`, `get_all_playlists`, `get_tracks_in_playlist`,
/// `get_recent_tracks`, `recent_played_paths`, `record_playback`,
/// `_apply_anaphora_resolution`'s NOCASE artist-track lookup, and the
/// read-only metadata queries (`get_artist_enrichment`, `get_ai_research`,
/// `get_pending_ai_research`, `get_artist_source_genres`,
/// `get_metadata_gap_artists`, `get_metadata_coverage`).
///
/// Exact SQL and Python fallback rules, golden-tested against Python's
/// `DatabaseManager` (`tool/export_assistant_db_goldens.py`,
/// `test/assistant_adapters_test.dart`).

final _byOrFrom = PyRe(r'\s+(?:by|from)\s+', ignoreCase: true);
final _ws = PyRe(r'\s+');
final _ftsPunct = PyRe(r'''["*\-^'\[\]{}~:()|,]''');

const _searchStopWords = {
  'a',
  'an',
  'the',
  'by',
  'from',
  'of',
  'in',
  'on',
  'at',
  'to',
  'and',
  'or',
  'for',
  'some',
  'any',
};

Map<String, Object?> _rowMap(Row r) => {for (final col in r.keys) col: r[col]};

/// `str.split()` on Python whitespace (`pyIsSpace`).
List<String> _pySplitWs(String s) {
  final stripped = pyStrip(s);
  if (stripped.isEmpty) return const [];
  return stripped.split(_ws.regExp);
}

/// `db_manager.search_tracks_simple(query, limit)`.
DbJob<List<Map<String, Object?>>> searchTracksSimpleJob(
  String query,
  int limit,
) => (db) {
  final q = pyStrip(query);
  if (q.isEmpty) return const [];

  // 1. Split by "by" or "from" (e.g. "comfortably numb by pink floyd").
  final byParts = q.split(_byOrFrom.regExp);
  if (byParts.length == 2) {
    final p1 = pyStrip(byParts[0]);
    final p2 = pyStrip(byParts[1]);
    if (p1.isNotEmpty && p2.isNotEmpty) {
      final like1 = '%$p1%';
      final like2 = '%$p2%';
      final rows = db.select(
        '''
        SELECT t.path, t.title, t.duration,
               ar.name AS artist, al.title AS album
        FROM tracks t
        LEFT JOIN albums  al ON al.id     = t.album_id
        LEFT JOIN artists ar ON ar.id     = al.artist_id
        WHERE (t.title LIKE ? COLLATE NOCASE AND ar.name LIKE ? COLLATE NOCASE)
           OR (t.title LIKE ? COLLATE NOCASE AND ar.name LIKE ? COLLATE NOCASE)
        ORDER BY t.added_date DESC
        LIMIT ?
        ''',
        [like1, like2, like2, like1, limit],
      );
      if (rows.isNotEmpty) return [for (final r in rows) _rowMap(r)];
    }
  }

  // 2. Multi-word intersection query across title / artist / album.
  final allWords = _pySplitWs(q);
  var words = [
    for (final w in allWords)
      if (!_searchStopWords.contains(pyLower(w))) w,
  ];
  if (words.isEmpty) words = allWords;

  if (words.isNotEmpty) {
    final clauses = <String>[];
    final params = <Object?>[];
    for (final w in words) {
      clauses.add(
        '(t.title LIKE ? COLLATE NOCASE OR ar.name LIKE ? COLLATE NOCASE '
        'OR al.title LIKE ? COLLATE NOCASE)',
      );
      final likeW = '%$w%';
      params.addAll([likeW, likeW, likeW]);
    }
    final exactLike = '%$q%';
    params.addAll([exactLike, exactLike, limit]);
    final rows = db.select('''
      SELECT t.path, t.title, t.duration,
             ar.name AS artist, al.title AS album
      FROM tracks t
      LEFT JOIN albums  al ON al.id     = t.album_id
      LEFT JOIN artists ar ON ar.id     = al.artist_id
      WHERE ${clauses.join(' AND ')}
      ORDER BY
          CASE
              WHEN t.title LIKE ? COLLATE NOCASE THEN 0
              WHEN ar.name LIKE ? COLLATE NOCASE THEN 1
              ELSE 2
          END,
          t.added_date DESC
      LIMIT ?
      ''', params);
    if (rows.isNotEmpty) return [for (final r in rows) _rowMap(r)];
  }

  // 3. Fallback to single rigid LIKE match.
  final like = '%$q%';
  final rows = db.select(
    '''
    SELECT t.path, t.title, t.duration,
           ar.name AS artist, al.title AS album
    FROM tracks t
    LEFT JOIN albums  al ON al.id     = t.album_id
    LEFT JOIN artists ar ON ar.id     = al.artist_id
    WHERE t.title  LIKE ? COLLATE NOCASE
       OR ar.name  LIKE ? COLLATE NOCASE
       OR al.title LIKE ? COLLATE NOCASE
    ORDER BY
        CASE
            WHEN t.title  LIKE ? COLLATE NOCASE THEN 0
            WHEN ar.name  LIKE ? COLLATE NOCASE THEN 1
            ELSE 2
        END,
        t.added_date DESC
    LIMIT ?
    ''',
    [like, like, like, like, like, limit],
  );
  return [for (final r in rows) _rowMap(r)];
};

/// `db_manager.get_total_tracks()`.
int totalTracksJob(Database db) {
  final rows = db.select('SELECT COUNT(*) FROM tracks');
  return rows.isEmpty ? 0 : (rows.first.columnAt(0) as int? ?? 0);
}

/// `db_manager.get_all_tracks(search_query, sort_mode="date")`.
DbJob<List<Map<String, Object?>>> allTracksRawJob([String searchQuery = '']) =>
    (db) {
      const order = 't.added_date DESC';
      const base = '''
        SELECT t.*, al.title AS album, ar.name AS artist, al.year, al.genre
        FROM tracks t
        JOIN albums al ON t.album_id = al.id
        JOIN artists ar ON al.artist_id = ar.id
      ''';
      if (searchQuery.isEmpty) {
        return [for (final r in db.select('$base ORDER BY $order')) _rowMap(r)];
      }
      final likeQ = '%$searchQuery%';
      final safeQ = _safeFts(searchQuery);
      if (safeQ.isNotEmpty) {
        try {
          final rows = db.select(
            '$base JOIN fts_search f ON f.rowid = t.id '
            'WHERE fts_search MATCH ? ORDER BY $order',
            ['$safeQ*'],
          );
          if (rows.isNotEmpty) return [for (final r in rows) _rowMap(r)];
        } catch (_) {}
      }
      final likeRows = db.select(
        '$base WHERE t.title LIKE ? OR al.title LIKE ? OR ar.name LIKE ? '
        'ORDER BY $order',
        [likeQ, likeQ, likeQ],
      );
      if (likeRows.isNotEmpty) return [for (final r in likeRows) _rowMap(r)];

      final allRows = db.select('$base ORDER BY $order');
      final scored = <(double, int, Map<String, Object?>)>[];
      for (var i = 0; i < allRows.length; i++) {
        final r = allRows[i];
        final title = (r['title'] as String?) ?? '';
        final album = (r['album'] as String?) ?? '';
        final artist = (r['artist'] as String?) ?? '';
        final score = math.max(
          _pyKmerSimilarity(searchQuery, title),
          math.max(
            _pyKmerSimilarity(searchQuery, album),
            _pyKmerSimilarity(searchQuery, artist),
          ),
        );
        if (score >= 0.25) scored.add((score, i, _rowMap(r)));
      }
      scored.sort((a, b) {
        final c = b.$1.compareTo(a.$1);
        return c != 0 ? c : a.$2.compareTo(b.$2);
      });
      return [for (final (_, _, m) in scored) m];
    };

/// `db_manager.get_track_full(path)`.
DbJob<Map<String, Object?>?> trackFullJob(String path) => (db) {
  final rows = db.select(
    '''
    SELECT t.path, t.title, t.duration, t.format, t.bpm, t.energy, t.brightness,
           ar.name AS artist, al.title AS album, al.year, al.genre,
           pc.timbre
    FROM tracks t
    LEFT JOIN albums      al ON al.id          = t.album_id
    LEFT JOIN artists     ar ON ar.id          = al.artist_id
    LEFT JOIN play_counts pc ON pc.track_path  = t.path
    WHERE t.path = ?
    ''',
    [path],
  );
  return rows.isEmpty ? null : _rowMap(rows.first);
};

/// `db_manager.get_most_played(limit)` with every column `t.*`, album, artist,
/// and count (the runner's `_handle_play_the_usual` reads `album` and
/// `duration` too).
DbJob<List<Map<String, Object?>>> mostPlayedFullJob(int limit) =>
    (db) => [
      for (final r in db.select(
        '''
        SELECT t.*, al.title AS album, ar.name AS artist, pc.count
        FROM play_counts pc
        JOIN tracks t ON t.path = pc.track_path
        JOIN albums al ON t.album_id = al.id
        JOIN artists ar ON al.artist_id = ar.id
        ORDER BY pc.count DESC, pc.last_played DESC
        LIMIT ?
        ''',
        [limit],
      ))
        _rowMap(r),
    ];

/// `db_manager.recent_played_paths(window_seconds)`.
DbJob<Set<String>> recentPlayedPathsJob(int windowSeconds) =>
    (db) => {
      for (final r in db.select(
        "SELECT DISTINCT track_path FROM playback_history "
        "WHERE event = 'played' "
        "AND played_at >= strftime('%s','now') - ?",
        [windowSeconds],
      ))
        r.columnAt(0) as String,
    };

/// `db_manager.record_playback(path, event, seed_path)`.
void Function(Database) recordPlaybackJob(
  String path,
  String event, {
  String? seedPath,
}) => (db) {
  if (path.isEmpty) return;
  db.execute(
    "INSERT INTO playback_history (track_path, played_at, event, seed_path) "
    "VALUES (?, strftime('%s','now'), ?, ?)",
    [path, event, seedPath],
  );
};

/// `db_manager._safe_fts`.
String _safeFts(String query) =>
    _ws.sub(pyStrip(_ftsPunct.sub(query, ' ')), ' ');

/// `db_manager._kmer_similarity` using Python's `lower()` and `strip()` over
/// code points.
double _pyKmerSimilarity(String query, String target) {
  if (query.isEmpty || target.isEmpty) return 0.0;
  final q = pyStrip(pyLower(query));
  final t = pyStrip(pyLower(target));
  if (q == t) return 1.0;
  if (t.contains(q)) return 0.8 + 0.2 * (q.runes.length / t.runes.length);
  Set<String> bigrams(String s) {
    final chars = [for (final r in s.runes) String.fromCharCode(r)];
    return {
      for (var i = 0; i < chars.length - 1; i++) '${chars[i]}${chars[i + 1]}',
    };
  }

  final qs = bigrams(q), ts = bigrams(t);
  if (qs.isEmpty || ts.isEmpty) return 0.0;
  return 2.0 * qs.intersection(ts).length / (qs.length + ts.length);
}

/// `db_manager.get_all_artists(search_query=query, sort_mode="name")`.
DbJob<List<Map<String, Object?>>> allArtistsRawJob([String query = '']) =>
    (db) {
      const order = 'name COLLATE NOCASE ASC';
      const base = 'SELECT id, name, album_count, track_count FROM artists';
      if (query.isEmpty) {
        return [for (final r in db.select('$base ORDER BY $order')) _rowMap(r)];
      }
      final likeQ = '%$query%';
      final safeQ = _safeFts(query);
      var ftsSubq = '';
      final params = <Object?>[likeQ];
      if (safeQ.isNotEmpty) {
        try {
          final hit = db.select(
            'SELECT rowid FROM fts_search WHERE fts_search MATCH ? LIMIT 1',
            ['$safeQ*'],
          );
          if (hit.isNotEmpty) {
            ftsSubq =
                'OR id IN (SELECT DISTINCT ar2.id FROM artists ar2 '
                'JOIN albums al2 ON al2.artist_id = ar2.id '
                'JOIN tracks t2 ON t2.album_id = al2.id '
                'WHERE t2.id IN (SELECT rowid FROM fts_search '
                'WHERE fts_search MATCH ?))';
            params.add('$safeQ*');
          }
        } catch (_) {}
      }
      final rows = db.select(
        '$base WHERE name LIKE ? $ftsSubq ORDER BY $order',
        params,
      );
      if (rows.isNotEmpty) {
        return [for (final r in rows) _rowMap(r)];
      }
      final allRows = db.select('$base ORDER BY $order');
      final scored = <(double, int, Map<String, Object?>)>[];
      for (var i = 0; i < allRows.length; i++) {
        final name = (allRows[i]['name'] as String?) ?? '';
        final score = _pyKmerSimilarity(query, name);
        if (score >= 0.25) scored.add((score, i, _rowMap(allRows[i])));
      }
      scored.sort((a, b) {
        final c = b.$1.compareTo(a.$1);
        return c != 0 ? c : a.$2.compareTo(b.$2);
      });
      return [for (final (_, _, m) in scored) m];
    };

/// The artist names `db_manager.get_all_artists(search_query=query)` returns,
/// in its order (LIKE + FTS5 match, else `_kmer_similarity >= 0.25`).
DbJob<List<String>> searchArtistNamesJob(String query) =>
    (db) => [for (final r in allArtistsRawJob(query)(db)) r['name'] as String];

/// `db_manager.get_all_albums(search_query=query, sort_mode="date")`.
DbJob<List<Map<String, Object?>>> allAlbumsRawJob([String query = '']) => (db) {
  const order = 'latest_added DESC';
  const base = '''
    SELECT al.id, al.title AS album, al.year, al.genre, ar.name AS artist, al.track_count,
           (SELECT MAX(added_date) FROM tracks WHERE album_id = al.id) AS latest_added
    FROM albums al JOIN artists ar ON al.artist_id = ar.id
  ''';
  if (query.isEmpty) {
    return [for (final r in db.select('$base ORDER BY $order')) _rowMap(r)];
  }
  final likeQ = '%$query%';
  final safeQ = _safeFts(query);
  var ftsSubq = '';
  final params = <Object?>[likeQ, likeQ];
  if (safeQ.isNotEmpty) {
    try {
      final hit = db.select(
        'SELECT rowid FROM fts_search WHERE fts_search MATCH ? LIMIT 1',
        ['$safeQ*'],
      );
      if (hit.isNotEmpty) {
        ftsSubq =
            'OR al.id IN (SELECT DISTINCT album_id FROM tracks '
            'WHERE id IN (SELECT rowid FROM fts_search '
            'WHERE fts_search MATCH ?))';
        params.add('$safeQ*');
      }
    } catch (_) {}
  }
  final rows = db.select(
    '$base WHERE al.title LIKE ? OR ar.name LIKE ? $ftsSubq ORDER BY $order',
    params,
  );
  if (rows.isNotEmpty) return [for (final r in rows) _rowMap(r)];

  final allRows = db.select('$base ORDER BY $order');
  final scored = <(double, int, Map<String, Object?>)>[];
  for (var i = 0; i < allRows.length; i++) {
    final r = allRows[i];
    final album = (r['album'] as String?) ?? '';
    final artist = (r['artist'] as String?) ?? '';
    final score = math.max(
      _pyKmerSimilarity(query, album),
      _pyKmerSimilarity(query, artist),
    );
    if (score >= 0.25) scored.add((score, i, _rowMap(r)));
  }
  scored.sort((a, b) {
    final c = b.$1.compareTo(a.$1);
    return c != 0 ? c : a.$2.compareTo(b.$2);
  });
  return [for (final (_, _, m) in scored) m];
};

/// `db_manager.get_albums_by_artist(artist_name)`.
DbJob<List<Map<String, Object?>>> albumsByArtistRawJob(String artistName) =>
    (db) => [
      for (final r in db.select(
        '''
        SELECT al.id, al.title AS album, al.year, al.genre, ar.name AS artist, al.track_count,
               (SELECT MAX(added_date) FROM tracks WHERE album_id = al.id) AS latest_added
        FROM albums al JOIN artists ar ON al.artist_id = ar.id
        WHERE ar.name = ? ORDER BY al.title ASC
        ''',
        [artistName],
      ))
        _rowMap(r),
    ];

/// `db_manager.get_tracks_by_album(album_title, artist_name)`.
DbJob<List<Map<String, Object?>>> tracksByAlbumRawJob(
  String albumTitle,
  String artistName,
) =>
    (db) => [
      for (final r in db.select(
        '''
        SELECT t.*, al.title AS album, ar.name AS artist, al.year, al.genre
        FROM tracks t JOIN albums al ON t.album_id = al.id JOIN artists ar ON al.artist_id = ar.id
        WHERE al.title = ? AND ar.name = ? ORDER BY t.track_num ASC
        ''',
        [albumTitle, artistName],
      ))
        _rowMap(r),
    ];

/// `db_manager.get_all_playlists(search_query=query, sort_mode="date")`.
DbJob<List<Map<String, Object?>>> allPlaylistsRawJob([String query = '']) =>
    (db) {
      const order = 'p.created DESC';
      final where = query.isNotEmpty ? 'WHERE p.name LIKE ?' : '';
      final params = query.isNotEmpty
          ? <Object?>['%$query%']
          : const <Object?>[];
      final rows = db.select('''
        SELECT p.id, p.name, p.created, p.color, COUNT(pt.track_path) AS track_count
        FROM playlists p
        LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
        $where
        GROUP BY p.id
        ORDER BY $order
      ''', params);
      if (rows.isNotEmpty || query.isEmpty) {
        return [for (final r in rows) _rowMap(r)];
      }
      final allRows = db.select('''
        SELECT p.id, p.name, p.created, p.color, COUNT(pt.track_path) AS track_count
        FROM playlists p
        LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
        GROUP BY p.id
        ORDER BY $order
      ''');
      final scored = <(double, int, Map<String, Object?>)>[];
      for (var i = 0; i < allRows.length; i++) {
        final name = (allRows[i]['name'] as String?) ?? '';
        final score = _pyKmerSimilarity(query, name);
        if (score >= 0.25) scored.add((score, i, _rowMap(allRows[i])));
      }
      scored.sort((a, b) {
        final c = b.$1.compareTo(a.$1);
        return c != 0 ? c : a.$2.compareTo(b.$2);
      });
      return [for (final (_, _, m) in scored) m];
    };

/// `db_manager.get_tracks_in_playlist(playlist_id)` (full track dict rows).
DbJob<List<Map<String, Object?>>> tracksInPlaylistRawJob(int playlistId) =>
    (db) => [
      for (final r in db.select(
        '''
        SELECT t.*, al.title AS album, ar.name AS artist, al.year, al.genre
        FROM playlist_tracks pt
        JOIN tracks t  ON t.path    = pt.track_path
        JOIN albums al ON al.id     = t.album_id
        JOIN artists ar ON ar.id    = al.artist_id
        WHERE pt.playlist_id = ?
        ORDER BY pt.order_index ASC
        ''',
        [playlistId],
      ))
        _rowMap(r),
    ];

/// `db_manager.get_recent_tracks(limit)`.
DbJob<List<Map<String, Object?>>> recentTracksRawJob(int limit) =>
    (db) => [
      for (final r in db.select(
        '''
        SELECT t.path, t.title, ar.name AS artist, al.title AS album, al.genre,
               MAX(ph.played_at) AS last_played
        FROM playback_history ph
        JOIN tracks t   ON t.path = ph.track_path
        JOIN albums al  ON al.id  = t.album_id
        JOIN artists ar ON ar.id  = al.artist_id
        WHERE ph.event IN ('played', 'completed')
        GROUP BY t.path
        ORDER BY last_played DESC
        LIMIT ?
        ''',
        [limit],
      ))
        _rowMap(r),
    ];

/// `_apply_anaphora_resolution`'s NOCASE artist seed lookup.
DbJob<String?> firstTrackPathByArtistJob(String name) => (db) {
  final rows = db.select(
    '''
    SELECT t.path
    FROM tracks t
    JOIN albums al ON al.id = t.album_id
    JOIN artists ar ON ar.id = al.artist_id
    WHERE ar.name = ? COLLATE NOCASE
    LIMIT 1
    ''',
    [name],
  );
  return rows.isEmpty ? null : rows.first['path'] as String?;
};

// ── Read-only metadata queries ──────────────────────────────────────────────

const _sourceTagJunk = {'divers', 'various', 'unknown', 'other', 'misc', 'n/a'};

const _sourceTagLocalised = {
  'électronique': 'Electronic',
  'electronique': 'Electronic',
  'danse': 'Dance',
  'musique du monde': 'World',
  'bandes originales': 'Soundtrack',
};

const _sourceTagCanon = {
  'alt': 'alternative rock',
  'folk/cntry': 'folk',
  'soul/r&b': 'soul',
  'rock/alt': 'rock',
  'asian-pop': 'asian',
};

final _sourceTagSplit = PyRe(r'[,/;|]');

/// `db_manager._clean_source_tags`.
List<String> cleanSourceTags(Iterable<String?> genreStrings) {
  final out = <String>[];
  final seen = <String>{};
  for (final genreStr in genreStrings) {
    final raw = pyStrip(genreStr ?? '');
    final parts = _sourceTagCanon.containsKey(pyLower(raw))
        ? [raw]
        : raw.split(_sourceTagSplit.regExp);
    for (final part in parts) {
      var tok = pyStrip(part);
      if (tok.isEmpty) continue;
      final low = pyLower(tok);
      if (_sourceTagJunk.contains(low)) continue;
      tok = _sourceTagCanon[low] ?? _sourceTagLocalised[low] ?? tok;
      final key = pyLower(tok);
      if (seen.add(key)) out.add(tok);
    }
  }
  return out;
}

/// `db_manager.get_artist_enrichment(name)`.
DbJob<Map<String, Object?>?> artistEnrichmentJob(String name) => (db) {
  if (name.isEmpty) return null;
  final rows = db.select(
    'SELECT artist_name, mbid, country, area, genres, source, score, status, '
    'provenance, fetched_at '
    'FROM artist_enrichment WHERE artist_name = ?',
    [name],
  );
  if (rows.isEmpty) return null;
  final out = _rowMap(rows.first);
  out['genres'] = _decodeJsonOr(out['genres'], <Object?>[]);
  out['provenance'] = _decodeJsonOr(out['provenance'], <String, Object?>{});
  return out;
};

/// `db_manager.get_ai_research(artist_name)`.
DbJob<Map<String, Object?>?> aiResearchJob(String artistName) => (db) {
  final rows = db.select(
    'SELECT payload, state, created_at FROM artist_ai_research '
    'WHERE artist_name = ?',
    [artistName],
  );
  if (rows.isEmpty) return null;
  final r = rows.first;
  final raw = r.columnAt(0);
  if (raw is! String) return null;
  final Object? payload;
  try {
    payload = jsonDecode(raw);
  } catch (_) {
    return null;
  }
  return {
    'payload': payload,
    'state': r.columnAt(1),
    'created_at': r.columnAt(2),
  };
};

/// `db_manager.get_pending_ai_research()`.
Map<String, Object?> pendingAiResearchJob(Database db) {
  final rows = db.select(
    'SELECT r.artist_name, r.payload FROM artist_ai_research r '
    'LEFT JOIN artist_enrichment e ON e.artist_name = r.artist_name '
    "WHERE r.state = 'pending' AND (e.source IS NULL OR e.source <> 'manual')",
  );
  final out = <String, Object?>{};
  for (final r in rows) {
    final name = r.columnAt(0);
    final raw = r.columnAt(1);
    if (name is! String || raw is! String) continue;
    try {
      out[name] = jsonDecode(raw);
    } catch (_) {}
  }
  return out;
}

/// `db_manager.get_artist_source_genres(artist_name)`.
DbJob<List<String>> artistSourceGenresJob(String artistName) => (db) {
  final rows = db.select(
    'SELECT al.genre FROM albums al JOIN artists ar ON ar.id = al.artist_id '
    "WHERE ar.name = ? AND al.genre IS NOT NULL AND al.genre <> ''",
    [artistName],
  );
  return cleanSourceTags([for (final r in rows) r.columnAt(0) as String?]);
};

const _gapBlocking = 0;
const _gapSuggested = 1;
const _gapThin = 2;
const _gapNone = 3;

/// `db_manager.get_metadata_gap_artists(limit=2000)`.
DbJob<List<Map<String, Object?>>> metadataGapArtistsJob(int limit) => (db) {
  final rows = db.select(
    'SELECT a.name AS artist_name, a.track_count AS track_count, '
    'e.country AS country, e.genres AS genres, e.source AS source, '
    'e.status AS status, e.provenance AS provenance, e.score AS score, '
    "(SELECT group_concat(al.genre, '|') FROM albums al "
    "   WHERE al.artist_id = a.id AND al.genre IS NOT NULL AND al.genre <> '' "
    ') AS raw_file_genres '
    'FROM artists a LEFT JOIN artist_enrichment e ON e.artist_name = a.name '
    "WHERE (e.artist_name IS NULL OR e.genres IS NULL OR e.genres = '' OR e.genres = '[]' "
    "       OR e.country IS NULL OR e.country = '') "
    "  AND (e.source IS NULL OR e.source <> 'manual') "
    'ORDER BY a.track_count DESC',
  );
  final out = <(int, int, int, Map<String, Object?>)>[];
  for (var i = 0; i < rows.length; i++) {
    final d = _rowMap(rows[i]);
    final genres = _decodeJsonOr(d['genres'], <Object?>[]);
    d['genres'] = genres;
    d['provenance'] = _decodeJsonOr(d['provenance'], <String, Object?>{});
    final rawFiles = (d.remove('raw_file_genres') as String?) ?? '';
    final sourceGenres = cleanSourceTags(rawFiles.split('|'));
    d['source_genres'] = sourceGenres;

    final hasGenres = genres is List ? genres.isNotEmpty : genres != null;
    final hasCountry = pyStrip((d['country'] as String?) ?? '').isNotEmpty;
    final hasFiles = sourceGenres.isNotEmpty;
    final sev = hasGenres && hasCountry
        ? _gapNone
        : (hasGenres || hasCountry)
        ? _gapThin
        : hasFiles
        ? _gapSuggested
        : _gapBlocking;
    d['gap_severity'] = sev;
    final tc = (d['track_count'] as int?) ?? 0;
    out.add((sev, -tc, i, d));
  }
  out.sort((a, b) {
    final c1 = a.$1.compareTo(b.$1);
    if (c1 != 0) return c1;
    final c2 = a.$2.compareTo(b.$2);
    if (c2 != 0) return c2;
    return a.$3.compareTo(b.$3);
  });
  return [for (final e in out.take(limit)) e.$4];
};

/// `db_manager.get_metadata_coverage(gaps=gaps)`.
DbJob<Map<String, Object?>> metadataCoverageJob([
  List<Map<String, Object?>>? gaps,
]) => (db) {
  int scalar(String sql) {
    final r = db.select(sql);
    if (r.isEmpty) return 0;
    final v = r.first.columnAt(0);
    return v is num ? v.toInt() : 0;
  }

  final total = scalar('SELECT COUNT(*) FROM tracks');
  const base =
      'FROM tracks t JOIN albums al ON al.id=t.album_id '
      'JOIN artists ar ON ar.id=al.artist_id '
      'LEFT JOIN artist_enrichment e ON e.artist_name=ar.name ';
  const hasG = "e.genres IS NOT NULL AND e.genres<>'' AND e.genres<>'[]'";
  const hasC = "e.country IS NOT NULL AND e.country<>''";
  final withGenres = scalar('SELECT COUNT(*) ${base}WHERE $hasG');
  final withCountry = scalar('SELECT COUNT(*) ${base}WHERE $hasC');
  final tracksCritical = scalar(
    'SELECT COUNT(*) ${base}WHERE NOT ($hasG) AND NOT ($hasC)',
  );
  final totalArtists = scalar('SELECT COUNT(*) FROM artists');
  final gapList = gaps ?? metadataGapArtistsJob(1000000)(db);
  var blocking = 0, suggested = 0, thin = 0;
  for (final g in gapList) {
    switch (g['gap_severity'] ?? _gapThin) {
      case _gapBlocking:
        blocking++;
      case _gapSuggested:
        suggested++;
      case _gapThin:
        thin++;
    }
  }
  return {
    'tracks': total,
    'tracks_with_genres': withGenres,
    'tracks_with_country': withCountry,
    'tracks_critical': tracksCritical,
    'tracks_partial': math.max(0, total - withGenres - tracksCritical),
    'genre_pct': total > 0 ? withGenres / total : 0.0,
    'country_pct': total > 0 ? withCountry / total : 0.0,
    'artists': totalArtists,
    'placeable': math.max(0, totalArtists - blocking),
    'gap_artists': gapList.length,
    'blocking': blocking,
    'suggested': suggested,
    'thin': thin,
    'critical': blocking,
    'no_genres': suggested,
    'no_country': thin,
  };
};

Object? _decodeJsonOr(Object? raw, Object fallback) {
  if (raw is! String || raw.isEmpty) return fallback;
  try {
    return jsonDecode(raw);
  } catch (_) {
    return fallback;
  }
}
