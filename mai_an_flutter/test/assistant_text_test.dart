import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/assistant/agent_text.dart';
import 'package:mai_an_lab/assistant/chat_memory.dart';
import 'package:mai_an_lab/assistant/intent.dart';
import 'package:mai_an_lab/assistant/web_text.dart';
import 'package:mai_an_lab/core/py_re.dart';
import 'package:mai_an_lab/core/py_text.dart';

/// Phase 5 stage a against Python's own outputs
/// (`tool/export_assistant_goldens.py`).
void main() {
  final golden =
      jsonDecode(File('test/goldens/assistant_text.json').readAsStringSync())
          as Map<String, Object?>;
  List<Object?> section(String k) => golden[k]! as List<Object?>;

  // Python reports code point offsets; Dart's are UTF-16.
  int cp(String s, int i) => s.substring(0, i).runes.length;

  test('py_re matches what Python re matches', () {
    var n = 0;
    for (final c in section('re').cast<Map<String, Object?>>()) {
      final flags = c['flags']! as String;
      final rx = PyRe(
        c['pattern']! as String,
        ignoreCase: flags.contains('I'),
        multiline: flags.contains('M'),
      );
      final s = c['subject']! as String;
      final why = '${c['pattern']} on ${jsonEncode(s)}';
      final m = rx.match(s);
      expect(
        m == null ? null : [cp(s, m.start), cp(s, m.end)],
        c['match'],
        reason: 'match: $why',
      );
      final se = rx.search(s);
      expect(
        se == null ? null : [cp(s, se.start), cp(s, se.end)],
        c['search'],
        reason: 'search: $why',
      );
      expect(rx.findall(s), c['findall'], reason: 'findall: $why');
      expect(rx.sub(s, '_'), c['sub'], reason: 'sub: $why');
      n++;
    }
    expect(n, greaterThan(60));
  });

  test('py_re refuses syntax it does not convert', () {
    expect(() => PyRe(r'\d'), throwsUnsupportedError);
    expect(() => PyRe(r'(?i)x'), throwsUnsupportedError);
    expect(() => PyRe('é', ignoreCase: true), throwsUnsupportedError);
    expect(() => PyRe('[a-z]', ignoreCase: true), throwsUnsupportedError);
    expect(() => PyRe(r'(x)\1'), throwsUnsupportedError);
  });

  test('parse gives Python\'s intent for every utterance', () {
    final cases = section('intents').cast<Map<String, Object?>>();
    var mismatches = <String>[];
    for (final c in cases) {
      final raw = c['raw']! as String;
      final i = parse(raw);
      final got = {
        'name': i.name,
        'query': i.query,
        'raw': i.raw,
        'extras': i.extras,
      };
      if (jsonEncode(got) != jsonEncode(c)) {
        mismatches.add(
          '${jsonEncode(raw)}: got ${jsonEncode(got)}, '
          'Python ${jsonEncode(c)}',
        );
      }
    }
    expect(mismatches, isEmpty, reason: mismatches.take(20).join('\n'));
    expect(cases.length, greaterThan(4000));
  });

  test('cleanQuery', () {
    for (final c in section('clean_query').cast<List<Object?>>()) {
      expect(cleanQuery(c[0]! as String), c[1], reason: jsonEncode(c[0]));
    }
  });

  test('stripMarkdown', () {
    for (final c in section('strip_markdown').cast<List<Object?>>()) {
      expect(stripMarkdown(c[0]! as String), c[1], reason: jsonEncode(c[0]));
    }
    expect(stripMarkdown(null), '');
  });

  test('slimTrack', () {
    for (final c in section('slim_track').cast<List<Object?>>()) {
      final row = Map<String, Object?>.of(c[0]! as Map<String, Object?>);
      for (final k in (c[1]! as List<Object?>).cast<String>()) {
        row[k] = Uint8List.fromList([0, 1]);
      }
      final got = slimTrack(row);
      expect(jsonEncode(got), jsonEncode(c[2]));
    }
  });

  test('namesMentioned', () {
    for (final c in section('names_mentioned').cast<List<Object?>>()) {
      expect(
        namesMentioned(
          c[0]! as String,
          (c[1]! as List<Object?>).cast<String>(),
          exclude: (c[2]! as List<Object?>).cast<String>(),
        ),
        c[3],
        reason: jsonEncode(c[0]),
      );
    }
  });

  for (final (name, key, fn) in [
    ('webFold equals Python _fold', 'fold_hashes', webFold),
    ('pyLower equals str.lower', 'lower_hashes', pyLower),
    ('pyCasefold equals str.casefold', 'casefold_hashes', pyCasefold),
  ]) {
    test('$name on every code point', () {
      final want = section(key).cast<int>();
      final got = <int>[];
      var h = 0x811C9DC5;
      for (var o = 0; o <= 0x10FFFF; o++) {
        if (o < 0xD800 || o > 0xDFFF) {
          for (final b in utf8.encode(fn(String.fromCharCode(o)))) {
            h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF;
          }
          h = ((h ^ 0) * 0x01000193) & 0xFFFFFFFF;
        }
        if (o % 4096 == 4095) {
          got.add(h);
          h = 0x811C9DC5;
        }
      }
      final bad = [
        for (var i = 0; i < want.length; i++)
          if (got[i] != want[i]) 'U+${(i * 4096).toRadixString(16)}',
      ];
      expect(bad, isEmpty, reason: 'blocks of 4096 from: $bad');
    });
  }

  group('ChatMemory', () {
    final g = golden['chat_memory']! as Map<String, Object?>;
    final clock = (g['clock']! as num).toDouble();
    late Directory dir;
    setUp(() => dir = Directory.systemTemp.createTempSync('chat_memory'));
    tearDown(() => dir.deleteSync(recursive: true));

    test('writes the files byte for byte as Python does', () {
      final mem = ChatMemory(dir.path, clock: () => clock);
      final messages = (g['messages']! as List<Object?>)
          .cast<Map<String, Object?>>();
      mem.saveSession(messages, true);
      mem.saveGraphState(1216, 0);
      expect(File(mem.historyPath).readAsStringSync(), g['history_file']);
      expect(
        File(mem.graphStatePath).readAsStringSync(),
        g['graph_state_file'],
      );
    });

    test('loads, keeps 50, and expires after 15 minutes as Python does', () {
      File(
        '${dir.path}/chat_history.json',
      ).writeAsStringSync(g['history_file']! as String);
      final loads = g['loads']! as Map<String, Object?>;
      for (final (label, t) in [
        ('fresh', clock + 900),
        ('expired', clock + 900.5),
      ]) {
        final want = loads[label]! as Map<String, Object?>;
        final s = ChatMemory(dir.path, clock: () => t).loadSession();
        expect(s.messages.length, want['messages'], reason: label);
        expect(s.initGreeted, want['init_greeted'], reason: label);
        expect(
          File('${dir.path}/chat_history.json').existsSync(),
          want['file_left'],
          reason: label,
        );
      }
    });

    test('touch, clear, a missing or broken file, graph state defaults', () {
      var now = 1000.0;
      final mem = ChatMemory(dir.path, clock: () => now);
      expect(mem.loadSession().messages, isEmpty);
      mem.touchSession(); // nothing to keep alive: no file
      expect(File(mem.historyPath).existsSync(), isFalse);
      mem.saveSession([
        {'sender': 'user', 'text': 'hi'},
      ], false);
      now += 800;
      mem.touchSession();
      now += 800; // 1600 s after the save, 800 after the touch
      expect(mem.loadSession().messages, hasLength(1));
      mem.clearSession();
      expect(File(mem.historyPath).existsSync(), isFalse);
      File(mem.historyPath).writeAsStringSync('{not json');
      expect(mem.loadSession().messages, isEmpty);
      File(mem.historyPath).writeAsStringSync('[1, 2]');
      expect(mem.loadSession().messages, isEmpty);
      expect(mem.loadGraphState(), {'total_tracks': -1, 'missing_count': -1});
      mem.saveGraphState(3, 1);
      expect(mem.loadGraphState()['total_tracks'], 3);
    });

    test('a message that cannot be serialised leaves the file as it was', () {
      final mem = ChatMemory(dir.path, clock: () => 5.0);
      mem.saveSession([
        {'sender': 'user', 'text': 'kept'},
      ], false);
      final before = File(mem.historyPath).readAsStringSync();
      mem.saveSession([
        {'sender': 'user', 'text': Object()},
      ], false);
      expect(File(mem.historyPath).readAsStringSync(), before);
    });
  });

  test('Intent.copy keeps a separate extras map', () {
    final a = Intent(
      intentPlayNow,
      query: 'x',
      raw: 'play x',
      extras: {'k': 1},
    );
    final b = a.copy()..extras['k'] = 2;
    expect(a.extras['k'], 1);
    expect(b.query, 'x');
  });
}
