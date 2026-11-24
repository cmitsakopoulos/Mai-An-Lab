import 'dart:convert';

import 'package:sqlite3/sqlite3.dart';

import '../core/db_worker.dart';
import '../core/py_json.dart';
import '../core/py_text.dart';

/// Database reads and writes for New From Your Artists (`artist_releases`,
/// `release_sync_log`, owned-title lookups) and Search ownership badges
/// (`get_track_by_meta`, `get_album_owned_count`, `get_artist_owned_count`).

/// One release entry stored inside `artist_releases.releases` JSON (and
/// returned with [artist] populated by `getNewReleases`).
class ReleaseItem {
  const ReleaseItem({
    required this.title,
    required this.date,
    required this.type,
    required this.cover,
    required this.source,
    this.artist = '',
  });

  final String title;
  final String date;
  final String type;
  final String cover;
  final String source;
  final String artist;

  ReleaseItem withArtist(String artistName) => ReleaseItem(
    title: title,
    date: date,
    type: type,
    cover: cover,
    source: source,
    artist: artistName,
  );

  Map<String, Object?> toJson() => {
    'title': title,
    'date': date,
    'type': type,
    'cover': cover,
    'source': source,
  };

  static ReleaseItem? fromJson(Object? raw) {
    if (raw is! Map) return null;
    return ReleaseItem(
      title: '${raw['title'] ?? ''}',
      date: '${raw['date'] ?? ''}',
      type: '${raw['type'] ?? 'album'}',
      cover: '${raw['cover'] ?? ''}',
      source: '${raw['source'] ?? ''}',
      artist: '${raw['artist'] ?? ''}',
    );
  }
}

/// One row from `artist_releases`.
class ArtistReleaseRow {
  const ArtistReleaseRow({
    required this.artistName,
    required this.source,
    required this.sourceId,
    required this.releases,
    required this.fetchedAt,
  });

  final String artistName;
  final String source;
  final String? sourceId;
  final List<ReleaseItem> releases;
  final double fetchedAt;
}

/// One row from `release_sync_log`.
class ReleaseSyncEntry {
  const ReleaseSyncEntry({
    required this.id,
    required this.syncDate,
    required this.startedAt,
    required this.finishedAt,
    required this.artistsChecked,
    required this.newFound,
    required this.triggerType,
    required this.status,
  });

  final int id;
  final String syncDate;
  final double startedAt;
  final double? finishedAt;
  final int artistsChecked;
  final int newFound;
  final String triggerType;
  final String status;
}

String formatIsoDate(DateTime d) =>
    '${d.year.toString().padLeft(4, '0')}-'
    '${d.month.toString().padLeft(2, '0')}-'
    '${d.day.toString().padLeft(2, '0')}';

/// `db_manager.get_artist_release_cache`: `{lower-cased artist: row}`.
Map<String, ArtistReleaseRow> artistReleaseCacheJob(Database db) {
  final out = <String, ArtistReleaseRow>{};
  for (final r in db.select(
    'SELECT artist_name, source, source_id, releases, fetched_at '
    'FROM artist_releases',
  )) {
    final name = r['artist_name'] as String;
    final rawReleases = r['releases'] as String?;
    final decoded = <ReleaseItem>[];
    if (rawReleases != null && rawReleases.isNotEmpty) {
      try {
        final list = jsonDecode(rawReleases);
        if (list is List) {
          for (final item in list) {
            final parsed = ReleaseItem.fromJson(item);
            if (parsed != null) decoded.add(parsed);
          }
        }
      } catch (_) {
        // Corrupt JSON decays to empty list, as in Python.
      }
    }
    out[pyLower(name)] = ArtistReleaseRow(
      artistName: name,
      source: r['source'] as String,
      sourceId: r['source_id'] as String?,
      releases: decoded,
      fetchedAt: (r['fetched_at'] as num?)?.toDouble() ?? 0.0,
    );
  }
  return out;
}

/// `db_manager.save_artist_releases`.
DbJob<void> saveArtistReleasesJob(
  String artistName,
  String source,
  String? sourceId,
  List<ReleaseItem> releases, {
  double? fetchedAt,
}) => (db) {
  final ts = fetchedAt ?? DateTime.now().millisecondsSinceEpoch / 1000.0;
  final encoded = pyJsonDumps([for (final r in releases) r.toJson()]);
  db.execute(
    'INSERT OR REPLACE INTO artist_releases '
    '(artist_name, source, source_id, releases, fetched_at) '
    'VALUES (?, ?, ?, ?, ?)',
    [artistName, source, sourceId, encoded, ts],
  );
};

/// `db_manager.get_release_lookup_artists`: most-owned first.
List<String> releaseLookupArtistsJob(Database db) => [
  for (final r in db.select(
    'SELECT name FROM artists WHERE track_count > 0 ORDER BY track_count DESC',
  ))
    if (r['name'] case final String n when n.isNotEmpty) n,
];

/// `db_manager.get_artist_owned_titles`.
DbJob<(List<String>, List<String>)> artistOwnedTitlesJob(String artistName) =>
    (db) {
      final albums = [
        for (final r in db.select(
          'SELECT al.title FROM albums al '
          'JOIN artists ar ON ar.id = al.artist_id '
          'WHERE ar.name = ? COLLATE NOCASE',
          [artistName],
        ))
          if (r['title'] case final String t when t.isNotEmpty) t,
      ];
      final tracks = [
        for (final r in db.select(
          'SELECT t.title FROM tracks t '
          'JOIN albums al ON al.id = t.album_id '
          'JOIN artists ar ON ar.id = al.artist_id '
          'WHERE ar.name = ? COLLATE NOCASE',
          [artistName],
        ))
          if (r['title'] case final String t when t.isNotEmpty) t,
      ];
      return (albums, tracks);
    };

/// `db_manager.get_all_owned_artist_titles`: single UNION query mapping
/// lower-cased artist name → set of owned album and track titles.
Map<String, Set<String>> allOwnedArtistTitlesJob(Database db) {
  final rows = db.select('''
    SELECT ar.name, al.title
    FROM albums al
    JOIN artists ar ON ar.id = al.artist_id
    WHERE ar.name IS NOT NULL AND al.title IS NOT NULL
    UNION
    SELECT ar.name, t.title
    FROM tracks t
    JOIN albums al ON al.id = t.album_id
    JOIN artists ar ON ar.id = al.artist_id
    WHERE ar.name IS NOT NULL AND t.title IS NOT NULL
  ''');
  final out = <String, Set<String>>{};
  for (final r in rows) {
    final artist = r.columnAt(0) as String?;
    final title = r.columnAt(1) as String?;
    if (artist != null &&
        artist.isNotEmpty &&
        title != null &&
        title.isNotEmpty) {
      (out[pyLower(artist)] ??= <String>{}).add(title);
    }
  }
  return out;
}

/// `db_manager.log_release_sync_start`.
DbJob<int> logReleaseSyncStartJob({
  String triggerType = 'daily_auto',
  DateTime? now,
}) => (db) {
  final t = now ?? DateTime.now();
  final todayStr = formatIsoDate(t);
  final startedAt = t.millisecondsSinceEpoch / 1000.0;
  db.execute(
    "INSERT INTO release_sync_log (sync_date, started_at, trigger_type, status) "
    "VALUES (?, ?, ?, 'running')",
    [todayStr, startedAt, triggerType],
  );
  return db.lastInsertRowId;
};

/// `db_manager.log_release_sync_finish`.
DbJob<void> logReleaseSyncFinishJob(
  int logId, {
  int artistsChecked = 0,
  int newFound = 0,
  String status = 'completed',
  DateTime? now,
}) => (db) {
  final finishedAt = (now ?? DateTime.now()).millisecondsSinceEpoch / 1000.0;
  db.execute(
    'UPDATE release_sync_log '
    'SET finished_at = ?, artists_checked = ?, new_found = ?, status = ? '
    'WHERE id = ?',
    [finishedAt, artistsChecked, newFound, status, logId],
  );
};

/// `db_manager.get_latest_release_sync`.
ReleaseSyncEntry? latestReleaseSyncJob(Database db) {
  final rows = db.select(
    'SELECT id, sync_date, started_at, finished_at, artists_checked, '
    'new_found, trigger_type, status '
    'FROM release_sync_log ORDER BY id DESC LIMIT 1',
  );
  if (rows.isEmpty) return null;
  final r = rows.first;
  return ReleaseSyncEntry(
    id: r['id'] as int,
    syncDate: r['sync_date'] as String,
    startedAt: (r['started_at'] as num).toDouble(),
    finishedAt: (r['finished_at'] as num?)?.toDouble(),
    artistsChecked: (r['artists_checked'] as int?) ?? 0,
    newFound: (r['new_found'] as int?) ?? 0,
    triggerType: r['trigger_type'] as String,
    status: r['status'] as String,
  );
}

/// `db_manager.should_run_daily_release_sync`:
/// true if no completed sync has run today (with a 12-hour cooldown across
/// midnight, 1-hour stale-running recovery, and clock-skew safety).
DbJob<bool> shouldRunDailyReleaseSyncJob({DateTime? now}) => (db) {
  final latest = latestReleaseSyncJob(db);
  if (latest == null) return true;

  final t = now ?? DateTime.now();
  final nowS = t.millisecondsSinceEpoch / 1000.0;

  if (latest.status == 'running') {
    if (nowS - latest.startedAt < 3600.0) return false;
    return true;
  }
  if (latest.status != 'completed') return true;

  final todayStr = formatIsoDate(t);
  final finishedAt = latest.finishedAt ?? 0.0;

  // Handle system clock rolled backward (clock skew safety).
  if (nowS < finishedAt - 3600.0) return true;

  final elapsed = nowS - finishedAt;
  return latest.syncDate.compareTo(todayStr) < 0 && elapsed >= 12 * 3600;
};

/// `db_manager.get_track_by_meta` (truthiness only, as Search uses it).
DbJob<bool> hasTrackByMetaJob(String title, String artist) =>
    (db) => db.select(
      'SELECT t.id FROM tracks t '
      'JOIN albums al ON t.album_id = al.id '
      'JOIN artists ar ON al.artist_id = ar.id '
      'WHERE t.title = ? COLLATE NOCASE AND ar.name = ? COLLATE NOCASE '
      'LIMIT 1',
      [title, artist],
    ).isNotEmpty;

/// `db_manager.get_album_owned_count`.
DbJob<int> albumOwnedCountJob(String title, String artist) => (db) {
  final rows = db.select(
    'SELECT COUNT(t.id) AS n FROM albums al '
    'JOIN artists ar ON al.artist_id = ar.id '
    'JOIN tracks t ON t.album_id = al.id '
    'WHERE al.title = ? COLLATE NOCASE AND ar.name = ? COLLATE NOCASE',
    [title, artist],
  );
  return rows.isEmpty ? 0 : (rows.first['n'] as int?) ?? 0;
};

/// `db_manager.get_artist_owned_count`.
DbJob<int> artistOwnedCountJob(String name) => (db) {
  final rows = db.select(
    'SELECT COUNT(t.id) AS n FROM artists ar '
    'JOIN albums al ON al.artist_id = ar.id '
    'JOIN tracks t ON t.album_id = al.id '
    'WHERE ar.name = ? COLLATE NOCASE',
    [name],
  );
  return rows.isEmpty ? 0 : (rows.first['n'] as int?) ?? 0;
};
