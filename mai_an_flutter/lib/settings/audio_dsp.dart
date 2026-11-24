import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../core/py_num.dart';
import '../player/dsp.dart';
import '../ui/tokens.dart';
import 'groups.dart' show SwitchRow, SettingsDropdown;
import 'settings_page.dart';

const kSystemPresets = [
  'Flat',
  'Rock',
  'Pop',
  'Jazz',
  'Classical',
  'Electronic',
  'Bass Booster',
  'Vocal Booster',
];

/// The names a custom preset may not take (`_on_save_custom_preset`).
const _reserved = [...kSystemPresets, 'Custom'];

/// settings.py `_build_audio_dsp_group` and its handlers: Dynamism, the
/// equalizer switch, system and custom presets, the curve, the sliders and
/// their gain fields, saving, applying and deleting custom presets.
///
/// Flet saves on every drag event of the curve and the sliders; here the
/// gains reach the player as they move and the file when the drag ends
/// (PORT_MAP §7). What is saved is the same: unrounded gains from the curve,
/// one-decimal ones from the sliders and fields.
class AudioDspGroup extends StatefulWidget {
  const AudioDspGroup({super.key, required this.deps});

  final SettingsDeps deps;

  @override
  State<AudioDspGroup> createState() => _AudioDspGroupState();
}

class _AudioDspGroupState extends State<AudioDspGroup> {
  SettingsDeps get d => widget.deps;

  late bool _dynamism;
  late bool _eq;

  /// `_eq_preset_type_radio`: "system" or "custom".
  String _type = 'system';
  String _preset = 'Flat';

  /// The file's custom presets, in file order (`dsp.custom_presets`).
  late Map<String, List<double>> _custom;

  /// `_eq_bands`: centre frequencies (Hz) and gains (dB).
  List<double> _freqs = [60, 230, 910, 4000, 14000];
  late List<double> _gains = List.filled(_freqs.length, 0.0);
  double _minDb = -15, _maxDb = 15;

  /// `_check_dynamism_availability`.
  bool _dynamismAvailable = true;
  String? _dynamismNote;

  final _presetName = TextEditingController();

  @override
  void initState() {
    super.initState();
    final cfg = DspConfig.fromToml(d.config.raw['dsp']);
    _dynamism = cfg.dynamismEnabled;
    _eq = cfg.equalizerEnabled;
    _custom = Map.of(cfg.customPresets);
    _refreshPresets(cfg.activePreset);
    _loadGains();
    _loadBands();
    _checkDynamism();
  }

  @override
  void dispose() {
    _presetName.dispose();
    super.dispose();
  }

  // ── reads ────────────────────────────────────────────────────────────────

  /// `_load_equalizer_bands`: the device's bands and range, when known.
  Future<void> _loadBands() async {
    final info = await d.player.value?.backend.equalizerInfo();
    if (info == null || !mounted || info.centerHz.isEmpty) return;
    setState(() {
      _freqs = info.centerHz;
      _minDb = info.minDb;
      _maxDb = info.maxDb;
      final g = List.filled(_freqs.length, 0.0);
      for (var i = 0; i < g.length && i < _gains.length; i++) {
        g[i] = _gains[i];
      }
      _gains = g;
    });
    _loadGains();
  }

  Future<void> _checkDynamism() async {
    final repo = d.library.repository;
    if (repo == null) return;
    try {
      final (total, missing) = await repo.featureCoverage();
      if (!mounted) return;
      setState(() {
        if (total == 0) {
          _dynamismAvailable = false;
          _dynamismNote = '⚠ Library is empty — add tracks first.';
        } else {
          _dynamismAvailable = missing < total;
          _dynamismNote = _dynamismAvailable
              ? null
              : '⚠ $missing track${missing != 1 ? 's' : ''} lack DSP '
                    'features — run Jarvis Analyser first.';
        }
      });
    } catch (e) {
      debugPrint('dynamism availability check failed: $e');
    }
  }

  /// `_update_eq_sliders_from_active_preset`: the active preset's gains, or
  /// zeros; bands past the preset keep theirs.
  void _loadGains() {
    final gains = kEqPresets[_preset] ?? _custom[_preset];
    setState(() {
      for (var i = 0; i < _gains.length; i++) {
        if (gains == null || gains.isEmpty) {
          _gains[i] = 0;
        } else if (i < gains.length) {
          _gains[i] = gains[i];
        }
      }
    });
  }

  /// `_refresh_eq_presets_dropdown(active_value, set_radio)`.
  void _refreshPresets(String active, {bool setRadio = true}) {
    if (setRadio) {
      _type = kSystemPresets.contains(active) ? 'system' : 'custom';
    }
    final options = _options;
    if (!options.contains(active)) {
      active = _type == 'system' ? 'Flat' : 'Custom';
    }
    _preset = active;
  }

  List<String> get _options => _type == 'system'
      ? kSystemPresets
      : ['Custom', ..._custom.keys.where((n) => n != 'Custom')];

  // ── writes ───────────────────────────────────────────────────────────────

  Future<void> _write(Map<String, Object?> dsp) =>
      d.config.update({'dsp': dsp});

  void _applyAll() {
    final backend = d.player.value?.backend;
    if (backend == null || !_eq) return;
    for (var i = 0; i < _gains.length; i++) {
      unawaited(backend.setEqBandGain(i, _gains[i]));
    }
  }

  Future<void> _onDynamism(bool v) async {
    setState(() => _dynamism = v);
    await _write({'dynamism_enabled': v});
    d.player.value?.reapplyDsp();
  }

  Future<void> _onEqualizer(bool v) async {
    setState(() => _eq = v);
    await _write({'equalizer_enabled': v});
    final backend = d.player.value?.backend;
    if (backend == null) return;
    for (var i = 0; i < (v ? _gains.length : 5); i++) {
      unawaited(backend.setEqBandGain(i, v ? _gains[i] : 0.0));
    }
  }

  /// `_on_preset_type_change`.
  Future<void> _onType(String type) async {
    final active = d.config.string('dsp', 'active_preset', 'Flat');
    final String target;
    if (type == 'system') {
      target = kSystemPresets.contains(active) ? active : 'Flat';
    } else {
      target = active == 'Custom' || _custom.containsKey(active)
          ? active
          : 'Custom';
    }
    setState(() {
      _type = type;
      _refreshPresets(target, setRadio: false);
    });
    await _write({'active_preset': _preset});
    _loadGains();
    _applyAll();
  }

  /// `_on_eq_preset_select`.
  Future<void> _onPreset(String name) async {
    setState(() => _preset = name);
    await _write({'active_preset': name});
    _loadGains();
    _applyAll();
  }

  /// A band moved (curve, slider or field): to the player now, and into
  /// the Custom preset (switching to it first).
  void _bandMoved(int i, double stored, double applied) {
    setState(() => _gains[i] = stored);
    if (_eq) {
      unawaited(d.player.value?.backend.setEqBandGain(i, applied));
    }
    if (_type != 'custom' || _preset != 'Custom') {
      setState(() => _refreshPresets('Custom'));
      unawaited(_write({'active_preset': 'Custom'}));
    }
  }

  /// `_save_current_slider_gains_to_preset(name)`.
  Future<void> _saveGainsAs(String name) async {
    _custom[name] = List.of(_gains);
    await _write({'custom_presets': Map.of(_custom)});
  }

  void _onCurve(int i, double gain) {
    final rounded = pyRound1(gain);
    if (rounded != pyRound1(_gains[i])) d.haptics.trigger('light');
    // The curve stores what the finger says; the player gets one decimal.
    _bandMoved(i, gain, rounded);
  }

  void _onSlider(int i, double v) {
    final val = pyRound1(v);
    if (_gains[i] != val) d.haptics.trigger('light');
    _bandMoved(i, val, val);
  }

  /// `_on_gain_text_field_submit`.
  void _onGainField(int i, String text) {
    var s = text.trim();
    if (s.toLowerCase().endsWith('db')) s = s.substring(0, s.length - 2).trim();
    final parsed = double.tryParse(s);
    final val = parsed == null
        ? _gains[i]
        : pyRound1(parsed).clamp(_minDb, _maxDb).toDouble();
    if (_gains[i] == val) {
      setState(() {}); // rewrite the field
      return;
    }
    d.haptics.trigger('light');
    _bandMoved(i, val, val);
    unawaited(_write({'active_preset': 'Custom'}));
    unawaited(_saveGainsAs('Custom'));
  }

  /// `_on_save_custom_preset`.
  Future<void> _savePreset() async {
    final name = _presetName.text.trim();
    if (name.isEmpty) {
      showSettingsSnack(context, 'Preset name cannot be empty.');
      return;
    }
    if (_reserved.contains(name)) {
      showSettingsSnack(context, 'Cannot overwrite default presets.');
      return;
    }
    await _saveGainsAs(name);
    await _write({'active_preset': name});
    if (!mounted) return;
    setState(() {
      _refreshPresets(name);
      _presetName.clear();
    });
    showSettingsSnack(context, "Preset '$name' saved successfully.");
  }

  /// `_apply_custom_preset`.
  Future<void> _applyPreset(String name) async {
    await _write({'active_preset': name});
    if (!mounted) return;
    setState(() => _refreshPresets(name));
    _loadGains();
    _applyAll();
    showSettingsSnack(context, "Applied custom preset '$name'.");
  }

  /// `_delete_custom_preset`.
  Future<void> _deletePreset(String name) async {
    _custom.remove(name);
    final active = d.config.string('dsp', 'active_preset', 'Flat');
    final next = active == name ? 'Flat' : active;
    final ok = await d.config.update({
      'dsp': {'custom_presets': Map.of(_custom), 'active_preset': next},
    });
    if (!mounted) return;
    if (!ok) {
      showSettingsSnack(context, 'Failed to delete preset: config not saved');
      return;
    }
    setState(() => _refreshPresets(next));
    _loadGains();
    _applyAll();
    showSettingsSnack(context, "Deleted preset '$name'.");
  }

  // ── UI ───────────────────────────────────────────────────────────────────

  static String _freqLabel(double f) => f >= 1000
      ? (f % 1000 != 0
            ? '${(f / 1000).toStringAsFixed(1)} kHz'
            : '${(f / 1000).truncate()} kHz')
      : '${f.truncate()} Hz';

  /// Python's `f"{g:+.1f}"`.
  static String _signed(double g) {
    final s = g.toStringAsFixed(1);
    return s.startsWith('-') ? s : '+$s';
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final custom = _type == 'custom';
    final customNames = _custom.keys.where((n) => n != 'Custom').toList()
      ..sort();
    return GroupBody(
      spacing: 20,
      children: [
        const Dim('Real-time digital signal processing adjustments.'),
        SettingsCard(
          radius: 10,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const CardTitle(Icons.graphic_eq_rounded, 'Dynamic Punchiness'),
              const SizedBox(height: 10),
              const Dim(
                'Automatically enhance output gain based on track energy and '
                'beat strength to increase dynamic punchiness of rhythmic '
                'tracks.',
              ),
              const SizedBox(height: 10),
              Opacity(
                opacity: _dynamismAvailable ? 1 : 0.4,
                child: SwitchRow(
                  value: _dynamism,
                  label: 'Enable Dynamism Enhancement',
                  onChanged: _dynamismAvailable ? _onDynamism : null,
                ),
              ),
              if (_dynamismNote != null) ...[
                const SizedBox(height: 10),
                Dim(_dynamismNote!, size: 11, color: Tokens.amber),
              ],
              _BoostCard(deps: d),
            ],
          ),
        ),
        SettingsCard(
          radius: 10,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const CardTitle(Icons.tune_rounded, 'Equalizer'),
              const SizedBox(height: 10),
              const Dim(
                'Boost or cut specific frequency bands to match your '
                'headphones or genre preferences.',
              ),
              const SizedBox(height: 10),
              SwitchRow(
                value: _eq,
                label: 'Enable 5-Band Equalizer',
                onChanged: _onEqualizer,
              ),
              const SizedBox(height: 15),
              RadioGroup<String>(
                groupValue: _type,
                onChanged: (v) {
                  if (v != null) _onType(v);
                },
                child: Wrap(
                  spacing: 20,
                  children: [
                    for (final (v, label) in const [
                      ('system', 'System Presets'),
                      ('custom', 'Custom Presets'),
                    ])
                      InkWell(
                        onTap: () => _onType(v),
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Radio<String>(
                              value: v,
                              fillColor: WidgetStatePropertyAll(accent),
                            ),
                            Text(
                              label,
                              style: const TextStyle(
                                color: Tokens.text,
                                fontSize: 12,
                              ),
                            ),
                          ],
                        ),
                      ),
                  ],
                ),
              ),
              const SizedBox(height: 15),
              SettingsDropdown(
                label: 'Equalizer Preset',
                value: _preset,
                options: [for (final o in _options) (o, o)],
                onChanged: _onPreset,
              ),
              const SizedBox(height: 15),
              EqCurve(
                freqs: _freqs,
                gains: _gains,
                minDb: _minDb,
                maxDb: _maxDb,
                enabled: _eq,
                onChanged: _onCurve,
                onEnd: () => _saveGainsAs('Custom'),
              ),
              const SizedBox(height: 15),
              for (var i = 0; i < _gains.length; i++)
                Padding(
                  padding: const EdgeInsets.only(bottom: 15),
                  child: Row(
                    children: [
                      SizedBox(width: 60, child: Dim(_freqLabel(_freqs[i]))),
                      Expanded(
                        child: Slider(
                          min: _minDb,
                          max: _maxDb,
                          divisions: ((_maxDb - _minDb) * 2).toInt(),
                          value: _gains[i].clamp(_minDb, _maxDb).toDouble(),
                          onChanged: _eq ? (v) => _onSlider(i, v) : null,
                          onChangeEnd: _eq
                              ? (_) => _saveGainsAs('Custom')
                              : null,
                        ),
                      ),
                      const SizedBox(width: 10),
                      _GainField(
                        key: ValueKey('gain$i'),
                        text: '${_signed(_gains[i])} dB',
                        enabled: _eq,
                        onSubmit: (t) => _onGainField(i, t),
                      ),
                    ],
                  ),
                ),
              if (custom) ...[
                Row(
                  children: [
                    Expanded(
                      child: TextField(
                        controller: _presetName,
                        style: const TextStyle(
                          color: Tokens.text,
                          fontSize: 13,
                        ),
                        decoration: InputDecoration(
                          labelText: 'Save Custom Preset',
                          hintText: 'Enter preset name',
                          labelStyle: TextStyle(color: accent, fontSize: 11),
                          isDense: true,
                          border: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(10),
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(width: 10),
                    OnyxButton(label: 'SAVE', width: 90, onTap: _savePreset),
                  ],
                ),
                if (customNames.isNotEmpty) ...[
                  const SizedBox(height: 15),
                  const AccentLabel('Custom Presets'),
                  const SizedBox(height: 5),
                  for (final name in customNames)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: _PresetCard(
                        name: name,
                        gains: _custom[name]!,
                        onApply: () => _applyPreset(name),
                        onDelete: () => _deletePreset(name),
                      ),
                    ),
                ],
              ],
            ],
          ),
        ),
      ],
    );
  }
}

/// The "Active Track Boost" card: Dynamism's boost for the playing track,
/// shown while it is above zero (`update_loudness_boost`).
class _BoostCard extends StatelessWidget {
  const _BoostCard({required this.deps});

  final SettingsDeps deps;

  @override
  Widget build(BuildContext context) => ValueListenableBuilder(
    valueListenable: deps.player,
    builder: (context, player, _) => player == null
        ? const SizedBox.shrink()
        : ValueListenableBuilder(
            valueListenable: player.loudnessBoost,
            builder: (context, boost, _) => boost <= 0
                ? const SizedBox.shrink()
                : Container(
                    margin: const EdgeInsets.only(top: 10),
                    padding: const EdgeInsets.symmetric(
                      horizontal: 12,
                      vertical: 8,
                    ),
                    decoration: BoxDecoration(
                      color: Tokens.text.withValues(alpha: 0.04),
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(color: Tokens.border),
                    ),
                    child: Row(
                      children: [
                        const Icon(
                          Icons.flash_on_rounded,
                          color: Tokens.amber,
                          size: 16,
                        ),
                        const SizedBox(width: 10),
                        const Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                'Active Track Boost',
                                style: TextStyle(
                                  color: Tokens.text,
                                  fontSize: 12,
                                  fontWeight: FontWeight.bold,
                                ),
                              ),
                              SizedBox(height: 2),
                              Dim(
                                'Acoustic energy enhancement active',
                                size: 10,
                              ),
                            ],
                          ),
                        ),
                        Text(
                          '+${boost.toStringAsFixed(1)} dB',
                          style: const TextStyle(
                            color: Tokens.amber,
                            fontSize: 13,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ],
                    ),
                  ),
          ),
  );
}

/// A band's "+0.0 dB" field: submitted on enter or when focus leaves.
class _GainField extends StatefulWidget {
  const _GainField({
    super.key,
    required this.text,
    required this.enabled,
    required this.onSubmit,
  });

  final String text;
  final bool enabled;
  final ValueChanged<String> onSubmit;

  @override
  State<_GainField> createState() => _GainFieldState();
}

class _GainFieldState extends State<_GainField> {
  late final _c = TextEditingController(text: widget.text);
  final _focus = FocusNode();

  @override
  void initState() {
    super.initState();
    _focus.addListener(() {
      if (!_focus.hasFocus) widget.onSubmit(_c.text);
    });
  }

  @override
  void didUpdateWidget(_GainField old) {
    super.didUpdateWidget(old);
    if (!_focus.hasFocus && _c.text != widget.text) _c.text = widget.text;
  }

  @override
  void dispose() {
    _c.dispose();
    _focus.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => SizedBox(
    width: 75,
    height: 30,
    child: TextField(
      controller: _c,
      focusNode: _focus,
      enabled: widget.enabled,
      textAlign: TextAlign.right,
      style: const TextStyle(
        color: Tokens.text,
        fontSize: 11,
        fontWeight: FontWeight.w700,
      ),
      decoration: InputDecoration(
        isDense: true,
        filled: true,
        fillColor: Tokens.surface2,
        contentPadding: const EdgeInsets.symmetric(vertical: 4, horizontal: 6),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(5),
          borderSide: BorderSide.none,
        ),
      ),
      onSubmitted: widget.onSubmit,
    ),
  );
}

class _PresetCard extends StatelessWidget {
  const _PresetCard({
    required this.name,
    required this.gains,
    required this.onApply,
    required this.onDelete,
  });

  final String name;
  final List<double> gains;
  final VoidCallback onApply, onDelete;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final text = gains.map(_AudioDspGroupState._signed).join(', ');
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: Tokens.text.withValues(alpha: 0.04),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: Tokens.border),
      ),
      child: Row(
        children: [
          Icon(Icons.equalizer_rounded, color: accent, size: 16),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  name,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 13,
                    fontWeight: FontWeight.bold,
                  ),
                ),
                const SizedBox(height: 2),
                Dim('Gains: [$text] dB', size: 10),
              ],
            ),
          ),
          IconButton(
            icon: const Icon(Icons.play_arrow_rounded, size: 18),
            color: accent,
            tooltip: 'Apply Preset',
            onPressed: onApply,
          ),
          IconButton(
            icon: const Icon(Icons.delete_outline_rounded, size: 18),
            color: const Color(0xFFFF4444),
            tooltip: 'Delete Preset',
            onPressed: onDelete,
          ),
        ],
      ),
    );
  }
}

/// settings.py `EqualizerCurve`: the bands as a smooth curve over a dB
/// grid. A drag moves the node nearest the finger (within 35 px), a tap the
/// band nearest in x (within 45 px).
class EqCurve extends StatefulWidget {
  const EqCurve({
    super.key,
    required this.freqs,
    required this.gains,
    required this.minDb,
    required this.maxDb,
    required this.enabled,
    required this.onChanged,
    required this.onEnd,
  });

  final List<double> freqs, gains;
  final double minDb, maxDb;
  final bool enabled;
  final void Function(int band, double gain) onChanged;
  final VoidCallback onEnd;

  @override
  State<EqCurve> createState() => _EqCurveState();
}

class _EqCurveState extends State<EqCurve> {
  int? _active;
  Size _size = Size.zero;

  _EqGeometry get _geo =>
      _EqGeometry(_size, widget.freqs.length, widget.minDb, widget.maxDb);

  void _tap(Offset p) {
    if (!widget.enabled) return;
    int? best;
    var bestDx = double.infinity;
    for (var i = 0; i < widget.freqs.length; i++) {
      final dx = (p.dx - _geo.x(i)).abs();
      if (dx < bestDx) {
        bestDx = dx;
        best = i;
      }
    }
    if (best != null && bestDx < 45) {
      widget.onChanged(best, _geo.gainAt(p.dy));
      widget.onEnd();
    }
  }

  void _start(Offset p) {
    if (!widget.enabled) return;
    _active = null;
    var best = double.infinity;
    for (var i = 0; i < widget.freqs.length; i++) {
      final dist = (p - Offset(_geo.x(i), _geo.y(widget.gains[i]))).distance;
      if (dist < best && dist < 35) {
        best = dist;
        _active = i;
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Container(
      height: 200,
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: Colors.black,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: Colors.white.withValues(alpha: 0.15)),
      ),
      child: LayoutBuilder(
        builder: (context, box) {
          _size = box.biggest;
          return GestureDetector(
            onTapDown: (e) => _tap(e.localPosition),
            onPanStart: (e) => _start(e.localPosition),
            onPanUpdate: (e) {
              final i = _active;
              if (!widget.enabled || i == null) return;
              widget.onChanged(i, _geo.gainAt(e.localPosition.dy));
            },
            onPanEnd: (_) {
              if (_active != null) widget.onEnd();
              _active = null;
            },
            child: CustomPaint(
              size: _size,
              painter: _EqPainter(
                geo: _geo,
                gains: widget.gains,
                freqs: widget.freqs,
                color: widget.enabled
                    ? accent
                    : Colors.white.withValues(alpha: 0.3),
                fillOpacity: widget.enabled ? 0.18 : 0.05,
              ),
            ),
          );
        },
      ),
    );
  }
}

class _EqGeometry {
  _EqGeometry(this.size, this.n, this.minDb, this.maxDb);

  final Size size;
  final int n;
  final double minDb, maxDb;

  static const _mx = 40.0, _my = 25.0;

  double x(int i) =>
      n <= 1 ? size.width / 2 : _mx + i * (size.width - 2 * _mx) / (n - 1);

  double y(double gain) =>
      size.height -
      _my -
      (gain - minDb) / (maxDb - minDb) * (size.height - 2 * _my);

  double gainAt(double y) {
    final g =
        minDb +
        (size.height - _my - y) / (size.height - 2 * _my) * (maxDb - minDb);
    return math.max(minDb, math.min(maxDb, g));
  }
}

class _EqPainter extends CustomPainter {
  _EqPainter({
    required this.geo,
    required this.gains,
    required this.freqs,
    required this.color,
    required this.fillOpacity,
  }) : _snapshot = List.of(gains);

  final _EqGeometry geo;
  final List<double> gains, freqs;
  final List<double> _snapshot;
  final Color color;
  final double fillOpacity;

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width;
    final faint = Colors.white.withValues(alpha: 0.06);
    final center = geo.y(0);
    canvas.drawLine(
      Offset(0, center),
      Offset(w, center),
      Paint()..color = Colors.white.withValues(alpha: 0.12),
    );
    for (final db in [10.0, -10.0]) {
      final y = geo.y(db);
      for (var x = 0.0; x < w; x += 8) {
        canvas.drawLine(
          Offset(x, y),
          Offset(math.min(x + 4, w), y),
          Paint()..color = faint,
        );
      }
    }
    void label(String text, double x, double y, {double alpha = 0.4}) {
      final tp = TextPainter(
        text: TextSpan(
          text: text,
          style: TextStyle(
            color: Colors.white.withValues(alpha: alpha),
            fontSize: 9,
          ),
        ),
        textDirection: TextDirection.ltr,
      )..layout();
      tp.paint(canvas, Offset(x, y));
    }

    label('+10 dB', 5, geo.y(10) - 12);
    label('0 dB', 5, center - 12);
    label('-10 dB', 5, geo.y(-10) - 12);

    final pts = [
      for (var i = 0; i < _snapshot.length; i++)
        Offset(geo.x(i), geo.y(_snapshot[i])),
    ];
    if (pts.isEmpty) return;
    Path curve(Path p) {
      for (var i = 0; i < pts.length - 1; i++) {
        final p0 = pts[i], p1 = pts[i + 1];
        final cx = p0.dx + (p1.dx - p0.dx) / 2;
        p.cubicTo(cx, p0.dy, cx, p1.dy, p1.dx, p1.dy);
      }
      return p;
    }

    final fill =
        curve(
            Path()
              ..moveTo(pts.first.dx, center)
              ..lineTo(pts.first.dx, pts.first.dy),
          )
          ..lineTo(pts.last.dx, center)
          ..close();
    canvas.drawPath(
      fill,
      Paint()..color = color.withValues(alpha: fillOpacity),
    );
    canvas.drawPath(
      curve(Path()..moveTo(pts.first.dx, pts.first.dy)),
      Paint()
        ..color = color
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2.5
        ..isAntiAlias = true,
    );
    for (var i = 0; i < pts.length; i++) {
      canvas.drawCircle(pts[i], 5, Paint()..color = color);
      canvas.drawCircle(pts[i], 1.5, Paint()..color = Colors.black);
      final f = i < freqs.length ? freqs[i] : 0.0;
      label(
        f >= 1000 ? '${(f / 1000).toStringAsFixed(1)}k' : '${f.truncate()}',
        pts[i].dx - 12,
        size.height - 18,
        alpha: 0.5,
      );
    }
  }

  @override
  bool shouldRepaint(_EqPainter old) =>
      old.color != color ||
      old.fillOpacity != fillOpacity ||
      old.geo.size != geo.size ||
      !listEquals(old._snapshot, _snapshot);
}
