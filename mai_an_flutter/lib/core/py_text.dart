import 'nfkd_fold_table.dart';

/// Python's string primitives that the ported code depends on and Dart lacks
/// or does differently: `str.lower` (final sigma), `str.isalnum`, `str.title`,
/// and `unicodedata.normalize("NFKD")` with combining marks dropped.

final _alnum = RegExp(r'^[\p{L}\p{N}]$', unicode: true);
final _cased = RegExp(r'^[\p{Lu}\p{Ll}\p{Lt}]$', unicode: true);

/// `str.isalnum()` for one code point: letters and numbers (L*, N*).
bool isAlnum(int rune) {
  if (rune < 0x80) {
    return (rune >= 0x30 && rune <= 0x39) ||
        (rune >= 0x41 && rune <= 0x5a) ||
        (rune >= 0x61 && rune <= 0x7a);
  }
  return _alnum.hasMatch(String.fromCharCode(rune));
}

/// Only the alphanumeric characters of [s] (`"".join(c for c in s if
/// c.isalnum())`).
String alnumOnly(String s) {
  final out = StringBuffer();
  for (final r in s.runes) {
    if (isAlnum(r)) out.writeCharCode(r);
  }
  return out.toString();
}

/// `str.lower()`. Dart's toLowerCase maps every Σ to σ; Python applies the
/// Final_Sigma rule (ς at the end of a word).
String pyLower(String s) {
  final lower = s.toLowerCase();
  if (!s.contains('Σ')) return lower;
  final src = s.runes.toList();
  final out = lower.runes.toList();
  // toLowerCase is length-preserving for the scripts that contain Σ; if it
  // ever isn't, keep Dart's result rather than misalign.
  if (src.length != out.length) return lower;
  for (var i = 0; i < src.length; i++) {
    if (src[i] != 0x3a3) continue;
    if (_finalSigma(src, i)) out[i] = 0x3c2;
  }
  return String.fromCharCodes(out);
}

/// Unicode Final_Sigma: preceded by a cased letter (skipping case-ignorable
/// characters) and not followed by one.
bool _finalSigma(List<int> s, int i) {
  bool cased(int r) => _cased.hasMatch(String.fromCharCode(r));
  bool ignorable(int r) =>
      r == 0x27 || r == 0x2e || r == 0x3a || r == 0xb7 || _isCombining(r);
  var j = i - 1;
  while (j >= 0 && ignorable(s[j])) {
    j--;
  }
  if (j < 0 || !cased(s[j])) return false;
  var k = i + 1;
  while (k < s.length && ignorable(s[k])) {
    k++;
  }
  return k >= s.length || !cased(s[k]);
}

/// `str.title()`: a cased character is upper-cased after an uncased one and
/// lower-cased after a cased one.
String pyTitle(String s) {
  final out = StringBuffer();
  var prevCased = false;
  for (final r in s.runes) {
    final c = String.fromCharCode(r);
    final isCased = _cased.hasMatch(c);
    out.write(isCased ? (prevCased ? pyLower(c) : c.toUpperCase()) : c);
    prevCased = isCased;
  }
  return out.toString();
}

bool _isCombining(int r) {
  if (r < 0x300) return false;
  var lo = 0, hi = kCombiningRanges.length ~/ 2 - 1;
  while (lo <= hi) {
    final mid = (lo + hi) >> 1;
    if (r < kCombiningRanges[2 * mid]) {
      hi = mid - 1;
    } else if (r > kCombiningRanges[2 * mid + 1]) {
      lo = mid + 1;
    } else {
      return true;
    }
  }
  return false;
}

/// `"".join(c for c in unicodedata.normalize("NFKD", s) if not
/// unicodedata.combining(c))`, for the Basic Multilingual Plane.
String nfkdStrip(String s) {
  final out = StringBuffer();
  for (final r in s.runes) {
    if (_isCombining(r)) continue;
    if (r >= 0xac00 && r <= 0xd7a3) {
      // Hangul syllables decompose arithmetically into conjoining jamo.
      final i = r - 0xac00;
      out
        ..writeCharCode(0x1100 + i ~/ 588)
        ..writeCharCode(0x1161 + (i % 588) ~/ 28);
      if (i % 28 != 0) out.writeCharCode(0x11a7 + i % 28);
      continue;
    }
    final m = r >= 0x80 ? kNfkdFold[r] : null;
    if (m != null) {
      out.write(m);
    } else {
      out.writeCharCode(r);
    }
  }
  return out.toString();
}
