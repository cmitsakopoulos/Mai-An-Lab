import '../core/py_re.dart';
import '../core/py_text.dart';

/// `web_research.py`'s text half: which library artists a web page names.

/// `_fold`: NFKD, casefold, combining marks dropped.
String webFold(String? s) => nfkdCasefoldStrip(s ?? '');

final _nonWord = PyRe(r"[^\w&']+");

/// `_COMMON_WORDS`: names that would match every page.
const commonWords = {
  'the', 'and', 'you', 'air', 'yes', 'low', 'live', 'love', 'music', //
  'band', 'rock', 'pop', 'jazz', 'soul', 'blues', 'home', 'light', 'moon',
  'sun', 'fire', 'rain', 'girls', 'boys', 'angel', 'angels', 'eden', 'ghost',
  'heart', 'hope', 'kids', 'lights', 'free', 'true', 'best', 'more', 'like',
  'artists', 'similar', 'also', 'fans', 'songs', 'album', 'albums', 'new',
  'one', 'two', 'three', 'various artists',
};

/// `names_mentioned`: which of [names] appear in [text] as whole words
/// (case- and accent-blind), in the order the text names them. Names
/// shorter than three characters, common words, and names with a comma
/// are skipped.
List<String> namesMentioned(
  String text,
  Iterable<String> names, {
  Iterable<String> exclude = const [],
}) {
  final hay = ' ${_nonWord.sub(webFold(text), ' ')} ';
  final skip = {for (final x in exclude) webFold(x)};
  final found = <String, int>{};
  for (final name in names) {
    final n = pyStrip(webFold(name));
    if (n.runes.length < 3 ||
        skip.contains(n) ||
        commonWords.contains(n) ||
        name.contains(',')) {
      continue;
    }
    final inner = pyStrip(_nonWord.sub(n, ' '));
    final needle = ' $inner ';
    final at = pyStrip(needle).isNotEmpty ? hay.indexOf(needle) : -1;
    if (at >= 0 && !found.containsKey(name)) found[name] = at;
  }
  // sorted() is stable: equal positions keep the order they were found in.
  final keys = found.keys.toList();
  final order = List.generate(keys.length, (i) => i)
    ..sort((a, b) {
      final c = found[keys[a]]!.compareTo(found[keys[b]]!);
      return c != 0 ? c : a - b;
    });
  return [for (final i in order) keys[i]];
}
