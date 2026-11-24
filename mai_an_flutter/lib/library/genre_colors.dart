import 'dart:ui';

import '../core/py_text.dart';
import '../stations/genre_taxonomy.dart';

/// The Genres tab's grouping and colours: a port of `ui/views/library.py`
/// `_canon_genre`, `_genre_group` and `_genre_color`, with the palettes they
/// read (`genre_taxonomy._GENRE_PALETTE`, `_CLUSTER_PALETTE`). Pinned against
/// Python's output in test/goldens/library_synthetic.json.

/// `_PLACEHOLDER_TAGS`: source values that mean "no genre".
const _placeholderTags = {
  '', 'various', 'various artists', 'misc', 'unknown', 'other', //
  'divers', 'musique diverse', 'special purpose artist', 'autre',
};

/// `_GENRE_ALIASES`: canonical token → (group key, label).
const _genreAliases = <String, (String, String)>{
  'electro': ('Electronic', 'Electronic'),
  'electronica': ('Electronic', 'Electronic'),
  'electronicdance': ('Electronic', 'Electronic'),
  'dance': ('Electronic', 'Electronic'),
  'house': ('Electronic', 'Electronic'),
  'techno': ('Electronic', 'Electronic'),
  'trance': ('Electronic', 'Electronic'),
  'synthpop': ('Electronic', 'Electronic'),
  'drumandbass': ('Electronic', 'Electronic'),
  'drumnbass': ('Electronic', 'Electronic'),
  'idm': ('Electronic', 'Electronic'),
  'ebm': ('Electronic', 'Electronic'),
  'coldwave': ('Electronic', 'Electronic'),
  'grime': ('Hip-Hop', 'Hip-Hop'),
  'laika': ('Folk/Cntry', 'Folk/Cntry'),
  'laiko': ('Folk/Cntry', 'Folk/Cntry'),
  'laiki': ('Folk/Cntry', 'Folk/Cntry'),
  'laikopo': ('Folk/Cntry', 'Folk/Cntry'),
  'laikopop': ('Folk/Cntry', 'Folk/Cntry'),
  'greekpop': ('Pop', 'Pop'),
  'greekfolk': ('Folk/Cntry', 'Folk/Cntry'),
  'entechno': ('Folk/Cntry', 'Folk/Cntry'),
  'rebetiko': ('Folk/Cntry', 'Folk/Cntry'),
  'postpunk': ('Alt', 'Alt'),
  'rhythmandblues': ('Soul/R&B', 'Soul/R&B'),
  'randb': ('Soul/R&B', 'Soul/R&B'),
  'triphop': ('Trip-Hop', 'Trip-Hop'),
  'jazz': ('Jazz', 'Jazz'),
  'nujazz': ('Jazz', 'Jazz'),
  'acidjazz': ('Jazz', 'Jazz'),
  'smoothjazz': ('Jazz', 'Jazz'),
  'reggae': ('Reggae', 'Reggae'),
  'ragga': ('Reggae', 'Reggae'),
  'dancehall': ('Reggae', 'Reggae'),
  'ambient': ('Ambient', 'Ambient'),
  'soundtrack': ('Soundtrack', 'Soundtrack'),
  'score': ('Soundtrack', 'Soundtrack'),
  'ost': ('Soundtrack', 'Soundtrack'),
  'disco': ('Disco', 'Disco'),
  'gospel': ('Gospel', 'Gospel'),
  'latin': ('Latin', 'Latin'),
  'salsa': ('Latin', 'Latin'),
  'reggaeton': ('Latin', 'Latin'),
  'bachata': ('Latin', 'Latin'),
  'world': ('World', 'World'),
  'afrobeat': ('World', 'World'),
  'afrobeats': ('World', 'World'),
};

/// `genre_taxonomy._GENRE_PALETTE`.
const genrePalette = <String, Color>{
  'Rock': Color(0xFF40C4FF),
  'Alt': Color(0xFF7C4DFF),
  'Pop': Color(0xFFFF80AB),
  'Metal': Color(0xFFFF5252),
  'Hip-Hop': Color(0xFFFFD740),
  'Electronic': Color(0xFF76FF03),
  'Folk/Cntry': Color(0xFFB388FF),
  'Classical': Color(0xFFFFFFFF),
  'Soul/R&B': Color(0xFFFF6E40),
  'Jazz': Color(0xFF26C6DA),
  'Latin': Color(0xFFFF7043),
  'Reggae': Color(0xFF66BB6A),
  'Gospel': Color(0xFFBA68C8),
  'Soundtrack': Color(0xFF5C6BC0),
  'Disco': Color(0xFFF50057),
  'Asian-Pop': Color(0xFFE040FB),
  'Other': Color(0xFF888888),
  'Unknown': Color(0xFF444444),
};

/// library.py `_CLUSTER_PALETTE` (no greens: green is the walk overlay's).
const _clusterPalette = [
  Color(0xFF40C4FF), Color(0xFFFF80AB), Color(0xFFFF5252), Color(0xFFFFD740), //
  Color(0xFFB388FF), Color(0xFFFFFFFF), Color(0xFFFF6E40), Color(0xFF26C6DA),
  Color(0xFFFF7043), Color(0xFFBA68C8), Color(0xFF5C6BC0), Color(0xFFF50057),
  Color(0xFFE040FB), Color(0xFFFF9100), Color(0xFFD500F9), Color(0xFF00E5FF),
  Color(0xFFFF3D00), Color(0xFFC0CA33), Color(0xFF00B0FF), Color(0xFFE6EE9C),
];

/// `_CLUSTER_NEUTRAL`: untagged.
const clusterNeutral = Color(0xFF8A93A0);

/// `_canon_genre`: lower case, alphanumerics only ('Hip-Hop' → 'hiphop').
String canonGenre(String? genre) =>
    (genre == null || genre.isEmpty) ? '' : alnumOnly(pyLower(genre));

final _ws = RegExp(r'\s+');
final _segmentSep = RegExp(r'[/,;&+]');

/// `_genre_group`: (grouping key, display label) for a genre tag. Known
/// families collapse through `genre_bucket` over the whole string (so the
/// order the source lists them in doesn't matter); otherwise the first
/// segment with an alias wins, else the first segment's canonical token.
(String, String) genreGroup(String? genre) {
  if (genre == null || genre.isEmpty) return ('', '');
  final raw = genre.trim();
  if (_placeholderTags.contains(pyLower(raw))) return ('', '');
  final normFull = raw.split(_ws).where((s) => s.isNotEmpty).join(' ');
  final bucket = genreBucket(normFull);
  if (bucket != 'Unknown' && bucket != 'Other') return (bucket, bucket);

  final segments = [
    for (final s in raw.split(_segmentSep))
      if (s.trim().isNotEmpty) s.trim(),
  ];
  for (final seg in segments) {
    final alias = _genreAliases[canonGenre(seg)];
    if (alias != null) return alias;
  }
  var canon = canonGenre(segments.isNotEmpty ? segments.first : '');
  if (canon.isEmpty) canon = canonGenre(raw);
  if (canon.isEmpty) return ('', '');
  return _genreAliases[canon] ?? (canon, pyTitle(raw));
}

/// `_genre_color`: the family's palette colour, else a stable hash (32-bit
/// FNV-1a over the key's code points) into the cluster palette.
Color genreColor(String? genre) {
  final (key, _) = genreGroup(genre);
  if (key.isEmpty) return clusterNeutral;
  final known = genrePalette[key];
  if (known != null) return known;
  var h = 2166136261;
  for (final ch in key.runes) {
    h = ((h ^ ch) * 16777619) & 0xFFFFFFFF;
  }
  return _clusterPalette[h % _clusterPalette.length];
}
