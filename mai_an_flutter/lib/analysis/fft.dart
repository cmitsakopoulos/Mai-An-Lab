import 'dart:math' as math;
import 'dart:typed_data';

/// Radix-2 FFTs for the audio analyser (dsp.py leans on `np.fft`). Sizes
/// are powers of two; the tables are built once per size and reused for
/// every frame.

/// An in-place complex FFT of size [n].
class ComplexFft {
  ComplexFft(this.n)
    : assert(n > 0 && (n & (n - 1)) == 0, 'n must be a power of two'),
      _cos = Float64List(n ~/ 2 > 0 ? n ~/ 2 : 1),
      _sin = Float64List(n ~/ 2 > 0 ? n ~/ 2 : 1),
      _rev = Int32List(n) {
    for (var k = 0; k < n ~/ 2; k++) {
      final a = -2 * math.pi * k / n;
      _cos[k] = math.cos(a);
      _sin[k] = math.sin(a);
    }
    var bits = 0;
    while ((1 << bits) < n) {
      bits++;
    }
    for (var i = 0; i < n; i++) {
      var r = 0;
      var v = i;
      for (var b = 0; b < bits; b++) {
        r = (r << 1) | (v & 1);
        v >>= 1;
      }
      _rev[i] = r;
    }
  }

  final int n;
  final Float64List _cos;
  final Float64List _sin;
  final Int32List _rev;

  /// Forward (e^{-iωt}) transform, or the unscaled inverse when [inverse].
  void transform(Float64List re, Float64List im, {bool inverse = false}) {
    for (var i = 0; i < n; i++) {
      final j = _rev[i];
      if (j > i) {
        final tr = re[i];
        re[i] = re[j];
        re[j] = tr;
        final ti = im[i];
        im[i] = im[j];
        im[j] = ti;
      }
    }
    final sign = inverse ? -1.0 : 1.0;
    for (var size = 2; size <= n; size <<= 1) {
      final half = size >> 1;
      final step = n ~/ size;
      for (var start = 0; start < n; start += size) {
        var t = 0;
        for (var k = 0; k < half; k++) {
          final wr = _cos[t];
          final wi = sign * _sin[t];
          final a = start + k;
          final b = a + half;
          final xr = re[b] * wr - im[b] * wi;
          final xi = re[b] * wi + im[b] * wr;
          re[b] = re[a] - xr;
          im[b] = im[a] - xi;
          re[a] += xr;
          im[a] += xi;
          t += step;
        }
      }
    }
  }
}

/// `np.fft.rfft` of a real signal of size [n] (n >= 4): the n/2+1 bins of
/// the half spectrum, through one n/2-point complex FFT.
class RealFft {
  RealFft(this.n)
    : _half = ComplexFft(n ~/ 2),
      _zr = Float64List(n ~/ 2),
      _zi = Float64List(n ~/ 2),
      _wr = Float64List(n ~/ 2),
      _wi = Float64List(n ~/ 2) {
    for (var k = 0; k < n ~/ 2; k++) {
      final a = -2 * math.pi * k / n;
      _wr[k] = math.cos(a);
      _wi[k] = math.sin(a);
    }
  }

  final int n;
  final ComplexFft _half;
  final Float64List _zr;
  final Float64List _zi;
  final Float64List _wr;
  final Float64List _wi;

  int get bins => n ~/ 2 + 1;

  /// [x] holds n samples; [outRe]/[outIm] receive [bins] values.
  void forward(List<double> x, Float64List outRe, Float64List outIm) {
    final m = n ~/ 2;
    for (var k = 0; k < m; k++) {
      _zr[k] = x[2 * k];
      _zi[k] = x[2 * k + 1];
    }
    _half.transform(_zr, _zi);
    // X[k] = E[k] + W^k O[k], with E/O split out of Z = FFT(even + i·odd).
    outRe[0] = _zr[0] + _zi[0];
    outIm[0] = 0;
    outRe[m] = _zr[0] - _zi[0];
    outIm[m] = 0;
    for (var k = 1; k < m; k++) {
      final ar = _zr[k], ai = _zi[k];
      final br = _zr[m - k], bi = -_zi[m - k]; // conj(Z[m-k])
      final er = 0.5 * (ar + br), ei = 0.5 * (ai + bi);
      // O = (Z[k] - conj(Z[m-k])) / 2i
      final dr = ar - br, di = ai - bi;
      final or = 0.5 * di, oi = -0.5 * dr;
      final wr = _wr[k], wi = _wi[k];
      outRe[k] = er + wr * or - wi * oi;
      outIm[k] = ei + wr * oi + wi * or;
    }
  }
}

/// `irfft(|rfft(x, pad)|², pad)[:n] / ac[0]`, dsp.py's autocorrelation:
/// the linear autocorrelation of [x] (zero-padded to the next power of two
/// at or above 2n), normalised so ac[0] = 1 (plus dsp.py's 1e-9).
Float64List normalisedAutocorr(List<double> x) {
  final n = x.length;
  var pad = 1;
  while (pad < 2 * n) {
    pad <<= 1;
  }
  final fft = ComplexFft(pad);
  final re = Float64List(pad);
  final im = Float64List(pad);
  for (var i = 0; i < n; i++) {
    re[i] = x[i];
  }
  fft.transform(re, im);
  for (var k = 0; k < pad; k++) {
    re[k] = re[k] * re[k] + im[k] * im[k];
    im[k] = 0;
  }
  fft.transform(re, im, inverse: true);
  final out = Float64List(n);
  final a0 = re[0] / pad;
  for (var i = 0; i < n; i++) {
    out[i] = (re[i] / pad) / (a0 + 1e-9);
  }
  return out;
}
