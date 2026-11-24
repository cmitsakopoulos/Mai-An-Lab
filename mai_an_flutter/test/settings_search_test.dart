// The Settings search against Flet's (PORT_MAP §0 rule 3): the rows
// settings.py `_render_hub_or_search` lists for each query, in order.
//
//   ~/miniconda3/bin/python tool/export_settings_goldens.py
//   flutter test test/settings_search_test.dart
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/settings/settings_search.dart';

void main() {
  final golden =
      jsonDecode(File('test/goldens/settings_search.json').readAsStringSync())
          as Map<String, dynamic>;

  test('every query lists what Flet lists', () {
    for (final q in (golden['queries'] as List).cast<Map<String, dynamic>>()) {
      final query = q['query'] as String;
      expect(
        [for (final e in searchSettings(query.trim().toLowerCase())) e.title],
        (q['titles'] as List).cast<String>(),
        reason: query,
      );
    }
  });

  test('levenshtein', () {
    expect(levenshtein('kitten', 'sitting'), 3);
    expect(levenshtein('', 'abc'), 3);
    expect(levenshtein('vibraton', 'vibration'), 1);
  });
}
