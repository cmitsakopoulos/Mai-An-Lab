import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/analysis/audio_features.dart';
import 'package:mai_an_lab/analysis/fft.dart';
import 'package:mai_an_lab/data/library_repository.dart' show kFeaturesVersion;

/// Phase 6d: the Dart port of utils/dsp.py against Python's own outputs
/// (tool/export_dsp_goldens.py → test/goldens/dsp.json). The signals come
/// from integer-only generators mirrored from the export script, so both
/// sides analyse identical PCM (checked by a checksum).

// ── Generators (tool/export_dsp_goldens.py) ────────────────────────────────

class _Lcg {
  _Lcg(int seed) : s = seed & 0x7FFFFFFF;
  int s;
  int next() => s = (s * 1103515245 + 12345) & 0x7FFFFFFF;
  int sym(int amp) => (next() % (2 * amp + 1)) - amp;
}

int _floorDiv(int a, int b) => (a / b).floor();

int _triangle(int t, int period, int amp) {
  final ph = t % period;
  final half = period ~/ 2;
  if (ph < half) return -amp + _floorDiv(2 * amp * ph, half);
  return amp - _floorDiv(2 * amp * (ph - half), period - half);
}

int _clip16(int v) => math.max(-32768, math.min(32767, v));

List<int> _gen(String kind, int n, [int seed = 1]) {
  final rng = _Lcg(seed);
  switch (kind) {
    case 'silence':
      return List.filled(n, 0);
    case 'noise':
      return [for (var i = 0; i < n; i++) rng.sym(8000)];
    case 'chord_am':
      return [
        for (var t = 0; t < n; t++)
          _clip16(
            _triangle(t, 100, 6000) +
                _triangle(t, 84, 5000) +
                _triangle(t, 67, 5000),
          ),
      ];
    case 'chord_g':
      return [
        for (var t = 0; t < n; t++)
          _clip16(
            _triangle(t, 112, 6000) +
                _triangle(t, 89, 5000) +
                _triangle(t, 75, 5000),
          ),
      ];
  }
  if (kind.startsWith('beat_')) {
    final bpm = int.parse(kind.split('_')[1]);
    final beat = kDspSampleRate * 60 ~/ bpm;
    final half = beat ~/ 2;
    final out = <int>[];
    for (var t = 0; t < n; t++) {
      var v = 0;
      final k = t % beat;
      if (k < 2000) {
        final g = 20000 * (2000 - k) ~/ 2000;
        v += (k ~/ 225) % 2 == 0 ? g : -g;
      }
      final h = t % half;
      if (h < 400) {
        v += _floorDiv(rng.sym(5000) * (400 - h), 400);
      } else {
        rng.next();
      }
      v += _triangle(t, 100, 1500) + _triangle(t, 67, 1200);
      out.add(_clip16(v));
    }
    return out;
  }
  throw ArgumentError(kind);
}

int _checksum(List<int> s) {
  var acc = 0;
  for (var i = 0; i < s.length; i++) {
    acc = (acc + (i + 1) * s[i]) % 1000000007;
  }
  return acc;
}

Float32List _toPcm(List<int> s) =>
    Float32List.fromList([for (final v in s) v / 32768.0]);

// ── Comparison ─────────────────────────────────────────────────────────────

/// float32 noise from numpy's single-precision FFT and BLAS: relative to
/// the magnitude of the value, with an absolute floor.
void _close(String what, double got, double want, {double tol = 2e-5}) {
  final err = (got - want).abs();
  final scale = math.max(1.0, want.abs());
  expect(
    err <= tol * scale,
    isTrue,
    reason: '$what: got $got, want $want (|Δ| = $err)',
  );
}

void _closeList(
  String what,
  List<double> got,
  List<Object?> want, {
  double tol = 2e-5,
}) {
  expect(got.length, want.length, reason: '$what length');
  for (var i = 0; i < got.length; i++) {
    _close('$what[$i]', got[i], (want[i]! as num).toDouble(), tol: tol);
  }
}

void expectFeatures(
  String name,
  AudioFeatures f,
  Map<String, Object?> want, {
  double tol = 2e-5,
}) {
  num w(String k) => want[k]! as num;
  _close('$name.bpm', f.bpm, w('bpm').toDouble(), tol: 1e-9);
  expect(f.keyIndex, w('key_index'), reason: '$name.key_index');
  for (final (k, v) in [
    ('energy', f.energy),
    ('brightness', f.brightness),
    ('rolloff', f.rolloff),
    ('beat_strength', f.beatStrength),
    ('spectral_flatness', f.spectralFlatness),
    ('spectral_contrast', f.spectralContrast),
  ]) {
    _close('$name.$k', v, w(k).toDouble(), tol: tol);
  }
  _closeList(
    '$name.mfcc_mean',
    f.mfccMean,
    want['mfcc_mean']! as List,
    tol: tol,
  );
  _closeList('$name.mfcc_std', f.mfccStd, want['mfcc_std']! as List, tol: tol);
  _closeList('$name.chroma', f.chroma, want['chroma']! as List, tol: tol);
  _closeList('$name.rhythm', f.rhythm, want['rhythm']! as List, tol: tol);
  expect(f.timbreBlob().length, want['timbre_len'], reason: '$name blob');
}

void main() {
  final golden =
      jsonDecode(File('test/goldens/dsp.json').readAsStringSync())
          as Map<String, Object?>;
  final tables = golden['tables']! as Map<String, Object?>;
  final signals = golden['signals']! as Map<String, Object?>;

  group('tables', () {
    test('constants match dsp.py and the DB contract', () {
      final c = tables['constants']! as Map<String, Object?>;
      expect(c['features_version'], kDspFeaturesVersion);
      expect(kDspFeaturesVersion, kFeaturesVersion);
      expect(c['n_fft'], kNFft);
      expect(c['hop'], kHop);
      expect(c['n_mels'], kNMels);
      expect(c['n_mfcc'], kNMfcc);
      expect(c['n_chroma'], kNChroma);
      expect(c['rhythm_dims'], kRhythmDims);
      expect(c['embed_dims'], kEmbedDims);
      expect(c['max_seconds'], kDspMaxSeconds);
      expect(c['sample_rate'], kDspSampleRate);
    });

    test('mel filterbank bin points', () {
      final pts = melBinPoints(kDspSampleRate, kNFft, kNMels);
      final want = (tables['mel_bin_points']! as List).cast<int>();
      expect([pts.first.$1, for (final p in pts) p.$2, pts.last.$3], want);
    });

    test('DCT rows, Hann window, rfft frequencies', () {
      final dct = dctMatrix(kNMels, kNMfcc + 1);
      (tables['dct_rows']! as Map).forEach((k, v) {
        final row = dct[int.parse(k as String)];
        final want = (v as List).cast<num>();
        for (var i = 0; i < want.length; i++) {
          expect(row[i], want[i].toDouble(), reason: 'dct[$k][$i]');
        }
      });
      final hann = hanningWindow(kNFft);
      (tables['hann_samples']! as Map).forEach((k, v) {
        expect(hann[int.parse(k as String)], (v as num).toDouble());
      });
      final freqs = rfftFreqs(kDspSampleRate);
      (tables['freq_samples']! as Map).forEach((k, v) {
        expect(freqs[int.parse(k as String)], (v as num).toDouble());
      });
    });

    test('FFT matches a direct DFT', () {
      final rng = math.Random(7);
      final x = [for (var i = 0; i < 64; i++) rng.nextDouble() * 2 - 1];
      final fft = RealFft(64);
      final re = Float64List(33), im = Float64List(33);
      fft.forward(x, re, im);
      for (var k = 0; k <= 32; k++) {
        var sr = 0.0, si = 0.0;
        for (var n = 0; n < 64; n++) {
          sr += x[n] * math.cos(-2 * math.pi * k * n / 64);
          si += x[n] * math.sin(-2 * math.pi * k * n / 64);
        }
        expect(re[k], closeTo(sr, 1e-9));
        expect(im[k], closeTo(si, 1e-9));
      }
      final ac = normalisedAutocorr(x);
      var a0 = 0.0, a5 = 0.0;
      for (var n = 0; n < 64; n++) {
        a0 += x[n] * x[n];
        if (n + 5 < 64) a5 += x[n] * x[n + 5];
      }
      expect(ac[0], closeTo(a0 / (a0 + 1e-9), 1e-9));
      expect(ac[5], closeTo(a5 / (a0 + 1e-9), 1e-9));
    });
  });

  group('extract_features_from_pcm', () {
    signals.forEach((name, raw) {
      final s = raw! as Map<String, Object?>;
      test(name, () {
        final samples = _gen(s['kind']! as String, s['samples']! as int);
        expect(_checksum(samples), s['checksum'], reason: 'generator drift');
        final f = extractFeatures(_toPcm(samples));
        expectFeatures(name, f, s['features']! as Map<String, Object?>);
      });
    });
  });

  test('timbre BLOB: v5 layout, float32 LE', () {
    final f = AudioFeatures(
      bpm: 120,
      energy: .5,
      brightness: .2,
      rolloff: .3,
      beatStrength: .4,
      spectralFlatness: .1,
      spectralContrast: .25,
      keyIndex: 3,
      mfccMean: Float32List.fromList(List.generate(20, (i) => i + 0.5)),
      mfccStd: Float32List.fromList(List.generate(20, (i) => -i - 0.25)),
      chroma: Float32List.fromList(List.generate(12, (i) => i / 12)),
      rhythm: Float32List.fromList(List.generate(16, (i) => i / 16)),
    );
    final b = ByteData.sublistView(f.timbreBlob());
    expect(b.lengthInBytes, 272);
    expect(b.getFloat32(0, Endian.little), 0.5);
    expect(b.getFloat32(20 * 4, Endian.little), -0.25);
    expect(b.getFloat32(40 * 4 + 4, Endian.little), closeTo(1 / 12, 1e-7));
    expect(b.getFloat32(52 * 4 + 8, Endian.little), 2 / 16);
  });

  test('pcmFromBytes: int16 LE / 32768', () {
    final b = ByteData(6)
      ..setInt16(0, -32768, Endian.little)
      ..setInt16(2, 16384, Endian.little)
      ..setInt16(4, 1, Endian.little);
    expect(pcmFromBytes(b.buffer.asUint8List()), [-1.0, 0.5, 1 / 32768]);
    expect(() => pcmFromBytes(Uint8List(1)), throwsFormatException);
  });
}
