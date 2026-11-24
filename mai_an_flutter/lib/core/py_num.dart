import 'dart:math' as math;
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

/// Python's `math.hypot(x, y)` (CPython 3.10+ `vector_norm`): scaled to
/// [0.5, 1), squared and summed without loss, then one differential
/// correction, so the result is (almost always) correctly rounded. A plain
/// `sqrt(x*x + y*y)` can be off by an ulp, which is enough to flip a
/// comparison the network declutter branches on.
double pyHypot(double x, double y) {
  x = x.abs();
  y = y.abs();
  if (x.isInfinite || y.isInfinite) return double.infinity;
  if (x.isNaN || y.isNaN) return double.nan;
  final max = x > y ? x : y;
  if (max == 0) return 0;
  final maxE = _frexpExponent(max);
  if (maxE < -1023) {
    // Subnormal: scale up first, as CPython does.
    const dblMin = 2.2250738585072014e-308;
    return dblMin * pyHypot(x / dblMin, y / dblMin);
  }
  final scale = _pow2(-maxE);
  var csum = 1.0, frac1 = 0.0, frac2 = 0.0;
  for (var v in [x, y]) {
    v *= scale;
    final (hi, lo) = _twoProduct(v, v);
    final (s, e) = _fastSum(csum, hi);
    csum = s;
    frac1 += lo;
    frac2 += e;
  }
  var h = math.sqrt(csum - 1.0 + (frac1 + frac2));
  final (hi, lo) = _twoProduct(-h, h);
  final (s, e) = _fastSum(csum, hi);
  csum = s;
  frac1 += lo;
  frac2 += e;
  final r = csum - 1.0 + (frac1 + frac2);
  h += r / (2.0 * h);
  return h / scale;
}

/// `frexp(x).exponent` for a positive finite x.
int _frexpExponent(double x) {
  final bits = (ByteData(8)..setFloat64(0, x)).getUint64(0);
  final exp = (bits >> 52) & 0x7FF;
  if (exp == 0) return -1023 - 52; // subnormal: below every scale we use
  return exp - 1022;
}

/// 2^e as an exact double, for -1022 <= e <= 1023.
double _pow2(int e) =>
    (ByteData(8)..setUint64(0, (e + 1023) << 52)).getFloat64(0);

/// a*b as hi + lo exactly (Dekker, with Veltkamp's split: what `fma` gives).
(double, double) _twoProduct(double a, double b) {
  final p = a * b;
  final (ah, al) = _split(a);
  final (bh, bl) = _split(b);
  final err = ((ah * bh - p) + ah * bl + al * bh) + al * bl;
  return (p, err);
}

(double, double) _split(double a) {
  final c = 134217729.0 * a; // 2^27 + 1
  final hi = c - (c - a);
  return (hi, a - hi);
}

/// a + b as hi + lo exactly, for |a| >= |b| (`dl_fast_sum`).
(double, double) _fastSum(double a, double b) {
  final x = a + b;
  final z = x - a;
  return (x, b - z);
}
