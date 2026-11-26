import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../assistant/llm_engine.dart';
import '../ui/frost.dart';
import '../ui/sliding_segmented.dart';
import '../ui/tokens.dart';
import 'groups.dart';
import 'settings_page.dart';

/// settings.py `_build_assistant_group`, `_save_assistant_settings`,
/// `_test_assistant_connection_async`, and `_refresh_gemini_models`.
class AssistantSettingsGroup extends StatefulWidget {
  const AssistantSettingsGroup({
    super.key,
    required this.deps,
    this.engineFactory,
    this.listModels,
  });

  final SettingsDeps deps;

  /// Optional test overrides for Test Connection and Gemini model refresh.
  final LlmEngine Function({
    required String provider,
    required String apiKey,
    required String model,
    required String ollamaEndpoint,
    required String ollamaModel,
  })?
  engineFactory;
  final Future<List<GeminiModel>> Function(String apiKey)? listModels;

  @override
  State<AssistantSettingsGroup> createState() => _AssistantSettingsGroupState();
}

class _AssistantSettingsGroupState extends State<AssistantSettingsGroup> {
  static const _researchModeOptions = [
    (
      'hybrid',
      'Smart Hybrid',
      'Web snippets + training weights (Recommended, free)',
    ),
    ('weights', 'Training Weights Only', 'Model memory only — fast, 100% free'),
    ('web_snippets', 'Web Search Snippets', 'Live DuckDuckGo Lite lookups'),
    (
      'google_grounding',
      'Google Search Grounding',
      'Native Google Search (requires paid API project)',
    ),
  ];

  static const _personalityPresets = [
    (
      'Default Jarvis',
      "You are Jarvis, a concise, sophisticated AI assistant embedded in the Mai An Lab high-fidelity music app. Address the user as 'sir'.",
      false,
    ),
    (
      'Casual Mate',
      "You are a chill, passionate music enthusiast mate who loves introducing hidden gems. Speak casually and address the user as 'mate'.",
      false,
    ),
    (
      'Sarcastic Critic',
      "You are a witty, slightly sarcastic audiophile and music critic. Give funny commentary on tracks and address the user as 'chief'.",
      false,
    ),
    (
      'ZAX AI (NCR)',
      "You are a ZAX AI Supercomputer from the Fallout universe. You are an unwavering patriot of the New California Republic (NCR), delivering music analysis with wasteland analytical precision and NCR civic pride. Address the user as 'Citizen'.",
      false,
    ),
    ('Reset to Default', '', true),
  ];

  late bool _llmEnabled;
  late String _provider;
  late String _model;
  late String _researchMode;
  late List<(String, String)> _modelOptions;
  final _apiKey = TextEditingController();
  final _ollamaUrl = TextEditingController();
  final _ollamaModel = TextEditingController();
  final _personality = TextEditingController();
  late Map<String, Object?> _baseline;
  bool _loadingModels = false;
  bool _testing = false;

  SettingsDeps get d => widget.deps;

  @override
  void initState() {
    super.initState();
    _loadFromConfig();
    _apiKey.addListener(_changed);
    _ollamaUrl.addListener(_changed);
    _ollamaModel.addListener(_changed);
    _personality.addListener(_changed);
  }

  void _loadFromConfig() {
    final a = d.config.config.assistant;
    _llmEnabled = a.llmEnabled;
    _provider = a.llmProvider.isEmpty ? 'gemini' : a.llmProvider;
    _model = a.geminiModel.isEmpty ? defaultGeminiModel : a.geminiModel;
    _researchMode = a.researchMode.isEmpty ? 'hybrid' : a.researchMode;
    _apiKey.text = a.geminiApiKey;
    _ollamaUrl.text = a.ollamaEndpoint.isEmpty
        ? defaultOllamaEndpoint
        : a.ollamaEndpoint;
    _ollamaModel.text = a.ollamaModel.isEmpty
        ? defaultOllamaModel
        : a.ollamaModel;
    _personality.text = a.personalityPrompt;
    _modelOptions = [
      (defaultGeminiModel, '$defaultGeminiModel (default)'),
      ('gemini-3.5-flash', 'gemini-3.5-flash'),
    ];
    _ensureModelOption(_model);
    _baseline = _snapshot();
  }

  void _ensureModelOption(String modelId, [String note = '']) {
    if (modelId.isEmpty || _modelOptions.any((o) => o.$1 == modelId)) return;
    _modelOptions = [..._modelOptions, (modelId, '$modelId$note')];
  }

  Map<String, Object?> _snapshot() => {
    'llm_enabled': _llmEnabled,
    'provider': _provider,
    'api_key': _apiKey.text.trim(),
    'model': _model,
    'research_mode': _researchMode,
    'ollama_url': _ollamaUrl.text.trim(),
    'ollama_model': _ollamaModel.text.trim(),
    'personality': _personality.text.trim(),
  };

  bool get _dirty => !mapEquals(_snapshot(), _baseline);

  void _changed() => setState(() {});

  @override
  void dispose() {
    _apiKey.dispose();
    _ollamaUrl.dispose();
    _ollamaModel.dispose();
    _personality.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    await d.config.update({
      'assistant': {
        'llm_enabled': _llmEnabled,
        'llm_provider': _provider.isEmpty ? 'gemini' : _provider,
        'gemini_api_key': _apiKey.text.trim(),
        'gemini_model': _model.isEmpty ? defaultGeminiModel : _model,
        'research_mode': _researchMode.isEmpty ? 'hybrid' : _researchMode,
        'ollama_endpoint': _ollamaUrl.text.trim().isEmpty
            ? defaultOllamaEndpoint
            : _ollamaUrl.text.trim(),
        'ollama_model': _ollamaModel.text.trim().isEmpty
            ? defaultOllamaModel
            : _ollamaModel.text.trim(),
        'personality_prompt': _personality.text.trim(),
      },
    });
    if (!mounted) return;
    setState(() => _baseline = _snapshot());
    showSettingsSnack(
      context,
      'Jarvis AI Assistant settings saved successfully.',
    );
  }

  Future<void> _refreshModels({bool announce = true}) async {
    final key = _apiKey.text.trim();
    if (key.isEmpty) {
      if (announce && mounted) {
        showSettingsSnack(context, 'Enter your Gemini API key first.');
      }
      return;
    }
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _loadingModels = true);
    try {
      final listFn = widget.listModels ?? listGeminiModels;
      final models = await listFn(key);
      if (!mounted) return;
      if (models.isEmpty) return;
      final current = _model.isEmpty ? defaultGeminiModel : _model;
      _modelOptions = [
        for (final m in models)
          (m.id, m.id == defaultGeminiModel ? '${m.id} (default)' : m.id),
      ];
      _ensureModelOption(current, ' (not offered for this key)');
      setState(() => _model = current);
      if (announce) {
        showSnackOn(messenger, '${models.length} Gemini models available.');
      }
    } catch (exc) {
      if (announce && mounted) {
        showSnackOn(
          messenger,
          "Couldn't load Gemini models: $exc",
          color: const Color(0xFFFF4444),
        );
      }
    } finally {
      if (mounted) setState(() => _loadingModels = false);
    }
  }

  Future<void> _testConnection() async {
    if (_testing) return;
    final messenger = ScaffoldMessenger.of(context);
    final provider = _provider.isEmpty ? 'gemini' : _provider;
    final factory =
        widget.engineFactory ??
        ({
          required String provider,
          required String apiKey,
          required String model,
          required String ollamaEndpoint,
          required String ollamaModel,
        }) => LlmEngine(
          provider: provider,
          apiKey: apiKey,
          model: model,
          ollamaEndpoint: ollamaEndpoint,
          ollamaModel: ollamaModel,
        );
    final engine = factory(
      provider: provider,
      apiKey: _apiKey.text.trim(),
      model: _model.isEmpty ? defaultGeminiModel : _model,
      ollamaEndpoint: _ollamaUrl.text.trim().isEmpty
          ? defaultOllamaEndpoint
          : _ollamaUrl.text.trim(),
      ollamaModel: _ollamaModel.text.trim().isEmpty
          ? defaultOllamaModel
          : _ollamaModel.text.trim(),
    );
    if (!engine.isConfigured) {
      showSnackOn(messenger, 'Enter your API key or a local endpoint first.');
      return;
    }
    setState(() => _testing = true);
    showSnackOn(messenger, 'Testing connection to $provider…');
    try {
      final res = await engine.chatCompletion([
        {'role': 'user', 'content': 'Reply with the single word: OK'},
      ]);
      if (!mounted) return;
      if (res.success) {
        showSnackOn(messenger, '✓ $provider connection OK.');
        if (provider == 'gemini') {
          await _refreshModels(announce: false);
        }
      } else {
        showSnackOn(messenger, '✗ Connection failed: ${res.errorMessage}');
      }
    } catch (exc) {
      if (mounted) {
        showSnackOn(
          messenger,
          '✗ Connection failed: $exc',
          color: const Color(0xFFFF4444),
        );
      }
    } finally {
      if (mounted) setState(() => _testing = false);
    }
  }

  Future<void> _pickModel() async {
    final accent = Theme.of(context).colorScheme.primary;
    final picked = await showFrostedSheet<String>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (ctx) => SafeArea(
        child: ConstrainedBox(
          constraints: BoxConstraints(
            maxHeight: MediaQuery.sizeOf(ctx).height * 0.6,
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Padding(
                padding: EdgeInsets.fromLTRB(20, 4, 20, 12),
                child: Text(
                  'Gemini Model',
                  style: TextStyle(
                    color: Tokens.text,
                    fontSize: 16,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              Flexible(
                child: ListView(
                  shrinkWrap: true,
                  children: [
                    for (final (id, label) in _modelOptions)
                      ListTile(
                        title: Text(
                          label,
                          style: TextStyle(
                            color: id == _model ? accent : Tokens.text,
                            fontWeight: id == _model
                                ? FontWeight.w600
                                : FontWeight.normal,
                            fontSize: 14,
                          ),
                        ),
                        trailing: id == _model
                            ? Icon(Icons.check_rounded, color: accent, size: 20)
                            : null,
                        onTap: () => Navigator.pop(ctx, id),
                      ),
                  ],
                ),
              ),
              const SizedBox(height: 8),
            ],
          ),
        ),
      ),
    );
    if (picked != null && mounted) setState(() => _model = picked);
  }

  Future<void> _pickResearchMode() async {
    final accent = Theme.of(context).colorScheme.primary;
    final picked = await showFrostedSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Padding(
              padding: EdgeInsets.fromLTRB(20, 4, 20, 12),
              child: Text(
                'Artist Research Paradigm',
                style: TextStyle(
                  color: Tokens.text,
                  fontSize: 16,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
            for (final (id, title, subtitle) in _researchModeOptions)
              ListTile(
                title: Text(
                  title,
                  style: TextStyle(
                    color: id == _researchMode ? accent : Tokens.text,
                    fontWeight: id == _researchMode
                        ? FontWeight.w600
                        : FontWeight.normal,
                    fontSize: 14,
                  ),
                ),
                subtitle: Text(
                  subtitle,
                  style: const TextStyle(color: Tokens.dim, fontSize: 12),
                ),
                trailing: id == _researchMode
                    ? Icon(Icons.check_rounded, color: accent, size: 20)
                    : null,
                onTap: () => Navigator.pop(ctx, id),
              ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    );
    if (picked != null && mounted) setState(() => _researchMode = picked);
  }

  String get _researchLabel => _researchModeOptions
      .firstWhere(
        (o) => o.$1 == _researchMode,
        orElse: () => _researchModeOptions.first,
      )
      .$2;

  String get _modelLabel => _modelOptions
      .firstWhere((o) => o.$1 == _model, orElse: () => (_model, _model))
      .$2;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final isGemini = _provider.toLowerCase() == 'gemini';

    return GroupBody(
      spacing: 20,
      onSave: _dirty ? _save : null,
      saveLabel: 'SAVE ASSISTANT SETTINGS',
      saveIcon: Icons.smart_toy_rounded,
      children: [
        // 1. Intelligence & Provider
        SettingsSection(
          label: 'JARVIS INTELLIGENCE',
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              InsetSwitchRow(
                label: 'Enable LLM Agent Intelligence',
                subtitle:
                    'Conversational AI, smart playlist curation & library analysis',
                value: _llmEnabled,
                onChanged: (v) => setState(() => _llmEnabled = v),
              ),
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 12),
                child: Divider(height: 1, color: Tokens.borderSubtle),
              ),
              const Text(
                'AI Provider',
                style: TextStyle(
                  color: Tokens.dim,
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 8),
              SlidingSegmented<String>(
                selected: isGemini ? 'gemini' : 'ollama',
                accent: accent,
                onSelect: (v) => setState(() => _provider = v),
                segments: const [
                  Segment(
                    value: 'gemini',
                    label: 'Google Gemini',
                    icon: Icons.auto_awesome_rounded,
                  ),
                  Segment(
                    value: 'ollama',
                    label: 'Local Server',
                    icon: Icons.dns_rounded,
                  ),
                ],
              ),
            ],
          ),
        ),

        // 2. Connection
        SettingsSection(
          label: isGemini
              ? 'GEMINI CONNECTION'
              : 'LOCAL SERVER (OLLAMA / LM STUDIO)',
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              if (isGemini) ...[
                ModernTextField(
                  fieldKey: const ValueKey('gemini_api_key'),
                  label: 'Google Gemini API Key',
                  controller: _apiKey,
                  hint: 'Paste your key from aistudio.google.com',
                  obscureToggle: true,
                ),
                const SizedBox(height: 6),
                const Dim(
                  'Free-tier API keys can be generated at aistudio.google.com with no credit card.',
                  size: 11,
                ),
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 12),
                  child: Divider(height: 1, color: Tokens.borderSubtle),
                ),
                Row(
                  children: [
                    Expanded(
                      child: SettingsPickerRow(
                        label: 'Gemini Model',
                        valueText: _modelLabel,
                        onTap: _pickModel,
                      ),
                    ),
                    const SizedBox(width: 4),
                    IconButton(
                      icon: _loadingModels
                          ? SizedBox(
                              width: 18,
                              height: 18,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                color: accent,
                              ),
                            )
                          : const Icon(Icons.refresh_rounded, size: 20),
                      color: accent,
                      tooltip: 'Load the models your key can use',
                      onPressed: _loadingModels
                          ? null
                          : () => unawaited(_refreshModels()),
                    ),
                  ],
                ),
                const Divider(height: 1, color: Tokens.borderSubtle),
                SettingsPickerRow(
                  label: 'Research Paradigm',
                  subtitle: 'How Jarvis researches artists & genres',
                  valueText: _researchLabel,
                  onTap: _pickResearchMode,
                ),
              ] else ...[
                ModernTextField(
                  label: 'Local Endpoint URL',
                  controller: _ollamaUrl,
                  hint: defaultOllamaEndpoint,
                ),
                const SizedBox(height: 14),
                ModernTextField(
                  label: 'Local Model Name',
                  controller: _ollamaModel,
                  hint: defaultOllamaModel,
                ),
              ],
              const SizedBox(height: 14),
              Align(
                alignment: Alignment.centerLeft,
                child: Material(
                  color: accent.withValues(alpha: 0.14),
                  borderRadius: BorderRadius.circular(12),
                  child: InkWell(
                    borderRadius: BorderRadius.circular(12),
                    onTap: _testing ? null : () => unawaited(_testConnection()),
                    child: Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 14,
                        vertical: 10,
                      ),
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(12),
                        border: Border.all(
                          color: accent.withValues(alpha: 0.4),
                        ),
                      ),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          if (_testing)
                            SizedBox(
                              width: 15,
                              height: 15,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                color: accent,
                              ),
                            )
                          else
                            Icon(
                              Icons.wifi_tethering_rounded,
                              size: 17,
                              color: accent,
                            ),
                          const SizedBox(width: 8),
                          Text(
                            _testing ? 'Testing…' : 'Test Connection',
                            style: TextStyle(
                              color: accent,
                              fontSize: 13,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ),
        ),

        // 3. Personality & System Directive
        SettingsSection(
          label: 'PERSONALITY & DIRECTIVE',
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Dim(
                'Customize how Jarvis addresses you, speaks, or behaves. Standard tool-calling directives remain active.',
                size: 12,
              ),
              const SizedBox(height: 12),
              ModernTextField(
                label: 'System Directive Prompt',
                controller: _personality,
                minLines: 3,
                maxLines: 6,
                hint:
                    "You are Jarvis, a concise, sophisticated AI assistant embedded in the Mai An Lab high-fidelity music app. Address the user as 'sir'.",
              ),
              const SizedBox(height: 12),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final (label, text, muted) in _personalityPresets)
                    _PresetChip(
                      label: label,
                      selected: !muted && _personality.text.trim() == text,
                      muted: muted,
                      accent: accent,
                      onTap: () => _personality.text = text,
                    ),
                ],
              ),
            ],
          ),
        ),

        const SizedBox(height: 50),
      ],
    );
  }
}

class _PresetChip extends StatelessWidget {
  const _PresetChip({
    required this.label,
    required this.selected,
    required this.muted,
    required this.accent,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final bool muted;
  final Color accent;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final fg = selected
        ? accent
        : muted
        ? Tokens.dim
        : Tokens.text;
    final bg = selected ? accent.withValues(alpha: 0.18) : Tokens.surface2;
    final border = selected
        ? accent.withValues(alpha: 0.65)
        : muted
        ? Tokens.borderSubtle
        : Tokens.border;
    return Material(
      color: bg,
      borderRadius: BorderRadius.circular(10),
      child: InkWell(
        borderRadius: BorderRadius.circular(10),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 7),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: border),
          ),
          child: Text(
            label,
            style: TextStyle(
              color: fg,
              fontSize: 12,
              fontWeight: selected ? FontWeight.w700 : FontWeight.w500,
            ),
          ),
        ),
      ),
    );
  }
}
