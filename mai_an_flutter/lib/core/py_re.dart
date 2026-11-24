import 'py_re_tables.dart';

/// Python's `re` (str patterns) for the patterns the port copies from Flet,
/// compiled to a Dart [RegExp] that matches the same strings.
///
/// Dart's RegExp is JavaScript's, and the two differ in ways a ported
/// pattern would notice. [PyRe] rewrites the Python pattern so they don't:
///
///  * `\s` is Python's whitespace (`str.isspace`): JavaScript's adds U+FEFF
///    and leaves out U+001C–U+001F and U+0085;
///  * `\w` and `\b` are Unicode-aware, from Python's own tables
///    (`tool/gen_py_re_tables.py`); JavaScript's are ASCII;
///  * `re.IGNORECASE` matches each ASCII letter against exactly the code
///    points Python's sre does ('i' also takes 'İ' and 'ı', 's' takes 'ſ',
///    'k' the Kelvin sign); the Dart pattern itself is case-sensitive;
///  * `.` is anything but `\n` (JavaScript also stops at `\r`, U+2028 and
///    U+2029);
///  * `$` also matches before a final `\n`, and with `re.MULTILINE` `^` and
///    `$` see only `\n` as a line break;
///  * `(?P<name>…)` is `(?<name>…)`.
///
/// Only the syntax the copied patterns use is accepted; anything else throws
/// at construction, so a new pattern can't slip through unconverted.
class PyRe {
  PyRe(this.pattern, {this.ignoreCase = false, this.multiline = false})
    : regExp = RegExp(_convert(pattern, ignoreCase, multiline), unicode: true);

  final String pattern;
  final bool ignoreCase;
  final bool multiline;
  final RegExp regExp;

  /// `pattern.match(s)`: a match starting exactly at [pos].
  PyMatch? match(String s, [int pos = 0]) {
    final m = regExp.matchAsPrefix(s, pos);
    return m == null ? null : PyMatch._(m as RegExpMatch);
  }

  /// `pattern.search(s)`: the first match anywhere.
  PyMatch? search(String s) {
    final m = regExp.firstMatch(s);
    return m == null ? null : PyMatch._(m);
  }

  /// `pattern.sub(repl, s)` with a literal replacement (no group
  /// references; use [subMapped] for those).
  String sub(String s, String repl) {
    if (repl.contains(r'\')) {
      throw ArgumentError('Group references need subMapped: $repl');
    }
    return s.replaceAll(regExp, repl);
  }

  /// `pattern.sub(fn, s)`.
  String subMapped(String s, String Function(PyMatch m) repl) =>
      s.replaceAllMapped(regExp, (m) => repl(PyMatch._(m as RegExpMatch)));

  /// `pattern.findall(s)` for a pattern with no groups (whole matches) or
  /// one group (that group's text, '' when it didn't take part).
  List<String> findall(String s) {
    final out = <String>[];
    for (final m in regExp.allMatches(s)) {
      if (m.groupCount == 0) {
        out.add(m[0]!);
      } else if (m.groupCount == 1) {
        out.add(m[1] ?? '');
      } else {
        throw UnsupportedError('findall with several groups');
      }
    }
    return out;
  }
}

class PyMatch {
  PyMatch._(this._m);
  final RegExpMatch _m;

  int get start => _m.start;
  int get end => _m.end;

  /// `m.group(0)`.
  String get text => _m[0]!;

  /// `m.group(i)`; null when the group didn't take part.
  String? group(int i) => _m[i];

  /// `m.group(name)`.
  String? named(String name) => _m.namedGroup(name);

  /// `m.groupdict()`: every named group in pattern order, null when it
  /// didn't take part.
  Map<String, String?> groupdict(List<String> names) => {
    for (final n in names) n: _m.namedGroup(n),
  };
}

// ── Pattern conversion ──────────────────────────────────────────────────────

String _cp(int o) => '\\u{${o.toRadixString(16)}}';

final String _spaceSet = [
  '${_cp(0x09)}-${_cp(0x0d)}',
  '${_cp(0x1c)}-${_cp(0x20)}',
  for (final o in const [0x85, 0xa0, 0x1680]) _cp(o),
  '${_cp(0x2000)}-${_cp(0x200a)}',
  for (final o in const [0x2028, 0x2029, 0x202f, 0x205f, 0x3000]) _cp(o),
].join();

final String _wordSet = () {
  final b = StringBuffer();
  for (var i = 0; i < kPyWordRanges.length; i += 2) {
    final lo = kPyWordRanges[i], hi = kPyWordRanges[i + 1];
    b.write(lo == hi ? _cp(lo) : '${_cp(lo)}-${_cp(hi)}');
  }
  return b.toString();
}();

final String _boundary =
    '(?:(?<=[$_wordSet])(?![$_wordSet])|(?<![$_wordSet])(?=[$_wordSet]))';

const _syntax = r'^$\.*+?()[]{}|/';

bool _isAsciiLetter(int c) =>
    (c >= 0x41 && c <= 0x5a) || (c >= 0x61 && c <= 0x7a);

bool _isCased(String c) => c.toLowerCase() != c || c.toUpperCase() != c;

String _letterSet(String c) =>
    kPyIgnoreCaseLetters[c.toLowerCase()]!.map(_cp).join();

String _convert(String p, bool ignoreCase, bool multiline) {
  Never bad(String why) => throw UnsupportedError('PyRe: $why in pattern: $p');
  final out = StringBuffer();
  var inClass = false;
  var classStart = false;
  final chars = p.runes.map(String.fromCharCode).toList();
  var i = 0;
  while (i < chars.length) {
    final c = chars[i];
    final o = c.runes.first;
    if (c == r'\') {
      if (i + 1 >= chars.length) bad('trailing backslash');
      final n = chars[i + 1];
      i += 2;
      classStart = false;
      switch (n) {
        case 's':
          out.write(inClass ? _spaceSet : '[$_spaceSet]');
        case 'S':
          if (inClass) bad(r'\S in a class');
          out.write('[^$_spaceSet]');
        case 'w':
          out.write(inClass ? _wordSet : '[$_wordSet]');
        case 'W':
          if (inClass) bad(r'\W in a class');
          out.write('[^$_wordSet]');
        case 'b':
          if (inClass) bad(r'\b in a class');
          out.write(_boundary);
        case 'n':
          out.write(r'\n');
        case 't':
          out.write(r'\t');
        default:
          final no = n.runes.first;
          if (_isAsciiLetter(no) || (no >= 0x30 && no <= 0x39)) {
            bad('escape \\$n');
          }
          if (_syntax.contains(n) || (inClass && n == '-')) {
            out.write('\\$n');
          } else {
            out.write(_cp(no));
          }
      }
      continue;
    }
    i++;
    if (inClass) {
      if (c == ']' && !classStart) {
        inClass = false;
        out.write(']');
        continue;
      }
      if (c == '^' && classStart) {
        out.write('^');
        continue; // still at the class start: a ']' next is literal
      }
      classStart = false;
      if (c == ']') {
        out.write(r'\]');
      } else if (ignoreCase && _isCased(c)) {
        if (!_isAsciiLetter(o)) bad('non-ASCII cased literal $c');
        if (i < chars.length && chars[i] == '-') bad('letter range');
        out.write(_letterSet(c));
      } else if (c == '[') {
        out.write(r'\[');
      } else {
        out.write(c);
      }
      continue;
    }
    switch (c) {
      case '[':
        inClass = true;
        classStart = true;
        out.write('[');
      case '(':
        if (i < chars.length && chars[i] == '?') {
          final rest = chars.skip(i).take(3).join();
          if (rest.startsWith('?P<')) {
            // The group name is copied as it is, never case-expanded.
            final close = chars.indexOf('>', i);
            if (close < 0) bad('unterminated group name');
            out.write('(?<${chars.sublist(i + 3, close).join()}>');
            i = close + 1;
          } else if (rest.startsWith('?:') ||
              rest.startsWith('?=') ||
              rest.startsWith('?!') ||
              rest.startsWith('?<=') ||
              rest.startsWith('?<!')) {
            out.write('(');
          } else {
            bad('group syntax (${chars.skip(i).take(4).join()}');
          }
        } else {
          out.write('(');
        }
      case '.':
        out.write(r'[^\n]');
      case '^':
        out.write(multiline ? r'(?:^|(?<=\n))' : '^');
      case r'$':
        out.write(multiline ? r'(?=\n|$)' : r'(?=\n?$)');
      default:
        if (ignoreCase && _isCased(c)) {
          if (!_isAsciiLetter(o)) bad('non-ASCII cased literal $c');
          out.write('[${_letterSet(c)}]');
        } else if (o > 0x7e) {
          out.write(_cp(o));
        } else {
          out.write(c);
        }
    }
  }
  if (inClass) bad('unterminated class');
  return out.toString();
}
