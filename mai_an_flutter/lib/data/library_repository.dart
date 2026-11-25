import 'dart:convert';
import 'dart:isolate';

import 'package:flutter/foundation.dart';
import 'package:sqlite3/sqlite3.dart';

import '../analysis/audio_features.dart' show AudioFeatures;
import '../core/db_worker.dart';
import '../core/py_text.dart';
import '../player/dsp.dart';
import '../stations/station.dart';
import 'assistant_queries.dart';
import 'browse_queries.dart';
import '../sync/companion_client.dart' show FeatureRow;
import 'feature_store.dart';
import 'graph_build.dart';
import 'landing_queries.dart';
import 'library_schema.dart';
import 'play_ledger.dart';
import 'playlists.dart';
import 'maintenance.dart';
import 'network_queries.dart';
import 'release_queries.dart';
import 'scan_writes.dart';

export 'graph_build.dart' show AcousticBuildResult, GraphReadiness, GraphStatus;
export 'library_schema.dart' show kGeometryVersion;
export 'landing_queries.dart'
    show LibraryCounts, MostPlayedGroup, MostPlayedTrack;
export 'network_queries.dart' show NeighborRow, NetworkTrackRow;
export 'playlists.dart' show PlaylistRow;
export 'release_queries.dart'
    show ArtistReleaseRow, ReleaseItem, ReleaseSyncEntry;
export 'scan_writes.dart' show ScannedTrack;

class Track {
  const Track({
    required this.id,
    required this.path,
    required this.title,
    required this.artist,
    required this.album,
    required this.duration,
    required this.format,
    this.trackNum,
    this.addedDate,
    this.albumId,
    this.artistId,
    this.bitrate,
  });

  final int id;
  final String path;
  final String title;
  final String artist;
  final String album;
  final double duration;
  final String format;
  final int? trackNum;

  /// Unix seconds (Python `time.time()`), null for rows that predate the column.
  final double? addedDate;

  /// `albums.id` and `artists.id` (via the album), for the library tabs'
  /// search: an album or artist matches when one of its tracks does. Null
  /// for rows built from a queue file.
  final int? albumId;
  final int? artistId;

  /// kbit/s, as tinytag reports it (the scan stores it); null when unknown.
  final num? bitrate;

  /// ui/widgets.py `fmt_time`: whole seconds, truncated.
  String get durationLabel {
    final s = duration.truncate();
    return '${s ~/ 60}:${(s % 60).toString().padLeft(2, '0')}';
  }

  /// library.py `_track_row`'s badge: the format column, else the file
  /// extension, else "AUD"; upper case, at most 5 characters.
  String get formatBadge {
    var f = format;
    if (f.isEmpty) {
      final name = path.split('/').last;
      final dot = name.lastIndexOf('.');
      f = dot > 0 ? name.substring(dot + 1) : '';
    }
    f = (f.isEmpty ? 'AUD' : f).toUpperCase();
    return f.length > 5 ? f.substring(0, 5) : f;
  }
}

/// The library database (`library.db`, the schema of
/// `utils/db_manager.py`).
///
/// Read-only by default. With [open]'s `writable`, the file is opened
/// read-write and migrated exactly as the Flet app migrates it on start
/// (library_schema.dart); only the port's own copy is ever opened that way
/// (PORT_MAP §10 step 4a), never the Flet app's live database.
class LibraryRepository {
  LibraryRepository._(this._db, {required this.writable});

  final DbWorker _db;
  final bool writable;

  static Future<LibraryRepository> open(
    String path, {
    bool writable = false,
  }) async {
    final db = await DbWorker.open(path, readOnly: !writable);
    if (writable) {
      try {
        await db.run(migrateLibrary);
      } catch (_) {
        await db.close();
        rethrow;
      }
    }
    return LibraryRepository._(db, writable: writable);
  }

  /// `record_listens`: one ledger batch, in one transaction. Returns how
  /// many listens counted toward play_counts.
  Future<int> recordListens(List<LedgerEntry> entries) {
    if (!writable) throw StateError('library opened read-only');
    return _db.run(recordListensJob(entries));
  }

  // The library scan's reads and writes (scan_writes.dart).
  Future<Map<String, double?>> pathMtimeMap() => _db.run(pathMtimeMapJob);
  Future<Map<String, String>> artistAliases() => _db.run(artistAliasesJob);
  Future<void> deleteTracksByPaths(List<String> paths) =>
      _write(deleteTracksByPathsJob(paths));
  Future<void> insertTracksBatch(
    List<ScannedTrack> tracks,
    Map<String, String> aliases,
  ) => _write(insertTracksBatchJob(tracks, aliases));
  Future<void> beginBulkImport() => _write(beginBulkImportJob);
  Future<void> endBulkImport() => _write(endBulkImportJob);
  Future<void> pruneOrphans() => _write(pruneOrphansJob);
  Future<void> checkpoint() => _write(checkpointJob);

  // Settings → Database Management (maintenance.dart).
  Future<void> clearLibraryIndex() => _write(clearLibraryIndexJob);
  Future<void> clearDspFeatures() => _write(clearDspFeaturesJob);
  Future<void> wipeDatabase() => _write(wipeDatabaseJob);

  /// Raw statements, for tests that set up rows the app makes elsewhere.
  @visibleForTesting
  Future<void> debugExecute(List<(String, List<Object?>)> statements) =>
      _write((db) {
        for (final (sql, args) in statements) {
          db.execute(sql, args);
        }
      });

  Future<void> _write(void Function(Database) job) {
    if (!writable) throw StateError('library opened read-only');
    return _db.run(job);
  }

  Future<List<Track>> allTracks() => _db.run(_selectAllTracks);

  /// The stations' coordinate and artist graphs. The rows come from the DB
  /// isolate; the graph is built on a one-off isolate so a library read
  /// isn't held up behind it.
  Future<StationSource> stations() async {
    final data = await _db.run(_selectStationData);
    return Isolate.run(() => StationSource.build(data));
  }

  /// `get_tracks_brief`: title/artist/album/duration per path, for queue rows
  /// built from station output.
  Future<Map<String, Track>> tracksBrief(List<String> paths) =>
      _db.run(_tracksBriefJob(paths));

  /// `has_current_features`: the track has DSP features a station can seed
  /// from.
  /// Settings → Audio & DSP's Dynamism check: the track count, and how
  /// many lack current features (`get_tracks_missing_features`).
  Future<(int total, int missing)> featureCoverage() =>
      _db.run(_featureCoverageJob);

  Future<bool> hasCurrentFeatures(String path) =>
      _db.run(_hasFeaturesJob(path));

  /// `get_recent_tracks`: distinct paths most recently played, newest first.
  Future<List<String>> recentTrackPaths(int limit) =>
      _db.run(_recentJob(limit));

  /// The play_counts row's audio features for [path] (what `reapply_dsp`
  /// selects), or null when the track has no row.
  Future<DspFeatures?> dspFeatures(String path) =>
      _db.run(_dspFeaturesJob(path));

  /// server.py `stored_features` (Send to Mac sends them along).
  Future<Map<String, Object?>?> storedFeatures(String path) =>
      _db.run((db) => storedFeaturesJob(db, path));

  /// server.py `persist_features`: features that came with a Mac transfer.
  Future<void> persistFeatures(String path, Map<String, Object?> dsp) =>
      _write(persistFeaturesJob(path, dsp));

  // The Albums, Artists and Genres tabs (browse_queries.dart). Sort modes
  // are db_manager's sort_map keys.
  Future<List<AlbumRow>> allAlbums(String sortMode) =>
      _db.run(allAlbumsJob(sortMode));
  Future<List<ArtistRow>> allArtists(String sortMode) =>
      _db.run(allArtistsJob(sortMode));
  Future<List<GenreRow>> allGenres(String sortMode) =>
      _db.run(allGenresJob(sortMode));
  Future<List<ArtistRow>> artistsByGenre(String genre) =>
      _db.run(artistsByGenreJob(genre));
  Future<List<AlbumRow>> albumsByArtist(String? artist, {String? genre}) =>
      _db.run(albumsByArtistJob(artist, genre));
  Future<List<Track>> tracksByAlbum(String? album, String? artist) =>
      _db.run(tracksByAlbumJob(album, artist));
  Future<List<Track>> tracksByGenre(String genre) =>
      _db.run(tracksByGenreJob(genre));

  // The Search tab's landing cards (landing_queries.dart).
  Future<List<MostPlayedTrack>> mostPlayed(int limit) =>
      _db.run(mostPlayedJob(limit));
  Future<List<MostPlayedGroup>> mostPlayedArtists(int limit) =>
      _db.run(mostPlayedArtistsJob(limit));
  Future<List<MostPlayedGroup>> mostPlayedGenres(int limit) =>
      _db.run(mostPlayedGenresJob(limit));
  Future<LibraryCounts> libraryCounts() => _db.run(libraryCountsJob);

  // New From Your Artists & Search ownership (release_queries.dart).
  Future<Map<String, ArtistReleaseRow>> artistReleaseCache() =>
      _db.run(artistReleaseCacheJob);
  Future<void> saveArtistReleases(
    String artistName,
    String source,
    String? sourceId,
    List<ReleaseItem> releases, {
    double? fetchedAt,
  }) => _write(
    saveArtistReleasesJob(
      artistName,
      source,
      sourceId,
      releases,
      fetchedAt: fetchedAt,
    ),
  );
  Future<List<String>> releaseLookupArtists() =>
      _db.run(releaseLookupArtistsJob);
  Future<(List<String>, List<String>)> artistOwnedTitles(String artistName) =>
      _db.run(artistOwnedTitlesJob(artistName));
  Future<Map<String, Set<String>>> allOwnedArtistTitles() =>
      _db.run(allOwnedArtistTitlesJob);
  Future<int> logReleaseSyncStart({
    String triggerType = 'daily_auto',
    DateTime? now,
  }) {
    if (!writable) throw StateError('library opened read-only');
    return _db.run(logReleaseSyncStartJob(triggerType: triggerType, now: now));
  }

  Future<void> logReleaseSyncFinish(
    int logId, {
    int artistsChecked = 0,
    int newFound = 0,
    String status = 'completed',
    DateTime? now,
  }) => _write(
    logReleaseSyncFinishJob(
      logId,
      artistsChecked: artistsChecked,
      newFound: newFound,
      status: status,
      now: now,
    ),
  );
  Future<ReleaseSyncEntry?> latestReleaseSync() =>
      _db.run(latestReleaseSyncJob);
  Future<bool> shouldRunDailyReleaseSync({DateTime? now}) =>
      _db.run(shouldRunDailyReleaseSyncJob(now: now));
  Future<bool> hasTrackByMeta(String title, String artist) =>
      _db.run(hasTrackByMetaJob(title, artist));
  Future<int> albumOwnedCount(String title, String artist) =>
      _db.run(albumOwnedCountJob(title, artist));
  Future<int> artistOwnedCount(String name) =>
      _db.run(artistOwnedCountJob(name));

  // Playlists (playlists.dart). Writes throw on a read-only library.
  Future<List<PlaylistRow>> allPlaylists(String sortMode) =>
      _db.run(allPlaylistsJob(sortMode));
  Future<List<Track>> tracksInPlaylist(int playlistId) =>
      _db.run(tracksInPlaylistJob(playlistId));
  Future<int> createPlaylist(String name) {
    if (!writable) throw StateError('library opened read-only');
    return _db.run(createPlaylistJob(name));
  }

  Future<void> addTracksToPlaylist(int playlistId, List<String> paths) =>
      _write(addTracksToPlaylistJob(playlistId, paths));
  Future<void> removeTrackFromPlaylist(int playlistId, String path) =>
      _write(removeTrackFromPlaylistJob(playlistId, path));
  Future<void> movePlaylistTrack(int playlistId, int from, int to) =>
      _write(movePlaylistTrackJob(playlistId, from, to));
  Future<void> updatePlaylist(int playlistId, {String? name, String? color}) =>
      _write(updatePlaylistJob(playlistId, name: name, color: color));
  Future<void> deletePlaylist(int playlistId) =>
      _write(deletePlaylistJob(playlistId));

  // The Acoustic Network view (network_queries.dart).
  Future<bool> hasPcaCoords() => _db.run(hasPcaCoordsJob);
  Future<String> anyPcaPath() => _db.run(anyPcaPathJob);
  Future<List<NeighborRow>> networkNeighbors(
    String path,
    int k, {
    String? edgeKind,
  }) => _db.run(neighborsJob(path, k, edgeKind: edgeKind));
  Future<List<NetworkTrackRow>> pcaCoordsForPaths(List<String> paths) =>
      _db.run(pcaCoordsForPathsJob(paths));

  // The assistant's queries and writes (assistant_queries.dart).
  Future<int> totalTracks() => _db.run(totalTracksJob);
  Future<List<Map<String, Object?>>> searchTracksSimple(
    String query, {
    int limit = 5,
  }) => _db.run(searchTracksSimpleJob(query, limit));
  Future<List<Map<String, Object?>>> allTracksRaw({String searchQuery = ''}) =>
      _db.run(allTracksRawJob(searchQuery));
  Future<Map<String, Object?>?> trackFull(String path) =>
      _db.run(trackFullJob(path));
  Future<List<Map<String, Object?>>> mostPlayedFull(int limit) =>
      _db.run(mostPlayedFullJob(limit));
  Future<Set<String>> recentPlayedPaths({int windowSeconds = 7 * 86400}) =>
      _db.run(recentPlayedPathsJob(windowSeconds));
  Future<void> recordPlayback(String path, String event, {String? seedPath}) =>
      _write(recordPlaybackJob(path, event, seedPath: seedPath));
  Future<List<Map<String, Object?>>> allArtistsRaw({String searchQuery = ''}) =>
      _db.run(allArtistsRawJob(searchQuery));
  Future<List<String>> searchArtistNames(String query) =>
      _db.run(searchArtistNamesJob(query));
  Future<List<Map<String, Object?>>> allAlbumsRaw({String searchQuery = ''}) =>
      _db.run(allAlbumsRawJob(searchQuery));
  Future<List<Map<String, Object?>>> albumsByArtistRaw(String artistName) =>
      _db.run(albumsByArtistRawJob(artistName));
  Future<List<Map<String, Object?>>> tracksByAlbumRaw(
    String albumTitle,
    String artistName,
  ) => _db.run(tracksByAlbumRawJob(albumTitle, artistName));
  Future<List<Map<String, Object?>>> allPlaylistsRaw({
    String searchQuery = '',
  }) => _db.run(allPlaylistsRawJob(searchQuery));
  Future<List<Map<String, Object?>>> tracksInPlaylistRaw(int playlistId) =>
      _db.run(tracksInPlaylistRawJob(playlistId));
  Future<List<Map<String, Object?>>> recentTracksRaw(int limit) =>
      _db.run(recentTracksRawJob(limit));
  Future<String?> firstTrackPathByArtist(String name) =>
      _db.run(firstTrackPathByArtistJob(name));
  Future<Map<String, Object?>?> artistEnrichment(String name) =>
      _db.run(artistEnrichmentJob(name));
  Future<Map<String, Object?>?> aiResearch(String artistName) =>
      _db.run(aiResearchJob(artistName));
  Future<Map<String, Object?>> pendingAiResearch() =>
      _db.run(pendingAiResearchJob);
  Future<List<String>> artistSourceGenres(String artistName) =>
      _db.run(artistSourceGenresJob(artistName));
  Future<List<Map<String, Object?>>> metadataGapArtists({int limit = 2000}) =>
      _db.run(metadataGapArtistsJob(limit));
  Future<Map<String, Object?>> metadataCoverage({
    List<Map<String, Object?>>? gaps,
  }) => _db.run(metadataCoverageJob(gaps));

  /// `track_graph.build_metadata_edges`: rewrites the artist tier of
  /// `track_neighbors`. Returns the edge count.
  Future<int> buildMetadataEdges([int k = kMetadataEdgesPerTrack]) {
    if (!writable) throw StateError('library opened read-only');
    return _db.run(buildMetadataEdgesJob(k));
  }

  // The similarity graph (graph_build.dart).

  /// `track_graph.build_acoustic_edges`: the coordinates of every analysed
  /// track recomputed, then the genre model. The caller reloads the
  /// stations (Python's `invalidate_coord_graph_cache`).
  Future<AcousticBuildResult> buildAcousticEdges() {
    if (!writable) throw StateError('library opened read-only');
    return _db.run(buildAcousticEdgesJob(featuresVersion: kFeaturesVersion));
  }

  /// The startup readiness check's counts.
  Future<GraphReadiness> graphReadiness() =>
      _db.run(graphReadinessJob(kFeaturesVersion));

  /// `track_graph.graph_status`: the assistant's init check.
  Future<GraphStatus> graphStatus() => _db.run(graphStatusJob);

  /// `get_tracks_missing_features`: the DSP offload's work list.
  Future<List<String>> tracksMissingFeatures() =>
      _db.run(tracksMissingFeaturesJob(kFeaturesVersion));

  /// `update_tracks_features_batch`: one batch of offloaded features.
  Future<void> storeFeatureRows(List<FeatureRow> rows) =>
      _write(storeFeatureRowsJob(rows));

  /// `update_track_features`: one track analysed on the phone.
  Future<void> updateTrackFeatures(String path, AudioFeatures f) =>
      _write(updateTrackFeaturesJob(path, f));

  Future<void> close() => _db.close();
}

// ── Jobs (run on the DB isolate) ────────────────────────────────────────────

/// Same rows as `db_manager.get_all_tracks` (INNER joins: a track whose album
/// or artist row is missing doesn't show in the Flet app either). Sorting
/// happens in memory; see track_query.dart.
List<Track> _selectAllTracks(Database db) => [
  for (final r in db.select('''
    SELECT t.id, t.path, t.title, t.duration, t.format, t.track_num,
           t.added_date, t.album_id, al.artist_id, t.bitrate,
           ar.name  AS artist,
           al.title AS album
    FROM tracks t
    JOIN albums  al ON t.album_id = al.id
    JOIN artists ar ON al.artist_id = ar.id
  '''))
    trackFromRow(r),
];

/// A track row with the columns of [_selectAllTracks]. The fallbacks are
/// library.py `_track_row`'s and main.py `_play_track_core`'s:
/// `title or basename(path)`, `artist or "Unknown"`, `album or "Unknown"`.
Track trackFromRow(Row r) => Track(
  id: r['id'] as int,
  path: r['path'] as String,
  title: _or(r['title'] as String?, _basename(r['path'] as String)),
  artist: _or(r['artist'] as String?, 'Unknown'),
  album: _or(r['album'] as String?, 'Unknown'),
  duration: (r['duration'] as num?)?.toDouble() ?? 0,
  format: (r['format'] as String?) ?? '',
  trackNum: r['track_num'] as int?,
  addedDate: (r['added_date'] as num?)?.toDouble(),
  albumId: r['album_id'] as int?,
  artistId: r['artist_id'] as int?,
  bitrate: r['bitrate'] as num?,
);

/// The database's coordinates were built by an older geometry pipeline. The
/// Flet app NULLs them on its next open and rebuilds; until then nothing may
/// walk them.
class StaleGeometryException implements Exception {
  StaleGeometryException(this.found);
  final int found;
  @override
  String toString() =>
      'Acoustic coordinates are from geometry v$found (current: '
      'v$kGeometryVersion). Open the library in the Flet app and re-run '
      'analysis to rebuild them.';
}

/// `db_manager.get_tracks_pca_coords` (same SQL, so the same row order as
/// the Flet app's), `get_artist_meta_for_paths` and `get_genre_affinity`,
/// including `_migrate_pca`'s rule that coordinates from an older
/// GEOMETRY_VERSION are invalid.
StationData _selectStationData(Database db) {
  final version = db.select('PRAGMA user_version').first.values.first as int;
  if (version < kGeometryVersion) throw StaleGeometryException(version);
  final rows = db.select('''
            SELECT pc.track_path AS path, pc.pca_coords, pc.cluster_id, pc.bpm, pc.energy,
                   t.title, t.duration,
                   ar.name  AS artist,
                   al.title AS album,
                   al.genre AS genre
            FROM play_counts pc
            INNER JOIN tracks  t  ON t.path  = pc.track_path
            LEFT JOIN albums  al ON al.id   = t.album_id
            LEFT JOIN artists ar ON ar.id   = al.artist_id
            WHERE pc.pca_coords IS NOT NULL
        ''');
  final stationRows = [
    for (final r in rows)
      StationRow(
        path: r['path'] as String,
        coords: r['pca_coords'] as Uint8List,
        bpm: (r['bpm'] as num?)?.toDouble() ?? 0,
        energy: (r['energy'] as num?)?.toDouble() ?? 0,
        title: r['title'] as String?,
        artist: r['artist'] as String?,
        album: r['album'] as String?,
      ),
  ];

  // Python logs and carries on without enrichment or the genre model (an
  // older database without the tables, a malformed row).
  final enrichment = <String, ArtistMeta>{};
  try {
    for (final r in db.select('''
      SELECT t.path AS path, ar.name AS artist,
             e.country AS country, e.genres AS genres, e.source AS source
      FROM tracks t
      JOIN albums al ON al.id = t.album_id
      JOIN artists ar ON ar.id = al.artist_id
      LEFT JOIN artist_enrichment e ON e.artist_name = ar.name
    ''')) {
      final toks = <String>{};
      final raw = r['genres'] as String?;
      if (raw != null && raw.isNotEmpty) {
        try {
          for (final g in jsonDecode(raw) as List) {
            final tk = alnumOnly(pyLower((g as Map)['name'] as String? ?? ''));
            if (tk.isNotEmpty) toks.add(tk);
          }
        } catch (_) {}
      }
      enrichment[r['path'] as String] = ArtistMeta(
        artist: r['artist'] as String?,
        country: r['country'] as String?,
        genres: toks.toList(),
        manual: r['source'] == 'manual',
      );
    }
  } on SqliteException {
    enrichment.clear();
  }
  final npmi = <String, double>{};
  try {
    final m = db.select('SELECT model FROM genre_affinity WHERE id = 1');
    final raw = m.isEmpty ? null : m.first.values.first as String?;
    if (raw != null && raw.isNotEmpty) {
      (jsonDecode(raw) as Map).forEach((k, v) {
        npmi[k as String] = (v as num).toDouble();
      });
    }
  } catch (_) {
    npmi.clear();
  }
  return StationData(rows: stationRows, enrichment: enrichment, npmi: npmi);
}

DbJob<Map<String, Track>> _tracksBriefJob(List<String> paths) =>
    (db) => _selectTracksBrief(db, paths);

Map<String, Track> _selectTracksBrief(Database db, List<String> paths) {
  final out = <String, Track>{};
  for (var i = 0; i < paths.length; i += 500) {
    final chunk = paths.sublist(
      i,
      i + 500 > paths.length ? paths.length : i + 500,
    );
    final rows = db.select('''
      SELECT t.id, t.path, t.title, t.duration, t.format, t.track_num,
             t.added_date, ar.name AS artist, al.title AS album
      FROM tracks t
      LEFT JOIN albums  al ON al.id = t.album_id
      LEFT JOIN artists ar ON ar.id = al.artist_id
      WHERE t.path IN (${List.filled(chunk.length, '?').join(',')})
    ''', chunk);
    for (final r in rows) {
      final path = r['path'] as String;
      // autoplay._tracks: `title or basename`, `artist or "Unknown Artist"`,
      // `album or "Unknown Album"`.
      out[path] = Track(
        id: r['id'] as int,
        path: path,
        title: _or(r['title'] as String?, _basename(path)),
        artist: _or(r['artist'] as String?, 'Unknown Artist'),
        album: _or(r['album'] as String?, 'Unknown Album'),
        duration: (r['duration'] as num?)?.toDouble() ?? 0,
        format: (r['format'] as String?) ?? '',
        trackNum: r['track_num'] as int?,
        addedDate: (r['added_date'] as num?)?.toDouble(),
      );
    }
  }
  return out;
}

/// `dsp.FEATURES_VERSION`.
const kFeaturesVersion = 5;

(int, int) _featureCoverageJob(Database db) {
  final total = db.select('SELECT COUNT(*) AS n FROM tracks').first['n'] as int;
  final missing =
      db.select(
            'SELECT COUNT(*) AS n FROM tracks t '
            'LEFT JOIN play_counts pc ON pc.track_path = t.path '
            'WHERE pc.timbre IS NULL '
            '   OR COALESCE(pc.features_version, 0) < ?',
            [kFeaturesVersion],
          ).first['n']
          as int;
  return (total, missing);
}

DbJob<bool> _hasFeaturesJob(String path) =>
    (db) => db.select(
      'SELECT 1 FROM play_counts WHERE track_path = ? AND timbre IS NOT NULL '
      'AND COALESCE(features_version, 0) >= ?',
      [path, kFeaturesVersion],
    ).isNotEmpty;

DbJob<List<String>> _recentJob(int limit) => (db) {
  try {
    return [
      for (final r in db.select(
        '''
        SELECT t.path, MAX(ph.played_at) AS last_played
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
        r['path'] as String,
    ];
  } on SqliteException {
    return const [];
  }
};

/// Top-level so the job closure captures only [path] (it crosses to the DB
/// isolate).
DbJob<DspFeatures?> _dspFeaturesJob(String path) =>
    (db) => _selectDspFeatures(db, path);

DspFeatures? _selectDspFeatures(Database db, String path) {
  final rows = db.select(
    'SELECT energy, beat_strength, spectral_contrast FROM play_counts '
    'WHERE track_path = ?',
    [path],
  );
  if (rows.isEmpty) return null;
  final r = rows.first;
  return (
    energy: (r['energy'] as num?)?.toDouble(),
    beatStrength: (r['beat_strength'] as num?)?.toDouble(),
    spectralContrast: (r['spectral_contrast'] as num?)?.toDouble(),
  );
}

String _or(String? v, String fallback) =>
    (v == null || v.isEmpty) ? fallback : v;

String _basename(String path) => path.split(RegExp(r'[/\\]')).last;
