import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:sqlite3/sqlite3.dart';

import '../core/db_worker.dart';
import '../core/py_json.dart';
import '../core/py_text.dart';
import '../stations/acoustic_geometry.dart';
import '../stations/genre_npmi.dart';
import '../sync/companion_client.dart' show FeatureRow;

/// The similarity graph's builders and their bookkeeping, as db_manager and
/// track_graph.py run them: `build_acoustic_edges` (the coordinates, then
/// the genre model), `build_genre_affinity`, `graph_status` with the
/// readiness check's count, `get_tracks_missing_features` and
/// `update_tracks_features_batch` (the DSP offload's work list and writes).

/// `get_tracks_with_features`' SQL, word for word (the joins shape SQLite's
/// plan, so the rows come in Python's order).
List<FeatureInput> tracksWithFeatures(Database db, int featuresVersion) => [
  for (final r in db.select(
    '''
            SELECT pc.track_path AS path, pc.timbre,
                   COALESCE(pc.bpm, 0)           AS bpm,
                   COALESCE(pc.brightness, 0)    AS brightness,
                   COALESCE(pc.energy, 0)        AS energy,
                   COALESCE(pc.rolloff, 0)       AS rolloff,
                   COALESCE(pc.beat_strength, 0) AS beat_strength,
                   COALESCE(pc.spectral_flatness, 0) AS spectral_flatness,
                   COALESCE(pc.spectral_contrast, 0) AS spectral_contrast,
                   COALESCE(pc.key_index, 0)         AS key_index,
                   t.title, t.duration,
                   ar.name  AS artist,
                   al.title AS album,
                   al.genre AS genre
            FROM play_counts pc
            INNER JOIN tracks  t  ON t.path  = pc.track_path
            LEFT JOIN albums  al ON al.id   = t.album_id
            LEFT JOIN artists ar ON ar.id   = al.artist_id
            WHERE pc.timbre IS NOT NULL
              AND COALESCE(pc.features_version, 0) >= ?
        ''',
    [featuresVersion],
  ))
    FeatureInput(
      r['path'] as String,
      // A TEXT timbre (never written by either app) is no embedding.
      switch (r['timbre']) {
        final Uint8List b => b,
        _ => null,
      },
      [for (final k in kScalarOrder) _num(r[k])],
    ),
];

/// `float(x or 0)`; a TEXT value in a REAL column reads as Python's float
/// would parse it.
double _num(Object? v) => switch (v) {
  final num n => n.toDouble(),
  final String s => double.tryParse(s.trim()) ?? 0,
  _ => 0,
};

/// What `build_acoustic_edges` did: the tracks projected (its return
/// value), the dimensions kept and the scalars cleaved.
typedef AcousticBuildResult = ({int tracks, int dims, Set<String> redundant});

/// `build_acoustic_edges`: the coordinates of every track with current
/// features rewritten (others keep theirs, as in Python), then the genre
/// model rebuilt. A genre-model failure is logged, as Python's is.
DbJob<AcousticBuildResult> buildAcousticEdgesJob({
  int featuresVersion = 5,
  bool zScore = true,
  double scalarWeight = 1.5,
}) => (db) {
  final rows = tracksWithFeatures(db, featuresVersion);
  final g = buildAcousticGeometry(
    rows,
    zScore: zScore,
    scalarWeight: scalarWeight,
  );
  if (g == null) return (tracks: 0, dims: 0, redundant: const <String>{});
  // update_tracks_pca_coords_batch: one transaction. Python logs a failure
  // here and carries on to the genre model.
  try {
    db.execute('BEGIN IMMEDIATE');
    try {
      final update = db.prepare(
        'UPDATE play_counts SET pca_coords = ? WHERE track_path = ?',
      );
      try {
        for (var i = 0; i < g.paths.length; i++) {
          final row = g.row(i);
          update.execute([
            Uint8List.sublistView(Float32List.fromList(row)),
            g.paths[i],
          ]);
        }
      } finally {
        update.close();
      }
      db.execute('COMMIT');
    } catch (_) {
      db.execute('ROLLBACK');
      rethrow;
    }
  } on SqliteException catch (e) {
    debugPrint('track_graph: persisting Zr geometry failed: $e');
  }
  try {
    buildGenreAffinityJob(db);
  } catch (e) {
    debugPrint('track_graph: genre affinity build skipped ($e)');
  }
  return (tracks: g.paths.length, dims: g.dims, redundant: g.redundant);
};

/// `get_all_artist_genre_sets`: one token set per enriched artist, empty
/// ones dropped.
List<Set<String>> artistGenreSets(Database db) {
  final out = <Set<String>>[];
  for (final r in db.select(
    'SELECT genres FROM artist_enrichment '
    "WHERE genres IS NOT NULL AND status IN ('ok', 'lowconfidence')",
  )) {
    final toks = <String>{};
    try {
      final raw = r['genres'] as String?;
      for (final g
          in jsonDecode(raw == null || raw.isEmpty ? '[]' : raw) as List) {
        final tk = alnumOnly(pyLower((g as Map)['name'] as String? ?? ''));
        if (tk.isNotEmpty) toks.add(tk);
      }
    } catch (_) {}
    if (toks.isNotEmpty) out.add(toks);
  }
  return out;
}

/// `build_genre_affinity`: the NPMI model rebuilt from the enrichment and
/// stored (`save_genre_affinity`, the JSON exactly as Python's
/// `json.dumps`). Returns the pair count.
int buildGenreAffinityJob(Database db) {
  final model = buildNpmiModel(artistGenreSets(db));
  db.execute(
    'INSERT OR REPLACE INTO genre_affinity (id, model, updated_at) '
    "VALUES (1, ?, strftime('%s','now'))",
    [pyJsonDumps(model)],
  );
  return model.length;
}

/// main.py `_ensure_graph_built_async`'s inputs: `graph_status`' track and
/// coordinate counts, and how many tracks `get_tracks_with_features` finds.
typedef GraphReadiness = ({int tracks, int withCoords, int analysed});

DbJob<GraphReadiness> graphReadinessJob([int featuresVersion = 5]) => (db) {
  int count(String sql, [List<Object?> args = const []]) =>
      db.select(sql, args).first.values.first as int;
  return (
    tracks: count('SELECT COUNT(*) FROM tracks'),
    withCoords: count(
      'SELECT COUNT(*) FROM play_counts WHERE pca_coords IS NOT NULL',
    ),
    analysed: count(
      'SELECT COUNT(*) FROM play_counts pc '
      'INNER JOIN tracks t ON t.path = pc.track_path '
      'WHERE pc.timbre IS NOT NULL '
      '  AND COALESCE(pc.features_version, 0) >= ?',
      [featuresVersion],
    ),
  );
};

/// `track_graph.graph_status`: the assistant's first-open check.
typedef GraphStatus = ({
  int totalTracks,
  int coordTracks,
  int acousticEdges,
  int artistEdges,
  int albumEdges,
});

/// `track_graph.graph_status` job.
DbJob<GraphStatus> graphStatusJob = (db) {
  int count(String sql, [List<Object?> args = const []]) {
    try {
      return db.select(sql, args).first.values.first as int;
    } on SqliteException {
      return 0;
    }
  }

  return (
    totalTracks: count('SELECT COUNT(*) FROM tracks'),
    coordTracks: count(
      'SELECT COUNT(*) FROM play_counts WHERE pca_coords IS NOT NULL',
    ),
    acousticEdges: count(
      "SELECT COUNT(*) FROM track_neighbors WHERE kind = 'acoustic'",
    ),
    artistEdges: count(
      "SELECT COUNT(*) FROM track_neighbors WHERE kind = 'artist'",
    ),
    albumEdges: count(
      "SELECT COUNT(*) FROM track_neighbors WHERE kind = 'album'",
    ),
  );
};

/// `get_tracks_missing_features`: paths whose features are absent or stale.
DbJob<List<String>> tracksMissingFeaturesJob([int featuresVersion = 5]) =>
    (db) => [
      for (final r in db.select(
        '''
            SELECT t.path
            FROM tracks t
            LEFT JOIN play_counts pc ON pc.track_path = t.path
            WHERE pc.timbre IS NULL
               OR COALESCE(pc.features_version, 0) < ?
        ''',
        [featuresVersion],
      ))
        r['path'] as String,
    ];

/// `update_tracks_features_batch`: the offload's features, one transaction
/// per batch (client.py `_row`: an empty timbre is stored as an empty BLOB).
DbJob<void> storeFeatureRowsJob(List<FeatureRow> rows) => (db) {
  if (rows.isEmpty) return;
  db.execute('BEGIN IMMEDIATE');
  try {
    final insert = db.prepare('''
                INSERT INTO play_counts
                    (track_path, count, last_played, bpm, energy, brightness,
                     rolloff, beat_strength, spectral_flatness,
                     spectral_contrast, key_index, timbre, features_version)
                VALUES (?, 0, strftime('%s','now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(track_path) DO UPDATE SET
                    bpm = EXCLUDED.bpm,
                    energy = EXCLUDED.energy,
                    brightness = EXCLUDED.brightness,
                    rolloff = EXCLUDED.rolloff,
                    beat_strength = EXCLUDED.beat_strength,
                    spectral_flatness = EXCLUDED.spectral_flatness,
                    spectral_contrast = EXCLUDED.spectral_contrast,
                    key_index = EXCLUDED.key_index,
                    timbre = EXCLUDED.timbre,
                    features_version = EXCLUDED.features_version
                ''');
    try {
      for (final r in rows) {
        insert.execute([
          r.path,
          r.bpm,
          r.energy,
          r.brightness,
          r.rolloff,
          r.beatStrength,
          r.spectralFlatness,
          r.spectralContrast,
          r.keyIndex,
          r.timbre,
          r.featuresVersion,
        ]);
      }
    } finally {
      insert.close();
    }
    db.execute('COMMIT');
  } catch (_) {
    db.execute('ROLLBACK');
    rethrow;
  }
};
