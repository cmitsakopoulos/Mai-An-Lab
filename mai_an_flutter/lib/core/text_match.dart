import 'dart:math' as math;

/// Port of `StreamripApp/utils/text_match.py`: small, dependency-free
/// string-similarity helpers.
///
/// Edit distance between two strings, computed with a rolling row. Operates on
/// Unicode scalar values (runes) to match Python's `str` indexing even when
/// characters lie outside the BMP.
int levenshteinDistance(String s1, String s2) {
  final a = s1.runes.toList(growable: false);
  final b = s2.runes.toList(growable: false);
  if (a.length < b.length) return _levenshteinRunes(b, a);
  return _levenshteinRunes(a, b);
}

int _levenshteinRunes(List<int> s1, List<int> s2) {
  if (s2.isEmpty) return s1.length;
  var previousRow = List<int>.generate(s2.length + 1, (i) => i);
  for (var i = 0; i < s1.length; i++) {
    final c1 = s1[i];
    final currentRow = List<int>.filled(s2.length + 1, 0);
    currentRow[0] = i + 1;
    for (var j = 0; j < s2.length; j++) {
      final insertions = previousRow[j + 1] + 1;
      final deletions = currentRow[j] + 1;
      final substitutions = previousRow[j] + (c1 == s2[j] ? 0 : 1);
      currentRow[j + 1] = math.min(
        math.min(insertions, deletions),
        substitutions,
      );
    }
    previousRow = currentRow;
  }
  return previousRow.last;
}
