import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/analysis/audio_features.dart';

import 'audio_features_test.dart' show expectFeatures;

/// Phase 6d parity on real music: `tool/export_dsp_goldens.py --real N`
/// decodes tracks with dsp.py's own ffmpeg command into tool/.dsp_cache/
/// (gitignored) with Python's features alongside. Skipped when absent.
void main() {
  final manifest = File('tool/.dsp_cache/manifest.json');
  if (!manifest.existsSync()) {
    test(
      'real-audio DSP parity',
      () {},
      skip: 'run tool/export_dsp_goldens.py --real N first',
    );
    return;
  }
  final entries = (jsonDecode(manifest.readAsStringSync()) as List)
      .cast<Map<String, Object?>>();
  for (final e in entries) {
    final pcm = File('tool/.dsp_cache/${e['pcm']}');
    test('${e['source']}', () {
      final sw = Stopwatch()..start();
      final f = extractFeaturesFromPcmBytes(
        pcm.readAsBytesSync(),
        e['sample_rate']! as int,
      );
      final want = e['features']! as Map<String, Object?>;
      var worst = 0.0;
      void track(double got, Object? w) {
        final d = (got - (w! as num)).abs() / math.max(1, (w as num).abs());
        if (d > worst) worst = d;
      }

      for (final (k, v) in [
        ('energy', f.energy),
        ('brightness', f.brightness),
        ('rolloff', f.rolloff),
        ('beat_strength', f.beatStrength),
        ('spectral_flatness', f.spectralFlatness),
        ('spectral_contrast', f.spectralContrast),
      ]) {
        track(v, want[k]);
      }
      for (final (k, v) in [
        ('mfcc_mean', f.mfccMean),
        ('mfcc_std', f.mfccStd),
        ('chroma', f.chroma),
        ('rhythm', f.rhythm),
      ]) {
        final w = want[k]! as List;
        for (var i = 0; i < v.length; i++) {
          track(v[i], w[i]);
        }
      }
      // ignore: avoid_print
      print(
        '${e['source']}: ${sw.elapsedMilliseconds} ms, worst rel Δ '
        '${worst.toStringAsExponential(2)}',
      );
      expectFeatures('${e['source']}', f, want);
    }, timeout: const Timeout(Duration(minutes: 2)));
  }
}
