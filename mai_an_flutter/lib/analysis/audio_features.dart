import 'dart:math' as math;
import 'dart:typed_data';

import 'fft.dart';

/// utils/dsp.py: the per-track acoustic features behind auto-play, stations
/// and the Network view, extracted from decoded mono PCM.
///
///     int16 mono PCM @ 22050 Hz → STFT → HPSS split → {
///         energy / brightness / rolloff / flatness / contrast   full mag
///         chroma + key + MFCC                                    harmonic
///         onset envelope + BPM + rhythm block                    percussive
///     }
///
/// Same maths, constants and v5 BLOB layout as Python, so features computed
/// here, on the Mac (offload) or by Flet are interchangeable. numpy keeps
/// most arrays in float32; this port rounds to float32 where numpy stores
/// (spectrogram, medians, cumulative sums) and accumulates in double, so
/// results agree to float32 precision rather than bit for bit (pinned by
/// test/goldens/dsp.json).

/// `FEATURES_VERSION`: kept equal to `kFeaturesVersion` in
/// library_repository.dart (test/audio_features_test.dart checks).
const kDspFeaturesVersion = 5;

const kNFft = 2048;
const kHop = 512;
const kNMels = 64;
const kNMfcc = 20;
const kNChroma = 12;
const _tempogramMultsFull = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0];
const _tempogramMultsBand = [0.5, 1.0, 2.0];
const kRhythmDims = 6 + 2 * 3 + 4;

/// Seconds of audio decoded per track (the Kotlin decoder's window).
const kDspMaxSeconds = 120;

/// v5 sound profile: mfcc_mean[20] + mfcc_std[20] + chroma[12] + rhythm[16].
const kEmbedDims = kNMfcc + kNMfcc + kNChroma + kRhythmDims;

/// Must match the Kotlin decoder's TARGET_SAMPLE_RATE.
const kDspSampleRate = 22050;

const _hpssKernel = 17;

/// `Features`: scalars for their own play_counts columns plus the packed
/// sound profile.
class AudioFeatures {
  const AudioFeatures({
    required this.bpm,
    required this.energy,
    required this.brightness,
    required this.rolloff,
    required this.beatStrength,
    required this.spectralFlatness,
    required this.spectralContrast,
    required this.keyIndex,
    required this.mfccMean,
    required this.mfccStd,
    required this.chroma,
    required this.rhythm,
  });

  final double bpm;
  final double energy;
  final double brightness;
  final double rolloff;
  final double beatStrength;
  final double spectralFlatness;
  final double spectralContrast;

  /// 0..11 = C..B major, 12..23 = C..B minor.
  final int keyIndex;
  final Float32List mfccMean;
  final Float32List mfccStd;
  final Float32List chroma;
  final Float32List rhythm;

  /// `timbre_blob`: the v5 sound profile as 68 float32 LE (272 bytes).
  Uint8List timbreBlob() {
    final out = Float32List(kEmbedDims)
      ..setAll(0, mfccMean)
      ..setAll(kNMfcc, mfccStd)
      ..setAll(2 * kNMfcc, chroma)
      ..setAll(2 * kNMfcc + kNChroma, rhythm);
    final bytes = ByteData(kEmbedDims * 4);
    for (var i = 0; i < kEmbedDims; i++) {
      bytes.setFloat32(i * 4, out[i], Endian.little);
    }
    return bytes.buffer.asUint8List();
  }

  Map<String, Object?> toJson() => {
    'bpm': bpm,
    'energy': energy,
    'brightness': brightness,
    'rolloff': rolloff,
    'beat_strength': beatStrength,
    'spectral_flatness': spectralFlatness,
    'spectral_contrast': spectralContrast,
    'key_index': keyIndex,
    'mfcc_mean': mfccMean.toList(),
    'mfcc_std': mfccStd.toList(),
    'chroma': chroma.toList(),
    'rhythm': rhythm.toList(),
  };
}

// ── float32 helpers ─────────────────────────────────────────────────────────

final _f32Box = Float32List(1);

/// Rounds to the nearest float32, as numpy does when it stores one.
double f32(double v) {
  _f32Box[0] = v;
  return _f32Box[0];
}

double _clip(double v, double lo, double hi) => v < lo ? lo : (v > hi ? hi : v);

/// Python's `round()` (half to even) to an int.
int _roundHalfEven(double v) {
  final r = v.roundToDouble();
  if ((r - v).abs() == 0.5) {
    final f = v.floorToDouble();
    return (f % 2 == 0 ? f : f + 1).toInt();
  }
  return r.toInt();
}

// ── PCM ─────────────────────────────────────────────────────────────────────

/// `load_pcm`: int16 LE mono PCM as float32 in [-1, 1].
Float32List pcmFromBytes(Uint8List bytes) {
  final n = bytes.length ~/ 2;
  if (n == 0) throw const FormatException('empty PCM file');
  final view = ByteData.sublistView(bytes);
  final out = Float32List(n);
  for (var i = 0; i < n; i++) {
    out[i] = view.getInt16(i * 2, Endian.little) / 32768.0;
  }
  return out;
}

// ── Spectrogram ─────────────────────────────────────────────────────────────

/// A (frames × bins) float32 matrix, frame-major like numpy's
/// `(n_frames, n_fft//2+1)`.
class Spectrogram {
  Spectrogram(this.frames, this.bins) : data = Float32List(frames * bins);
  Spectrogram._(this.frames, this.bins, this.data);

  final int frames;
  final int bins;
  final Float32List data;

  double at(int f, int b) => data[f * bins + b];
}

/// `np.hanning(N).astype(float32)`.
Float32List hanningWindow(int n) {
  final w = Float32List(n);
  for (var i = 0; i < n; i++) {
    w[i] = 0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1));
  }
  return w;
}

/// `np.fft.rfftfreq(N_FFT, 1/sr).astype(float32)`.
Float32List rfftFreqs(int sr) {
  final val = 1.0 / (kNFft * (1.0 / sr));
  final out = Float32List(kNFft ~/ 2 + 1);
  for (var k = 0; k < out.length; k++) {
    out[k] = k * val;
  }
  return out;
}

/// `_stft_magnitude`: Hann-windowed frames (no tail padding; a signal
/// shorter than one frame is zero-padded to one).
Spectrogram stftMagnitude(Float32List x) {
  var sig = x;
  if (sig.length < kNFft) {
    sig = Float32List(kNFft)..setAll(0, x);
  }
  final nFrames = 1 + (sig.length - kNFft) ~/ kHop;
  const bins = kNFft ~/ 2 + 1;
  final out = Spectrogram(nFrames, bins);
  final win = hanningWindow(kNFft);
  final fft = RealFft(kNFft);
  final buf = Float32List(kNFft);
  final re = Float64List(bins);
  final im = Float64List(bins);
  for (var f = 0; f < nFrames; f++) {
    final off = f * kHop;
    for (var i = 0; i < kNFft; i++) {
      buf[i] = sig[off + i] * win[i];
    }
    fft.forward(buf, re, im);
    final row = f * bins;
    for (var k = 0; k < bins; k++) {
      out.data[row + k] = math.sqrt(re[k] * re[k] + im[k] * im[k]);
    }
  }
  return out;
}

// ── Scalars on the full spectrum ────────────────────────────────────────────

/// `_energy_db`: RMS mapped through dBFS, -60 dB → 0, 0 dB → 1.
double energyDb(Float32List x) {
  var s = 0.0;
  for (final v in x) {
    s += v * v;
  }
  final rms = math.sqrt(s / x.length + 1e-12);
  final db = 20.0 * math.log(rms + 1e-9) / math.ln10;
  return _clip((db + 60.0) / 60.0, 0.0, 1.0);
}

/// `_brightness`: mean spectral centroid / Nyquist.
double brightness(Spectrogram m, Float32List freqs, int sr) {
  var total = 0.0;
  var count = 0;
  for (var f = 0; f < m.frames; f++) {
    final row = f * m.bins;
    var s = 0.0, w = 0.0;
    for (var b = 0; b < m.bins; b++) {
      final v = m.data[row + b];
      s += v;
      w += v * freqs[b];
    }
    if (f32(s) > 1e-6) {
      total += w / s;
      count++;
    }
  }
  if (count == 0) return 0.0;
  return _clip(total / count / (sr / 2.0), 0.0, 1.0);
}

/// `_spectral_rolloff`: mean 85 % rolloff / Nyquist. numpy's cumsum runs
/// in float32, which decides the crossing bin, so it does here too.
double spectralRolloff(
  Spectrogram m,
  Float32List freqs,
  int sr, [
  double pct = 0.85,
]) {
  final cum = Float32List(m.bins);
  final p = f32(pct);
  var total = 0.0;
  var count = 0;
  for (var f = 0; f < m.frames; f++) {
    final row = f * m.bins;
    var acc = 0.0;
    for (var b = 0; b < m.bins; b++) {
      cum[b] = acc + m.data[row + b];
      acc = cum[b];
    }
    final t = cum[m.bins - 1];
    if (!(t > 1e-6)) continue;
    final thr = f32(p * t);
    var idx = 0;
    for (var b = 0; b < m.bins; b++) {
      if (cum[b] >= thr) {
        idx = b;
        break;
      }
    }
    total += freqs[idx];
    count++;
  }
  if (count == 0) return 0.0;
  return _clip(total / count / (sr / 2.0), 0.0, 1.0);
}

/// `_spectral_flatness`: per-frame geometric / arithmetic mean, averaged.
double spectralFlatness(Spectrogram m) {
  var total = 0.0;
  for (var f = 0; f < m.frames; f++) {
    final row = f * m.bins;
    var logSum = 0.0, sum = 0.0;
    for (var b = 0; b < m.bins; b++) {
      final v = f32(m.data[row + b] + 1e-10);
      logSum += math.log(v);
      sum += v;
    }
    final geom = math.exp(logSum / m.bins);
    total += geom / (sum / m.bins);
  }
  return m.frames == 0 ? 0.0 : total / m.frames;
}

/// numpy `percentile(..., method="linear")` of an ascending [sorted].
double _percentile(Float64List sorted, int n, double q) {
  final idx = (n - 1) * (q / 100.0);
  final lo = idx.floor();
  final hi = math.min(lo + 1, n - 1);
  final t = idx - lo;
  final a = sorted[lo], b = sorted[hi];
  final d = b - a;
  return t >= 0.5 ? b - d * (1 - t) : a + d * t;
}

/// `_spectral_contrast`: mean 80th/20th-percentile dB gap over 6 bands,
/// scaled by a 40 dB ceiling.
double spectralContrast(Spectrogram m, [int nBands = 6]) {
  final nBins = m.bins;
  final edges = [
    for (var i = 0; i <= nBands; i++)
      i == nBands ? nBins : (i * (nBins / nBands)).floor(),
  ];
  var sum = 0.0;
  var count = 0;
  final buf = Float64List(nBins);
  for (var i = 0; i < nBands; i++) {
    final lo = edges[i], hi = edges[i + 1];
    if (hi <= lo + 1) continue;
    final n = hi - lo;
    for (var f = 0; f < m.frames; f++) {
      final row = f * m.bins;
      for (var b = 0; b < n; b++) {
        buf[b] = f32(m.data[row + lo + b] + 1e-10);
      }
      final view = Float64List.sublistView(buf, 0, n)..sort();
      final peak = f32(_percentile(view, n, 80));
      final valley = f32(_percentile(view, n, 20));
      sum += 20.0 * math.log(peak / valley) / math.ln10;
      count++;
    }
  }
  if (count == 0) return 0.0;
  return _clip(sum / count / 40.0, 0.0, 1.0);
}

// ── Chroma & key ────────────────────────────────────────────────────────────

/// `_chroma_mean`: L1-normalised pitch-class profile over 80 Hz–5 kHz.
Float32List chromaMean(Spectrogram m, Float32List freqs) {
  final bins = <int>[];
  final pcs = <int>[];
  for (var b = 0; b < freqs.length; b++) {
    final f = freqs[b];
    if (f >= 80.0 && f <= 5000.0) {
      bins.add(b);
      final semis = f32(12.0 * f32(math.log(f32(f / 440.0)) / math.ln2));
      final r = _roundHalfEven(semis);
      pcs.add(((r % kNChroma) + kNChroma) % kNChroma);
    }
  }
  final out = Float32List(kNChroma);
  if (bins.isEmpty) return out;
  final acc = Float64List(kNChroma);
  final bucket = Float64List(kNChroma);
  var safe = 0;
  for (var f = 0; f < m.frames; f++) {
    bucket.fillRange(0, kNChroma, 0);
    final row = f * m.bins;
    for (var i = 0; i < bins.length; i++) {
      bucket[pcs[i]] += m.data[row + bins[i]];
    }
    var s = 0.0;
    for (var c = 0; c < kNChroma; c++) {
      bucket[c] = f32(bucket[c]);
      s += bucket[c];
    }
    if (!(f32(s) > 1e-6)) continue;
    for (var c = 0; c < kNChroma; c++) {
      acc[c] += bucket[c] / s;
    }
    safe++;
  }
  if (safe == 0) return out;
  for (var c = 0; c < kNChroma; c++) {
    out[c] = acc[c] / safe;
  }
  return out;
}

/// Krumhansl–Schmuckler (1982) key profiles.
const _ksMajor = [
  6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88, //
];
const _ksMinor = [
  6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17, //
];

/// `_estimate_key`: best of 24 rotated templates; 0 (C major) for silence.
int estimateKey(Float32List chroma) {
  var total = 0.0;
  for (final v in chroma) {
    total += v;
  }
  if (total < 1e-6) return 0;
  final mean = total / kNChroma;
  final c = [for (final v in chroma) v - mean];
  final normC = math.sqrt(c.fold(0.0, (a, v) => a + v * v)) + 1e-9;
  var bestScore = double.negativeInfinity;
  var bestIdx = 0;
  for (final (mode, template) in [(0, _ksMajor), (1, _ksMinor)]) {
    final t32 = [for (final v in template) f32(v)];
    final tMean = t32.fold(0.0, (a, v) => a + v) / kNChroma;
    final t = [for (final v in t32) v - tMean];
    final normT = math.sqrt(t.fold(0.0, (a, v) => a + v * v)) + 1e-9;
    for (var root = 0; root < 12; root++) {
      var dot = 0.0;
      for (var i = 0; i < 12; i++) {
        dot += c[i] * t[(i - root + 12) % 12]; // np.roll(t, root)
      }
      final score = dot / (normC * normT);
      if (score > bestScore) {
        bestScore = score;
        bestIdx = mode * 12 + root;
      }
    }
  }
  return bestIdx;
}

// ── Onsets, tempo & rhythm ──────────────────────────────────────────────────

/// `_onset_envelope` over bins [lo, hi): spectral flux of log1p magnitude,
/// minus a boxcar local mean (np.convolve "same"), then z-scored.
Float64List onsetEnvelope(Spectrogram m, [int lo = 0, int? hi]) {
  final end = hi ?? m.bins;
  final n = m.frames - 1;
  if (n <= 0) return Float64List(0);
  var flux = Float64List(n);
  var prev = Float64List(end - lo);
  for (var b = lo; b < end; b++) {
    prev[b - lo] = f32(_log1p(m.data[b]));
  }
  var cur = Float64List(end - lo);
  for (var f = 1; f < m.frames; f++) {
    final row = f * m.bins;
    var s = 0.0;
    for (var b = lo; b < end; b++) {
      final v = f32(_log1p(m.data[row + b]));
      cur[b - lo] = v;
      final d = v - prev[b - lo];
      if (d > 0) s += d;
    }
    flux[f - 1] = f32(s);
    final t = prev;
    prev = cur;
    cur = t;
  }
  if (n > 8) {
    final win = math.max(8, n ~/ 32);
    final k = f32(1.0 / win);
    final offset = (win - 1) ~/ 2;
    final prefix = Float64List(n + 1);
    for (var i = 0; i < n; i++) {
      prefix[i + 1] = prefix[i] + flux[i];
    }
    final hp = Float64List(n);
    for (var i = 0; i < n; i++) {
      final top = math.min(i + offset, n - 1);
      final bottom = math.max(i + offset - win + 1, 0);
      final local = top >= bottom
          ? f32(k * (prefix[top + 1] - prefix[bottom]))
          : 0.0;
      final v = f32(flux[i] - local);
      hp[i] = v > 0 ? v : 0.0;
    }
    flux = hp;
  }
  var mean = 0.0;
  for (final v in flux) {
    mean += v;
  }
  mean /= n;
  var varSum = 0.0;
  for (final v in flux) {
    varSum += (v - mean) * (v - mean);
  }
  final sd = math.sqrt(varSum / n) + 1e-9;
  final out = Float64List(n);
  for (var i = 0; i < n; i++) {
    out[i] = f32((flux[i] - mean) / sd);
  }
  return out;
}

double _log1p(double x) {
  // log(1+x) without the cancellation for small x.
  final u = 1.0 + x;
  return u == 1.0 ? x : math.log(u) * x / (u - 1.0);
}

/// `_estimate_bpm`: (bpm, beat_strength) from the onset autocorrelation,
/// searched over 60–200 BPM under a log-Gaussian prior at 120.
(double, double) estimateBpm(Float64List env, int sr) {
  final n = env.length;
  if (n < 16) return (0.0, 0.0);
  final ac = normalisedAutocorr(env);
  var best = -1;
  var bestScore = double.negativeInfinity;
  for (var lag = 1; lag < n; lag++) {
    final bpm = 60.0 * sr / (lag * kHop);
    if (!(bpm >= 60.0 && bpm <= 200.0)) continue;
    final z = (math.log(bpm / 120.0) / math.ln2) / 0.9;
    final score = ac[lag] * math.exp(-0.5 * z * z);
    if (score > bestScore) {
      bestScore = score;
      best = lag;
    }
  }
  if (best < 0 || !bestScore.isFinite) return (0.0, 0.0);
  return (60.0 * sr / (best * kHop), _clip(ac[best], 0.0, 1.0));
}

/// `_autocorr` (float32, like numpy's).
Float64List _autocorr(Float64List env) {
  final n = env.length;
  if (n < 4) return Float64List(math.max(n, 1));
  final ac = normalisedAutocorr(env);
  for (var i = 0; i < n; i++) {
    ac[i] = f32(ac[i]);
  }
  return ac;
}

List<double> _sampleTempogram(
  Float64List ac,
  double beatFrames,
  List<double> mults,
) => [
  for (final m in mults)
    () {
      final lag = _roundHalfEven(beatFrames * m);
      return lag >= 1 && lag < ac.length ? _clip(ac[lag], -1.0, 1.0) : 0.0;
    }(),
];

double _onsetRate(Float64List env, double fps) {
  final n = env.length;
  if (n < 3 || fps <= 0) return 0.0;
  var mean = 0.0;
  for (final v in env) {
    mean += v;
  }
  mean /= n;
  var varSum = 0.0;
  for (final v in env) {
    varSum += (v - mean) * (v - mean);
  }
  final thr = mean + 0.5 * math.sqrt(varSum / n);
  var peaks = 0;
  for (var i = 1; i < n - 1; i++) {
    final v = env[i];
    if (v > env[i - 1] && v >= env[i + 1] && v > thr) peaks++;
  }
  final dur = n / fps;
  return dur > 0 ? peaks / dur : 0.0;
}

double _pulseClarity(Float64List ac, double fps) {
  final n = ac.length;
  final lo = math.max(1, (60.0 * fps / 200.0).truncate());
  final hi = math.min(n - 1, (60.0 * fps / 60.0).truncate());
  if (hi <= lo) return 0.0;
  var mx = double.negativeInfinity;
  for (var i = lo; i < hi; i++) {
    if (ac[i] > mx) mx = ac[i];
  }
  return _clip(mx, 0.0, 1.0);
}

/// `_rhythm_features`: beat-relative tempograms (full, <200 Hz, >2 kHz),
/// onset density, hat/kick ratio and sub-band pulse clarity.
Float32List rhythmFeatures(
  Spectrogram pMag,
  Float32List freqs,
  int sr,
  double bpm,
) {
  final r = Float32List(kRhythmDims);
  if (pMag.frames < 16) return r;
  final fps = sr / kHop;
  final fullEnv = onsetEnvelope(pMag);
  // The masks are contiguous: freqs ascend.
  var lowEnd = 0;
  while (lowEnd < freqs.length && freqs[lowEnd] < 200.0) {
    lowEnd++;
  }
  var highStart = freqs.length;
  while (highStart > 0 && freqs[highStart - 1] > 2000.0) {
    highStart--;
  }
  final lowEnv = lowEnd > 0 ? onsetEnvelope(pMag, 0, lowEnd) : fullEnv;
  final highEnv = highStart < freqs.length
      ? onsetEnvelope(pMag, highStart, freqs.length)
      : fullEnv;
  final acFull = _autocorr(fullEnv);
  final acLow = _autocorr(lowEnv);
  final acHigh = _autocorr(highEnv);

  const nf = 6, nb = 3;
  if (bpm > 0) {
    final beatFrames = 60.0 * fps / bpm;
    r.setAll(0, _sampleTempogram(acFull, beatFrames, _tempogramMultsFull));
    r.setAll(nf, _sampleTempogram(acLow, beatFrames, _tempogramMultsBand));
    r.setAll(
      nf + nb,
      _sampleTempogram(acHigh, beatFrames, _tempogramMultsBand),
    );
  }
  const i = nf + 2 * nb;
  final dFull = _onsetRate(fullEnv, fps);
  final dLow = _onsetRate(lowEnv, fps);
  final dHigh = _onsetRate(highEnv, fps);
  r[i] = _clip(dFull / 8.0, 0.0, 1.5);
  r[i + 1] = _clip(dHigh / (dLow + 1e-6), 0.0, 4.0) / 4.0;
  r[i + 2] = _pulseClarity(acLow, fps);
  r[i + 3] = _pulseClarity(acHigh, fps);
  return r;
}

// ── Mel filterbank & MFCC ───────────────────────────────────────────────────

double _hzToMel(double hz) => 2595.0 * math.log(1.0 + hz / 700.0) / math.ln10;
double _melToHz(double mel) =>
    700.0 * (math.pow(10.0, mel / 2595.0).toDouble() - 1.0);

/// `_mel_filterbank`'s (left, centre, right) bin per band; a band with
/// c == l or c == r stays all zero.
List<(int, int, int)> melBinPoints(int sr, int nFft, int nMels) {
  final a = _hzToMel(0.0), b = _hzToMel(sr / 2.0);
  final step = (b - a) / (nMels + 1);
  final pts = <int>[];
  for (var i = 0; i < nMels + 2; i++) {
    final mel = i == nMels + 1 ? b : i * step + a;
    final hz = _melToHz(mel);
    final bin = ((nFft + 1) * hz / sr).floor();
    pts.add(bin.clamp(0, nFft ~/ 2));
  }
  return [for (var i = 0; i < nMels; i++) (pts[i], pts[i + 1], pts[i + 2])];
}

class _MelBank {
  _MelBank(int sr) {
    for (final (l, c, r) in melBinPoints(sr, kNFft, kNMels)) {
      if (c == l || c == r) {
        bands.add((0, Float32List(0)));
        continue;
      }
      final w = Float32List(r - l);
      for (var k = l; k < c; k++) {
        w[k - l] = (k - l) / (c - l);
      }
      for (var k = c; k < r; k++) {
        w[k - l] = (r - k) / (r - c);
      }
      bands.add((l, w));
    }
  }

  /// (first bin, weights) per mel band.
  final bands = <(int, Float32List)>[];
}

/// `_dct_matrix(n_in, n_out)`: orthonormal DCT-II rows, float32.
List<Float32List> dctMatrix(int nIn, int nOut) {
  final s0 = 1.0 / math.sqrt(nIn);
  final s = math.sqrt(2.0 / nIn);
  return [
    for (var k = 0; k < nOut; k++)
      Float32List.fromList([
        for (var n = 0; n < nIn; n++)
          f32(
            f32(math.cos(math.pi * k * (2 * n + 1) / (2 * nIn))) *
                (k == 0 ? s0 : s),
          ),
      ]),
  ];
}

/// `_mfcc_per_frame` reduced to its mean and std (ddof 0) per coefficient.
(Float32List, Float32List) mfccStats(Spectrogram m, int sr) {
  final bank = _MelBank(sr).bands;
  final dct = dctMatrix(kNMels, kNMfcc + 1);
  final sum = Float64List(kNMfcc);
  final sumSq = Float64List(kNMfcc);
  final logMel = Float64List(kNMels);
  for (var f = 0; f < m.frames; f++) {
    final row = f * m.bins;
    for (var i = 0; i < kNMels; i++) {
      final (l, w) = bank[i];
      var s = 0.0;
      for (var j = 0; j < w.length; j++) {
        final v = f32(m.data[row + l + j] * m.data[row + l + j]);
        s += v * w[j];
      }
      logMel[i] = f32(math.log(f32(f32(s) + 1e-9)));
    }
    for (var k = 1; k <= kNMfcc; k++) {
      final d = dct[k];
      var c = 0.0;
      for (var i = 0; i < kNMels; i++) {
        c += logMel[i] * d[i];
      }
      c = f32(c);
      sum[k - 1] += c;
      sumSq[k - 1] += c * c;
    }
  }
  final mean = Float32List(kNMfcc);
  final std = Float32List(kNMfcc);
  if (m.frames == 0) return (mean, std);
  for (var k = 0; k < kNMfcc; k++) {
    final mu = sum[k] / m.frames;
    mean[k] = mu;
    final v = sumSq[k] / m.frames - mu * mu;
    std[k] = math.sqrt(v > 0 ? v : 0);
  }
  return (mean, std);
}

// ── HPSS ────────────────────────────────────────────────────────────────────

/// numpy `pad(mode="reflect")` index for position [i] of an [n]-long axis.
int _reflect(int i, int n) {
  if (n == 1) return 0;
  final period = 2 * (n - 1);
  var j = i % period;
  if (j < 0) j += period;
  return j >= n ? period - j : j;
}

/// Sliding odd-size median over [src] (reflect-padded), written to [dst]
/// at `dstOff + t * dstStride`. Keeps the window sorted between steps.
void _slidingMedian(
  Float64List src,
  int n,
  int size,
  Float32List dst,
  int dstOff,
  int dstStride,
  Float64List win,
) {
  final pad = size ~/ 2;
  double at(int i) => src[_reflect(i, n)];
  for (var k = 0; k < size; k++) {
    win[k] = at(k - pad);
  }
  final w = Float64List.sublistView(win, 0, size)..sort();
  dst[dstOff] = w[pad];
  for (var t = 1; t < n; t++) {
    final out = at(t - 1 - pad);
    final inn = at(t + pad);
    if (out != inn) {
      // Find the outgoing value, overwrite it, and slide it into place.
      var lo = 0, hi = size - 1;
      while (lo < hi) {
        final mid = (lo + hi) >> 1;
        if (w[mid] < out) {
          lo = mid + 1;
        } else {
          hi = mid;
        }
      }
      var p = lo;
      w[p] = inn;
      while (p > 0 && w[p - 1] > w[p]) {
        final tmp = w[p - 1];
        w[p - 1] = w[p];
        w[p] = tmp;
        p--;
      }
      while (p < size - 1 && w[p + 1] < w[p]) {
        final tmp = w[p + 1];
        w[p + 1] = w[p];
        w[p] = tmp;
        p++;
      }
    }
    dst[dstOff + t * dstStride] = w[pad];
  }
}

/// `_hpss`: median-filter harmonic/percussive split with a soft Wiener
/// mask. Returns (H_mag, P_mag).
(Spectrogram, Spectrogram) hpss(Spectrogram m) {
  final nF = m.frames, nB = m.bins;
  final h = Float32List(nF * nB);
  final p = Float32List(nF * nB);
  final win = Float64List(_hpssKernel);
  // Harmonic: median along time, per bin.
  final col = Float64List(nF);
  for (var b = 0; b < nB; b++) {
    for (var f = 0; f < nF; f++) {
      col[f] = m.data[f * nB + b];
    }
    _slidingMedian(col, nF, _hpssKernel, h, b, nB, win);
  }
  // Percussive: median along frequency, per frame.
  final row = Float64List(nB);
  for (var f = 0; f < nF; f++) {
    for (var b = 0; b < nB; b++) {
      row[b] = m.data[f * nB + b];
    }
    _slidingMedian(row, nB, _hpssKernel, p, f * nB, 1, win);
  }
  const eps = 1e-10;
  for (var i = 0; i < h.length; i++) {
    final hs = f32(h[i] * h[i]);
    final ps = f32(p[i] * p[i]);
    final denom = f32(f32(hs + ps) + eps);
    final x = m.data[i];
    h[i] = x * f32(hs / denom);
    p[i] = x * f32(ps / denom);
  }
  return (Spectrogram._(nF, nB, h), Spectrogram._(nF, nB, p));
}

// ── Pipeline ────────────────────────────────────────────────────────────────

/// `extract_features_from_pcm` on samples already loaded.
AudioFeatures extractFeatures(Float32List x, [int sr = kDspSampleRate]) {
  final mag = stftMagnitude(x);
  final freqs = rfftFreqs(sr);
  final (hMag, pMag) = hpss(mag);

  final energy = energyDb(x);
  final bright = brightness(mag, freqs, sr);
  final rolloff = spectralRolloff(mag, freqs, sr);
  final flatness = spectralFlatness(mag);
  final contrast = spectralContrast(mag);

  final chroma = chromaMean(hMag, freqs);
  final key = estimateKey(chroma);

  final onset = onsetEnvelope(pMag);
  final (bpm, strength) = estimateBpm(onset, sr);
  final rhythm = rhythmFeatures(pMag, freqs, sr, bpm);

  final (mfccMean, mfccStd) = mfccStats(hMag, sr);

  return AudioFeatures(
    bpm: bpm,
    energy: energy,
    brightness: bright,
    rolloff: rolloff,
    beatStrength: strength,
    spectralFlatness: flatness,
    spectralContrast: contrast,
    keyIndex: key,
    mfccMean: mfccMean,
    mfccStd: mfccStd,
    chroma: chroma,
    rhythm: rhythm,
  );
}

/// `extract_features_from_pcm(pcm_path, sr)` on the decoder's raw bytes.
AudioFeatures extractFeaturesFromPcmBytes(Uint8List bytes, int sr) =>
    extractFeatures(pcmFromBytes(bytes), sr);
