import 'dart:convert';

import 'package:sqlite3/sqlite3.dart';

import '../core/db_worker.dart';
import '../core/py_json.dart';
import '../core/py_text.dart';
import '../core/py_values.dart';
import '../stations/genre_taxonomy.dart';
import 'assistant_queries.dart'
    show aiResearchJob, artistEnrichmentJob, cleanSourceTags;

/// Phase 7a: the artist-metadata writes and reads of `db_manager.py` behind
/// the enrichment pass, the AI research cache, the metadata audit, artist
/// aliases and the workbench — as DB isolate jobs with Python's exact SQL.
///
/// Rows travel as Python's dicts (`Map<String, Object?>`, `genres` and
/// `provenance` decoded), so the enrichment pass and the workbench read them
/// as Flet does.

Map<String, Object?> _rowMap(Row r) => {for (final c in r.keys) c: r[c]};

Object? _decode(Object? raw, Object fallback) {
  if (raw is! String || raw.isEmpty) return fallback;
  try {
    return jsonDecode(raw);
  } catch (_) {
    return fallback;
  }
}

/// Python's `(g.get("name") if isinstance(g, dict) else str(g))`.
String? genreName(Object? g) => g is Map
    ? (g['name'] is String ? g['name'] as String : null)
    : (g == null ? 'None' : pyStr(g));

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

double _now() => DateTime.now().microsecondsSinceEpoch / 1e6;

// ── artist_enrichment writes ────────────────────────────────────────────────

/// `upsert_artist_enrichment`: insert/replace one artist's row. A manual row
/// is kept unless [source] is 'manual' or [force]. [genres] null stores
/// NULL; [provenance] empty or null stores NULL.
void upsertArtistEnrichment(
  Database db,
  String artistName, {
  Object? mbid,
  String? country,
  String? area,
  List<Object?>? genres,
  String source = 'musicbrainz',
  Object? score,
  String status = 'ok',
  bool force = false,
  Map<String, Object?>? provenance,
}) {
  final genresText = genres == null ? null : pyJsonDumps(genres);
  final provText = provenance == null || provenance.isEmpty
      ? null
      : pyJsonDumps(provenance);
  if (!force && source != 'manual') {
    final existing = db.select(
      'SELECT source FROM artist_enrichment WHERE artist_name = ?',
      [artistName],
    );
    if (existing.isNotEmpty && existing.first.columnAt(0) == 'manual') return;
  }
  db.execute(
    '''
                INSERT OR REPLACE INTO artist_enrichment
                    (artist_name, mbid, country, area, genres, source, score, status,
                     provenance, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))
            ''',
    [
      artistName,
      mbid,
      country,
      area,
      genresText,
      source,
      score,
      status,
      provText,
    ],
  );
}

/// [upsertArtistEnrichment] as a job.
DbJob<void> upsertArtistEnrichmentJob(
  String artistName, {
  Object? mbid,
  String? country,
  String? area,
  List<Object?>? genres,
  String source = 'musicbrainz',
  Object? score,
  String status = 'ok',
  bool force = false,
  Map<String, Object?>? provenance,
}) =>
    (db) => upsertArtistEnrichment(
      db,
      artistName,
      mbid: mbid,
      country: country,
      area: area,
      genres: genres,
      source: source,
      score: score,
      status: status,
      force: force,
      provenance: provenance,
    );

/// `record_enrichment_failure`: one failed lookup counted. 'retry' below
/// [maxAttempts], 'error' (parked) at it; null when the row was left alone
/// (a manual row, or one holding a real answer).
DbJob<String?> recordEnrichmentFailureJob(
  String artistName, {
  required String source,
  int maxAttempts = 3,
}) => (db) {
  final rows = db.select(
    'SELECT status, source, COALESCE(attempts, 0) '
    'FROM artist_enrichment WHERE artist_name = ?',
    [artistName],
  );
  final row = rows.isEmpty ? null : rows.first;
  if (row != null &&
      (row.columnAt(1) == 'manual' ||
          !const ['retry', 'error'].contains(row.columnAt(0)))) {
    return null;
  }
  final attempts = ((row?.columnAt(2) as int?) ?? 0) + 1;
  final status = attempts >= maxAttempts ? 'error' : 'retry';
  db.execute(
    '''
                INSERT OR REPLACE INTO artist_enrichment
                    (artist_name, source, status, attempts, fetched_at)
                VALUES (?, ?, ?, ?, strftime('%s','now'))
            ''',
    [artistName, source, status, attempts],
  );
  return status;
};

/// `set_manual_artist_enrichment`: the user's own answer (source 'manual',
/// never overwritten by a sync). [origin] labels the provenance ('ai' for an
/// accepted suggestion).
void setManualArtistEnrichment(
  Database db,
  String artistName, {
  String? country,
  List<Object?>? genres,
  String origin = 'manual',
}) {
  final genreObjs = <Object?>[];
  for (final g in genres ?? const <Object?>[]) {
    if (g is Map) {
      genreObjs.add(g);
    } else if (g is String && pyStrip(g).isNotEmpty) {
      genreObjs.add({'name': pyStrip(g), 'count': 1});
    }
  }
  final hasCountry = country != null && country.isNotEmpty;
  upsertArtistEnrichment(
    db,
    artistName,
    country: hasCountry ? country : null,
    genres: genreObjs,
    source: 'manual',
    status: 'ok',
    score: 100,
    force: true,
    provenance: {
      'country': hasCountry ? origin : null,
      'genres': genreObjs.isNotEmpty ? origin : null,
    },
  );
}

/// [setManualArtistEnrichment] as a job.
DbJob<void> setManualArtistEnrichmentJob(
  String artistName, {
  String? country,
  List<Object?>? genres,
  String origin = 'manual',
}) =>
    (db) => setManualArtistEnrichment(
      db,
      artistName,
      country: country,
      genres: genres,
      origin: origin,
    );

/// `set_manual_artist_enrichment_bulk` (without the model refresh, which
/// the caller schedules): the same answer for many artists.
DbJob<int> setManualArtistEnrichmentBulkJob(
  List<String> names, {
  String? country,
  List<String>? genres,
}) => (db) {
  var n = 0;
  for (final name in names) {
    try {
      setManualArtistEnrichment(db, name, country: country, genres: genres);
      n++;
    } catch (_) {}
  }
  return n;
};

/// `set_artists_genres` (refresh left to the caller): REPLACE many artists'
/// genres with one list; each keeps its country unless [country] is given.
DbJob<int> setArtistsGenresJob(
  List<String> names,
  List<String> genres, {
  String? country,
}) => (db) {
  final tags = <String>[];
  for (final g in genres) {
    final t = pyLower(pyStrip(g));
    if (t.isNotEmpty && !tags.contains(t)) tags.add(t);
  }
  final c = pyStrip(country ?? '').toUpperCase();
  var n = 0;
  for (final name in names) {
    try {
      final cur = artistEnrichmentJob(name)(db) ?? const {};
      final curCountry = cur['country'];
      setManualArtistEnrichment(
        db,
        name,
        country: c.isNotEmpty
            ? c
            : (pyTruthy(curCountry) ? curCountry as String : null),
        genres: tags,
      );
      n++;
    } catch (_) {}
  }
  return n;
};

/// `bulk_tag_artists` (refresh left to the caller): MERGE one genre (comma
/// pieces) and/or country into many artists, keeping what each has.
DbJob<int> bulkTagArtistsJob(
  List<String> names, {
  String? genre,
  String? country,
}) => (db) {
  final g = pyLower(pyStrip(genre ?? ''));
  final c = pyStrip(country ?? '').toUpperCase();
  if (g.isEmpty && c.isEmpty) return 0;
  var n = 0;
  for (final name in names) {
    try {
      final cur = artistEnrichmentJob(name)(db) ?? const {};
      final merged = <Object?>[
        for (final x in (cur['genres'] as List?) ?? const []) genreName(x),
      ];
      if (g.isNotEmpty) {
        for (final piece in g.split(',')) {
          final p = pyLower(pyStrip(piece));
          if (p.isEmpty) continue;
          final have = {
            for (final s in merged)
              if (s is String && s.isNotEmpty) pyLower(s),
          };
          if (!have.contains(p)) merged.add(p);
        }
      }
      final curCountry = cur['country'];
      setManualArtistEnrichment(
        db,
        name,
        country: c.isNotEmpty
            ? c
            : (pyTruthy(curCountry) ? curCountry as String : null),
        genres: merged,
      );
      n++;
    } catch (_) {}
  }
  return n;
};

/// `accept_artist_matches`: 'lowconfidence' → 'ok' for these artists.
DbJob<int> acceptArtistMatchesJob(List<String> names) => (db) {
  final ns = [
    for (final n in names)
      if (n.isNotEmpty) n,
  ];
  if (ns.isEmpty) return 0;
  final marks = List.filled(ns.length, '?').join(',');
  db.execute(
    "UPDATE artist_enrichment SET status='ok' "
    "WHERE status='lowconfidence' AND artist_name IN ($marks)",
    ns,
  );
  return db.updatedRows;
};

/// `confirm_artist_match`: a chosen MusicBrainz match written as confirmed.
DbJob<void> confirmArtistMatchJob(
  String artistName, {
  Object? mbid,
  String? country,
  String? area,
  List<Object?>? genres,
  String status = 'ok',
  int score = 100,
}) =>
    (db) => upsertArtistEnrichment(
      db,
      artistName,
      mbid: mbid,
      country: country,
      area: area,
      genres: genres,
      source: 'musicbrainz',
      score: score,
      status: status,
      force: true,
      provenance: {
        'country': pyTruthy(country) ? 'musicbrainz' : null,
        'genres': pyTruthy(genres) ? 'musicbrainz' : null,
      },
    );

// ── artist_enrichment reads ─────────────────────────────────────────────────

/// `get_artists_needing_enrichment(with_attempts=True)`: (name, failed
/// attempts) the next pass should fetch, most-played first. Manual rows are
/// never re-fetched.
DbJob<List<(String, int)>> artistsNeedingEnrichmentJob({
  int? limit,
  bool includeFailed = false,
  bool retryIncomplete = false,
}) => (db) {
  final clauses = ['e.artist_name IS NULL', "e.status = 'retry'"];
  if (includeFailed) clauses.add("e.status = 'error'");
  if (retryIncomplete) {
    clauses.add("(e.genres IS NULL OR e.genres = '' OR e.genres = '[]')");
  }
  var sql =
      'SELECT a.name, COALESCE(e.attempts, 0) FROM artists a '
      'LEFT JOIN artist_enrichment e ON e.artist_name = a.name '
      'WHERE (${clauses.join(' OR ')}) '
      "  AND (e.source IS NULL OR e.source <> 'manual') "
      'ORDER BY a.track_count DESC';
  if (limit != null) sql += ' LIMIT $limit';
  return [
    for (final r in db.select(sql))
      if (r.columnAt(0) is String && (r.columnAt(0) as String).isNotEmpty)
        (r.columnAt(0) as String, (r.columnAt(1) as num?)?.toInt() ?? 0),
  ];
};

/// `get_genre_vocabulary`: the library's genre tokens, most-attested first,
/// as `[{'name', 'artists'}]`.
DbJob<List<Map<String, Object?>>> genreVocabularyJob([int limit = 60]) => (db) {
  final counts = <String, int>{};
  final display = <String, String>{};
  for (final r in db.select(
    'SELECT genres FROM artist_enrichment '
    "WHERE genres IS NOT NULL AND genres <> '' AND genres <> '[]'",
  )) {
    final Object? parsed;
    try {
      final raw = r.columnAt(0);
      parsed = jsonDecode(raw is String && raw.isNotEmpty ? raw : '[]');
    } catch (_) {
      continue;
    }
    if (parsed is! List) continue;
    final seen = <String>{};
    for (final g in parsed) {
      final name = pyStrip(genreName(g) ?? '');
      if (name.isEmpty) continue;
      final key = pyLower(name);
      if (!seen.add(key)) continue;
      counts[key] = (counts[key] ?? 0) + 1;
      display.putIfAbsent(key, () => name);
    }
  }
  final ordered = counts.entries.toList()
    ..sort((a, b) {
      final c = b.value.compareTo(a.value);
      return c != 0 ? c : pyStrCompare(a.key, b.key);
    });
  return [
    for (final e in ordered.take(limit))
      {'name': display[e.key], 'artists': e.value},
  ];
};

/// `get_library_countries`: `[{'code', 'artists'}]`, most-used first.
List<Map<String, Object?>> libraryCountriesJob(Database db) => [
  for (final r in db.select(
    'SELECT country, COUNT(*) FROM artist_enrichment '
    "WHERE country IS NOT NULL AND country <> '' "
    'GROUP BY country ORDER BY COUNT(*) DESC',
  ))
    if (pyTruthy(r.columnAt(0)))
      {'code': r.columnAt(0), 'artists': r.columnAt(1)},
];

/// `get_low_confidence_artists`.
DbJob<List<Map<String, Object?>>> lowConfidenceArtistsJob([int limit = 200]) =>
    (db) => [
      for (final r in db.select(
        'SELECT a.name AS artist_name, a.track_count AS track_count, '
        'e.mbid AS mbid, e.country AS country, e.area AS area, e.genres AS genres, '
        'e.source AS source, e.score AS score, e.status AS status '
        'FROM artists a JOIN artist_enrichment e ON e.artist_name = a.name '
        "WHERE e.status = 'lowconfidence' "
        'ORDER BY a.track_count DESC LIMIT ?',
        [limit],
      ))
        {..._rowMap(r), 'genres': _decode(r['genres'], <Object?>[])},
    ];

/// `GAP_*` severities.
const kGapBlocking = 0, kGapSuggested = 1, kGapThin = 2, kGapNone = 3;

/// `get_artist_workbench_item`: one artist's workbench row.
DbJob<Map<String, Object?>?> artistWorkbenchItemJob(String name) => (db) {
  if (name.isEmpty) return null;
  final rows = db.select(
    'SELECT a.name AS artist_name, a.track_count AS track_count, '
    'e.country AS country, e.genres AS genres, e.source AS source, '
    'e.status AS status, e.provenance AS provenance, '
    "(SELECT group_concat(al.genre, '|') FROM albums al "
    "   WHERE al.artist_id = a.id AND al.genre IS NOT NULL AND al.genre <> '' "
    ') AS raw_file_genres '
    'FROM artists a LEFT JOIN artist_enrichment e ON e.artist_name = a.name '
    'WHERE a.name = ?',
    [name],
  );
  if (rows.isEmpty) return null;
  final d = _rowMap(rows.first);
  d['genres'] = _decode(d['genres'], <Object?>[]);
  d['provenance'] = _decode(d['provenance'], <String, Object?>{});
  final raw = d.remove('raw_file_genres') as String? ?? '';
  final source = cleanSourceTags(raw.split('|'));
  d['source_genres'] = source;
  final hasGenres = pyTruthy(d['genres']);
  final hasCountry = pyStrip((d['country'] as String?) ?? '').isNotEmpty;
  d['gap_severity'] = hasGenres || hasCountry
      ? kGapThin
      : source.isNotEmpty
      ? kGapSuggested
      : kGapBlocking;
  return d;
};

/// `get_artist_aliases(canonical)`.
DbJob<List<String>> artistAliasesForJob(String canonical) =>
    (db) => [
      for (final r in db.select(
        'SELECT alias FROM artist_aliases WHERE canonical = ? ORDER BY alias',
        [canonical],
      ))
        r.columnAt(0) as String,
    ];

/// `get_alias_rejections`: casefolded (a, b) pairs, a < b.
Set<(String, String)> aliasRejections(Database db) => {
  for (final r in db.select('SELECT a, b FROM artist_alias_rejections'))
    (r.columnAt(0) as String, r.columnAt(1) as String),
};

(String, String) _sortedPair(String x, String y) {
  final a = pyCasefold(x), b = pyCasefold(y);
  return pyStrCompare(a, b) <= 0 ? (a, b) : (b, a);
}

/// `reject_alias_pair`: 'not the same artist', never suggested again.
DbJob<void> rejectAliasPairJob(String nameA, String nameB) => (db) {
  final (a, b) = _sortedPair(nameA, nameB);
  db.execute(
    'INSERT OR IGNORE INTO artist_alias_rejections (a, b) VALUES (?, ?)',
    [a, b],
  );
};

/// `get_audit_dismissals`: {(artist casefolded, rule): confirmed country}.
Map<(String, String), String> auditDismissals(Database db) => {
  for (final r in db.select(
    'SELECT artist_name, rule, value FROM metadata_audit_dismissed',
  ))
    (pyCasefold(r.columnAt(0) as String), r.columnAt(1) as String):
        (r.columnAt(2) as String?) ?? '',
};

/// `dismiss_audit_flag`: hide this flag while the country stays [value].
DbJob<void> dismissAuditFlagJob(String artist, String rule, String value) =>
    (db) => db.execute(
      'INSERT OR REPLACE INTO metadata_audit_dismissed (artist_name, rule, value) '
      'VALUES (?, ?, ?)',
      [artist, rule, value],
    );

// ── AI research cache ───────────────────────────────────────────────────────

/// `save_ai_research`.
DbJob<void> saveAiResearchJob(
  String artistName,
  Map<String, Object?> payload, {
  String state = 'pending',
}) =>
    (db) => db.execute(
      'INSERT OR REPLACE INTO artist_ai_research '
      '(artist_name, payload, state, created_at) '
      "VALUES (?, ?, ?, strftime('%s','now'))",
      [artistName, pyJsonDumps(payload), state],
    );

/// `set_ai_research_state`.
DbJob<void> setAiResearchStateJob(String artistName, String state) =>
    (db) => db.execute(
      'UPDATE artist_ai_research SET state = ? WHERE artist_name = ?',
      [state, artistName],
    );

/// `get_researched_artist_names`: lower-cased, any state.
Set<String> researchedArtistNamesJob(Database db) => {
  for (final r in db.select('SELECT artist_name FROM artist_ai_research'))
    if (pyTruthy(r.columnAt(0))) pyLower(r.columnAt(0) as String),
};

/// `get_artist_research_evidence`: what the library knows about an artist,
/// for the model to tell namesakes apart.
DbJob<Map<String, Object?>> artistResearchEvidenceJob(String artist) => (db) {
  final albums = [
    for (final r in db.select(
      'SELECT al.title, al.year, al.genre FROM albums al '
      'JOIN artists ar ON ar.id = al.artist_id WHERE ar.name = ? '
      'ORDER BY al.track_count DESC LIMIT 12',
      [artist],
    ))
      {
        'title': r.columnAt(0),
        'year': pyTruthy(r.columnAt(1)) ? r.columnAt(1) : '',
      },
  ];
  final tracks = [
    for (final r in db.select(
      'SELECT t.title FROM tracks t JOIN albums al ON al.id = t.album_id '
      'JOIN artists ar ON ar.id = al.artist_id '
      "WHERE ar.name = ? AND t.title IS NOT NULL AND t.title <> '' "
      'ORDER BY t.id LIMIT 12',
      [artist],
    ))
      r.columnAt(0),
  ];
  final enr = artistEnrichmentJob(artist)(db) ?? const {};
  final fileTags = cleanSourceTags([
    for (final r in db.select(
      'SELECT al.genre FROM albums al JOIN artists ar ON ar.id = al.artist_id '
      "WHERE ar.name = ? AND al.genre IS NOT NULL AND al.genre <> ''",
      [artist],
    ))
      r.columnAt(0) as String?,
  ]);
  return {
    'artist': artist,
    'albums': albums,
    'tracks': tracks,
    'file_tags': fileTags,
    'known_country': pyTruthy(enr['country']) ? enr['country'] : '',
    'known_area': pyTruthy(enr['area']) ? enr['area'] : '',
    'known_genres': [
      for (final g in (enr['genres'] as List?) ?? const []) genreName(g),
    ],
  };
};

// ── artist aliases: merge & separate ────────────────────────────────────────

/// `merge_artists`: fold [alias] into [canonical], library-only. Throws a
/// [PyValueError] for two names that aren't two library artists.
DbJob<void> mergeArtistsJob(String alias0, String canonical0) => (db) {
  if (alias0.isEmpty ||
      canonical0.isEmpty ||
      pyCasefold(alias0) == pyCasefold(canonical0)) {
    throw PyValueError('Pick two different artists to merge.');
  }
  _transaction(db, () {
    final ids = <String, (int, String)>{};
    for (final n in [alias0, canonical0]) {
      final rows = db.select('SELECT id, name FROM artists WHERE name = ?', [
        n,
      ]);
      if (rows.isEmpty) {
        throw PyValueError("No artist named '$n' in the library.");
      }
      ids[n] = (
        rows.first.columnAt(0) as int,
        rows.first.columnAt(1) as String,
      );
    }
    final (aId, alias) = ids[alias0]!;
    final (cId, canonical) = ids[canonical0]!;

    db.execute(
      'UPDATE tracks SET artist_tag = ? WHERE artist_tag IS NULL AND album_id IN '
      '(SELECT id FROM albums WHERE artist_id = ?)',
      [alias, aId],
    );
    final albums = db.select(
      'SELECT id, title FROM albums WHERE artist_id = ?',
      [aId],
    );
    for (final al in albums) {
      final alId = al.columnAt(0) as int;
      final same = db.select(
        'SELECT id FROM albums WHERE artist_id = ? AND title = ?',
        [cId, al.columnAt(1)],
      );
      if (same.isNotEmpty) {
        db.execute('UPDATE tracks SET album_id = ? WHERE album_id = ?', [
          same.first.columnAt(0),
          alId,
        ]);
        db.execute('DELETE FROM albums WHERE id = ?', [alId]);
      } else {
        db.execute('UPDATE albums SET artist_id = ? WHERE id = ?', [cId, alId]);
      }
    }
    db.execute('DELETE FROM artists WHERE id = ?', [aId]);

    final aRows = db.select(
      'SELECT * FROM artist_enrichment WHERE artist_name = ?',
      [alias],
    );
    final snapshot = aRows.isEmpty ? null : _rowMap(aRows.first);
    if (snapshot != null) {
      final cRows = db.select(
        'SELECT country, genres, provenance FROM artist_enrichment '
        'WHERE artist_name = ?',
        [canonical],
      );
      if (cRows.isEmpty) {
        db.execute(
          'UPDATE artist_enrichment SET artist_name = ? WHERE artist_name = ?',
          [canonical, alias],
        );
      } else {
        final c = cRows.first;
        final rawProv = c.columnAt(2);
        final prov = pyTruthy(rawProv)
            ? Map<String, Object?>.from(
                jsonDecode(rawProv as String) as Map? ?? const {},
              )
            : <String, Object?>{};
        final sProv = snapshot['provenance'];
        final aProv = pyTruthy(sProv)
            ? (jsonDecode(sProv as String) as Map? ?? const {})
            : const <String, Object?>{};
        var country = c.columnAt(0);
        var genres = c.columnAt(1);
        if (!pyTruthy(country) && pyTruthy(snapshot['country'])) {
          country = snapshot['country'];
          prov['country'] = aProv['country'];
        }
        final sg = snapshot['genres'];
        if ((!pyTruthy(genres) || genres == '[]') &&
            !(sg == null || sg == '' || sg == '[]')) {
          genres = sg;
          prov['genres'] = aProv['genres'];
        }
        db.execute(
          'UPDATE artist_enrichment SET country = ?, genres = ?, provenance = ? '
          'WHERE artist_name = ?',
          [country, genres, pyJsonDumps(prov), canonical],
        );
        db.execute('DELETE FROM artist_enrichment WHERE artist_name = ?', [
          alias,
        ]);
      }
    }
    for (final table in const [
      'artist_ai_research',
      'artist_releases',
      'metadata_audit_dismissed',
    ]) {
      db.execute('DELETE FROM $table WHERE artist_name = ?', [alias]);
    }
    db.execute('UPDATE artist_aliases SET canonical = ? WHERE canonical = ?', [
      canonical,
      alias,
    ]);
    db.execute(
      'INSERT OR REPLACE INTO artist_aliases (alias, canonical, enrichment, created_at) '
      'VALUES (?, ?, ?, ?)',
      [
        alias,
        canonical,
        snapshot == null ? null : pyJsonDumps(snapshot),
        _now(),
      ],
    );
  });
};

/// `separate_artist`: undo a merge — the tracks tagged [alias0] go back to
/// an artist of that name with its old enrichment, and the pair is never
/// suggested again.
DbJob<void> separateArtistJob(String alias0) => (db) {
  final rows = db.select(
    'SELECT alias, canonical, enrichment FROM artist_aliases WHERE alias = ?',
    [alias0],
  );
  if (rows.isEmpty) {
    throw PyValueError("'$alias0' isn't merged into another artist.");
  }
  final alias = rows.first.columnAt(0) as String;
  final canonical = rows.first.columnAt(1) as String;
  final snapshot = rows.first.columnAt(2) as String?;
  _transaction(db, () {
    db.execute('DELETE FROM artist_aliases WHERE alias = ?', [alias]);
    final aliasFold = pyCasefold(alias);
    final tracks = [
      for (final r in db.select(
        'SELECT t.id, t.artist_tag, al.title, al.year, al.genre FROM tracks t '
        'JOIN albums al ON al.id = t.album_id JOIN artists ar ON ar.id = al.artist_id '
        'WHERE ar.name = ? AND t.artist_tag IS NOT NULL',
        [canonical],
      ))
        if (pyCasefold((r.columnAt(1) as String?) ?? '') == aliasFold) r,
    ];
    // _get_or_create_artist, with the alias no longer redirecting (aliases
    // still standing could, as in Python).
    final aliases = {
      for (final r in db.select('SELECT alias, canonical FROM artist_aliases'))
        pyCasefold(r.columnAt(0) as String): r.columnAt(1) as String,
    };
    var artistName = pyStrip(alias);
    artistName = aliases[pyCasefold(artistName)] ?? artistName;
    db.execute('INSERT OR IGNORE INTO artists (name) VALUES (?)', [artistName]);
    final aId =
        db
                .select('SELECT id FROM artists WHERE name = ?', [artistName])
                .first
                .columnAt(0)
            as int;
    for (final t in tracks) {
      final rawTitle = t.columnAt(2) as String?;
      final title = rawTitle == null || rawTitle.isEmpty
          ? 'Unknown Album'
          : pyStrip(rawTitle);
      db.execute(
        'INSERT OR IGNORE INTO albums (artist_id, title, year, genre) VALUES (?, ?, ?, ?)',
        [aId, title, t.columnAt(3), t.columnAt(4)],
      );
      final alId = db
          .select('SELECT id FROM albums WHERE artist_id = ? AND title = ?', [
            aId,
            title,
          ])
          .first
          .columnAt(0);
      db.execute(
        'UPDATE tracks SET album_id = ?, artist_tag = NULL WHERE id = ?',
        [alId, t.columnAt(0)],
      );
    }
    db.execute('DELETE FROM albums WHERE track_count <= 0');
    db.execute('DELETE FROM artists WHERE album_count <= 0');

    final snap = pyTruthy(snapshot) ? jsonDecode(snapshot!) : null;
    if (snap is Map && snap.isNotEmpty) {
      final exists = db.select(
        'SELECT 1 FROM artist_enrichment WHERE artist_name = ?',
        [alias],
      );
      if (exists.isEmpty) {
        final m = Map<String, Object?>.from(snap)..['artist_name'] = alias;
        final cols = m.keys.join(', ');
        final marks = List.filled(m.length, '?').join(', ');
        db.execute(
          'INSERT INTO artist_enrichment ($cols) VALUES ($marks)',
          m.values.toList(),
        );
      }
    }
    final (a, b) = _sortedPair(alias, canonical);
    db.execute(
      'INSERT OR IGNORE INTO artist_alias_rejections (a, b) VALUES (?, ?)',
      [a, b],
    );
  });
};

// ── album genre normalisation (fix_and_normalize_track_genres) ──────────────

const _placeholder = {
  '',
  'various',
  'various artists',
  'misc',
  'unknown',
  'other',
  'divers',
  'musique diverse',
  'special purpose artist',
  'autre',
};

bool _isPlaceholder(String tag) => _placeholder.contains(pyLower(pyStrip(tag)));

Set<String> _realTokens(String? tag) {
  if (tag == null || tag.isEmpty || _isPlaceholder(tag)) return {};
  return {
    for (final t in genreTokens(tag))
      if (t != 'Unknown' && t != 'Other') t,
  };
}

/// `Counter.most_common(1)[0][0]`, or null for an empty counter.
String? _top(Map<String, int> votes) {
  String? best;
  var bestN = 0;
  for (final MapEntry(:key, :value) in votes.entries) {
    if (best == null || value > bestN) {
      best = key;
      bestN = value;
    }
  }
  return best;
}

String _bucketCsv(Set<String> s) => (s.toList()..sort()).join(',');

/// `fix_and_normalize_track_genres`: album display genres re-derived where
/// they were placeholders or bucket labels, the multi-label `genre_bucket`
/// repaired against the artist's consensus, and a hand-set genre list
/// replacing both. Returns `{'scanned', 'genre_rederived', 'bucket_updated',
/// 'consensus_repaired'}`.
Map<String, Object?> fixAndNormalizeTrackGenresJob(Database db) {
  final rows = [
    for (final r in db.select(
      'SELECT al.id AS album_id, al.genre AS current_genre, '
      'al.genre_bucket AS current_bucket, ar.name AS artist_name, '
      'e.genres AS api_genres, e.status AS api_status, e.source AS api_source '
      'FROM albums al '
      'JOIN artists ar ON ar.id = al.artist_id '
      'LEFT JOIN artist_enrichment e ON e.artist_name = ar.name',
    ))
      _rowMap(r),
  ];

  final byArtist = <String, List<Map<String, Object?>>>{};
  for (final row in rows) {
    byArtist.putIfAbsent(row['artist_name']! as String, () => []).add(row);
  }
  final coreBy = <String, Set<String>>{};
  final unionBy = <String, Set<String>>{};
  final mbBy = <String, Set<String>>{};
  for (final MapEntry(key: artist, value: arows) in byArtist.entries) {
    final albumTokens = [
      for (final r in arows) _realTokens((r['current_genre'] as String?) ?? ''),
    ].where((t) => t.isNotEmpty).toList();
    final votes = <String, int>{};
    for (final toks in albumTokens) {
      for (final t in toks) {
        votes[t] = (votes[t] ?? 0) + 1;
      }
    }
    final n = albumTokens.length;
    final threshold = n > 0 ? (2 > (n + 1) ~/ 2 ? 2 : (n + 1) ~/ 2) : 0;
    coreBy[artist] = {
      for (final e in votes.entries)
        if (e.value >= threshold) e.key,
    };
    unionBy[artist] = votes.keys.toSet();
    final mb = <String>{};
    final first = arows.first;
    if ((first['api_status'] ?? '') == 'ok' && pyTruthy(first['api_genres'])) {
      try {
        for (final g in jsonDecode(first['api_genres']! as String) as List) {
          if (g is Map && pyTruthy(g['name'])) {
            mb.addAll(_realTokens(g['name'] as String));
          }
        }
      } catch (_) {}
    }
    mbBy[artist] = mb;
  }

  final genreUpdates = <(String, int)>[];
  final bucketUpdates = <(String?, int)>[];
  var consensusRepaired = 0;

  for (final row in rows) {
    final albumId = row['album_id']! as int;
    final artist = row['artist_name']! as String;
    final currGenre = pyStrip((row['current_genre'] as String?) ?? '');
    final currBucket = pyStrip((row['current_bucket'] as String?) ?? '');
    final apiStatus = (row['api_status'] as String?) ?? '';
    final apiRaw = row['api_genres'];

    final bucketVotes = <String, int>{};
    var tagsByCount = <String>[];
    if (pyTruthy(apiRaw) && apiStatus == 'ok') {
      try {
        final parsed = jsonDecode(apiRaw! as String);
        if (parsed is List) {
          final scored = <(int, String)>[];
          for (final g in parsed) {
            if (g is Map && pyTruthy(g['name'])) {
              final tagName = g['name'] as String;
              var cnt = _pyIntOr1(g['count']);
              if (cnt < 1) cnt = 1;
              scored.add((cnt, tagName));
              final b = genreBucket(tagName);
              if (b != 'Unknown' && b != 'Other') {
                bucketVotes[b] = (bucketVotes[b] ?? 0) + cnt;
              }
            }
          }
          final idx = [for (var i = 0; i < scored.length; i++) i]
            ..sort((a, b) {
              final c = scored[b].$1.compareTo(scored[a].$1);
              return c != 0 ? c : a.compareTo(b);
            });
          tagsByCount = [for (final i in idx) scored[i].$2];
        }
      } catch (_) {}
    }
    final apiConsensus = _top(bucketVotes);

    final rederivable =
        _isPlaceholder(currGenre) || genreBucketLabels.contains(currGenre);
    var display = currGenre;
    if (rederivable) {
      if (apiConsensus != null) {
        String? rep;
        for (final t in tagsByCount) {
          if (genreBucket(t) == apiConsensus) {
            rep = t;
            break;
          }
        }
        if (rep != null) display = pyTitle(rep);
      } else if (_isPlaceholder(currGenre) && tagsByCount.isNotEmpty) {
        display = genreDisplayLabel(tagsByCount.first);
      }
    }

    var manualTags = <String>[];
    if ((row['api_source'] ?? '') == 'manual' && pyTruthy(apiRaw)) {
      try {
        manualTags = [
          for (final g in jsonDecode(apiRaw! as String) as List)
            if (g is Map && pyTruthy(g['name'])) g['name'] as String,
        ];
      } catch (_) {
        manualTags = [];
      }
    }
    if (manualTags.isNotEmpty) {
      final fams = <String, int>{};
      for (final t in manualTags) {
        final b = genreBucket(t);
        if (b != 'Unknown' && b != 'Other') fams[b] = (fams[b] ?? 0) + 1;
      }
      final top = _top(fams);
      final rep = manualTags.firstWhere(
        (t) => genreBucket(t) == top,
        orElse: () => manualTags.first,
      );
      display = pyTitle(pyStrip(rep));
      final fin = <String>{};
      for (final t in manualTags) {
        fin.addAll(_realTokens(t));
      }
      if (display != currGenre) genreUpdates.add((display, albumId));
      final newBucket = fin.isNotEmpty ? _bucketCsv(fin) : null;
      if (newBucket != (currBucket.isEmpty ? null : currBucket)) {
        bucketUpdates.add((newBucket, albumId));
      }
      continue;
    }

    if (display != currGenre) genreUpdates.add((display, albumId));

    var own = _realTokens(currGenre);
    if (own.isEmpty) own = _realTokens(display);
    final mbTags = mbBy[artist] ?? const <String>{};
    Set<String> fin;
    if (own.isNotEmpty) {
      fin = {...own, ...?coreBy[artist], ...mbTags};
    } else {
      fin = {...?unionBy[artist], ...mbTags};
      if (fin.isEmpty && apiConsensus != null) fin = {apiConsensus};
      if (fin.isEmpty && display.isNotEmpty) {
        final b = genreBucket(display);
        if (b != 'Unknown' && b != 'Other') fin = {b};
      }
    }
    if (own.isNotEmpty && fin.length > own.length && fin.containsAll(own)) {
      consensusRepaired++;
    }
    final newBucket = fin.isNotEmpty ? _bucketCsv(fin) : null;
    if (newBucket != null && newBucket != currBucket) {
      bucketUpdates.add((newBucket, albumId));
    }
  }

  if (genreUpdates.isNotEmpty || bucketUpdates.isNotEmpty) {
    _transaction(db, () {
      for (final (g, id) in genreUpdates) {
        db.execute('UPDATE albums SET genre = ? WHERE id = ?', [g, id]);
      }
      for (final (b, id) in bucketUpdates) {
        db.execute('UPDATE albums SET genre_bucket = ? WHERE id = ?', [b, id]);
      }
    });
  }
  return {
    'scanned': rows.length,
    'genre_rederived': genreUpdates.length,
    'bucket_updated': bucketUpdates.length,
    'consensus_repaired': consensusRepaired,
  };
}

/// `int(g.get("count", 1) or 1)`.
int _pyIntOr1(Object? v) {
  if (v == null || v == false || v == 0 || v == '') return 1;
  if (v == true) return 1;
  if (v is int) return v;
  if (v is double) return v.isFinite ? v.truncate() : 1;
  if (v is String) return int.tryParse(pyStrip(v)) ?? 1;
  return 1;
}

/// `metadata_ai.accept_suggestion`: an AI suggestion written as the user's
/// own answer (source 'manual'), labelled 'ai' in provenance and merged with
/// what the artist already has; the research row is marked accepted.
DbJob<void> acceptAiSuggestionJob(
  String artist, {
  List<String>? genres,
  String? country,
}) => (db) {
  final cached = aiResearchJob(artist)(db);
  final payload = cached?['payload'] is Map
      ? cached!['payload']! as Map
      : const <Object?, Object?>{};
  final add = <Object?>[
    ...?(genres ??
        (payload['genres'] is List ? payload['genres'] as List : null)),
  ];
  final rawCountry = country ?? payload['country'];
  final aiCountry = pyTruthy(rawCountry) ? '$rawCountry' : null;
  final cur = artistEnrichmentJob(artist)(db) ?? const <String, Object?>{};
  final have = [
    for (final g in (cur['genres'] as List?) ?? const []) genreName(g),
  ];
  final merged = List<Object?>.of(have);
  for (final g in add) {
    if (!pyTruthy(g)) continue;
    final lowered = {
      for (final h in merged)
        if (h is String && h.isNotEmpty) pyLower(h),
    };
    if (!lowered.contains(pyLower('$g'))) merged.add(g);
  }
  final curCountry = pyTruthy(cur['country']) ? cur['country'] as String : null;
  final finalCountry = aiCountry ?? curCountry;
  final oldProv = cur['provenance'] is Map
      ? cur['provenance']! as Map
      : const <Object?, Object?>{};
  upsertArtistEnrichment(
    db,
    artist,
    mbid: cur['mbid'],
    country: finalCountry,
    area: cur['area'] as String?,
    genres: [
      for (final g in merged) {'name': g, 'count': 1},
    ],
    source: 'manual',
    status: 'ok',
    score: 100,
    force: true,
    provenance: {
      'country': aiCountry != null
          ? 'ai'
          : (finalCountry != null ? oldProv['country'] : null),
      'genres': add.isNotEmpty ? 'ai' : oldProv['genres'],
    },
  );
  if (cached != null) setAiResearchStateJob(artist, 'accepted')(db);
};
