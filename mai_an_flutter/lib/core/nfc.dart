import 'nfc_table.dart';

/// Python's `unicodedata.normalize("NFC", s)` (filepath_utils
/// `normalize_nfc`): canonical decomposition, canonical ordering, then
/// canonical composition, from tables generated out of Python's own
/// unicodedata (tool/gen_nfc_table.py).
String nfc(String s) {
  // Nothing below U+0300 decomposes into something NFC would change, and
  // nothing there has a combining class: the common case returns at once.
  if (s.runes.every((r) => r < 0x300)) return s;
  final d = <int>[];
  for (final r in s.runes) {
    _decompose(r, d);
  }
  _reorder(d);
  return String.fromCharCodes(_compose(d));
}

const _sBase = 0xAC00, _lBase = 0x1100, _vBase = 0x1161, _tBase = 0x11A7;
const _lCount = 19, _vCount = 21, _tCount = 28;
const _nCount = _vCount * _tCount, _sCount = _lCount * _nCount;

final Map<int, List<int>> _decomp = () {
  final m = <int, List<int>>{};
  for (var i = 0; i < kCanonicalDecomp.length;) {
    final cp = kCanonicalDecomp[i], n = kCanonicalDecomp[i + 1];
    m[cp] = kCanonicalDecomp.sublist(i + 2, i + 2 + n);
    i += 2 + n;
  }
  return m;
}();

/// (first, second) → composite, for every primary composite.
final Map<int, int> _compose2 = () {
  final excluded = kCompositionExcluded.toSet();
  final m = <int, int>{};
  _decomp.forEach((cp, parts) {
    if (parts.length == 2 && !excluded.contains(cp)) {
      m[(parts[0] << 21) | parts[1]] = cp;
    }
  });
  return m;
}();

int _ccc(int r) {
  if (r < 0x300) return 0;
  var lo = 0, hi = kCccRanges.length ~/ 3 - 1;
  while (lo <= hi) {
    final mid = (lo + hi) >> 1;
    if (r < kCccRanges[mid * 3]) {
      hi = mid - 1;
    } else if (r > kCccRanges[mid * 3 + 1]) {
      lo = mid + 1;
    } else {
      return kCccRanges[mid * 3 + 2];
    }
  }
  return 0;
}

void _decompose(int r, List<int> out) {
  final s = r - _sBase;
  if (s >= 0 && s < _sCount) {
    out
      ..add(_lBase + s ~/ _nCount)
      ..add(_vBase + (s % _nCount) ~/ _tCount);
    if (s % _tCount != 0) out.add(_tBase + s % _tCount);
    return;
  }
  final parts = _decomp[r];
  if (parts == null) {
    out.add(r);
    return;
  }
  for (final p in parts) {
    _decompose(p, out);
  }
}

/// The canonical ordering algorithm: a stable sort of each run of
/// non-starters by combining class.
void _reorder(List<int> d) {
  for (var i = 1; i < d.length; i++) {
    final c = _ccc(d[i]);
    if (c == 0) continue;
    var j = i;
    while (j > 0) {
      final p = _ccc(d[j - 1]);
      if (p == 0 || p <= c) break;
      final t = d[j];
      d[j] = d[j - 1];
      d[j - 1] = t;
      j--;
    }
  }
}

int? _pair(int a, int b) {
  final l = a - _lBase, v = b - _vBase;
  if (l >= 0 && l < _lCount && v >= 0 && v < _vCount) {
    return _sBase + (l * _vCount + v) * _tCount;
  }
  final s = a - _sBase, t = b - _tBase;
  if (s >= 0 && s < _sCount && s % _tCount == 0 && t > 0 && t < _tCount) {
    return a + t;
  }
  return _compose2[(a << 21) | b];
}

List<int> _compose(List<int> d) {
  if (d.isEmpty) return d;
  final out = <int>[];
  var starter = -1; // index in out of the last starter
  var lastCcc = -1;
  for (final r in d) {
    final c = _ccc(r);
    if (starter >= 0) {
      // Blocked when a character between has a class of 0 or >= this one's.
      final blocked = lastCcc != -1 && (lastCcc == 0 || lastCcc >= c);
      if (!blocked) {
        final comp = _pair(out[starter], r);
        if (comp != null) {
          out[starter] = comp;
          continue;
        }
      }
    }
    if (c == 0) {
      starter = out.length;
      lastCcc = -1;
    } else {
      lastCcc = c;
    }
    out.add(r);
  }
  return out;
}
