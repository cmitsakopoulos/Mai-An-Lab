import 'dart:convert' show utf8;

import 'nfkd_fold_table.dart';
import 'py_unicode_tables.dart';

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
/// lower-cased after a cased one. The lower-casing sees the whole string, so
/// Σ at the end of a word becomes ς ('ΟΔΟΣ' → 'Οδος').
String pyTitle(String s) {
  final out = StringBuffer();
  final src = s.runes.toList();
  var prevCased = false;
  for (var i = 0; i < src.length; i++) {
    final r = src[i];
    final c = String.fromCharCode(r);
    final isCased = _cased.hasMatch(c);
    if (!isCased) {
      out.write(c);
    } else if (!prevCased) {
      out.write(c.toUpperCase());
    } else if (r == 0x3a3) {
      out.writeCharCode(_finalSigma(src, i) ? 0x3c2 : 0x3c3);
    } else {
      out.write(c.toLowerCase());
    }
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

int _decimalValue(int rune) {
  if (rune >= 0x30 && rune <= 0x39) return rune - 0x30;
  if (rune < 0x660) return -1;
  for (final z in kDecimalZeros) {
    if (rune >= z && rune <= z + 9) return rune - z;
  }
  return -1;
}

/// `str.isdecimal()`: non-empty, every character a decimal digit (Nd), in
/// any script.
bool pyIsDecimal(String s) =>
    s.isNotEmpty && s.runes.every((r) => _decimalValue(r) >= 0);

/// `int(s)` for a string [pyIsDecimal] accepted.
int pyDecimalInt(String s) {
  var v = 0;
  for (final r in s.runes) {
    v = v * 10 + _decimalValue(r);
  }
  return v;
}

/// `str.casefold()`: lower case, plus Unicode full case folding where it
/// differs (ß is ss, final ς is σ).
String pyCasefold(String s) {
  final out = StringBuffer();
  for (final r in s.runes) {
    final m = kCasefoldExtra[r];
    if (m != null) {
      out.write(m);
    } else {
      out.write(String.fromCharCode(r).toLowerCase());
    }
  }
  return out.toString();
}

/// `str.isspace()` for one code point.
bool pyIsSpace(int r) =>
    (r >= 0x09 && r <= 0x0d) ||
    (r >= 0x1c && r <= 0x20) ||
    r == 0x85 ||
    r == 0xa0 ||
    r == 0x1680 ||
    (r >= 0x2000 && r <= 0x200a) ||
    r == 0x2028 ||
    r == 0x2029 ||
    r == 0x202f ||
    r == 0x205f ||
    r == 0x3000;

/// `str.strip()`. Dart's trim also strips U+FEFF and keeps U+001C–U+001F.
String pyStrip(String s) {
  final r = s.runes.toList();
  var a = 0, b = r.length;
  while (a < b && pyIsSpace(r[a])) {
    a++;
  }
  while (b > a && pyIsSpace(r[b - 1])) {
    b--;
  }
  return String.fromCharCodes(r, a, b);
}

const _quoteAlwaysSafe =
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~';

/// `urllib.parse.quote(s, safe)`: UTF-8, every byte outside the always-safe
/// set and [safe] as `%XX` (upper-case hex). Dart's `Uri.encodeComponent`
/// leaves `!'()*` alone and has no `safe`.
String pyQuote(String s, {String safe = '/'}) {
  final out = StringBuffer();
  for (final b in utf8.encode(s)) {
    final c = String.fromCharCode(b);
    if (b < 0x80 && (_quoteAlwaysSafe.contains(c) || safe.contains(c))) {
      out.write(c);
    } else {
      out
        ..write('%')
        ..write(b.toRadixString(16).toUpperCase().padLeft(2, '0'));
    }
  }
  return out.toString();
}
