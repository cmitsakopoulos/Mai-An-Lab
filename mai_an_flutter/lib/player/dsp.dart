/// Per-track sound settings: the EQ preset and "Dynamism" (a loudness boost
/// and EQ contour scaled by the track's own energy). Port of
/// utils/audio_engine.py `reapply_dsp`, `_compute_dynamism_scaler` and
/// `apply_combined_dsp`, which the Flet engine runs on every track change.
library;

/// The `[dsp]` table of config.toml (written by Flet's Settings → Sound).
class DspConfig {
  const DspConfig({
    this.equalizerEnabled = false,
    this.dynamismEnabled = false,
    this.activePreset = 'Flat',
    this.customPresets = const {},
  });

  final bool equalizerEnabled;
  final bool dynamismEnabled;
  final String activePreset;
  final Map<String, List<double>> customPresets;

  /// `bool(dsp.get(...))` and friends, from the parsed TOML table.
  static DspConfig fromToml(Object? table) {
    if (table is! Map) return const DspConfig();
    final custom = <String, List<double>>{};
    final raw = table['custom_presets'];
    if (raw is Map) {
      raw.forEach((name, gains) {
        if (gains is List && gains.every((g) => g is num)) {
          custom['$name'] = [for (final g in gains) (g as num).toDouble()];
        }
      });
    }
    final preset = table['active_preset'];
    return DspConfig(
      equalizerEnabled: _truthy(table['equalizer_enabled']),
      dynamismEnabled: _truthy(table['dynamism_enabled']),
      activePreset: preset is String ? preset : 'Flat',
      customPresets: custom,
    );
  }

  static bool _truthy(Object? v) => switch (v) {
    null => false,
    bool b => b,
    num n => n != 0,
    String s => s.isNotEmpty,
    _ => true,
  };
}

/// The audio features `reapply_dsp` reads (play_counts columns).
typedef DspFeatures = ({
  double? energy,
  double? beatStrength,
  double? spectralContrast,
});

/// What the player applies: the loudness boost (dB, as sent to
/// `set_loudness_boost`; the handler clamps to ≥ 0) and five EQ band gains.
typedef DspSettings = ({double boostDb, List<double> bands});

const kEqBands = 5;
const _flat = [0.0, 0.0, 0.0, 0.0, 0.0];

/// `reapply_dsp`'s PRESETS.
const kEqPresets = <String, List<double>>{
  'Flat': [0.0, 0.0, 0.0, 0.0, 0.0],
  'Rock': [4.0, 2.0, -2.0, 2.0, 4.0],
  'Pop': [2.0, 3.0, 1.0, -1.0, 2.0],
  'Jazz': [3.0, 2.0, 1.0, 2.0, 3.0],
  'Classical': [3.0, 2.0, -1.0, 2.0, 3.0],
  'Electronic': [4.0, 2.0, 0.0, 2.0, 3.0],
  'Bass Booster': [5.0, 3.0, 0.0, 0.0, 0.0],
  'Vocal Booster': [-2.0, -1.0, 3.0, 2.0, -1.0],
};

/// The EQ contour Dynamism applies (scaled by the score) when the manual EQ
/// is off.
const _dynContour = [3.0, 1.5, 0.0, 1.0, 2.5];

/// Nothing applied: what Flet sets with no current track, or on any failure.
const DspSettings kNeutralDsp = (boostDb: 0.0, bands: _flat);

/// `_compute_dynamism_scaler`: a per-track score in [0, 1], rounded to 2 dp.
double dynamismScaler(double? energy, double? beat, double? contrast) {
  final e = energy ?? 0.5;
  final b = beat ?? 0.5;
  final c = contrast ?? 0.3;
  final normContrast = ((c - 0.2) / 0.2).clamp(0.0, 1.0);
  final score = (0.4 * e + 0.3 * b + 0.3 * normContrast).clamp(0.0, 1.0);
  return pyRound2(score);
}

/// Python's `round(x, 2)`: correctly rounded on the exact binary value, ties
/// to even. `toStringAsFixed` is also exact but breaks ties away from zero;
/// an exact tie needs x·200 odd, which in [0, 1] means x is a multiple of
/// 1/8 (0.125, 0.375, …), tested exactly via x·8 (a power-of-two scale).
double pyRound2(double x) {
  final x8 = x * 8;
  if (x8 == x8.roundToDouble() && (x * 200).round().isOdd) {
    final lo = (x * 100).floor();
    return (lo.isEven ? lo : lo + 1) / 100;
  }
  return double.parse(x.toStringAsFixed(2));
}

/// The base EQ: the active preset's gains when the EQ is on, else flat.
List<double> baseEq(DspConfig cfg) {
  if (!cfg.equalizerEnabled) return _flat;
  final gains =
      kEqPresets[cfg.activePreset] ?? cfg.customPresets[cfg.activePreset];
  if (gains == null || gains.isEmpty) return _flat;
  // Python indexes bands 0..4 of whatever list it got.
  return [for (var i = 0; i < kEqBands; i++) i < gains.length ? gains[i] : 0.0];
}

/// True when [features] is enough to score the track: `reapply_dsp` needs
/// energy and beat strength (contrast falls back to 0.3).
bool hasDspFeatures(DspFeatures? f) =>
    f != null && f.energy != null && f.beatStrength != null;

/// `reapply_dsp` for one track. [features] null = the track has no
/// play_counts row (Flet then applies the base EQ with no boost).
DspSettings computeDsp(DspConfig cfg, DspFeatures? features) {
  final base = baseEq(cfg);
  if (!cfg.dynamismEnabled || features == null) {
    return (boostDb: 0.0, bands: base);
  }
  final score = dynamismScaler(
    features.energy,
    features.beatStrength,
    features.spectralContrast,
  );
  final boost = 1.0 + 3.0 * score;
  // Manual EQ has exclusive control of the bands; otherwise Dynamism shapes
  // them.
  final bands = cfg.equalizerEnabled
      ? base
      : [for (final g in _dynContour) score * g];
  return (boostDb: boost, bands: bands);
}
