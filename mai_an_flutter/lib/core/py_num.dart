import 'dart:typed_data';

/// Python's `round(x, 1)`: the decimal with one digit nearest to the exact
/// binary value of [x], ties to even, returned as the nearest double.
/// `(x * 10).round() / 10` differs: 61.25 is exact in binary, so Python
/// gives 61.2 where Dart's round-half-away gives 61.3; and 0.15 is stored
/// just below .15, so Python gives 0.1 where `0.15 * 10` rounds up to 1.5.
double pyRound1(double x) {
  if (!x.isFinite || x == 0) return x;
  final bits = (ByteData(8)..setFloat64(0, x)).getUint64(0);
  final negative = (bits >> 63) != 0;
  final exp = (bits >> 52) & 0x7FF;
  final frac = bits & 0xFFFFFFFFFFFFF;
  // |x| = mant * 2^e exactly.
  final mant = BigInt.from(exp == 0 ? frac : frac | (1 << 52));
  final e = (exp == 0 ? 1 : exp) - 1075;
  // 10|x| = num / den exactly.
  var num = mant * BigInt.from(10);
  var den = BigInt.one;
  if (e >= 0) {
    num <<= e;
  } else {
    den <<= -e;
  }
  var q = num ~/ den;
  final twice = (num - q * den) * BigInt.two;
  if (twice > den || (twice == den && q.isOdd)) q += BigInt.one;
  final out = q.toDouble() / 10;
  return negative ? -out : out;
}

/// Python's `repr(float)` / `str(float)`: the shortest round-trip digits,
/// like Dart, but Python switches to exponent form outside [1e-4, 1e16)
/// and writes the exponent with at least two digits.
String pyFloatRepr(double x) {
  if (x.isNaN) return 'nan';
  if (x.isInfinite) return x > 0 ? 'inf' : '-inf';
  if (x == 0) return x.isNegative ? '-0.0' : '0.0';
  final a = x.abs();
  if (a >= 1e-4 && a < 1e16) {
    // Dart only uses exponents from 1e21 or below 1e-6.
    final s = x.toString();
    return s.contains('.') ? s : '$s.0';
  }
  // Shortest round-trip mantissa from Dart's exponential form.
  var e = x.toStringAsExponential();
  for (var p = 0; p <= 17; p++) {
    final t = x.toStringAsExponential(p);
    if (double.parse(t) == x) {
      e = t;
      break;
    }
  }
  final m = RegExp(r'^(-?[0-9.]+)e([+-])([0-9]+)$').firstMatch(e)!;
  var mant = m[1]!;
  if (mant.contains('.')) mant = mant.replaceFirst(RegExp(r'\.?0+$'), '');
  return '${mant}e${m[2]}${m[3]!.padLeft(2, '0')}';
}
