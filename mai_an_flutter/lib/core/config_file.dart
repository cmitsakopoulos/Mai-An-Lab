import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart' show rootBundle;
import 'package:toml/toml.dart';

import 'app_config.dart';
import 'py_num.dart';

/// config.toml's writer: utils/streamrip_api.py `ensure_config_exists` (the
/// part that creates the file) and `update_config_params`, which parse the
/// file with tomlkit, set `cfg[section][key] = value` for each param and dump
/// it again. tomlkit keeps everything it was not asked to change (comments,
/// spacing, key order), so this edits the text in place the same way, byte
/// for byte (tomlkit 0.15.2, the APK's; test/config_file_test.dart, goldens
/// from tool/export_config_goldens.py).
///
/// Values: bool, int, double, String, List of those, and one level of Map
/// (`[dsp] custom_presets`, kept in whatever form the file has it: an inline
/// table, a `[dsp.custom_presets]` table, or a new table when absent).
class ConfigStore {
  ConfigStore(this.dataDir, {Future<String> Function()? template})
    : _template = template ?? _bundledTemplate;

  final String dataDir;
  final Future<String> Function() _template;

  static Future<String> _bundledTemplate() =>
      rootBundle.loadString('assets/config_template.toml');

  String get path => AppConfig.pathFor(dataDir);

  /// `ensure_config_exists`, the creating half: a missing file becomes the
  /// template (its `[downloads] folder` is already empty). The repair half
  /// (fill_missing from a newer template) has nothing to do until the
  /// template changes; see PORT_MAP §7.
  Future<void> ensureExists() async {
    final file = File(path);
    if (await file.exists()) return;
    await file.parent.create(recursive: true);
    await file.writeAsString(await _template());
  }

  /// The file as Python's text mode reads it (universal newlines).
  Future<String> read() async {
    await ensureExists();
    return normalizeNewlines(await File(path).readAsString());
  }

  /// `update_config_params`: false (and the file untouched) when it does not
  /// parse, as tomlkit's parse error is caught and logged there.
  Future<bool> update(Map<String, Map<String, Object?>> params) async {
    try {
      final text = await read();
      final out = updateConfigText(text, params);
      await _write(out);
      return true;
    } catch (e) {
      debugPrint('ConfigStore: failed to update config: $e');
      return false;
    }
  }

  /// Settings → Advanced's SAVE CONFIG FILE: the editor's text, as is.
  Future<void> writeRaw(String text) => _write(text);

  Future<void> _write(String text) async {
    final file = File(path);
    await file.parent.create(recursive: true);
    final tmp = File('$path.tmp');
    await tmp.writeAsString(text, flush: true);
    await tmp.rename(path);
  }
}

String normalizeNewlines(String s) =>
    s.replaceAll('\r\n', '\n').replaceAll('\r', '\n');

/// `update_config_params` on the file's text. Throws [TomlException] (or
/// [FormatException]) when [text] is not valid TOML, before changing anything.
String updateConfigText(String text, Map<String, Map<String, Object?>> params) {
  var s = normalizeNewlines(text);
  TomlDocument.parse(s);
  for (final MapEntry(key: section, value: values) in params.entries) {
    // `if section not in cfg: cfg[section] = {}`. A table that holds only
    // tables renders without its own header, so one created for a map
    // value alone never shows `[section]`.
    final first = values.values.firstOrNull;
    if (first is! Map) s = _ensureTable(s, section);
    for (final MapEntry(:key, :value) in values.entries) {
      s = value is Map
          ? _setTable(s, section, key, _stringMap(value))
          : _setValue(s, section, key, value);
    }
  }
  return s;
}

Map<String, Object?> _stringMap(Map<Object?, Object?> m) => {
  for (final e in m.entries) '${e.key}': e.value,
};

// ---------------------------------------------------------------------------
// Values, as tomlkit renders new items.

String tomlValue(Object? v) => switch (v) {
  bool b => b ? 'true' : 'false',
  int i => '$i',
  double d => pyFloatRepr(d),
  String s => '"${_escape(s)}"',
  List<Object?> l => '[${l.map(tomlValue).join(', ')}]',
  Map<Object?, Object?> m =>
    '{${[for (final e in m.entries) '${tomlKey('${e.key}')} = ${tomlValue(e.value)}'].join(', ')}}',
  _ => throw ArgumentError('config.toml cannot hold $v'),
};

final _bare = RegExp(r'^[A-Za-z0-9_-]+$');

String tomlKey(String k) => _bare.hasMatch(k) ? k : '"${_escape(k)}"';

/// tomlkit's `escape_string`: the short escapes, `\uXXXX` (lower-case hex)
/// for other control characters and DEL; everything else as is.
String _escape(String s) {
  final b = StringBuffer();
  for (final r in s.runes) {
    switch (r) {
      case 0x22:
        b.write(r'\"');
      case 0x5C:
        b.write(r'\\');
      case 0x08:
        b.write(r'\b');
      case 0x09:
        b.write(r'\t');
      case 0x0A:
        b.write(r'\n');
      case 0x0C:
        b.write(r'\f');
      case 0x0D:
        b.write(r'\r');
      default:
        if (r < 0x20 || r == 0x7F) {
          b.write('\\u${r.toRadixString(16).padLeft(4, '0')}');
        } else {
          b.writeCharCode(r);
        }
    }
  }
  return b.toString();
}

// ---------------------------------------------------------------------------
// The document as tomlkit sees it, one item per logical line.

enum _K { blank, comment, kv, header }

class _Item {
  _Item(
    this.kind,
    this.start,
    this.end, {
    this.path = const [],
    this.valueStart = 0,
    this.valueEnd = 0,
    this.array = false,
  });

  final _K kind;

  /// [start, end): the whole item, its newline included.
  final int start, end;

  /// A header's table name or a key-value's key, unquoted.
  final List<String> path;

  /// The value's text, for a key-value.
  final int valueStart, valueEnd;

  /// `[[array of tables]]`.
  final bool array;

  bool get isItem => kind == _K.kv || kind == _K.comment;
}

bool _isWs(String c) => c == ' ' || c == '\t';

int _skipWs(String s, int i) {
  while (i < s.length && _isWs(s[i])) {
    i++;
  }
  return i;
}

int _lineEnd(String s, int i) {
  final n = s.indexOf('\n', i);
  return n < 0 ? s.length : n + 1;
}

List<_Item> _lex(String s) {
  final items = <_Item>[];
  var i = 0;
  while (i < s.length) {
    final start = i;
    i = _skipWs(s, i);
    if (i >= s.length || s[i] == '\n') {
      i = i < s.length ? i + 1 : i;
      items.add(_Item(_K.blank, start, i));
    } else if (s[i] == '#') {
      i = _lineEnd(s, i);
      items.add(_Item(_K.comment, start, i));
    } else if (s[i] == '[') {
      final array = s.startsWith('[[', i);
      final (path, j) = _parseKey(s, i + (array ? 2 : 1));
      i = _lineEnd(s, j);
      items.add(_Item(_K.header, start, i, path: path, array: array));
    } else {
      final (path, j) = _parseKey(s, i);
      var k = _skipWs(s, j);
      if (k < s.length && s[k] == '=') k++;
      final vs = _skipWs(s, k);
      final ve = _scanValue(s, vs);
      i = _lineEnd(s, ve);
      items.add(
        _Item(_K.kv, start, i, path: path, valueStart: vs, valueEnd: ve),
      );
    }
  }
  return items;
}

/// A dotted key from [i] (leading whitespace allowed): its parts, unquoted,
/// and the index right after the last part.
(List<String>, int) _parseKey(String s, int i) {
  final parts = <String>[];
  while (true) {
    i = _skipWs(s, i);
    if (i >= s.length) break;
    if (s[i] == '"') {
      final end = _scanValue(s, i);
      parts.add(_unescape(s.substring(i + 1, end - 1)));
      i = end;
    } else if (s[i] == "'") {
      final end = _scanValue(s, i);
      parts.add(s.substring(i + 1, end - 1));
      i = end;
    } else {
      final st = i;
      while (i < s.length && _bare.hasMatch(s[i])) {
        i++;
      }
      parts.add(s.substring(st, i));
    }
    final after = _skipWs(s, i);
    if (after < s.length && s[after] == '.') {
      i = after + 1;
      continue;
    }
    break;
  }
  return (parts, i);
}

String _unescape(String s) =>
    s.replaceAllMapped(RegExp(r'\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)'), (m) {
      final e = m[1]!;
      if (e.length > 1) {
        return String.fromCharCode(int.parse(e.substring(1), radix: 16));
      }
      return switch (e) {
        'b' => '\b',
        't' => '\t',
        'n' => '\n',
        'f' => '\f',
        'r' => '\r',
        'e' => '\x1b',
        _ => e,
      };
    });

/// The end of the value starting at [i].
int _scanValue(String s, int i) {
  if (i >= s.length) return i;
  int closeTriple(String q, int from) {
    var j = from;
    while (j < s.length) {
      if (q == '"' && s[j] == r'\') {
        j += 2;
      } else if (s.startsWith('$q$q$q', j)) {
        var end = j + 3;
        // Up to two quotes may sit right before the closing three.
        while (end < s.length && s[end] == q && end - j - 3 < 2) {
          end++;
        }
        return end;
      } else {
        j++;
      }
    }
    return s.length;
  }

  if (s.startsWith('"""', i)) return closeTriple('"', i + 3);
  if (s.startsWith("'''", i)) return closeTriple("'", i + 3);
  final c = s[i];
  if (c == '"') {
    var j = i + 1;
    while (j < s.length && s[j] != '"' && s[j] != '\n') {
      j += s[j] == r'\' ? 2 : 1;
    }
    return j < s.length ? j + 1 : s.length;
  }
  if (c == "'") {
    final j = s.indexOf("'", i + 1);
    return j < 0 ? s.length : j + 1;
  }
  if (c == '[' || c == '{') {
    var depth = 0;
    var j = i;
    while (j < s.length) {
      final ch = s[j];
      if (ch == '[' || ch == '{') {
        depth++;
        j++;
      } else if (ch == ']' || ch == '}') {
        depth--;
        j++;
        if (depth == 0) return j;
      } else if (ch == '"' || ch == "'") {
        j = _scanValue(s, j);
      } else if (ch == '#') {
        final n = s.indexOf('\n', j);
        j = n < 0 ? s.length : n;
      } else {
        j++;
      }
    }
    return s.length;
  }
  var j = i;
  while (j < s.length && !' \t\n#,]}'.contains(s[j])) {
    j++;
  }
  return j;
}

bool _samePath(List<String> a, List<String> b) =>
    a.length == b.length && listEquals(a, b);

// ---------------------------------------------------------------------------
// Edits.

class _Table {
  _Table(this.header, this.body, this.bodyEnd);

  /// Null for a table that only exists through its sub-tables.
  final _Item? header;
  final List<_Item> body;

  /// Where the next header starts (or the end of the file).
  final int bodyEnd;
}

_Table? _findTable(List<_Item> items, int textLength, List<String> path) {
  final h = items.indexWhere(
    (it) => it.kind == _K.header && !it.array && _samePath(it.path, path),
  );
  if (h < 0) return null;
  final next = items.indexWhere((it) => it.kind == _K.header, h + 1);
  final body = items.sublist(h + 1, next < 0 ? items.length : next);
  return _Table(items[h], body, next < 0 ? textLength : items[next].start);
}

/// The first header of a sub-table of [section], if any.
_Item? _firstChild(List<_Item> items, String section) {
  for (final it in items) {
    if (it.kind == _K.header &&
        it.path.length > 1 &&
        it.path.first == section) {
      return it;
    }
  }
  return null;
}

/// Text to put before a new table appended to [s].
String _tablePrefix(String s) {
  if (s.isEmpty) return '';
  if (!s.endsWith('\n')) return '\n';
  return _endsWithBlank(s) ? '' : '\n';
}

bool _endsWithBlank(String s) {
  if (!s.endsWith('\n')) return false;
  final prev = s.lastIndexOf('\n', s.length - 2);
  return s.substring(prev + 1, s.length - 1).trim().isEmpty;
}

String _ensureTable(String s, String section) {
  final items = _lex(s);
  if (_findTable(items, s.length, [section]) != null) return s;
  if (_firstChild(items, section) != null) return s;
  return '$s${_tablePrefix(s)}[${tomlKey(section)}]\n';
}

String _kvLine(String key, Object? value) =>
    '${tomlKey(key)} = ${tomlValue(value)}\n';

String _setValue(String s, String section, String key, Object? value) {
  final items = _lex(s);
  final table = _findTable(items, s.length, [section]);
  if (table == null) {
    // Only its sub-tables exist: the new key gives it its own header, right
    // before the first of them.
    final child = _firstChild(items, section)!;
    return s.replaceRange(
      child.start,
      child.start,
      '[${tomlKey(section)}]\n${_kvLine(key, value)}\n',
    );
  }
  for (final it in table.body) {
    if (it.kind == _K.kv && _samePath(it.path, [key])) {
      return s.replaceRange(it.valueStart, it.valueEnd, tomlValue(value));
    }
  }
  return _appendKv(s, items, table, key, value);
}

/// tomlkit's `Container.append` into a table: after the last key or comment
/// (blank lines stay after the new key), and a blank line before a
/// sub-table that would otherwise follow the new key directly.
String _appendKv(
  String s,
  List<_Item> items,
  _Table table,
  String key,
  Object? value,
) {
  final body = table.body;
  final lastAt = body.lastIndexWhere((it) => it.isItem);
  final pos = lastAt >= 0 ? body[lastAt].end : table.header!.end;
  var ins = _kvLine(key, value);
  if (pos > 0 && s[pos - 1] != '\n') ins = '\n$ins';
  final rest = body.sublist(lastAt + 1);
  final next = items.firstWhereOrNull(
    (it) => it.kind == _K.header && it.start >= table.bodyEnd,
  );
  final header = table.header!;
  if (next != null &&
      next.path.length > header.path.length &&
      listEquals(next.path.sublist(0, header.path.length), header.path) &&
      !rest.any((it) => it.kind == _K.blank)) {
    ins = '$ins\n';
  }
  return s.replaceRange(pos, pos, ins);
}

String _setTable(
  String s,
  String section,
  String key,
  Map<String, Object?> value,
) {
  final items = _lex(s);
  final parent = _findTable(items, s.length, [section]);
  if (parent != null) {
    for (final it in parent.body) {
      if (it.kind == _K.kv && _samePath(it.path, [key])) {
        final text = s.substring(it.valueStart, it.valueEnd);
        final out = text.startsWith('{')
            ? _InlineTable.parse(text).setAll(value)
            : tomlValue(value);
        return s.replaceRange(it.valueStart, it.valueEnd, out);
      }
    }
  }
  final path = [section, key];
  if (_findTable(items, s.length, path) != null) {
    return _editTable(s, path, value);
  }
  final header = '[${tomlKey(section)}.${tomlKey(key)}]\n';
  final entries = [for (final e in value.entries) _kvLine(e.key, e.value)];
  if (parent == null) {
    return '$s${_tablePrefix(s)}$header${entries.join()}';
  }
  // After the parent's own keys and the blank lines that follow them.
  final pos = parent.bodyEnd;
  final before = s.substring(0, pos);
  final prefix = before.isEmpty || _endsWithBlank(before) ? '' : '\n';
  return s.replaceRange(pos, pos, '$prefix$header${entries.join()}');
}

/// A `[section.key]` table set to [value]: keys gone from it are removed,
/// changed ones rewritten, new ones appended.
String _editTable(String s, List<String> path, Map<String, Object?> value) {
  _Table table() => _findTable(_lex(s), s.length, path)!;
  for (final it in table().body.reversed) {
    if (it.kind == _K.kv && it.path.length == 1) {
      final k = it.path.single;
      if (!value.containsKey(k)) {
        s = s.replaceRange(it.start, it.end, '');
      } else if (!_sameValue(
        s.substring(it.valueStart, it.valueEnd),
        value[k],
      )) {
        s = s.replaceRange(it.valueStart, it.valueEnd, tomlValue(value[k]));
      }
    }
  }
  for (final MapEntry(:key, value: v) in value.entries) {
    final t = table();
    final has = t.body.any(
      (it) => it.kind == _K.kv && _samePath(it.path, [key]),
    );
    if (!has) s = _appendKv(s, _lex(s), t, key, v);
  }
  return s;
}

/// Whether the TOML value [text] already equals [v]: tomlkit keeps the text
/// of an entry the caller did not reassign.
bool _sameValue(String text, Object? v) {
  try {
    return _deepEquals(TomlDocument.parse('v = $text').toMap()['v'], v);
  } catch (_) {
    return false;
  }
}

bool _deepEquals(Object? a, Object? b) {
  if (a is List && b is List) {
    if (a.length != b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (!_deepEquals(a[i], b[i])) return false;
    }
    return true;
  }
  if (a is Map && b is Map) {
    if (a.length != b.length) return false;
    for (final k in a.keys) {
      if (!b.containsKey(k) || !_deepEquals(a[k], b[k])) return false;
    }
    return true;
  }
  if (a is num && b is num) {
    // tomlkit keeps 1 and 1.0 apart; so does the comparison.
    return a.runtimeType == b.runtimeType && a == b;
  }
  return a == b;
}

// ---------------------------------------------------------------------------
// tomlkit's InlineTable: its body as the parser leaves it (whitespace runs,
// each comma on its own, key-values; a removed key becomes a Null), and
// `as_string` rendering it back. `render` is translated from tomlkit 0.15.2
// (tomlkit/items.py, InlineTable.as_string; https://github.com/python-poetry/tomlkit):
//
//   Copyright (c) 2018 Sébastien Eustace
//
//   Permission is hereby granted, free of charge, to any person obtaining a
//   copy of this software and associated documentation files (the
//   "Software"), to deal in the Software without restriction, including
//   without limitation the rights to use, copy, modify, merge, publish,
//   distribute, sublicense, and/or sell copies of the Software, and to
//   permit persons to whom the Software is furnished to do so, subject to
//   the following conditions:
//
//   The above copyright notice and this permission notice shall be included
//   in all copies or substantial portions of the Software.
//
//   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
//   OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
//   MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
//   IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
//   CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
//   TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
//   SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

sealed class _El {}

class _Ws extends _El {
  _Ws(this.s);
  final String s;
}

/// A comment inside the table, kept verbatim.
class _Raw extends _El {
  _Raw(this.s);
  final String s;
}

class _Null extends _El {}

class _Kv extends _El {
  _Kv(this.key, this.keyText, this.sep, this.value);
  final String key, keyText, sep;
  String value;
}

class _InlineTable {
  _InlineTable(this.body);

  final List<_El> body;

  static _InlineTable parse(String t) {
    final body = <_El>[];
    var i = 1; // after '{'
    var expectKey = true;
    while (i < t.length) {
      while (true) {
        final st = i;
        while (i < t.length && ' \t\n\r'.contains(t[i])) {
          i++;
        }
        if (i > st) body.add(_Ws(t.substring(st, i)));
        if (i >= t.length || t[i] != '#') break;
        final st2 = i;
        i = _lineEnd(t, i);
        body.add(_Raw(t.substring(st2, i)));
      }
      if (i >= t.length || t[i] == '}') break;
      if (expectKey) {
        final ks = i;
        final (path, ke) = _parseKey(t, i);
        var j = ke;
        while (j < t.length && ' \t='.contains(t[j])) {
          j++;
        }
        final ve = _scanValue(t, j);
        body.add(
          _Kv(
            path.join('.'),
            t.substring(ks, ke),
            t.substring(ke, j),
            t.substring(j, ve),
          ),
        );
        i = ve;
        expectKey = false;
      } else {
        body.add(_Ws(','));
        i++;
        expectKey = true;
      }
    }
    return _InlineTable(body);
  }

  /// The table reassigned to [value]: missing keys removed, changed values
  /// replaced, new keys appended (Container.append on a parsed body).
  String setAll(Map<String, Object?> value) {
    for (var i = 0; i < body.length; i++) {
      final el = body[i];
      if (el is! _Kv) continue;
      if (!value.containsKey(el.key)) {
        body[i] = _Null();
      } else if (!_sameValue(el.value, value[el.key])) {
        el.value = tomlValue(value[el.key]);
      }
    }
    for (final MapEntry(:key, value: v) in value.entries) {
      if (!body.any((el) => el is _Kv && el.key == key)) {
        body.add(_Kv(key, tomlKey(key), ' = ', tomlValue(v)));
      }
    }
    return render();
  }

  /// `InlineTable.as_string` for a parsed (not `_new`) table.
  String render() {
    var buf = '{';
    var emittedKey = false;
    var needsSeparator = false;
    var hasExplicitCommas = false;
    int? lastItemIdx;
    var lastNullIdx = -1;
    for (var i = 0; i < body.length; i++) {
      final v = body[i];
      if (v is _Kv) lastItemIdx = i;
      if (v is _Ws && v.s.contains(',')) hasExplicitCommas = true;
      if (v is _Null) lastNullIdx = i;
    }
    var pendingSeparator = false;
    var lastValueEnd = buf.length;
    for (var i = 0; i < body.length; i++) {
      final v = body[i];
      if (v is! _Kv) {
        final vs = switch (v) {
          _Ws(:final s) => s,
          _Raw(:final s) => s,
          _ => '',
        };
        if (v is _Ws && vs.contains(',')) {
          final dropped = vs.replaceFirst(',', '');
          if (!emittedKey) {
            buf += dropped;
            continue;
          }
          final hasFollowingNull = lastNullIdx > i;
          final hasFollowingKey = lastItemIdx != null && lastItemIdx > i;
          if (hasFollowingNull && !hasFollowingKey) {
            buf += dropped;
            continue;
          }
          if (pendingSeparator) {
            buf += dropped;
            continue;
          }
          pendingSeparator = true;
        }
        if (i == body.length - 1) {
          if (!hasExplicitCommas || vs.contains(',')) {
            buf = _rstrip(buf, ',');
          }
        }
        buf += vs;
        if (vs.contains(',')) needsSeparator = false;
        continue;
      }
      if (hasExplicitCommas && needsSeparator) {
        final following = buf.substring(lastValueEnd);
        final sep = following.startsWith(' ') ? ',' : ', ';
        buf = '${buf.substring(0, lastValueEnd)}$sep$following';
        needsSeparator = false;
      }
      buf += '${v.keyText}${v.sep}${v.value}';
      lastValueEnd = buf.length;
      emittedKey = true;
      pendingSeparator = false;
      if (hasExplicitCommas) needsSeparator = true;
      if (!hasExplicitCommas && lastItemIdx != null && i < lastItemIdx) {
        buf += ',';
      }
    }
    return '$buf}';
  }
}

String _rstrip(String s, String chars) {
  var end = s.length;
  while (end > 0 && chars.contains(s[end - 1])) {
    end--;
  }
  return s.substring(0, end);
}

extension<T> on Iterable<T> {
  T? firstWhereOrNull(bool Function(T) test) {
    for (final e in this) {
      if (test(e)) return e;
    }
    return null;
  }
}
