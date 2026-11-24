import 'dart:math' as math;

import '../core/py_num.dart';
import '../core/py_text.dart';

/// genre_similarity.py: the NPMI genre×genre model learned from which genre
/// tokens co-tag the same artist. Stations read it (artist_graph.dart); the
/// acoustic build writes it (`build_genre_affinity`).

/// A pair attested by fewer artists is dropped.
const kNpmiMinSupport = 2;

/// The estimate is shrunk by cab / (cab + k).
const kNpmiShrinkageK = 5.0;

/// `build_npmi_model`: `{"a|b": npmi}` with a < b by code point, npmi > 0,
/// rounded to 4 places, in Python's dict order (each pair where its first
/// artist introduced it).
Map<String, double> buildNpmiModel(
  List<Set<String>> tokenSets, {
  int minSupport = kNpmiMinSupport,
  double shrinkageK = kNpmiShrinkageK,
}) {
  final docs = [
    for (final s in tokenSets)
      if (s.isNotEmpty) s,
  ];
  final n = docs.length;
  final model = <String, double>{};
  if (n == 0) return model;
  final df = <String, int>{};
  // Keyed "a|b": tokens are alphanumeric, so "|" can't occur in them.
  final co = <String, (String, String, int)>{};
  for (final s in docs) {
    final toks = s.toList()..sort(pyStrCompare);
    for (final a in toks) {
      df[a] = (df[a] ?? 0) + 1;
    }
    for (var i = 0; i < toks.length; i++) {
      for (var j = i + 1; j < toks.length; j++) {
        final key = '${toks[i]}|${toks[j]}';
        final prev = co[key];
        co[key] = (toks[i], toks[j], (prev?.$3 ?? 0) + 1);
      }
    }
  }
  co.forEach((key, e) {
    final (a, b, cab) = e;
    if (cab < minSupport) return;
    final pab = cab / n;
    final pmi = math.log(pab / ((df[a]! / n) * (df[b]! / n)));
    final denom = -math.log(pab);
    var npmi = denom > 0 ? pmi / denom : 0.0;
    if (npmi <= 0) return;
    if (shrinkageK > 0) npmi *= cab / (cab + shrinkageK);
    if (npmi > 0) model[key] = pyRound(npmi, 4);
  });
  return model;
}
