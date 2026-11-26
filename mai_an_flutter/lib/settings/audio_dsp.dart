import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../analysis/library_analysis.dart';
import '../core/py_num.dart';
import '../library/library_dialogs.dart' show confirmDelete;
import '../player/dsp.dart';
import '../ui/sliding_segmented.dart';
import '../ui/tokens.dart';
import 'groups.dart' show InsetSwitchRow, LegacySettingsSection;
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
  late bool _autoAnalyse;

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
    _autoAnalyse = cfg.autoAnalyse;
    _custom = Map.of(cfg.customPresets);
    _refreshPresets(cfg.activePreset);
    _loadGains();
    _loadBands();
    _checkDynamism();
    unawaited(d.library.refreshDspCoverage());
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

  Future<void> _onAutoAnalyse(bool v) async {
    setState(() => _autoAnalyse = v);
    await _write({'auto_analyse': v});
  }

  Future<void> _runAnalysis(DspEntry entry) async {
    await d.library.analysis.run(
      entry,
      notice: (n) {
        if (!mounted) return;
        showSettingsSnack(context, n.text);
      },
    );
    await d.library.refreshDspCoverage();
    if (mounted) await _checkDynamism();
  }

  Future<void> _confirmRebuildAll() async {
    final total = d.library.dspTotal;
    final ok = await confirmDelete(
      context,
      title: 'Rebuild Acoustic Index?',
      message:
          'This will purge all existing DSP features and recompute '
          '${total > 0 ? 'all $total tracks' : 'every track'} in your library '
          'from scratch. Depending on library size, this may take a while.\n\n'
          'Your audio files and play counts will NOT be touched.',
      confirmLabel: 'Rebuild All',
    );
    if (!ok || !mounted) return;
    await _runAnalysis(DspEntry.recomputeAll);
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
    final hasNonZero = _gains.any((g) => g.abs() >= 0.05);

    return GroupBody(
      spacing: 20,
      children: [
        // 1. Acoustic Index (DSP feature coverage & auto-analysis)
        ListenableBuilder(
          listenable: Listenable.merge([d.library, d.library.analysis]),
          builder: (context, _) {
            final lib = d.library;
            final analysis = lib.analysis;
            final total = lib.dspTotal;
            final missing = lib.dspMissing;
            final analysed = lib.dspAnalysed;
            final ratio = total > 0 ? (analysed / total).clamp(0.0, 1.0) : 0.0;
            final allReady = total > 0 && missing == 0;
            final canRun = lib.canWrite && total > 0;

            final badgeText = analysis.running
                ? 'Analysing…'
                : total == 0
                ? 'No Tracks'
                : allReady
                ? 'All Ready'
                : '$missing pending';
            final badgeColor = analysis.running
                ? accent
                : allReady
                ? Tokens.accentGreen
                : Tokens.amber;

            return LegacySettingsSection(
              label: 'ACOUSTIC INDEX',
              trailing: Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                decoration: BoxDecoration(
                  color: badgeColor.withValues(alpha: 0.16),
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Text(
                  badgeText,
                  style: TextStyle(
                    color: badgeColor,
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          total == 0
                              ? 'Add tracks to build acoustic features'
                              : '$analysed / $total tracks indexed',
                          style: const TextStyle(
                            color: Tokens.text,
                            fontSize: 14,
                            fontWeight: FontWeight.w600,
                            fontFeatures: [FontFeature.tabularFigures()],
                          ),
                        ),
                      ),
                      if (total > 0)
                        Text(
                          '${(ratio * 100).round()}%',
                          style: TextStyle(
                            color: badgeColor,
                            fontSize: 12,
                            fontWeight: FontWeight.w700,
                            fontFeatures: const [FontFeature.tabularFigures()],
                          ),
                        ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  ClipRRect(
                    borderRadius: BorderRadius.circular(3),
                    child: LinearProgressIndicator(
                      value: analysis.running ? analysis.progress : ratio,
                      minHeight: 5,
                      color: badgeColor,
                      backgroundColor: Tokens.surface2,
                    ),
                  ),
                  const SizedBox(height: 6),
                  Dim(
                    analysis.running
                        ? (analysis.status.isEmpty
                              ? 'Analysing acoustic features…'
                              : analysis.status)
                        : 'Powers Auto-Play similarity walks, Dynamic Punchiness, and Acoustic Network.',
                    size: 11,
                  ),
                  const SizedBox(height: 12),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      if (analysis.running)
                        Material(
                          color: Tokens.accentRed.withValues(alpha: 0.16),
                          borderRadius: BorderRadius.circular(10),
                          child: InkWell(
                            borderRadius: BorderRadius.circular(10),
                            onTap: analysis.cancel,
                            child: Container(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 14,
                                vertical: 9,
                              ),
                              decoration: BoxDecoration(
                                borderRadius: BorderRadius.circular(10),
                                border: Border.all(
                                  color: Tokens.accentRed.withValues(
                                    alpha: 0.45,
                                  ),
                                ),
                              ),
                              child: const Text(
                                'Cancel Analysis',
                                style: TextStyle(
                                  color: Tokens.accentRed,
                                  fontSize: 12,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                            ),
                          ),
                        )
                      else if (missing > 0) ...[
                        Material(
                          color: canRun ? accent : Tokens.surface2,
                          borderRadius: BorderRadius.circular(10),
                          child: InkWell(
                            borderRadius: BorderRadius.circular(10),
                            onTap: canRun
                                ? () =>
                                      unawaited(_runAnalysis(DspEntry.settings))
                                : null,
                            child: Padding(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 14,
                                vertical: 9,
                              ),
                              child: Text(
                                'Analyse $missing Track${missing == 1 ? '' : 's'}',
                                style: TextStyle(
                                  color: canRun ? Colors.black : Tokens.dim,
                                  fontSize: 12,
                                  fontWeight: FontWeight.w800,
                                ),
                              ),
                            ),
                          ),
                        ),
                        if (analysed > 0)
                          TextButton(
                            onPressed: canRun
                                ? () => unawaited(_confirmRebuildAll())
                                : null,
                            child: const Text(
                              'Rebuild All',
                              style: TextStyle(color: Tokens.dim, fontSize: 12),
                            ),
                          ),
                      ] else
                        OutlinedButton.icon(
                          icon: const Icon(Icons.refresh_rounded, size: 16),
                          label: const Text(
                            'Rebuild Acoustic Index',
                            style: TextStyle(fontSize: 12),
                          ),
                          style: OutlinedButton.styleFrom(
                            foregroundColor: Tokens.dim,
                            side: const BorderSide(color: Tokens.borderSubtle),
                            shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(10),
                            ),
                          ),
                          onPressed: canRun
                              ? () => unawaited(_confirmRebuildAll())
                              : null,
                        ),
                    ],
                  ),
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 10),
                    child: Divider(height: 1, color: Tokens.borderSubtle),
                  ),
                  InsetSwitchRow(
                    label: 'Auto-analyse new tracks',
                    subtitle:
                        'Automatically compute DSP features after scans & downloads',
                    value: _autoAnalyse,
                    onChanged: _onAutoAnalyse,
                  ),
                ],
              ),
            );
          },
        ),

        // 2. Dynamic Punchiness
        LegacySettingsSection(
          label: 'DYNAMIC PUNCHINESS',
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Opacity(
                opacity: _dynamismAvailable ? 1 : 0.45,
                child: InsetSwitchRow(
                  label: 'Enable Dynamism Enhancement',
                  subtitle:
                      'Automatically boost output gain from track energy & beat strength',
                  value: _dynamism,
                  onChanged: _dynamismAvailable ? _onDynamism : null,
                ),
              ),
              if (_dynamismNote != null) ...[
                const SizedBox(height: 10),
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 10,
                    vertical: 8,
                  ),
                  decoration: BoxDecoration(
                    color: Tokens.amber.withValues(alpha: 0.12),
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(
                      color: Tokens.amber.withValues(alpha: 0.35),
                    ),
                  ),
                  child: Dim(_dynamismNote!, size: 11, color: Tokens.amber),
                ),
              ],
              _BoostCard(deps: d),
            ],
          ),
        ),

        // 2. 5-Band Equalizer
        LegacySettingsSection(
          label: 'EQUALIZER',
          trailing: _eq && hasNonZero
              ? GestureDetector(
                  onTap: () => _onPreset('Flat'),
                  child: Text(
                    'Reset Flat',
                    style: TextStyle(
                      color: accent,
                      fontSize: 11,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                )
              : null,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              InsetSwitchRow(
                label: 'Enable 5-Band Equalizer',
                subtitle:
                    'Shape frequency bands to match your headphones or genre',
                value: _eq,
                onChanged: _onEqualizer,
              ),
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 12),
                child: Divider(height: 1, color: Tokens.borderSubtle),
              ),
              SlidingSegmented<String>(
                selected: _type,
                accent: accent,
                onSelect: _onType,
                segments: const [
                  Segment(value: 'system', label: 'System Presets'),
                  Segment(value: 'custom', label: 'Custom Presets'),
                ],
              ),
              const SizedBox(height: 12),
              SizedBox(
                height: 36,
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: _options.length,
                  separatorBuilder: (_, _) => const SizedBox(width: 8),
                  itemBuilder: (context, idx) {
                    final name = _options[idx];
                    final selected = name == _preset;
                    return Material(
                      color: selected
                          ? accent.withValues(alpha: 0.20)
                          : Tokens.surface2,
                      borderRadius: BorderRadius.circular(10),
                      child: InkWell(
                        borderRadius: BorderRadius.circular(10),
                        onTap: () => _onPreset(name),
                        child: Container(
                          padding: const EdgeInsets.symmetric(horizontal: 13),
                          alignment: Alignment.center,
                          decoration: BoxDecoration(
                            borderRadius: BorderRadius.circular(10),
                            border: Border.all(
                              color: selected
                                  ? accent.withValues(alpha: 0.7)
                                  : Tokens.borderSubtle,
                            ),
                          ),
                          child: Text(
                            name,
                            style: TextStyle(
                              color: selected ? accent : Tokens.text,
                              fontSize: 12,
                              fontWeight: selected
                                  ? FontWeight.w700
                                  : FontWeight.w500,
                            ),
                          ),
                        ),
                      ),
                    );
                  },
                ),
              ),
              const SizedBox(height: 14),
              EqCurve(
                freqs: _freqs,
                gains: _gains,
                minDb: _minDb,
                maxDb: _maxDb,
                enabled: _eq,
                onChanged: _onCurve,
                onEnd: () => _saveGainsAs('Custom'),
              ),
              const SizedBox(height: 16),
              for (var i = 0; i < _gains.length; i++)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Row(
                    children: [
                      SizedBox(
                        width: 56,
                        child: Text(
                          _freqLabel(_freqs[i]),
                          style: const TextStyle(
                            color: Tokens.dim,
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                            fontFeatures: [FontFeature.tabularFigures()],
                          ),
                        ),
                      ),
                      Expanded(
                        child: SliderTheme(
                          data: SliderTheme.of(context).copyWith(
                            activeTrackColor: _eq ? accent : Tokens.dim,
                            inactiveTrackColor: Tokens.surface2,
                            thumbColor: _eq ? Tokens.text : Tokens.dim,
                            trackHeight: 4,
                          ),
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
                const Padding(
                  padding: EdgeInsets.only(top: 4, bottom: 12),
                  child: Divider(height: 1, color: Tokens.borderSubtle),
                ),
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
                          hintText: 'Save Custom Preset',
                          hintStyle: const TextStyle(
                            color: Tokens.textTertiary,
                            fontSize: 13,
                          ),
                          filled: true,
                          fillColor: Tokens.surface2,
                          contentPadding: const EdgeInsets.symmetric(
                            horizontal: 14,
                            vertical: 11,
                          ),
                          enabledBorder: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(12),
                            borderSide: const BorderSide(
                              color: Tokens.borderSubtle,
                            ),
                          ),
                          focusedBorder: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(12),
                            borderSide: BorderSide(color: accent, width: 1.3),
                          ),
                          border: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(12),
                            borderSide: const BorderSide(
                              color: Tokens.borderSubtle,
                            ),
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(width: 10),
                    Material(
                      color: accent,
                      borderRadius: BorderRadius.circular(12),
                      child: InkWell(
                        borderRadius: BorderRadius.circular(12),
                        onTap: _savePreset,
                        child: const Padding(
                          padding: EdgeInsets.symmetric(
                            horizontal: 18,
                            vertical: 12,
                          ),
                          child: Text(
                            'SAVE',
                            style: TextStyle(
                              color: Colors.black,
                              fontSize: 12,
                              fontWeight: FontWeight.w800,
                              letterSpacing: 0.5,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
                if (customNames.isNotEmpty) ...[
                  const SizedBox(height: 16),
                  const AccentLabel('SAVED CUSTOM PRESETS', size: 11),
                  const SizedBox(height: 6),
                  for (final name in customNames)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: _PresetCard(
                        name: name,
                        gains: _custom[name]!,
                        active: name == _preset,
                        onApply: () => _applyPreset(name),
                        onDelete: () => _deletePreset(name),
                      ),
                    ),
                ],
              ],
            ],
          ),
        ),
        const SizedBox(height: 24),
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
                            fontFeatures: [FontFeature.tabularFigures()],
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
    width: 72,
    height: 32,
    child: TextField(
      controller: _c,
      focusNode: _focus,
      enabled: widget.enabled,
      textAlign: TextAlign.right,
      style: const TextStyle(
        color: Tokens.text,
        fontSize: 11,
        fontWeight: FontWeight.w700,
        fontFeatures: [FontFeature.tabularFigures()],
      ),
      decoration: InputDecoration(
        isDense: true,
        filled: true,
        fillColor: Tokens.surface2,
        contentPadding: const EdgeInsets.symmetric(vertical: 7, horizontal: 8),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(8),
          borderSide: const BorderSide(color: Tokens.borderSubtle),
        ),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(8),
          borderSide: const BorderSide(color: Tokens.borderSubtle),
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
    required this.active,
    required this.onApply,
    required this.onDelete,
  });

  final String name;
  final List<double> gains;
  final bool active;
  final VoidCallback onApply, onDelete;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final text = gains.map(_AudioDspGroupState._signed).join(', ');
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: active ? accent.withValues(alpha: 0.12) : Tokens.surface2,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(
          color: active ? accent.withValues(alpha: 0.5) : Tokens.borderSubtle,
        ),
      ),
      child: Row(
        children: [
          Icon(Icons.equalizer_rounded, color: accent, size: 17),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  name,
                  style: TextStyle(
                    color: active ? accent : Tokens.text,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 2),
                Dim('[$text] dB', size: 10),
              ],
            ),
          ),
          IconButton(
            icon: const Icon(Icons.play_arrow_rounded, size: 19),
            color: accent,
            tooltip: 'Apply Preset',
            onPressed: onApply,
          ),
          IconButton(
            icon: const Icon(Icons.delete_outline_rounded, size: 19),
            color: Tokens.accentRed,
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
      height: 196,
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        gradient: const LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [Color(0xFF141416), Color(0xFF0A0A0C)],
        ),
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: Tokens.borderSubtle),
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
