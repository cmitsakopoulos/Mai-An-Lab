/// Lowercase + strip diacritics, so "bjork" finds "Björk" and "αγαπη" finds
/// "Αγάπη".
///
/// The Python app searches through SQLite FTS5 with `tokenize='unicode61'`,
/// which removes diacritics by default; this keeps in-memory search in the
/// Flutter app consistent with it. Dart has no Unicode normalisation in the
/// core library, so this is a table over the Latin and Greek letters a music
/// library actually contains. Combining marks (U+0300–U+036F, i.e. NFD text
/// such as macOS file names) are dropped outright.
String fold(String s) {
  final lower = s.toLowerCase();
  final out = StringBuffer();
  for (final c in lower.runes) {
    if (c >= 0x300 && c <= 0x36F) continue; // combining diacritical marks
    final mapped = _table[c];
    if (mapped != null) {
      out.write(mapped);
    } else {
      out.writeCharCode(c);
    }
  }
  return out.toString();
}

final Map<int, String> _table = () {
  const groups = <String, String>{
    'a': 'àáâãäåāăą',
    'c': 'çćĉċč',
    'd': 'ďđ',
    'e': 'èéêëēĕėęě',
    'g': 'ĝğġģ',
    'h': 'ĥħ',
    'i': 'ìíîïĩīĭįı',
    'j': 'ĵ',
    'k': 'ķ',
    'l': 'ĺļľŀł',
    'n': 'ñńņňŉ',
    'o': 'òóôõöøōŏő',
    'r': 'ŕŗř',
    's': 'śŝşšș',
    't': 'ţťŧț',
    'u': 'ùúûüũūŭůűų',
    'w': 'ŵ',
    'y': 'ýÿŷ',
    'z': 'źżž',
    'α': 'ά',
    'ε': 'έ',
    'η': 'ή',
    'ι': 'ίϊΐ',
    'ο': 'ό',
    'υ': 'ύϋΰ',
    'ω': 'ώ',
  };
  final table = <int, String>{
    'ß'.runes.single: 'ss',
    'æ'.runes.single: 'ae',
    'œ'.runes.single: 'oe',
    'þ'.runes.single: 'th',
  };
  groups.forEach((base, variants) {
    for (final r in variants.runes) {
      table[r] = base;
    }
  });
  return table;
}();
