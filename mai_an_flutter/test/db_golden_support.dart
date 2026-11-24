// Shared by the database golden tests: reads test/goldens/db_synthetic.json
// (tool/export_db_goldens.py) and dumps a database the way the export does.
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:sqlite3/sqlite3.dart';

Map<String, dynamic> loadDbGolden() =>
    jsonDecode(File('test/goldens/db_synthetic.json').readAsStringSync())
        as Map<String, dynamic>;

String _hex(Uint8List b) =>
    [for (final x in b) x.toRadixString(16).padLeft(2, '0')].join();

/// The export's `_value`: blobs as hex, reals compared by value.
Object? dbValue(Object? v) => switch (v) {
  Uint8List b => {'blob': _hex(b)},
  double d => {'real': d},
  _ => v,
};

/// A golden value in the same shape as [dbValue]'s output.
Object? goldenValue(Object? v) => switch (v) {
  {'real': final String r} => {'real': double.parse(r)},
  {'blob': final String h} => {'blob': h},
  _ => v,
};

List<List<Object?>> goldenRows(List<dynamic> rows) => [
  for (final r in rows) [for (final v in r as List) goldenValue(v)],
];

List<List<Object?>> dbRows(ResultSet rs) => [
  for (final r in rs) [for (final v in r.values) dbValue(v)],
];

String _collapse(String s) => s.replaceAll(RegExp(r'\s+'), ' ').trim();

/// The export's `dump`.
Map<String, Object?> dumpDb(Database db) {
  final master = [
    for (final r in db.select(
      'SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name',
    ))
      [
        r['type'],
        r['name'],
        r['tbl_name'],
        r['sql'] == null ? null : _collapse(r['sql'] as String),
      ],
  ];
  final tables = <String, Object?>{};
  for (final t in db.select(
    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
  )) {
    final name = t['name'] as String;
    if (name.startsWith('fts_search_')) continue;
    final cols = [
      for (final c in db.select('PRAGMA table_info("$name")')) c['name'],
    ];
    final fts = name == 'fts_search';
    tables[name] = {
      'columns': ['rowid', ...cols],
      'rows': dbRows(
        db.select('SELECT ${fts ? '' : 'rowid, '}* FROM "$name" ORDER BY 1'),
      ),
    };
  }
  return {
    'user_version': db.select('PRAGMA user_version').first.values.first,
    'master': master,
    'tables': tables,
  };
}

/// The golden's result in [dumpDb]'s shape.
Map<String, Object?> goldenDump(Map<String, dynamic> g) => {
  'user_version': g['user_version'],
  'master': g['master'],
  'tables': {
    for (final MapEntry(:key, :value) in (g['tables'] as Map).entries)
      key: {
        'columns': (value as Map)['columns'],
        'rows': goldenRows(value['rows'] as List),
      },
  },
};

/// Compares table by table, so a failure names the table.
void expectSameDump(Map<String, Object?> actual, Map<String, Object?> want) {
  expect(actual['user_version'], want['user_version'], reason: 'user_version');
  final am = actual['master'] as List;
  final wm = want['master'] as List;
  final names = {
    for (final r in [...am, ...wm]) (r as List)[1],
  };
  for (final n in names) {
    expect(
      am.where((r) => (r as List)[1] == n).toList(),
      wm.where((r) => (r as List)[1] == n).toList(),
      reason: 'sqlite_master row $n',
    );
  }
  final at = actual['tables'] as Map;
  final wt = want['tables'] as Map;
  expect(at.keys.toSet(), wt.keys.toSet(), reason: 'tables');
  for (final n in wt.keys) {
    expect(at[n], wt[n], reason: 'table $n');
  }
}
