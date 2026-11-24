import 'package:sqlite3/sqlite3.dart';

import '../core/db_worker.dart';
import '../core/py_re.dart';
import '../core/py_text.dart';

/// Database queries for `AssistantRunner` (phase 5, stage c2):
/// `db_manager.search_tracks_simple`, `get_all_tracks` (raw dict rows),
/// `get_track_full`, `get_most_played` (full rows), `recent_played_paths`,
/// `record_playback`, `get_all_artists(search_query)` (names), and
/// `_apply_anaphora_resolution`'s NOCASE artist-track lookup.
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

/// `db_manager.get_all_tracks()` (unfiltered, default `sort_mode="date"`).
List<Map<String, Object?>> allTracksRawJob(Database db) => [
  for (final r in db.select('''
    SELECT t.*, al.title AS album, ar.name AS artist, al.year, al.genre
    FROM tracks t
    JOIN albums al ON t.album_id = al.id
    JOIN artists ar ON al.artist_id = ar.id
    ORDER BY t.added_date DESC
  '''))
    _rowMap(r),
];

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

/// The artist names `db_manager.get_all_artists(search_query=query)` returns,
/// in its order (LIKE + FTS5 match, else `_kmer_similarity >= 0.25`).
DbJob<List<String>> searchArtistNamesJob(String query) => (db) {
  const order = 'name COLLATE NOCASE ASC';
  if (query.isEmpty) {
    return [
      for (final r in db.select('SELECT name FROM artists ORDER BY $order'))
        r['name'] as String,
    ];
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
    'SELECT name FROM artists WHERE name LIKE ? $ftsSubq ORDER BY $order',
    params,
  );
  if (rows.isNotEmpty) {
    return [for (final r in rows) r['name'] as String];
  }
  // Fallback to closest match (stable descending sort on score).
  final allRows = db.select('SELECT name FROM artists ORDER BY $order');
  final scored = <(double, int, String)>[];
  for (var i = 0; i < allRows.length; i++) {
    final name = (allRows[i]['name'] as String?) ?? '';
    final score = _pyKmerSimilarity(query, name);
    if (score >= 0.25) scored.add((score, i, name));
  }
  scored.sort((a, b) {
    final c = b.$1.compareTo(a.$1);
    return c != 0 ? c : a.$2.compareTo(b.$2);
  });
  return [for (final (_, _, name) in scored) name];
};

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
