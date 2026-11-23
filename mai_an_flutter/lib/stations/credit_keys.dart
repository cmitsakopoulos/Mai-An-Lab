import '../core/py_text.dart';

/// Artist-credit keys: `metadata_enrich.split_artist_credits`, `credit_keys`
/// and the name folding under them.

/// `_fold`: lower-case, NFKD, combining marks dropped.
String foldName(String s) => nfkdStrip(pyLower(s));

/// `_norm_name`: folded, alphanumerics only.
String normName(String s) => alnumOnly(foldName(s));

const _separators = [
  '/', ',', '&', ' x ', ' X ', ' vs ', ' vs. ', ' feat ', ' feat. ', //
  ' ft ', ' ft. ', ' featuring ', ' with ', ' + ', ' · ',
];

const _stripChars = ' \t-–—';

String _strip(String s, String chars) {
  var a = 0, b = s.length;
  while (a < b && chars.contains(s[a])) {
    a++;
  }
  while (b > a && chars.contains(s[b - 1])) {
    b--;
  }
  return s.substring(a, b);
}

/// The member names of a multi-artist credit, or [] for a single artist.
List<String> splitArtistCredits(String? name) {
  final raw = (name ?? '').trim();
  if (raw.isEmpty) return const [];
  var parts = [raw];
  for (final sep in _separators) {
    parts = [for (final p in parts) ...p.split(sep)];
  }
  final members = <String>[];
  final seen = <String>{};
  for (var p in parts) {
    p = _strip(p, _stripChars);
    // Python's len() counts code points.
    if (p.runes.length < 2) continue;
    final key = normName(p);
    if (key.isEmpty || !seen.add(key)) continue;
    members.add(p);
  }
  return members.length > 1 ? members : const [];
}

/// `{whole string}` for a solo artist, `{member, member, …}` for a collab.
Set<String> creditKeys(String? name) {
  final members = splitArtistCredits(name);
  if (members.isEmpty) {
    final k = normName(name ?? '');
    return k.isEmpty ? const {} : {k};
  }
  return {
    for (final m in members)
      if (normName(m).isNotEmpty) normName(m),
  };
}

/// `track_graph._credit_key_set`: [creditKeys], memoised per credit string.
class CreditKeyCache {
  final _cache = <String, Set<String>>{};
  Set<String> call(String? name) {
    if (name == null || name.isEmpty) return const {};
    return _cache.putIfAbsent(name, () => creditKeys(name));
  }
}
