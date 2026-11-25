import '../core/py_text.dart';

/// Coarse genre taxonomy: a port of `utils/genre_taxonomy.py`. The rule order
/// and every key are copied verbatim; see the Python module for why each is
/// where it is.

const _regionalKeys = [
  'laiko', 'laika', 'laïko', 'laïka', 'laiki', 'λαϊκό', 'λαϊκά', //
  'rebetiko', 'ρεμπέτικο', 'entechno', 'entehno', 'έντεχνο', 'greek folk',
  'musiques du monde', 'world music', 'worldbeat',
  'morna', 'coladeira', 'fado', 'enka', 'bhangra',
];

const _genreRules = <(String, List<String>)>[
  ('Folk/Cntry', _regionalKeys),
  ('Classical', ['classical', 'classique']),
  (
    'Hip-Hop',
    [
      'rap', 'hip hop', 'hip-hop', 'hiphop', 'trap', 'хип', 'рэп', 'grime', //
      'boom bap', 'drill',
    ],
  ),
  (
    'Electronic',
    [
      'électronique', 'electronica', 'electro', 'électro', 'house', 'techno', //
      'edm', 'trance', 'drum & bass', 'drum and bass', 'dnb', 'dubstep',
      'ambient', 'trip hop', 'downtempo',
    ],
  ),
  ('Soul/R&B', ['rhythm and blues']),
  (
    'Folk/Cntry',
    [
      'folk', 'country', 'blues', 'bluegrass', 'americana', 'laiko', 'laika', //
      'laïko', 'laïka', 'laiki', 'λαϊκό', 'λαϊκά', 'rebetiko', 'ρεμπέτικο',
      'entechno', 'έντεχνο', 'greek folk', 'world', 'musiques du monde',
    ],
  ),
  ('Soul/R&B', ['soul', 'r&b', 'funk', 'rnb', 'motown', 'neo soul']),
  ('Jazz', ['jazz', 'bebop']),
  (
    'Latin',
    [
      'latin', 'salsa', 'reggaeton', 'bachata', 'cumbia', 'merengue', //
      'bossa nova', 'samba', 'flamenco',
    ],
  ),
  ('Reggae', ['reggae', 'dancehall', 'ragga']),
  ('Gospel', ['gospel', 'worship']),
  ('Soundtrack', ['soundtrack', 'film score']),
  ('Disco', ['disco']),
  ('Metal', ['metal', 'métal', 'hard rock', 'grunge', 'heavy metal']),
  (
    'Alt',
    [
      'alternatif', 'alternative', 'indé', 'indie', 'punk', 'new wave', //
      'post-punk', 'new romantic', 'darkwave', 'shoegaze', 'post-hardcore',
    ],
  ),
  ('Rock', ['rock', 'рок']),
  ('Asian-Pop', ['k-pop', 'j-pop', 'jpop', 'mandopop', 'cantopop', 'c-pop']),
  ('Pop', ['pop', 'поп', 'greek pop', 'greek']),
];

/// `GENRE_BUCKET_LABELS`: every family label, plus the two sentinels.
final Set<String> genreBucketLabels = {
  for (final (label, _) in _genreRules) label,
  'Other',
  'Unknown',
};

/// `NON_FAMILIES`: the sentinels [genreBucket] can return.
const nonFamilies = {'Other', 'Unknown'};

const _regionalBuckets = {'Latin', 'Reggae', 'Asian-Pop'};

/// `CULTURE_WORDS`: tags that name only a culture, never a genre.
const cultureWords = {'greek', 'greece', 'ελληνικά', 'ελληνικο', 'ελληνικό'};

const _minCompact = 6;

class _Compiled {
  _Compiled(this.keys)
    : compact = [
        for (final k in keys)
          if (alnumOnly(k) != k && alnumOnly(k).runes.length >= _minCompact)
            alnumOnly(k),
      ];
  final List<String> keys;
  final List<String> compact;

  bool hit(String g, String gc) =>
      keys.any(g.contains) || compact.any(gc.contains);
}

final _rules = [
  for (final (label, keys) in _genreRules) (label, _Compiled(keys)),
];
final _regional = _Compiled(_regionalKeys);

final _cultureTags = {
  'GR': _Compiled([
    'laiko', 'laika', 'laïko', 'laïka', 'laiki', 'λαϊκ', 'rebetiko', //
    'ρεμπέτικο', 'entechno', 'entehno', 'έντεχνο', 'greek', 'ελληνικ',
  ]),
};

const _cultureScripts = {
  'GR': [(0x0370, 0x03ff), (0x1f00, 0x1fff)],
};

String _norm(String? genre) => pyLower((genre ?? '').trim());

/// One coarse family for a free-text tag: the first rule that matches.
String genreBucket(String? genre) {
  final g = _norm(genre);
  if (g.isEmpty) return 'Unknown';
  final gc = alnumOnly(g);
  for (final (label, c) in _rules) {
    if (c.hit(g, gc)) return label;
  }
  return 'Other';
}

/// Every family a tag matches.
Set<String> genreTokens(String? genre) {
  final g = _norm(genre);
  if (g.isEmpty) return {'Unknown'};
  final gc = alnumOnly(g);
  final toks = {
    for (final (label, c) in _rules)
      if (c.hit(g, gc)) label,
  };
  return toks.isEmpty ? {'Other'} : toks;
}

/// True for a scene tied to a language or country.
bool isRegionalTag(String? genre) {
  final g = _norm(genre);
  if (g.isEmpty) return false;
  if (_regional.hit(g, alnumOnly(g))) return true;
  return _regionalBuckets.contains(genreBucket(g));
}

/// The bucket, or the tag itself title-cased when it maps to none.
String genreDisplayLabel(String? genre) {
  final g = (genre ?? '').trim();
  if (g.isEmpty) return 'Unknown';
  final b = genreBucket(g);
  return b == 'Other' ? pyTitle(g) : b;
}

/// The culture a tag names ('laiko' → 'GR'), or null.
String? tagCulture(String? tag) {
  final g = _norm(tag);
  if (g.isEmpty) return null;
  final gc = alnumOnly(g);
  for (final MapEntry(:key, :value) in _cultureTags.entries) {
    if (value.hit(g, gc)) return key;
  }
  return null;
}

/// The culture whose script any of [texts] is written in, or null.
String? scriptCulture(Iterable<String?> texts) {
  for (final text in texts) {
    for (final o in (text ?? '').runes) {
      if (o < 0x370) continue;
      for (final MapEntry(:key, :value) in _cultureScripts.entries) {
        if (value.any((b) => b.$1 <= o && o <= b.$2)) return key;
      }
    }
  }
  return null;
}
