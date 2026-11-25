import 'package:flutter/material.dart';

import '../scan/storage_paths.dart';
import '../settings/config_controller.dart';
import '../settings/folder_browser.dart';
import '../ui/tokens.dart';
import 'onboarding.dart';

/// onboarding_modal.py `OnboardingWizardModal`: three slides (what the app
/// does; the library folder and optional streaming accounts; accent and
/// startup screen), shown once on a fresh install. Not dismissible: it ends
/// with ENTER MAI-AN LAB, which saves everything.
///
/// Returns the chosen startup page ("Library", "Search" or "Jarvis") and
/// the folder saved, once finished.
Future<(String, String)?> showOnboardingWizard(
  BuildContext context, {
  required String dataDir,
  required ConfigController config,
  required StoragePaths storage,
}) => showDialog<(String, String)>(
  context: context,
  barrierDismissible: false,
  builder: (_) => PopScope(
    canPop: false,
    child: OnboardingWizard(dataDir: dataDir, config: config, storage: storage),
  ),
);

class OnboardingWizard extends StatefulWidget {
  const OnboardingWizard({
    super.key,
    required this.dataDir,
    required this.config,
    required this.storage,
  });

  final String dataDir;
  final ConfigController config;
  final StoragePaths storage;

  @override
  State<OnboardingWizard> createState() => _OnboardingWizardState();
}

Color _hex(String h) => Color(int.parse('FF${h.substring(1)}', radix: 16));

class _OnboardingWizardState extends State<OnboardingWizard> {
  int slide = 0;
  String accentHex = '#FFD60A';
  String startup = 'Library';
  String streamingTab = 'qobuz';
  late final _folder = TextEditingController(
    text: suggestedMusicDir(widget.storage),
  );
  final _qUser = TextEditingController();
  final _qToken = TextEditingController();
  final _arl = TextEditingController();
  bool _finishing = false;

  Color get accent => _hex(accentHex);

  @override
  void dispose() {
    for (final c in [_folder, _qUser, _qToken, _arl]) {
      c.dispose();
    }
    super.dispose();
  }

  void _go(int i) => setState(() => slide = i);

  Future<void> _browse() async {
    final path = await showFolderBrowser(
      context,
      label: 'Select Music Library Folder',
      isPrivate: widget.storage.isAppPrivate,
    );
    if (path != null && mounted) setState(() => _folder.text = path);
  }

  Future<void> _finish() async {
    if (_finishing) return;
    setState(() => _finishing = true);
    final folder = await finishOnboarding(
      dataDir: widget.dataDir,
      config: widget.config,
      storage: widget.storage,
      choices: OnboardingChoices(
        accent: accentHex,
        startup: startup,
        folder: _folder.text.trim(),
        qobuzUser: _qUser.text,
        qobuzToken: _qToken.text,
        deezerArl: _arl.text,
      ),
    );
    if (mounted) Navigator.of(context).pop((startup, folder));
  }

  @override
  Widget build(BuildContext context) {
    final slides = [_welcome, _sources, _personalize];
    return Dialog(
      backgroundColor: Colors.transparent,
      insetPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 24),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        constraints: const BoxConstraints(maxWidth: 420, maxHeight: 560),
        padding: const EdgeInsets.fromLTRB(18, 18, 18, 14),
        decoration: BoxDecoration(
          color: Tokens.surface,
          borderRadius: BorderRadius.circular(22),
          border: Border.all(color: Tokens.text.withValues(alpha: 0.18)),
          boxShadow: [
            BoxShadow(
              blurRadius: 28,
              color: accent.withValues(alpha: 0.18),
              offset: const Offset(0, 8),
            ),
          ],
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Flexible(
              child: AnimatedSwitcher(
                duration: const Duration(milliseconds: 220),
                child: KeyedSubtree(
                  key: ValueKey(slide),
                  child: slides[slide](),
                ),
              ),
            ),
            const SizedBox(height: 12),
            _dots(),
          ],
        ),
      ),
    );
  }

  // ── pieces ────────────────────────────────────────────────────────────────

  Widget _dots() => Row(
    mainAxisAlignment: MainAxisAlignment.center,
    children: [
      for (var i = 0; i < 3; i++)
        AnimatedContainer(
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOut,
          margin: const EdgeInsets.symmetric(horizontal: 4),
          width: i == slide ? 22 : 7,
          height: 7,
          decoration: BoxDecoration(
            color: i == slide ? accent : Tokens.text.withValues(alpha: 0.25),
            borderRadius: BorderRadius.circular(4),
          ),
        ),
    ],
  );

  Widget _header(IconData icon, Color color, String title, String sub, int n) =>
      Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Row(
          children: [
            Container(
              width: 40,
              height: 40,
              decoration: BoxDecoration(
                color: color.withValues(alpha: 0.14),
                borderRadius: BorderRadius.circular(12),
              ),
              child: Icon(icon, color: color, size: 22),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: const TextStyle(
                      color: Tokens.text,
                      fontSize: 17,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    sub,
                    style: const TextStyle(color: Tokens.dim, fontSize: 11),
                  ),
                ],
              ),
            ),
            Container(
              padding: const EdgeInsets.symmetric(vertical: 3, horizontal: 8),
              decoration: BoxDecoration(
                color: accent.withValues(alpha: 0.12),
                border: Border.all(color: accent.withValues(alpha: 0.24)),
                borderRadius: BorderRadius.circular(8),
              ),
              child: Text(
                'STEP $n OF 3',
                style: TextStyle(
                  color: accent,
                  fontSize: 9,
                  fontWeight: FontWeight.w800,
                ),
              ),
            ),
          ],
        ),
      );

  Widget _divider() =>
      Divider(color: Tokens.text.withValues(alpha: 0.12), height: 1);

  Widget _primary(String label, IconData icon, VoidCallback? onTap) => SizedBox(
    height: 44,
    child: FilledButton(
      onPressed: onTap,
      style: FilledButton.styleFrom(
        backgroundColor: accent,
        foregroundColor: Tokens.bg,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        padding: const EdgeInsets.symmetric(horizontal: 12),
      ),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.center,
        mainAxisSize: MainAxisSize.min,
        children: [
          Flexible(
            child: Text(
              label,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w800),
            ),
          ),
          const SizedBox(width: 8),
          Icon(icon, size: 15),
        ],
      ),
    ),
  );

  Widget _outlined(String label, VoidCallback onTap, {Color? color}) =>
      SizedBox(
        height: 44,
        child: OutlinedButton(
          onPressed: onTap,
          style: OutlinedButton.styleFrom(
            foregroundColor: color ?? Tokens.text,
            side: BorderSide(color: Tokens.text.withValues(alpha: 0.2)),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(12),
            ),
            padding: const EdgeInsets.symmetric(horizontal: 12),
          ),
          child: Text(label),
        ),
      );

  InputDecoration _input(String label, String hint) => InputDecoration(
    labelText: label,
    hintText: hint,
    isDense: true,
    filled: true,
    fillColor: Tokens.text.withValues(alpha: 0.06),
    labelStyle: const TextStyle(color: Tokens.dim, fontSize: 12),
    hintStyle: const TextStyle(color: Tokens.textTertiary, fontSize: 12),
    enabledBorder: OutlineInputBorder(
      borderRadius: BorderRadius.circular(8),
      borderSide: BorderSide(color: Tokens.text.withValues(alpha: 0.15)),
    ),
    focusedBorder: OutlineInputBorder(
      borderRadius: BorderRadius.circular(8),
      borderSide: BorderSide(color: accent),
    ),
  );

  Widget _card(List<Widget> children) => Container(
    padding: const EdgeInsets.all(12),
    decoration: BoxDecoration(
      color: Tokens.surface2,
      borderRadius: BorderRadius.circular(14),
      border: Border.all(color: Tokens.text.withValues(alpha: 0.1)),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: children,
    ),
  );

  Widget _note(IconData icon, String text) => Row(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Icon(icon, size: 13, color: Tokens.dim),
      const SizedBox(width: 6),
      Expanded(
        child: Text(
          text,
          style: const TextStyle(color: Tokens.dim, fontSize: 10),
        ),
      ),
    ],
  );

  /// A slide: the header, a scrolling body, and the buttons pinned below.
  Widget _slide(Widget header, List<Widget> body, Widget nav) => Column(
    mainAxisSize: MainAxisSize.min,
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      header,
      _divider(),
      Flexible(
        child: SingleChildScrollView(
          padding: const EdgeInsets.symmetric(vertical: 10),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: body,
          ),
        ),
      ),
      nav,
    ],
  );

  // ── slide 0: what the app does ────────────────────────────────────────────

  static const _features = [
    (
      Icons.download_rounded,
      Color(0xFF30D158),
      'Streamrip Downloader',
      'Download and stream lossless FLAC & MP3 from Qobuz or Deezer directly '
          'into your library.',
    ),
    (
      Icons.auto_awesome_rounded,
      Color(0xFF64D2FF),
      'Customizable AI Assistant',
      'Voice and chat control powered by cloud LLMs (Gemini, Ollama with your '
          'API keys) or offline tokenizer.',
    ),
    (
      Icons.tune_rounded,
      Color(0xFFFF9F0A),
      'Studio DSP & Dynamism',
      '5-band graphic equalizer with custom presets, real-time beat punch '
          'enhancement, and dynamic theme.',
    ),
    (
      Icons.polyline_rounded,
      Color(0xFFBF5AF2),
      'Interactive Network & Auto-Play',
      'Explore your music as an interactive 2D acoustic map with endless '
          'similarity-based auto-play.',
    ),
    (
      Icons.auto_fix_high_rounded,
      Color(0xFFFF375F),
      'Playlists & Metadata Wizard',
      'Curate custom playlists and run MusicBrainz tag enrichment to boost '
          'auto-play accuracy.',
    ),
  ];

  Widget _welcome() => _slide(
    _header(
      Icons.auto_awesome_rounded,
      accent,
      'Welcome to Mai An Lab',
      'Dual-Platform Studio Music Hub',
      1,
    ),
    [
      for (final (icon, color, title, desc) in _features)
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 5),
          child: Row(
            children: [
              Container(
                width: 32,
                height: 32,
                decoration: BoxDecoration(
                  color: color.withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(9),
                ),
                child: Icon(icon, color: color, size: 16),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      style: const TextStyle(
                        color: Tokens.text,
                        fontSize: 11.5,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                    Text(
                      desc,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(color: Tokens.dim, fontSize: 10),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
    ],
    _primary('GET STARTED', Icons.arrow_forward_rounded, () => _go(1)),
  );

  // ── slide 1: folder and accounts ──────────────────────────────────────────

  Widget _tabChip(String key, String label, IconData icon) {
    final on = streamingTab == key;
    return InkWell(
      borderRadius: BorderRadius.circular(10),
      onTap: () => setState(() => streamingTab = key),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
        decoration: BoxDecoration(
          color: on ? accent : Tokens.text.withValues(alpha: 0.06),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(
            color: on ? accent : Tokens.text.withValues(alpha: 0.12),
          ),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 14, color: on ? Tokens.bg : Tokens.text),
            const SizedBox(width: 6),
            Text(
              label,
              style: TextStyle(
                color: on ? Tokens.bg : Tokens.text,
                fontSize: 11,
                fontWeight: FontWeight.bold,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _sources() {
    const field = TextStyle(color: Tokens.text, fontSize: 12.5);
    final deezer = streamingTab == 'deezer';
    return _slide(
      _header(
        Icons.folder_special_rounded,
        Tokens.accentGreen,
        'Music Sources',
        'Choose where to index and save music',
        2,
      ),
      [
        _card([
          const Text(
            'Local Library Folder',
            style: TextStyle(
              color: Tokens.text,
              fontSize: 12,
              fontWeight: FontWeight.bold,
            ),
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(
                child: TextField(
                  key: const ValueKey('onboarding-folder'),
                  controller: _folder,
                  style: field,
                  decoration: _input('Music folder', ''),
                ),
              ),
              const SizedBox(width: 6),
              IconButton.filledTonal(
                tooltip: 'Browse folders',
                color: accent,
                style: IconButton.styleFrom(
                  backgroundColor: accent.withValues(alpha: 0.1),
                ),
                icon: const Icon(Icons.folder_open_rounded, size: 18),
                onPressed: _browse,
              ),
            ],
          ),
          const SizedBox(height: 6),
          const Text(
            'Indexed when you finish. You can change or add folders anytime '
            'in Settings.',
            style: TextStyle(color: Tokens.dim, fontSize: 10),
          ),
        ]),
        const SizedBox(height: 10),
        _card([
          const Row(
            children: [
              Icon(Icons.stream_rounded, color: Tokens.amber, size: 16),
              SizedBox(width: 6),
              Expanded(
                child: Text(
                  'Streaming Accounts (Optional)',
                  style: TextStyle(
                    color: Tokens.text,
                    fontSize: 12,
                    fontWeight: FontWeight.bold,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              _tabChip('qobuz', 'Qobuz', Icons.music_note_rounded),
              const SizedBox(width: 8),
              _tabChip('deezer', 'Deezer', Icons.graphic_eq_rounded),
            ],
          ),
          const SizedBox(height: 10),
          if (deezer) ...[
            TextField(
              controller: _arl,
              obscureText: true,
              style: field,
              decoration: _input(
                'Your Deezer ARL Cookie',
                'Paste your 192-char personal ARL cookie',
              ),
            ),
            const SizedBox(height: 6),
            _note(
              Icons.info_outline_rounded,
              'Requires your own personal Deezer account. Retrieve ARL from '
              'browser cookies. Leave blank to skip.',
            ),
          ] else ...[
            TextField(
              controller: _qUser,
              style: field,
              decoration: _input(
                'Your Qobuz Email or User ID',
                'e.g. user@example.com or numeric ID',
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _qToken,
              obscureText: true,
              style: field,
              decoration: _input(
                'Your Password or Session Token',
                'Your personal account password or token',
              ),
            ),
            const SizedBox(height: 6),
            _note(
              Icons.info_outline_rounded,
              'Requires your own personal Qobuz subscription. Leave blank to '
              'skip.',
            ),
          ],
        ]),
        const SizedBox(height: 8),
        _note(
          Icons.info_outline_rounded,
          'Streaming accounts are optional. Users must supply their own '
          'subscriptions, which can be configured anytime in Settings.',
        ),
      ],
      Row(
        children: [
          _outlined('BACK', () => _go(0)),
          const SizedBox(width: 8),
          _outlined('SKIP', () => _go(2), color: Tokens.dim),
          const SizedBox(width: 8),
          Expanded(
            child: _primary(
              'PERSONALIZE',
              Icons.arrow_forward_rounded,
              () => _go(2),
            ),
          ),
        ],
      ),
    );
  }

  // ── slide 2: accent and startup screen ────────────────────────────────────

  static const _startupIcons = {
    'Library': Icons.library_music_rounded,
    'Search': Icons.search_rounded,
    'Jarvis': Icons.auto_awesome_rounded,
  };

  Widget _personalize() => _slide(
    _header(
      Icons.palette_rounded,
      accent,
      'Make It Yours',
      'Select your accent and default start view',
      3,
    ),
    [
      const SizedBox(height: 2),
      const Text(
        'THEME ACCENT',
        style: TextStyle(
          color: Tokens.dim,
          fontSize: 10,
          fontWeight: FontWeight.w700,
        ),
      ),
      const SizedBox(height: 6),
      Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          for (final (hex, name) in kOnboardingAccents)
            Tooltip(
              message: name,
              child: GestureDetector(
                onTap: () => setState(() => accentHex = hex),
                child: Container(
                  width: 40,
                  height: 40,
                  decoration: BoxDecoration(
                    color: _hex(hex),
                    shape: BoxShape.circle,
                    border: Border.all(
                      width: hex == accentHex ? 3 : 1,
                      color: hex == accentHex
                          ? Colors.white
                          : Tokens.text.withValues(alpha: 0.2),
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
      const SizedBox(height: 18),
      const Text(
        'STARTUP SCREEN',
        style: TextStyle(
          color: Tokens.dim,
          fontSize: 10,
          fontWeight: FontWeight.w700,
        ),
      ),
      const SizedBox(height: 6),
      Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          for (final (key, label) in kOnboardingStartups)
            InkWell(
              borderRadius: BorderRadius.circular(10),
              onTap: () => setState(() => startup = key),
              child: Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 8,
                ),
                decoration: BoxDecoration(
                  color: key == startup
                      ? accent
                      : Tokens.text.withValues(alpha: 0.08),
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(
                    color: key == startup
                        ? accent
                        : Tokens.text.withValues(alpha: 0.12),
                  ),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(
                      _startupIcons[key],
                      size: 14,
                      color: key == startup ? Tokens.bg : accent,
                    ),
                    const SizedBox(width: 6),
                    Text(
                      label,
                      style: TextStyle(
                        color: key == startup ? Tokens.bg : Tokens.text,
                        fontSize: 11,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ],
                ),
              ),
            ),
        ],
      ),
      const SizedBox(height: 18),
      Container(
        padding: const EdgeInsets.all(8),
        decoration: BoxDecoration(
          color: Tokens.surface2,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: Tokens.text.withValues(alpha: 0.08)),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.lightbulb_rounded, color: accent, size: 15),
            const SizedBox(width: 8),
            const Expanded(
              child: Text(
                'Pro Tip: Long-press any track for technical audio specs and '
                'instant acoustic similarity walks.',
                style: TextStyle(color: Tokens.dim, fontSize: 10),
              ),
            ),
          ],
        ),
      ),
    ],
    Row(
      children: [
        _outlined('BACK', () => _go(1)),
        const SizedBox(width: 8),
        Expanded(
          child: _primary(
            'ENTER MAI-AN LAB',
            Icons.rocket_launch_rounded,
            _finishing ? null : _finish,
          ),
        ),
      ],
    ),
  );
}
