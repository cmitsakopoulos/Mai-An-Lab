import 'py_num.dart';

/// Python's `json.dumps(value)` with its defaults: `", "` and `": "`
/// separators, `ensure_ascii` (every non-ASCII character as a lowercase
/// `\uXXXX`, astral ones as a surrogate pair), floats as `repr`, and keys in
/// the map's own order. Covers what the app stores: maps, lists, strings,
/// numbers, booleans and null.
String pyJsonDumps(Object? value) {
  final out = StringBuffer();
  _write(out, value);
  return out.toString();
}

void _write(StringBuffer out, Object? v) {
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
      _string(out, s);
    case Map<Object?, Object?> m:
      out.write('{');
      var first = true;
      m.forEach((k, val) {
        if (!first) out.write(', ');
        first = false;
        _string(out, k as String);
        out.write(': ');
        _write(out, val);
      });
      out.write('}');
    case Iterable<Object?> l:
      out.write('[');
      var first = true;
      for (final e in l) {
        if (!first) out.write(', ');
        first = false;
        _write(out, e);
      }
      out.write(']');
    default:
      throw ArgumentError(
        'Object of type ${v.runtimeType} is not JSON '
        'serializable',
      );
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

void _string(StringBuffer out, String s) {
  out.write('"');
  // UTF-16 units: an astral character is already its surrogate pair, which
  // is what ensure_ascii writes.
  for (final c in s.codeUnits) {
    final short = _short[c];
    if (short != null) {
      out.write(short);
    } else if (c < 0x20 || c > 0x7E) {
      out.write(r'\u');
      out.write(c.toRadixString(16).padLeft(4, '0'));
    } else {
      out.writeCharCode(c);
    }
  }
  out.write('"');
}
