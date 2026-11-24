import 'py_num.dart';

/// Python's `json.dumps(value)` with its defaults: `", "` and `": "`
/// separators, `ensure_ascii` (every non-ASCII character as a lowercase
/// `\uXXXX`, astral ones as a surrogate pair), floats as `repr`, and keys in
/// the map's own order. Covers what the app stores: maps, lists, strings,
/// numbers, booleans and null.
///
/// [indent] is `json.dump(…, indent=n)`: one item per line, and `","`
/// between items (Python drops the space after the comma when indenting).
/// [ensureAscii] false writes non-ASCII characters as they are, escaping
/// only `"`, `\` and the control characters below U+0020.
String pyJsonDumps(Object? value, {int? indent, bool ensureAscii = true}) {
  final out = StringBuffer();
  _Writer(out, indent, ensureAscii).write(value, 0);
  return out.toString();
}

class _Writer {
  _Writer(this.out, this.indent, this.ensureAscii);
  final StringBuffer out;
  final int? indent;
  final bool ensureAscii;

  void _newline(int level) {
    out.write('\n');
    out.write(' ' * (indent! * level));
  }

  void write(Object? v, int level) {
    switch (v) {
      case null:
        out.write('null');
      case bool b:
        out.write(b ? 'true' : 'false');
      case int i:
        out.write(i);
      case double d:
        // json.dumps writes NaN and the infinities as JavaScript names.
        out.write(
          d.isNaN
              ? 'NaN'
              : d.isInfinite
              ? (d > 0 ? 'Infinity' : '-Infinity')
              : pyFloatRepr(d),
        );
      case String s:
        _string(out, s, ensureAscii);
      case Map<Object?, Object?> m:
        if (m.isEmpty) {
          out.write('{}');
          return;
        }
        out.write('{');
        var first = true;
        m.forEach((k, val) {
          if (!first) out.write(indent == null ? ', ' : ',');
          first = false;
          if (indent != null) _newline(level + 1);
          _string(out, k as String, ensureAscii);
          out.write(': ');
          write(val, level + 1);
        });
        if (indent != null) _newline(level);
        out.write('}');
      case Iterable<Object?> l:
        if (l.isEmpty) {
          out.write('[]');
          return;
        }
        out.write('[');
        var first = true;
        for (final e in l) {
          if (!first) out.write(indent == null ? ', ' : ',');
          first = false;
          if (indent != null) _newline(level + 1);
          write(e, level + 1);
        }
        if (indent != null) _newline(level);
        out.write(']');
      default:
        throw ArgumentError(
          'Object of type ${v.runtimeType} is not JSON '
          'serializable',
        );
    }
  }
}

const _short = {
  0x22: r'\"',
  0x5C: r'\\',
  0x0A: r'\n',
  0x0D: r'\r',
  0x09: r'\t',
  0x08: r'\b',
  0x0C: r'\f',
};

void _string(StringBuffer out, String s, bool ensureAscii) {
  out.write('"');
  // UTF-16 units: an astral character is already its surrogate pair, which
  // is what ensure_ascii writes.
  for (final c in s.codeUnits) {
    final short = _short[c];
    if (short != null) {
      out.write(short);
    } else if (c < 0x20 || (ensureAscii && c > 0x7E)) {
      out.write(r'\u');
      out.write(c.toRadixString(16).padLeft(4, '0'));
    } else {
      out.writeCharCode(c);
    }
  }
  out.write('"');
}
