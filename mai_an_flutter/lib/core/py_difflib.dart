/// `difflib.SequenceMatcher(None, a, b).ratio()` over code points, as
/// CPython 3.13 computes it: the b2j index with the autojunk rule (in a b
/// of 200 or more elements, an element occurring more than 1% + 1 times is
/// "popular" and left out of the index), `find_longest_match` with its
/// extension over popular elements, and the recursive matching blocks.
/// Translated from Lib/difflib.py (PSF License v2, PORT_MAP §12).
double pySequenceRatio(String a, String b) {
  final ra = a.runes.toList(), rb = b.runes.toList();
  final length = ra.length + rb.length;
  if (length == 0) return 1.0;
  return 2.0 * _SequenceMatcher(ra, rb).matches() / length;
}

class _SequenceMatcher {
  _SequenceMatcher(this.a, this.b) {
    for (var i = 0; i < b.length; i++) {
      (b2j[b[i]] ??= []).add(i);
    }
    final n = b.length;
    if (n >= 200) {
      final ntest = n ~/ 100 + 1;
      b2j.removeWhere((_, idxs) => idxs.length > ntest);
    }
  }

  final List<int> a, b;
  final Map<int, List<int>> b2j = {};

  /// No isjunk, so bjunk is empty: only the first pair of extension loops
  /// can move (and they do cross popular elements).
  (int, int, int) _longest(int alo, int ahi, int blo, int bhi) {
    var besti = alo, bestj = blo, bestsize = 0;
    var j2len = <int, int>{};
    for (var i = alo; i < ahi; i++) {
      final newj2len = <int, int>{};
      for (final j in b2j[a[i]] ?? const <int>[]) {
        if (j < blo) continue;
        if (j >= bhi) break;
        final k = newj2len[j] = (j2len[j - 1] ?? 0) + 1;
        if (k > bestsize) {
          besti = i - k + 1;
          bestj = j - k + 1;
          bestsize = k;
        }
      }
      j2len = newj2len;
    }
    while (besti > alo && bestj > blo && a[besti - 1] == b[bestj - 1]) {
      besti--;
      bestj--;
      bestsize++;
    }
    while (besti + bestsize < ahi &&
        bestj + bestsize < bhi &&
        a[besti + bestsize] == b[bestj + bestsize]) {
      bestsize++;
    }
    return (besti, bestj, bestsize);
  }

  /// The sum of `get_matching_blocks()` sizes (the order and the merging
  /// of adjacent blocks don't change it).
  int matches() {
    var total = 0;
    final queue = <(int, int, int, int)>[(0, a.length, 0, b.length)];
    while (queue.isNotEmpty) {
      final (alo, ahi, blo, bhi) = queue.removeLast();
      final (i, j, k) = _longest(alo, ahi, blo, bhi);
      if (k == 0) continue;
      total += k;
      if (alo < i && blo < j) queue.add((alo, i, blo, j));
      if (i + k < ahi && j + k < bhi) queue.add((i + k, ahi, j + k, bhi));
    }
    return total;
  }
}
