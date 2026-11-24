// The tinytag port against tinytag 2.3.2 itself (PORT_MAP §0 rule 3), on a
// corpus that covers every container the scanner accepts:
//
//   ~/miniconda3/bin/python tool/export_scan_goldens.py
//   flutter test test/tinytag_test.dart
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/py_num.dart';
import 'package:mai_an_lab/core/py_text.dart';
import 'package:mai_an_lab/scan/tinytag.dart';

/// What the export records for one file: the attributes tinytag set (minus
/// `filename`) and `other`, or the exception's class name.
Map<String, Object?> tagResult(String path) {
  try {
    final t = TinyTag.get(path);
    return {
      for (final e in t.fields.entries)
        if (e.key != 'filename') e.key: e.value,
      'other': t.other,
    };
  } on TinyTagException catch (e) {
    return {'error': e.runtimeType.toString()};
  }
}

void expectSameTags(
  Map<String, Object?> got,
  Map<String, dynamic> want,
  String name,
) {
  expect(got.keys.toSet(), want.keys.toSet(), reason: '$name: fields');
  for (final k in want.keys) {
    final w = want[k], g = got[k];
    if (w is num && g is num) {
      expect(g.toDouble(), w.toDouble(), reason: '$name: $k');
    } else {
      expect(g, w, reason: '$name: $k');
    }
  }
}

void main() {
  final golden =
      jsonDecode(File('test/goldens/scan_synthetic.json').readAsStringSync())
          as Map<String, dynamic>;

  group('tinytag ${golden['tinytag']}', () {
    final tags = golden['tags'] as Map<String, dynamic>;
    for (final MapEntry(:key, :value) in tags.entries) {
      test(key, () {
        expectSameTags(
          tagResult('test/goldens/tags/$key'),
          value as Map<String, dynamic>,
          key,
        );
      });
    }
  });

  test('str.isdecimal and int()', () {
    expect(pyIsDecimal('0123'), isTrue);
    expect(pyIsDecimal('١٢'), isTrue); // Arabic-Indic
    expect(pyDecimalInt('١٢'), 12);
    expect(pyIsDecimal(''), isFalse);
    expect(pyIsDecimal('1.5'), isFalse);
    expect(pyIsDecimal('²'), isFalse); // a digit, not a decimal
  });

  test('str.casefold and str.strip', () {
    expect(pyCasefold('Straße ΣΊΣΥΦΟΣ'), 'strasse σίσυφοσ');
    expect(pyStrip('\u001f a  '), 'a');
    expect(pyStrip('﻿a'), '﻿a');
  });

  test('repr(float)', () {
    expect(pyFloatRepr(14.172569444444445), '14.172569444444445');
    expect(pyFloatRepr(32.0), '32.0');
    expect(pyFloatRepr(1e16), '1e+16');
    expect(pyFloatRepr(0.00001), '1e-05');
    expect(pyFloatRepr(0.0001), '0.0001');
    expect(pyFloatRepr(1.5e-7), '1.5e-07');
  });
}
