import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../assistant/llm_engine.dart';
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
  static const _providerOptions = [
    ('gemini', 'Google Gemini API (Cloud)'),
    ('ollama', 'Local Server (Ollama / LM Studio)'),
  ];

  static const _researchModeOptions = [
    ('hybrid', 'Smart Hybrid (Web Snippets + Training Weights) [Recommended]'),
    ('weights', 'Training Weights Only (Memory, Reddit, Forums)'),
    ('web_snippets', 'Web Search Snippets (DuckDuckGo Lite)'),
    ('google_grounding', 'Google Search Grounding (Requires Paid Project)'),
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

  InputDecoration _decoration(
    BuildContext context,
    String label, {
    String? hint,
  }) {
    final accent = Theme.of(context).colorScheme.primary;
    OutlineInputBorder border(Color c) => OutlineInputBorder(
      borderRadius: BorderRadius.circular(10),
      borderSide: BorderSide(color: c),
    );
    return InputDecoration(
      labelText: label,
      hintText: hint,
      labelStyle: TextStyle(color: accent, fontSize: 11),
      hintStyle: const TextStyle(color: Tokens.dim, fontSize: 12),
      filled: true,
      fillColor: Tokens.surface2,
      enabledBorder: border(Tokens.border),
      focusedBorder: border(accent),
      border: border(Tokens.border),
      isDense: true,
    );
  }

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
        const Dim(
          'Configure Large Language Model integration for your voice/chat assistant Jarvis.',
        ),
        SettingsCard(
          radius: 10,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const CardTitle(
                Icons.smart_toy_rounded,
                'Jarvis AI Intelligence',
              ),
              const SizedBox(height: 10),
              const Dim(
                'Enable conversational AI, smart playlist curation, and deep library analysis.',
              ),
              const SizedBox(height: 10),
              SwitchRow(
                value: _llmEnabled,
                label: 'Enable LLM Agent Intelligence',
                onChanged: (v) => setState(() => _llmEnabled = v),
              ),
              const SizedBox(height: 12),
              SettingsDropdown(
                label: 'AI Provider',
                value: _provider,
                options: _providerOptions,
                onChanged: (v) => setState(() => _provider = v),
              ),
              const SizedBox(height: 12),
              if (isGemini) ...[
                TextField(
                  controller: _apiKey,
                  style: const TextStyle(color: Tokens.text, fontSize: 13),
                  decoration: _decoration(
                    context,
                    'Google Gemini API Key',
                    hint: 'Paste your key from aistudio.google.com',
                  ),
                ),
                const SizedBox(height: 8),
                const Dim(
                  '• Free tier API keys can be generated at aistudio.google.com with 0 cost & 0 credit card.',
                  size: 11,
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: SettingsDropdown(
                        label: 'Gemini Model',
                        value: _model,
                        options: _modelOptions,
                        onChanged: (v) => setState(() => _model = v),
                      ),
                    ),
                    const SizedBox(width: 6),
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
                          : const Icon(Icons.refresh_rounded),
                      color: accent,
                      tooltip: 'Load the models your key can use',
                      onPressed: _loadingModels
                          ? null
                          : () => unawaited(_refreshModels()),
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                SettingsDropdown(
                  label: 'Artist Research Paradigm',
                  value: _researchMode,
                  options: _researchModeOptions,
                  onChanged: (v) => setState(() => _researchMode = v),
                ),
                const SizedBox(height: 8),
                const Dim(
                  '• Smart Hybrid & Training Weights work 100% free with no credit card required.',
                  size: 11,
                ),
              ] else ...[
                TextField(
                  controller: _ollamaUrl,
                  style: const TextStyle(color: Tokens.text, fontSize: 13),
                  decoration: _decoration(
                    context,
                    'Local Endpoint URL',
                    hint: defaultOllamaEndpoint,
                  ),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: _ollamaModel,
                  style: const TextStyle(color: Tokens.text, fontSize: 13),
                  decoration: _decoration(
                    context,
                    'Local Model Name',
                    hint: defaultOllamaModel,
                  ),
                ),
              ],
              const Divider(color: Tokens.border, height: 24),
              const Text(
                'Jarvis Personality & System Directive',
                style: TextStyle(
                  color: Tokens.text,
                  fontSize: 13,
                  fontWeight: FontWeight.bold,
                ),
              ),
              const SizedBox(height: 6),
              const Dim(
                'Customize how Jarvis addresses you, speaks, or behaves. Standard tool-calling directives remain active.',
                size: 11,
              ),
              const SizedBox(height: 10),
              TextField(
                controller: _personality,
                minLines: 3,
                maxLines: 6,
                style: const TextStyle(color: Tokens.text, fontSize: 12),
                decoration: _decoration(
                  context,
                  'Personality / System Directive Prompt',
                  hint:
                      "You are Jarvis, a concise, sophisticated AI assistant embedded in the Mai An Lab high-fidelity music app. Address the user as 'sir'.",
                ),
              ),
              const SizedBox(height: 10),
              Wrap(
                spacing: 6,
                runSpacing: 6,
                children: [
                  for (final (label, text, muted) in _personalityPresets)
                    OutlinedButton(
                      style: OutlinedButton.styleFrom(
                        foregroundColor: muted ? Tokens.dim : accent,
                        side: BorderSide(
                          color: (muted ? Tokens.dim : accent).withValues(
                            alpha: muted ? 0.2 : 0.4,
                          ),
                        ),
                      ),
                      onPressed: () {
                        _personality.text = text;
                      },
                      child: Text(label),
                    ),
                ],
              ),
              const SizedBox(height: 14),
              Align(
                alignment: Alignment.centerLeft,
                child: OutlinedButton.icon(
                  icon: const Icon(Icons.wifi_tethering_rounded),
                  label: const Text('Test Connection'),
                  style: OutlinedButton.styleFrom(
                    foregroundColor: accent,
                    side: BorderSide(color: accent),
                  ),
                  onPressed: _testing
                      ? null
                      : () => unawaited(_testConnection()),
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 50),
      ],
    );
  }
}
