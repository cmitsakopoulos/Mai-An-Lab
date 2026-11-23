import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/app_config.dart';
import 'package:mai_an_lab/core/haptics.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/player/dsp.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/player/queue_store.dart';

import 'fake_backend.dart';

/// (energy, beat_strength, spectral_contrast, score) as printed by
/// `AudioEngine._compute_dynamism_scaler` (utils/audio_engine.py). Includes
/// the exact binary ties 0.125 → 0.12 and 0.375 → 0.38 (round half to even).
const _pythonScores = [
  [null, null, null, 0.5],
  [1.0, 1.0, 0.36, 0.94],
  [0.0, 0.0, 0.25, 0.07],
  [0.8, 0.6, 0.35, 0.72],
  [0.3125, 0.0, 0.2, 0.12],
  [0.9375, 0.0, 0.2, 0.38],
  [0.0, 0.0, 0.2833333333333333, 0.12],
  [0.324, 0.151, 0.345, 0.39],
  [0.072, 0.536, 0.26, 0.28],
  [0.058, 0.507, 0.161, 0.18],
  [0.434, 0.07, 0.177, 0.19],
  [0.425, 0.827, 0.187, 0.42],
  [0.223, 0.627, 0.434, 0.58],
  [0.577, 0.397, 0.443, 0.65],
  [0.047, 0.858, 0.237, 0.33],
  [0.144, 0.118, 0.243, 0.16],
  [0.816, 0.181, 0.324, 0.57],
  [0.639, 0.372, 0.314, 0.54],
  [0.063, 0.06, 0.212, 0.06],
  [0.68, 0.428, 0.244, 0.47],
  [0.586, 0.453, 0.24, 0.43],
  [0.794, 0.699, 0.223, 0.56],
  [0.574, 0.525, 0.413, 0.69],
  [0.729, 0.288, 0.444, 0.68],
  [0.118, 0.418, 0.377, 0.44],
  [0.152, 0.489, 0.162, 0.21],
  [0.668, 0.765, 0.322, 0.68],
  [0.875, 0.314, 0.359, 0.68],
  [0.594, 0.58, 0.287, 0.54],
  [0.84, 0.945, 0.292, 0.76],
  [0.664, 0.061, 0.36, 0.52],
  [0.647, 0.993, 0.397, 0.85],
  [0.285, 0.386, 0.351, 0.46],
  [0.023, 0.462, 0.2, 0.15],
  [0.117, 0.059, 0.38, 0.33],
  [0.129, 0.248, 0.267, 0.23],
  [0.871, 0.081, 0.285, 0.5],
  [0.549, 0.883, 0.396, 0.78],
  [0.864, 0.278, 0.275, 0.54],
  [0.359, 0.884, 0.437, 0.71],
  [0.151, 0.176, 0.22, 0.14],
  [0.233, 0.485, 0.327, 0.43],
  [0.263, 0.004, 0.276, 0.22],
  [0.369, 0.566, 0.436, 0.62],
  [0.69, 0.515, 0.335, 0.63],
  [0.676, 0.054, 0.42, 0.59],
  [0.78, 0.875, 0.389, 0.86],
];

DspFeatures f(double? e, double? b, double? c) =>
    (energy: e, beatStrength: b, spectralContrast: c);

void expectBands(List<double> got, List<double> want) {
  expect(got.length, want.length);
  for (var i = 0; i < want.length; i++) {
    expect(got[i], closeTo(want[i], 1e-9), reason: 'band $i');
  }
}

void main() {
  group('dynamism score', () {
    test('matches Python on every reference value', () {
      for (final r in _pythonScores) {
        expect(dynamismScaler(r[0], r[1], r[2]), r[3], reason: '$r');
      }
    });
  });

  // tests/test_dsp.py
  group('reapply_dsp', () {
    test('defaults: no boost, flat EQ', () {
      final out = computeDsp(const DspConfig(), f(0.3, null, null));
      expect(out.boostDb, 0.0);
      expectBands(out.bands, [0, 0, 0, 0, 0]);
    });

    test('dynamism alone shapes the EQ and boosts', () {
      const cfg = DspConfig(dynamismEnabled: true);
      var out = computeDsp(cfg, f(1.0, 1.0, 0.36));
      expect(out.boostDb, closeTo(3.82, 1e-9));
      expectBands(out.bands, [2.82, 1.41, 0.0, 0.94, 2.35]);
      out = computeDsp(cfg, f(0.0, 0.0, 0.25));
      expect(out.boostDb, closeTo(1.21, 1e-9));
      expectBands(out.bands, [0.21, 0.105, 0.0, 0.07, 0.175]);
    });

    test('with the EQ on, the preset owns the bands; the boost stays', () {
      const cfg = DspConfig(
        equalizerEnabled: true,
        dynamismEnabled: true,
        activePreset: 'Rock',
      );
      final out = computeDsp(cfg, f(1.0, 1.0, 0.36));
      expect(out.boostDb, closeTo(3.82, 1e-9));
      expectBands(out.bands, [4, 2, -2, 2, 4]);
    });

    test('presets: built-in, custom, unknown', () {
      expectBands(
        computeDsp(
          const DspConfig(equalizerEnabled: true, activePreset: 'Rock'),
          null,
        ).bands,
        [4, 2, -2, 2, 4],
      );
      final custom = DspConfig.fromToml({
        'equalizer_enabled': true,
        'active_preset': 'My Custom Setup',
        'custom_presets': {
          'My Custom Setup': [1, 2.0, 3, 4, 5],
        },
      });
      expectBands(computeDsp(custom, null).bands, [1, 2, 3, 4, 5]);
      expectBands(
        computeDsp(
          const DspConfig(equalizerEnabled: true, activePreset: 'Custom'),
          null,
        ).bands,
        [0, 0, 0, 0, 0],
      );
    });

    test('no play_counts row: base EQ, no boost', () {
      final out = computeDsp(
        const DspConfig(
          dynamismEnabled: true,
          equalizerEnabled: true,
          activePreset: 'Pop',
        ),
        null,
      );
      expect(out.boostDb, 0.0);
      expectBands(out.bands, [2, 3, 1, -1, 2]);
    });

    test('a row with NULL features scores with the defaults', () {
      final out = computeDsp(
        const DspConfig(dynamismEnabled: true),
        f(null, null, null),
      );
      expect(out.boostDb, closeTo(2.5, 1e-9));
    });
  });

  group('config.toml', () {
    test('[dsp] and [haptics] parse', () {
      final c = AppConfig.parse('''
[dsp]
equalizer_enabled = true
dynamism_enabled = false
active_preset = "Jazz"

[haptics]
haptic_feedback_enabled = true
long_press_intensity = "light"
swipe_queue_intensity = "none"
''');
      expect(c.dsp.equalizerEnabled, isTrue);
      expect(c.dsp.dynamismEnabled, isFalse);
      expect(c.dsp.activePreset, 'Jazz');
      expect(c.haptics.intensityFor('long_press'), 'light');
      expect(c.haptics.intensityFor('swipe_queue'), isNull);
      // Unset actions fall back to main.py's defaults.
      expect(c.haptics.intensityFor('network_tap'), 'selection');
      expect(c.haptics.intensityFor('unknown_action'), 'light');
    });

    test('haptics off silences everything; absent tables are defaults', () {
      expect(
        HapticsConfig.fromToml({
          'haptic_feedback_enabled': false,
        }).intensityFor('long_press'),
        isNull,
      );
      final c = AppConfig.parse('[general]\nlibrary_sort = "artist"\n');
      expect(c.haptics.intensityFor('long_press'), 'heavy');
      expect(c.dsp.dynamismEnabled, isFalse);
    });
  });

  group('player applies it per track', () {
    late Directory dir;
    late FakeBackend backend;
    late PlayerController c;
    var cfg = const DspConfig(dynamismEnabled: true);
    final features = <String, DspFeatures>{
      '/m/a.mp3': f(1.0, 1.0, 0.36),
      '/m/b.mp3': f(0.0, 0.0, 0.25),
    };

    Track t(String name) => Track(
      id: name.hashCode,
      path: '/m/$name.mp3',
      title: name,
      artist: 'X',
      album: 'Y',
      duration: 100,
      format: 'mp3',
    );

    setUp(() async {
      dir = Directory.systemTemp.createTempSync('mai_an_dsp');
      backend = FakeBackend();
      cfg = const DspConfig(dynamismEnabled: true);
      c = PlayerController(
        backend: backend,
        store: QueueStore(dir.path),
        prefs: Prefs(dir.path),
        libraryTracks: () async => const [],
        dspConfig: () async => cfg,
        dspFeatures: (p) async => features[p],
      );
      await c.init();
    });

    tearDown(() async {
      await c.flush();
      c.dispose();
      dir.deleteSync(recursive: true);
    });

    Future<void> settle() => Future<void>.delayed(Duration.zero);

    test('on play, on next, and nothing for a track without a row', () async {
      final list = [t('a'), t('b'), t('c')];
      await c.playTrack(list[0], context: list);
      await settle();
      expect(backend.boostDb, closeTo(3.82, 1e-9));
      expect(c.loudnessBoost.value, closeTo(3.82, 1e-9));
      expectBands(backend.eq, [2.82, 1.41, 0.0, 0.94, 2.35]);

      await c.settled;
      c.next();
      await settle();
      expect(backend.boostDb, closeTo(1.21, 1e-9));

      await c.settled;
      c.next();
      await settle();
      expect(backend.boostDb, 0.0);
      expect(c.loudnessBoost.value, 0.0);
      expectBands(backend.eq, [0, 0, 0, 0, 0]);
    });

    test('the player moving on by itself re-applies it', () async {
      final list = [t('a'), t('b')];
      await c.playTrack(list[0], context: list);
      await c.settled;
      await settle();
      backend.advance();
      await settle();
      expect(backend.boostDb, closeTo(1.21, 1e-9));
    });
  });
}
