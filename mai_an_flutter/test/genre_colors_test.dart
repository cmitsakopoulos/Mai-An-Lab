// library.py `_genre_group` / `_genre_color` against Python's output
// (tool/export_library_goldens.py writes the golden).
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/library/genre_colors.dart';

String _hex(int argb) =>
    '#${(argb & 0xFFFFFF).toRadixString(16).padLeft(6, '0').toUpperCase()}';

void main() {
  final golden =
      jsonDecode(File('test/goldens/library_synthetic.json').readAsStringSync())
          as Map<String, dynamic>;

  test('genre group and colour match Python for every tag', () {
    final cases = golden['genres'] as List;
    expect(cases, hasLength(greaterThan(100)));
    for (final c in cases) {
      final tag = c['tag'] as String;
      final (key, label) = genreGroup(tag);
      expect([key, label], c['group'], reason: tag);
      expect(_hex(genreColor(tag).toARGB32()), c['color'], reason: tag);
    }
  });
}
