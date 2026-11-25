import 'dart:convert';

import 'package:sqlite3/sqlite3.dart';

import '../core/db_worker.dart';
import '../core/py_text.dart';
import '../core/py_values.dart';
import '../data/metadata_queries.dart'
    show aliasRejections, auditDismissals, genreName;
import '../stations/credit_keys.dart';
import '../stations/genre_taxonomy.dart';

/// `metadata_audit.py`: mistakes in metadata the library already has — a
/// country that contradicts the artist's own titles, tags, collaborators or
/// a web-backed AI answer, and one artist under two spellings. Plain rules,
/// run on the DB isolate on every workbench load.

const _greekCountries = {'GR', 'CY'};
const _scriptShare = 0.5;
const _minTitles = 2;
const _trustedAiBasis = {'web', 'grounding', 'script'};
const _placeholderArtists = {
  'various artists',
  'unknown artist',
  'various',
  'va',
  'unknown',
};
const _minCollabs = 2;

final _tagSplit = RegExp(r'[,;/|]');

Map<String, Object?> _flag(String rule, String reason, [String suggest = '']) =>
    {'rule': rule, 'reason': reason, 'suggest_country': suggest};

String? _scriptOf(String? t) => scriptCulture([t]);

/// `audit_flags`: {artist: [flag, ...]} for artists whose country looks
/// wrong. A flag is {'rule', 'reason', 'suggest_country'} (+
/// 'suggest_genres' for 'collab'). Manual answers are never second-guessed;
/// a dismissed flag stays hidden while the country is unchanged.
Map<String, List<Map<String, Object?>>> auditFlagsJob(Database db) {
  final enriched = <String, (String, String?)>{};
  for (final r in db.select(
    'SELECT e.artist_name, UPPER(e.country), e.genres FROM artist_enrichment e '
    'JOIN artists a ON a.name = e.artist_name '
    "WHERE e.country IS NOT NULL AND e.country <> '' "
    "AND (e.source IS NULL OR e.source <> 'manual')",
  )) {
    enriched[r.columnAt(0) as String] = (
      r.columnAt(1) as String,
      r.columnAt(2) as String?,
    );
  }
  if (enriched.isEmpty) return {};

  final titles = <String, Set<String>>{};
  final fileGenres = <String, Set<String>>{};
  for (final r in db.select(
    'SELECT ar.name, al.title, al.genre, t.title FROM tracks t '
    'JOIN albums al ON al.id = t.album_id JOIN artists ar ON ar.id = al.artist_id '
    'JOIN artist_enrichment e ON e.artist_name = ar.name '
    "WHERE e.country IS NOT NULL AND e.country <> ''",
  )) {
    final name = r.columnAt(0) as String;
    for (final t in [r.columnAt(1), r.columnAt(3)]) {
      if (t is String && t.isNotEmpty) {
        titles.putIfAbsent(name, () => {}).add(t);
      }
    }
    final g = r.columnAt(2);
    if (g is String && g.isNotEmpty) {
      fileGenres.putIfAbsent(name, () => {}).add(g);
    }
  }

  final ai = <String, Map<String, Object?>>{};
  for (final r in db.select(
    'SELECT artist_name, payload FROM artist_ai_research '
    "WHERE state <> 'dismissed'",
  )) {
    try {
      final p = jsonDecode(r.columnAt(1) as String);
      if (p is Map<String, Object?>) {
        ai[pyCasefold(r.columnAt(0) as String)] = p;
      }
    } catch (_) {}
  }

  final collab = _collaboratorCultures(db);
  final dismissed = auditDismissals(db);
  final out = <String, List<Map<String, Object?>>>{};
  for (final MapEntry(key: name, value: (country, genresJson))
      in enriched.entries) {
    final flags = <Map<String, Object?>>[];
    final ts = titles[name] ?? const <String>{};

    final c = collab[name];
    if (c != null && !_greekCountries.contains(country)) {
      final (cc, nGr, nAll, tagsLinked) = c;
      flags.add({
        ..._flag(
          'collab',
          '$nGr of the $nAll credits it shares here are Greek, but '
              'the country is $country. Probably a different artist '
              'with the same name.',
          cc,
        ),
        'suggest_genres': tagsLinked,
      });
    }

    final greek = ts.where((t) => _scriptOf(t) == 'GR').length;
    if (ts.length >= _minTitles &&
        greek / ts.length >= _scriptShare &&
        !_greekCountries.contains(country)) {
      flags.add(
        _flag(
          'script',
          'Most titles are in Greek, but the country is $country.',
          'GR',
        ),
      );
    }

    final tags = <String>{};
    try {
      final parsed = jsonDecode(
        genresJson == null || genresJson.isEmpty ? '[]' : genresJson,
      );
      for (final g in parsed as List) {
        final n = genreName(g);
        if (n != null) tags.add(n);
      }
    } catch (_) {}
    for (final raw in fileGenres[name] ?? const <String>{}) {
      for (final p in raw.split(_tagSplit)) {
        final s = pyStrip(p);
        if (s.isNotEmpty) tags.add(s);
      }
    }
    final cultural = [
      for (final t in tags)
        if (t.isNotEmpty && tagCulture(t) == 'GR') t,
    ]..sort(pyStrCompare);
    if (cultural.isNotEmpty &&
        !_greekCountries.contains(country) &&
        !flags.any((f) => f['rule'] == 'script' || f['rule'] == 'collab')) {
      flags.add(
        _flag(
          'tags',
          'Tagged ${cultural.first}, but the country is $country.',
          'GR',
        ),
      );
    }

    final p = ai[pyCasefold(name)] ?? const <String, Object?>{};
    final aiCc = (pyTruthy(p['country']) ? '${p['country']}' : '')
        .toUpperCase();
    if (aiCc.isNotEmpty &&
        aiCc != country &&
        _trustedAiBasis.contains(p['country_basis']) &&
        !(_greekCountries.contains(aiCc) && flags.isNotEmpty)) {
      flags.add(
        _flag('ai', 'The web says $aiCc; the library says $country.', aiCc),
      );
    }

    final kept = [
      for (final f in flags)
        if (dismissed[(pyCasefold(name), f['rule']! as String)] != country) f,
    ];
    if (kept.isNotEmpty) out[name] = kept;
  }
  return out;
}

/// `_collaborator_cultures`: {artist: ('GR', n_greek, n_linked, top genres)}
/// for solo artists most of whose library credits are Greek.
Map<String, (String, int, int, List<String>)> _collaboratorCultures(
  Database db,
) {
  final rows = [
    for (final r in db.select(
      "SELECT a.name, UPPER(COALESCE(e.country, '')), e.genres FROM artists a "
      'LEFT JOIN artist_enrichment e ON e.artist_name = a.name',
    ))
      (r.columnAt(0) as String, r.columnAt(1) as String, r.columnAt(2)),
  ];
  final keys = {for (final (n, _, _) in rows) n: creditKeys(n)};
  final byKey = <String, Set<String>>{};
  for (final MapEntry(key: name, value: ks) in keys.entries) {
    for (final k in ks) {
      byKey.putIfAbsent(k, () => {}).add(name);
    }
  }
  final info = {for (final (n, cc, g) in rows) n: (cc, g)};
  bool greek(String name) =>
      _greekCountries.contains(info[name]!.$1) || _scriptOf(name) == 'GR';

  final out = <String, (String, int, int, List<String>)>{};
  for (final MapEntry(key: name, value: ks) in keys.entries) {
    if (ks.length != 1) continue;
    final linked = <String>{for (final k in ks) ...?byKey[k]}..remove(name);
    final nGr = linked.where(greek).length;
    if (nGr < _minCollabs || nGr <= linked.length / 2) continue;
    final votes = <String, int>{};
    // Python iterates a set here (hash order); sorted keeps it stable.
    for (final v in linked.toList()..sort(pyStrCompare)) {
      if (!greek(v)) continue;
      try {
        final raw = info[v]!.$2;
        final parsed = jsonDecode(raw is String && raw.isNotEmpty ? raw : '[]');
        for (final g in parsed as List) {
          final nm = genreName(g) ?? '';
          if (nm.isNotEmpty && tagCulture(nm) != 'GR') {
            final k = pyLower(nm);
            votes[k] = (votes[k] ?? 0) + 1;
          }
        }
      } catch (_) {}
    }
    final ordered = votes.entries.toList();
    final idx = [for (var i = 0; i < ordered.length; i++) i]
      ..sort((a, b) {
        final c = ordered[b].value.compareTo(ordered[a].value);
        return c != 0 ? c : a.compareTo(b);
      });
    out[name] = (
      'GR',
      nGr,
      linked.length,
      [for (final i in idx.take(3)) ordered[i].key],
    );
  }
  return out;
}

// ── duplicates ──────────────────────────────────────────────────────────────

const _greekDigraphs = [
  ('ου', 'u'), ('αι', 'e'), ('ει', 'i'), ('οι', 'i'), ('υι', 'i'), //
  ('αυ', 'av'), ('ευ', 'ev'), ('μπ', 'b'), ('ντ', 'd'), ('γκ', 'g'),
  ('γγ', 'ng'), ('τσ', 'ts'), ('τζ', 'tz'),
];

const _greekLetters = {
  'α': 'a', 'β': 'v', 'γ': 'g', 'δ': 'd', 'ε': 'e', 'ζ': 'z', 'η': 'i', //
  'θ': 'th', 'ι': 'i', 'κ': 'k', 'λ': 'l', 'μ': 'm', 'ν': 'n', 'ξ': 'x',
  'ο': 'o', 'π': 'p', 'ρ': 'r', 'σ': 's', 'τ': 't', 'υ': 'i', 'φ': 'f',
  'χ': 'h', 'ψ': 'ps', 'ω': 'o',
};

const _latinFolds = [
  ('ou', 'u'), ('ai', 'e'), ('ei', 'i'), ('oi', 'i'), ('y', 'i'), //
  ('eu', 'ev'), ('ef', 'ev'), ('au', 'av'), ('af', 'av'),
  ('mp', 'b'), ('nt', 'd'), ('gk', 'g'), ('gg', 'ng'),
  ('ch', 'h'), ('kh', 'h'), ('ph', 'f'), ('c', 'k'), ('w', 'o'),
];

/// `_plain`: NFKD, casefolded, combining marks dropped.
String plainName(String? name) => nfkdCasefoldStrip(name ?? '');

final _articleRe = RegExp(r'^(the|οι|ο|η|τα)\s+', unicode: true);
final _wordRe = RegExp('[a-z0-9]+');

/// `skeleton`: a spelling-blind key (Greek transliterated, romanisation
/// variants folded, word order ignored, doubled letters collapsed).
String skeleton(String? name) {
  var s = plainName(name);
  // Python's \s; the article is followed by ordinary spaces in practice.
  final m = _articleRe.firstMatch(s);
  if (m != null) s = s.substring(m.end);
  for (final (g, l) in _greekDigraphs) {
    s = s.replaceAll(g, l);
  }
  final b = StringBuffer();
  for (final r in s.runes) {
    final ch = String.fromCharCode(r);
    b.write(_greekLetters[ch] ?? ch);
  }
  s = b.toString();
  final words = <String>[];
  for (final wm in _wordRe.allMatches(s)) {
    var w = wm[0]!;
    for (final (a, z) in _latinFolds) {
      w = w.replaceAll(a, z);
    }
    w = _collapseRuns(w);
    if (w.isNotEmpty) words.add(w);
  }
  words.sort();
  return words.join(' ');
}

/// `re.sub(r"(.)\1+", r"\1", w)` on an ASCII word.
String _collapseRuns(String w) {
  final b = StringBuffer();
  for (var i = 0; i < w.length; i++) {
    if (i > 0 && w[i] == w[i - 1]) continue;
    b.write(w[i]);
  }
  return b.toString();
}

/// `duplicate_artists`: pairs of artist rows that look like one artist,
/// `[{'alias', 'canonical', 'alias_tracks', 'canonical_tracks'}]`; the name
/// with more tracks is proposed to keep. Rejected pairs never return.
List<Map<String, Object?>> duplicateArtistsJob(Database db) {
  final rows = [
    for (final r in db.select('SELECT name, track_count FROM artists'))
      if (pyTruthy(r.columnAt(0)))
        (r.columnAt(0) as String, (r.columnAt(1) as num?)?.toInt() ?? 0),
  ];
  final rejected = aliasRejections(db);
  final groups = <String, List<(String, int)>>{};
  for (final (name, n) in rows) {
    if (_placeholderArtists.contains(pyStrip(plainName(name)))) continue;
    final key = skeleton(name);
    if (key.replaceAll(' ', '').length >= 3) {
      groups.putIfAbsent(key, () => []).add((name, n));
    }
  }
  final out = <Map<String, Object?>>[];
  for (final members in groups.values) {
    if (members.length < 2) continue;
    members.sort((a, b) {
      final c = b.$2.compareTo(a.$2);
      return c != 0 ? c : pyStrCompare(a.$1, b.$1);
    });
    final (keep, keepN) = members.first;
    for (final (name, n) in members.skip(1)) {
      final a = pyCasefold(name), b = pyCasefold(keep);
      final pair = pyStrCompare(a, b) <= 0 ? (a, b) : (b, a);
      if (rejected.contains(pair)) continue;
      out.add({
        'alias': name,
        'canonical': keep,
        'alias_tracks': n,
        'canonical_tracks': keepN,
      });
    }
  }
  final idx = [for (var i = 0; i < out.length; i++) i]
    ..sort((x, y) {
      int sum(int i) =>
          (out[i]['alias_tracks']! as int) +
          (out[i]['canonical_tracks']! as int);
      final c = sum(y).compareTo(sum(x));
      return c != 0 ? c : x.compareTo(y);
    });
  return [for (final i in idx) out[i]];
}

/// `album_titles`: a few of an artist's album titles, biggest first.
DbJob<List<String>> albumTitlesJob(String artist, [int limit = 3]) =>
    (db) => [
      for (final r in db.select(
        'SELECT al.title FROM albums al JOIN artists ar ON ar.id = al.artist_id '
        'WHERE ar.name = ? ORDER BY al.track_count DESC LIMIT ?',
        [artist, limit],
      ))
        r.columnAt(0) as String,
    ];
