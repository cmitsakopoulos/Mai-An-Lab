import 'dart:async';

import 'package:flutter/material.dart';

import '../assistant/llm_engine.dart';
import '../ui/tokens.dart';
import 'settings_kit.dart';
import 'settings_page.dart';

/// settings.py `_build_assistant_group`, `_save_assistant_settings`,
/// `_test_assistant_connection_async`, and `_refresh_gemini_models`.
///
/// The switch, provider and pickers apply at once; typed values (the API
/// key, the local server, the directive) each have a Save row.
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

  /// The typed values as last saved.
  late String _baseKey, _baseUrl, _baseOllamaModel, _basePersonality;
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
    _apiKey.text = _baseKey = a.geminiApiKey;
    _ollamaUrl.text = _baseUrl = a.ollamaEndpoint.isEmpty
        ? defaultOllamaEndpoint
        : a.ollamaEndpoint;
    _ollamaModel.text = _baseOllamaModel = a.ollamaModel.isEmpty
        ? defaultOllamaModel
        : a.ollamaModel;
    _personality.text = _basePersonality = a.personalityPrompt;
    _modelOptions = [
      (defaultGeminiModel, '$defaultGeminiModel (default)'),
      ('gemini-3.5-flash', 'gemini-3.5-flash'),
    ];
    _ensureModelOption(_model);
  }

  void _ensureModelOption(String modelId, [String note = '']) {
    if (modelId.isEmpty || _modelOptions.any((o) => o.$1 == modelId)) return;
    _modelOptions = [..._modelOptions, (modelId, '$modelId$note')];
  }

  bool get _keyDirty => _apiKey.text.trim() != _baseKey.trim();
  bool get _serverDirty =>
      _ollamaUrl.text.trim() != _baseUrl.trim() ||
      _ollamaModel.text.trim() != _baseOllamaModel.trim();
  bool get _personalityDirty =>
      _personality.text.trim() != _basePersonality.trim();

  void _changed() => setState(() {});

  @override
  void dispose() {
    _apiKey.dispose();
    _ollamaUrl.dispose();
    _ollamaModel.dispose();
    _personality.dispose();
    super.dispose();
  }

  Future<void> _write(Map<String, Object?> assistant) =>
      d.config.update({'assistant': assistant});

  Future<void> _onEnabled(bool v) async {
    setState(() => _llmEnabled = v);
    await _write({'llm_enabled': v});
  }

  Future<void> _onProvider(String v) async {
    setState(() => _provider = v);
    await _write({'llm_provider': v});
  }

  Future<void> _onModel(String v) async {
    setState(() => _model = v);
    await _write({'gemini_model': v});
  }

  Future<void> _onResearchMode(String v) async {
    setState(() => _researchMode = v);
    await _write({'research_mode': v});
  }

  Future<void> _saveKey() async {
    final key = _apiKey.text.trim();
    await _write({'gemini_api_key': key});
    if (!mounted) return;
    setState(() => _baseKey = key);
    showSettingsSnack(context, 'Gemini API key saved.');
  }

  Future<void> _saveServer() async {
    final url = _ollamaUrl.text.trim().isEmpty
        ? defaultOllamaEndpoint
        : _ollamaUrl.text.trim();
    final model = _ollamaModel.text.trim().isEmpty
        ? defaultOllamaModel
        : _ollamaModel.text.trim();
    await _write({'ollama_endpoint': url, 'ollama_model': model});
    if (!mounted) return;
    setState(() {
      _baseUrl = _ollamaUrl.text = url;
      _baseOllamaModel = _ollamaModel.text = model;
    });
    showSettingsSnack(context, 'Local server saved.');
  }

  Future<void> _savePersonality() async {
    final text = _personality.text.trim();
    await _write({'personality_prompt': text});
    if (!mounted) return;
    setState(() => _basePersonality = text);
    showSettingsSnack(context, 'System directive saved.');
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

  static final _researchOptions = [
    for (final (id, title, subtitle) in _researchModeOptions)
      SettingsOption(id, title, subtitle: subtitle),
  ];

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final isGemini = _provider.toLowerCase() == 'gemini';
    final spinner = SizedBox(
      width: 18,
      height: 18,
      child: CircularProgressIndicator(strokeWidth: 2, color: accent),
    );

    return GroupBody(
      spacing: 26,
      children: [
        SettingsSection(
          header: 'Jarvis Intelligence',
          children: [
            SettingsTile.toggle(
              title: 'Enable LLM Agent Intelligence',
              subtitle:
                  'Conversational AI, smart playlist curation & library '
                  'analysis',
              value: _llmEnabled,
              onChanged: _onEnabled,
            ),
            SettingsSegmentedTile<String>(
              title: 'AI Provider',
              inline: false,
              value: isGemini ? 'gemini' : 'ollama',
              onChanged: _onProvider,
              options: const [
                SettingsOption(
                  'gemini',
                  'Google Gemini',
                  icon: Icons.auto_awesome_rounded,
                ),
                SettingsOption(
                  'ollama',
                  'Local Server',
                  icon: Icons.dns_rounded,
                ),
              ],
            ),
          ],
        ),
        if (isGemini)
          SettingsSection(
            header: 'Gemini Connection',
            footer:
                'Free-tier API keys can be generated at aistudio.google.com '
                'with no credit card.',
            children: [
              SettingsCustomTile(
                child: ModernTextField(
                  fieldKey: const ValueKey('gemini_api_key'),
                  label: 'Google Gemini API Key',
                  controller: _apiKey,
                  hint: 'Paste your key from aistudio.google.com',
                  obscureToggle: true,
                ),
              ),
              SettingsTile.action(
                leading: const SettingsIcon(Icons.key_rounded),
                title: 'Save API Key',
                onTap: _keyDirty ? _saveKey : null,
              ),
              SettingsPickerTile<String>(
                title: 'Gemini Model',
                value: _model,
                options: [
                  for (final (id, label) in _modelOptions)
                    SettingsOption(id, label),
                ],
                onChanged: _onModel,
              ),
              SettingsTile(
                leading: const SettingsIcon(Icons.refresh_rounded),
                title: 'Load Available Models',
                subtitle: 'The models your key can use',
                trailing: _loadingModels ? spinner : null,
                onTap: _loadingModels
                    ? null
                    : () => unawaited(_refreshModels()),
              ),
              SettingsPickerTile<String>(
                title: 'Research Paradigm',
                subtitle: 'How Jarvis researches artists & genres',
                sheetTitle: 'Artist Research Paradigm',
                value: _researchMode,
                options: _researchOptions,
                onChanged: _onResearchMode,
              ),
              _testRow(spinner),
            ],
          )
        else
          SettingsSection(
            header: 'Local Server (Ollama / LM Studio)',
            children: [
              SettingsCustomTile(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
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
                ),
              ),
              SettingsTile.action(
                leading: const SettingsIcon(Icons.save_rounded),
                title: 'Save Server',
                onTap: _serverDirty ? _saveServer : null,
              ),
              _testRow(spinner),
            ],
          ),
        SettingsSection(
          header: 'Personality & Directive',
          footer:
              'Customize how Jarvis addresses you, speaks, or behaves. '
              'Standard tool-calling directives remain active.',
          children: [
            SettingsCustomTile(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  ModernTextField(
                    label: 'System Directive Prompt',
                    controller: _personality,
                    minLines: 3,
                    maxLines: 6,
                    hint:
                        "You are Jarvis, a concise, sophisticated AI "
                        "assistant embedded in the Mai An Lab high-fidelity "
                        "music app. Address the user as 'sir'.",
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
            SettingsTile.action(
              leading: const SettingsIcon(Icons.save_rounded),
              title: 'Save Directive',
              onTap: _personalityDirty ? _savePersonality : null,
            ),
          ],
        ),
      ],
    );
  }

  Widget _testRow(Widget spinner) => SettingsTile(
    leading: const SettingsIcon(Icons.wifi_tethering_rounded),
    title: _testing ? 'Testing…' : 'Test Connection',
    subtitle: 'Uses what is typed above, saved or not',
    trailing: _testing ? spinner : null,
    onTap: _testing ? null : () => unawaited(_testConnection()),
  );
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
