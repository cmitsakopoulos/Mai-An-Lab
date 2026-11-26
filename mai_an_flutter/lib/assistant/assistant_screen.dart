import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:url_launcher/url_launcher.dart';

import '../settings/settings_page.dart';
import '../ui/frost.dart';
import '../ui/page_header.dart';
import '../ui/size_reporter.dart';
import '../ui/toast.dart';
import '../ui/tokens.dart';
import 'agent_progress.dart';
import 'assistant_controller.dart';

/// `ui/views/assistant.py`: the Jarvis chat tab.
class AssistantScreen extends StatefulWidget {
  const AssistantScreen({
    super.key,
    required this.controller,
    this.onOpenSettings,
  });

  final AssistantController controller;
  final void Function([SettingsPage?])? onOpenSettings;

  @override
  State<AssistantScreen> createState() => _AssistantScreenState();
}

class _AssistantScreenState extends State<AssistantScreen> {
  final _input = TextEditingController();
  final _focus = FocusNode();
  final _scroll = ScrollController();
  int _lastCount = 0;

  /// The composer's measured height (it grows with multi-line input), so
  /// the last message always clears it.
  double _composerHeight = 66;
  bool _lastThinking = false;
  bool _lastVoiceActive = false;
  int _lastSteps = 0;
  int _lastDraft = 0;

  @override
  void initState() {
    super.initState();
    _lastCount = widget.controller.messages.length;
    _lastThinking = widget.controller.thinking;
    _lastVoiceActive =
        widget.controller.micListening || widget.controller.micProcessing;
    widget.controller.addListener(_onChanged);
  }

  @override
  void didUpdateWidget(AssistantScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.controller != widget.controller) {
      oldWidget.controller.removeListener(_onChanged);
      widget.controller.addListener(_onChanged);
    }
  }

  @override
  void dispose() {
    widget.controller.removeListener(_onChanged);
    _input.dispose();
    _focus.dispose();
    _scroll.dispose();
    super.dispose();
  }

  void _onChanged() {
    final c = widget.controller;
    final voiceActive = c.micListening || c.micProcessing;
    final steps = c.liveTurn?.steps.length ?? 0;
    final grew =
        c.messages.length != _lastCount ||
        c.thinking != _lastThinking ||
        voiceActive != _lastVoiceActive ||
        steps != _lastSteps;
    _lastCount = c.messages.length;
    _lastThinking = c.thinking;
    _lastVoiceActive = voiceActive;
    _lastSteps = steps;
    final draft = c.liveTurn?.draft.length ?? 0;
    final drafted = draft != _lastDraft;
    _lastDraft = draft;
    if (grew) {
      _scrollToBottom();
    } else if (drafted && draft > 0) {
      _followStream();
    }
  }

  /// Keeps streamed text in view, but only while the reader is already at
  /// the bottom; scrolling up to reread isn't fought.
  void _followStream() {
    if (!_scroll.hasClients || _scroll.position.extentAfter > 96) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_scroll.hasClients) return;
      _scroll.jumpTo(_scroll.position.maxScrollExtent);
    });
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_scroll.hasClients) return;
      _scroll
          .animateTo(
            _scroll.position.maxScrollExtent,
            duration: Tokens.motionBase,
            curve: Tokens.curveIn,
          )
          .then((_) {
            if (!mounted || !_scroll.hasClients) return;
            final max = _scroll.position.maxScrollExtent;
            if ((max - _scroll.offset).abs() > 1) {
              _scroll.jumpTo(max);
            }
          });
    });
  }

  void _send() {
    final text = _input.text.trim();
    if (text.isEmpty) return;
    _input.clear();
    unawaited(widget.controller.sendUserText(text));
  }

  Future<void> _openLink(String? href) async {
    if (href == null || href.isEmpty) return;
    final uri = Uri.tryParse(href);
    if (uri == null) return;
    try {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    } catch (e) {
      debugPrint('AssistantScreen: could not open link $href: $e');
    }
  }

  void _copyText(String text) {
    Clipboard.setData(ClipboardData(text: text));
    showAppToast(context, 'Copied to clipboard', icon: Icons.check_rounded);
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final hPad = Tokens.pagePad(context);
    final shellBottom = ShellInsets.bottomOf(context);
    final composerBottomPad = shellBottom > 0 ? shellBottom + 8.0 : 10.0;
    // Composer + its 10px top inset.
    final composerReserve = _composerHeight + 10;

    return ListenableBuilder(
      listenable: widget.controller,
      builder: (context, _) {
        final c = widget.controller;
        final msgs = c.messages;
        return PageHeader(
          title: 'Jarvis',
          actions: [
            HeaderAction(
              icon: c.ttsEnabled
                  ? Icons.volume_up_rounded
                  : Icons.volume_off_rounded,
              tooltip: 'Toggle voice replies',
              accent: c.ttsEnabled,
              onPressed: c.toggleTts,
            ),
            HeaderAction(
              icon: Icons.delete_outline_rounded,
              tooltip: 'Clear conversation history',
              color: msgs.isNotEmpty ? null : Tokens.textTertiary,
              onPressed: c.clearChat,
            ),
            if (widget.onOpenSettings case final open?)
              HeaderAction(
                icon: Icons.settings_outlined,
                tooltip: 'Settings',
                onPressed: () => open(),
              ),
          ],
          child: Builder(
            builder: (context) {
              final headerHeight = HeaderInsets.topOf(context);
              final voiceBubble = c.micListening || c.micProcessing;
              final showEmpty = msgs.isEmpty && !c.thinking && !voiceBubble;
              final itemCount =
                  msgs.length + (voiceBubble ? 1 : 0) + (c.thinking ? 1 : 0);

              return Stack(
                fit: StackFit.expand,
                children: [
                  // Messages or welcome state (scrolls behind floating header & composer)
                  if (showEmpty)
                    Padding(
                      padding: EdgeInsets.only(
                        top: headerHeight,
                        bottom: composerBottomPad + composerReserve,
                      ),
                      child: _EmptyState(
                        accent: accent,
                        onPrompt: (q) => unawaited(c.sendUserText(q)),
                      ),
                    )
                  else
                    Scrollbar(
                      controller: _scroll,
                      child: ListView.separated(
                        controller: _scroll,
                        keyboardDismissBehavior:
                            ScrollViewKeyboardDismissBehavior.onDrag,
                        padding: EdgeInsets.fromLTRB(
                          hPad,
                          headerHeight + 8,
                          hPad,
                          composerBottomPad + composerReserve + 12,
                        ),
                        itemCount: itemCount,
                        separatorBuilder: (_, _) => const SizedBox(height: 14),
                        itemBuilder: (context, index) {
                          if (index < msgs.length) {
                            return _BubbleRow(
                              msg: msgs[index],
                              accent: accent,
                              onOptionTap: (id) =>
                                  unawaited(c.sendUserText(id)),
                              onLinkTap: _openLink,
                              onCopy: _copyText,
                            );
                          }
                          if (voiceBubble && index == msgs.length) {
                            return _StatusBubble(
                              listening: c.micListening,
                              label: c.micListening
                                  ? 'Listening, sir...'
                                  : 'Thinking...',
                              accent: accent,
                            );
                          }
                          final live = c.liveTurn;
                          if (live != null &&
                              (live.steps.isNotEmpty ||
                                  live.draft.isNotEmpty)) {
                            return Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                if (live.steps.isNotEmpty)
                                  _LiveTurnCard(turn: live, accent: accent),
                                if (live.steps.isNotEmpty &&
                                    live.draft.isNotEmpty)
                                  const SizedBox(height: 14),
                                if (live.draft.isNotEmpty)
                                  _BubbleRow(
                                    msg: {
                                      'sender': 'assistant',
                                      'text': live.draft,
                                    },
                                    accent: accent,
                                    onOptionTap: (_) {},
                                    onLinkTap: _openLink,
                                    onCopy: _copyText,
                                    streaming: true,
                                  ),
                              ],
                            );
                          }
                          return _StatusBubble(
                            listening: false,
                            label: 'Thinking...',
                            accent: accent,
                          );
                        },
                      ),
                    ),

                  // Floating frosted-glass composer
                  Positioned(
                    left: 0,
                    right: 0,
                    bottom: 0,
                    child: DecoratedBox(
                      decoration: const BoxDecoration(
                        gradient: LinearGradient(
                          begin: Alignment.bottomCenter,
                          end: Alignment.topCenter,
                          stops: [0.0, 0.75, 1.0],
                          colors: [
                            Color(0xE6000000),
                            Color(0x88000000),
                            Color(0x00000000),
                          ],
                        ),
                      ),
                      child: Padding(
                        padding: EdgeInsets.fromLTRB(
                          Tokens.dockPad(context),
                          10,
                          Tokens.dockPad(context),
                          composerBottomPad,
                        ),
                        child: SizeReporter(
                          onSize: (size) {
                            if (mounted && size.height != _composerHeight) {
                              setState(() => _composerHeight = size.height);
                            }
                          },
                          child: _GlassComposer(
                            controller: _input,
                            focusNode: _focus,
                            accent: accent,
                            hasVoice: c.hasVoice,
                            micListening: c.micListening,
                            onMicDown: c.onMicDown,
                            onMicUp: c.onMicUp,
                            onSend: _send,
                          ),
                        ),
                      ),
                    ),
                  ),
                ],
              );
            },
          ),
        );
      },
    );
  }
}

class _GlassComposer extends StatefulWidget {
  const _GlassComposer({
    required this.controller,
    required this.focusNode,
    required this.accent,
    required this.hasVoice,
    required this.micListening,
    required this.onMicDown,
    required this.onMicUp,
    required this.onSend,
  });

  final TextEditingController controller;
  final FocusNode focusNode;
  final Color accent;
  final bool hasVoice;
  final bool micListening;
  final VoidCallback onMicDown;
  final VoidCallback onMicUp;
  final VoidCallback onSend;

  @override
  State<_GlassComposer> createState() => _GlassComposerState();
}

class _GlassComposerState extends State<_GlassComposer> {
  bool _focused = false;

  @override
  void initState() {
    super.initState();
    _focused = widget.focusNode.hasFocus;
    widget.focusNode.addListener(_onFocus);
  }

  @override
  void didUpdateWidget(_GlassComposer old) {
    super.didUpdateWidget(old);
    if (old.focusNode != widget.focusNode) {
      old.focusNode.removeListener(_onFocus);
      _focused = widget.focusNode.hasFocus;
      widget.focusNode.addListener(_onFocus);
    }
  }

  void _onFocus() {
    final now = widget.focusNode.hasFocus;
    if (now != _focused) setState(() => _focused = now);
  }

  @override
  void dispose() {
    widget.focusNode.removeListener(_onFocus);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final accent = widget.accent;
    final onAccent = accent.computeLuminance() > 0.45
        ? Tokens.bg
        : Colors.white;
    final op = GlassScope.opacityOf(context);
    final isSolid = op >= 0.99;

    final borderColor = widget.micListening
        ? Tokens.accentRed.withValues(alpha: 0.65)
        : _focused
        ? (isSolid
              ? Colors.white.withValues(alpha: 0.24)
              : Colors.white.withValues(alpha: 0.32))
        : null;

    return GlassSurface(
      radius: 26,
      borderColor: borderColor,
      padding: EdgeInsets.fromLTRB(widget.hasVoice ? 6 : 16, 5, 6, 5),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          if (widget.hasVoice) ...[
            Tooltip(
              triggerMode: TooltipTriggerMode.manual,
              message: widget.micListening
                  ? 'Release to Send'
                  : 'Hold to Speak',
              child: Listener(
                behavior: HitTestBehavior.opaque,
                onPointerDown: (_) => widget.onMicDown(),
                onPointerUp: (_) => widget.onMicUp(),
                onPointerCancel: (_) => widget.onMicUp(),
                child: AnimatedContainer(
                  duration: Tokens.motionFast,
                  width: 38,
                  height: 38,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: widget.micListening
                        ? Tokens.accentRed.withValues(alpha: 0.22)
                        : Colors.transparent,
                    border: widget.micListening
                        ? Border.all(
                            color: Tokens.accentRed.withValues(alpha: 0.65),
                            width: 1.5,
                          )
                        : null,
                  ),
                  child: Icon(
                    Icons.mic_rounded,
                    size: 20,
                    color: widget.micListening ? Tokens.accentRed : Tokens.dim,
                  ),
                ),
              ),
            ),
            const SizedBox(width: 4),
          ],
          Expanded(
            child: TextField(
              controller: widget.controller,
              focusNode: widget.focusNode,
              cursorColor: accent,
              minLines: 1,
              maxLines: 4,
              style: const TextStyle(
                color: Tokens.text,
                fontSize: 15,
                height: 1.3,
              ),
              textInputAction: TextInputAction.send,
              onSubmitted: (_) => widget.onSend(),
              decoration: InputDecoration(
                hintText: 'Ask me anything, sir…',
                hintStyle: TextStyle(
                  color: Tokens.dim.withValues(alpha: 0.9),
                  fontSize: 15,
                  height: 1.3,
                ),
                filled: false,
                isDense: true,
                border: InputBorder.none,
                enabledBorder: InputBorder.none,
                focusedBorder: InputBorder.none,
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 4,
                  vertical: 9,
                ),
              ),
            ),
          ),
          const SizedBox(width: 6),
          ValueListenableBuilder<TextEditingValue>(
            valueListenable: widget.controller,
            builder: (context, value, _) {
              final hasText = value.text.trim().isNotEmpty;
              return Tooltip(
                message: 'Send',
                child: Material(
                  color: Colors.transparent,
                  child: InkWell(
                    customBorder: const CircleBorder(),
                    onTap: widget.onSend,
                    child: AnimatedContainer(
                      duration: Tokens.motionFast,
                      curve: Tokens.curveIn,
                      width: 36,
                      height: 36,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: hasText
                            ? accent
                            : Colors.white.withValues(alpha: 0.08),
                      ),
                      child: Icon(
                        Icons.arrow_upward_rounded,
                        size: 19,
                        color: hasText ? onAccent : Tokens.dim,
                      ),
                    ),
                  ),
                ),
              );
            },
          ),
        ],
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({required this.accent, required this.onPrompt});

  final Color accent;
  final ValueChanged<String> onPrompt;

  static const _prompts = <(IconData, String, String)>[
    (Icons.spa_rounded, 'Play something chill', 'play something chill'),
    (Icons.all_inclusive_rounded, 'Play similar tracks', 'play similar'),
    (Icons.bar_chart_rounded, 'Library stats', 'how big is my library?'),
    (Icons.help_outline_rounded, 'What can you do?', 'help'),
  ];

  @override
  Widget build(BuildContext context) {
    final hPad = Tokens.pagePad(context);
    return Center(
      child: SingleChildScrollView(
        padding: EdgeInsets.symmetric(horizontal: hPad, vertical: 16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 68,
              height: 68,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                gradient: RadialGradient(
                  colors: [
                    accent.withValues(alpha: 0.28),
                    accent.withValues(alpha: 0.06),
                    Colors.transparent,
                  ],
                ),
                border: Border.all(
                  color: accent.withValues(alpha: 0.35),
                  width: 1,
                ),
              ),
              child: Icon(Icons.auto_awesome_rounded, color: accent, size: 32),
            ),
            const SizedBox(height: 14),
            const Text(
              'Jarvis',
              textAlign: TextAlign.center,
              style: TextStyle(
                color: Tokens.text,
                fontSize: 22,
                fontWeight: FontWeight.w800,
                letterSpacing: -0.3,
              ),
            ),
            const SizedBox(height: 6),
            const Text(
              'At your service, sir. Pick a suggestion or ask anything.',
              textAlign: TextAlign.center,
              style: TextStyle(color: Tokens.dim, fontSize: 13, height: 1.35),
            ),
            const SizedBox(height: 22),
            LayoutBuilder(
              builder: (context, box) {
                final cardW = ((box.maxWidth - 10) / 2).clamp(140.0, 220.0);
                return Wrap(
                  alignment: WrapAlignment.center,
                  spacing: 10,
                  runSpacing: 10,
                  children: [
                    for (final (icon, label, query) in _prompts)
                      SizedBox(
                        width: cardW,
                        child: GlassCard(
                          radius: 16,
                          padding: const EdgeInsets.all(14),
                          onTap: () => onPrompt(query),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              Container(
                                width: 30,
                                height: 30,
                                decoration: BoxDecoration(
                                  color: accent.withValues(alpha: 0.14),
                                  borderRadius: BorderRadius.circular(9),
                                ),
                                child: Icon(icon, color: accent, size: 17),
                              ),
                              const SizedBox(height: 10),
                              Text(
                                label,
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  color: Tokens.text,
                                  fontSize: 13,
                                  fontWeight: FontWeight.w600,
                                  height: 1.25,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                  ],
                );
              },
            ),
          ],
        ),
      ),
    );
  }
}

class _StatusBubble extends StatelessWidget {
  const _StatusBubble({
    required this.listening,
    required this.label,
    required this.accent,
  });

  final bool listening;
  final String label;
  final Color accent;

  @override
  Widget build(BuildContext context) {
    final color = listening ? Tokens.accentRed : accent;
    return Row(
      mainAxisAlignment: MainAxisAlignment.start,
      children: [
        GlassSurface(
          radius: 999,
          elevated: false,
          borderColor: color.withValues(alpha: 0.28),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 9),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                listening ? Icons.mic_rounded : Icons.auto_awesome_rounded,
                color: color,
                size: 16,
              ),
              const SizedBox(width: 8),
              Text(
                label,
                style: const TextStyle(
                  color: Tokens.text,
                  fontSize: 13,
                  fontWeight: FontWeight.w500,
                ),
              ),
              const SizedBox(width: 10),
              _PulseDots(color: color),
            ],
          ),
        ),
      ],
    );
  }
}

class _PulseDots extends StatefulWidget {
  const _PulseDots({required this.color});
  final Color color;

  @override
  State<_PulseDots> createState() => _PulseDotsState();
}

class _PulseDotsState extends State<_PulseDots>
    with SingleTickerProviderStateMixin {
  late final _ctrl = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1100),
  )..repeat();

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _ctrl,
      builder: (_, _) => Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          for (var i = 0; i < 3; i++) ...[
            if (i > 0) const SizedBox(width: 3),
            Opacity(
              opacity:
                  0.3 +
                  0.7 *
                      (0.5 +
                          0.5 *
                              math.sin(
                                (_ctrl.value * 2 * math.pi) - (i * 0.85),
                              )),
              child: Container(
                width: 5,
                height: 5,
                decoration: BoxDecoration(
                  color: widget.color,
                  shape: BoxShape.circle,
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _BubbleRow extends StatelessWidget {
  const _BubbleRow({
    required this.msg,
    required this.accent,
    required this.onOptionTap,
    required this.onLinkTap,
    required this.onCopy,
    this.streaming = false,
  });

  /// Text still arriving: no Copy row yet.
  final bool streaming;
  final Map<String, Object?> msg;
  final Color accent;
  final ValueChanged<String> onOptionTap;
  final ValueChanged<String?> onLinkTap;
  final ValueChanged<String> onCopy;

  @override
  Widget build(BuildContext context) {
    final sender = '${msg['sender'] ?? 'assistant'}';
    final text = '${msg['text'] ?? ''}';
    final isUser = sender == 'user';
    final maxW = MediaQuery.sizeOf(context).width;

    if (isUser) {
      final onAccent = accent.computeLuminance() > 0.45
          ? Tokens.bg
          : Colors.white;
      return Row(
        mainAxisAlignment: MainAxisAlignment.end,
        children: [
          ConstrainedBox(
            constraints: BoxConstraints(maxWidth: maxW * 0.78),
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 15, vertical: 10),
              decoration: BoxDecoration(
                color: accent,
                borderRadius: const BorderRadius.only(
                  topLeft: Radius.circular(20),
                  topRight: Radius.circular(20),
                  bottomLeft: Radius.circular(20),
                  bottomRight: Radius.circular(6),
                ),
              ),
              child: SelectableText(
                text,
                style: TextStyle(
                  color: onAccent,
                  fontSize: 14.5,
                  fontWeight: FontWeight.w500,
                  height: 1.35,
                ),
              ),
            ),
          ),
        ],
      );
    }

    List<Map<String, Object?>>? options;
    final intent = msg['intent'];
    if (intent is Map<String, Object?>) {
      final extras = intent['extras'];
      if (extras is Map<String, Object?> && extras['options'] is List) {
        final raw = extras['options']! as List<Object?>;
        final parsed = [
          for (final o in raw)
            if (o is Map<String, Object?>) o,
        ];
        if (parsed.isNotEmpty) options = parsed;
      }
    }

    final rawTools = msg['tools'];
    final tools = [
      if (rawTools is List)
        for (final t in rawTools)
          if (t is Map<String, Object?>) t,
    ];

    final markdown = MarkdownBody(
      data: text,
      selectable: true,
      onTapLink: (_, href, _) => onLinkTap(href),
      styleSheet: MarkdownStyleSheet(
        p: const TextStyle(color: Tokens.text, fontSize: 14.5, height: 1.45),
        strong: const TextStyle(
          color: Tokens.text,
          fontSize: 14.5,
          fontWeight: FontWeight.w700,
        ),
        em: const TextStyle(
          color: Tokens.text,
          fontSize: 14.5,
          fontStyle: FontStyle.italic,
        ),
        h1: const TextStyle(
          color: Tokens.text,
          fontSize: 19,
          fontWeight: FontWeight.w700,
        ),
        h2: const TextStyle(
          color: Tokens.text,
          fontSize: 16.5,
          fontWeight: FontWeight.w700,
        ),
        h3: const TextStyle(
          color: Tokens.text,
          fontSize: 15,
          fontWeight: FontWeight.w700,
        ),
        a: TextStyle(color: accent, decoration: TextDecoration.underline),
        code: const TextStyle(
          color: Tokens.text,
          backgroundColor: Tokens.surface2,
          fontFamily: 'monospace',
          fontSize: 12.5,
        ),
        codeblockPadding: const EdgeInsets.all(12),
        codeblockDecoration: BoxDecoration(
          color: Tokens.surface,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: Tokens.borderSubtle),
        ),
        listBullet: const TextStyle(color: Tokens.dim, fontSize: 14.5),
      ),
    );

    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Container(
          width: 26,
          height: 26,
          margin: const EdgeInsets.only(top: 2),
          decoration: BoxDecoration(
            color: accent.withValues(alpha: 0.14),
            shape: BoxShape.circle,
            border: Border.all(
              color: accent.withValues(alpha: 0.28),
              width: 0.75,
            ),
          ),
          child: Icon(Icons.auto_awesome_rounded, color: accent, size: 14),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              if (tools.isNotEmpty) ...[
                _ToolTrail(tools: tools, accent: accent),
                const SizedBox(height: 6),
              ],
              markdown,
              if (options != null) ...[
                const SizedBox(height: 10),
                for (final (i, opt) in options.indexed) ...[
                  if (i > 0) const SizedBox(height: 6),
                  _OptionButton(
                    id: '${opt['id'] ?? ''}',
                    title: '${opt['title'] ?? ''}',
                    subtitle: '${opt['subtitle'] ?? ''}',
                    accent: accent,
                    onTap: () => onOptionTap('${opt['id'] ?? ''}'),
                  ),
                ],
              ],
              if (!streaming && text.trim().isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Row(
                    children: [
                      InkWell(
                        borderRadius: BorderRadius.circular(8),
                        onTap: () => onCopy(text),
                        child: const Padding(
                          padding: EdgeInsets.symmetric(
                            horizontal: 4,
                            vertical: 4,
                          ),
                          child: Row(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              Icon(
                                Icons.copy_rounded,
                                size: 13,
                                color: Tokens.textTertiary,
                              ),
                              SizedBox(width: 4),
                              Text(
                                'Copy',
                                style: TextStyle(
                                  color: Tokens.textTertiary,
                                  fontSize: 11,
                                  fontWeight: FontWeight.w500,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ],
    );
  }
}

class _OptionButton extends StatelessWidget {
  const _OptionButton({
    required this.id,
    required this.title,
    required this.subtitle,
    required this.accent,
    required this.onTap,
  });

  final String id;
  final String title;
  final String subtitle;
  final Color accent;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return OutlinedButton(
      style: OutlinedButton.styleFrom(
        backgroundColor: Tokens.surface,
        foregroundColor: Tokens.text,
        side: const BorderSide(color: Tokens.borderSubtle),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        alignment: Alignment.centerLeft,
      ),
      onPressed: onTap,
      child: Row(
        children: [
          Container(
            width: 22,
            height: 22,
            decoration: BoxDecoration(
              color: accent.withValues(alpha: 0.16),
              borderRadius: BorderRadius.circular(7),
            ),
            alignment: Alignment.center,
            child: Text(
              id,
              style: TextStyle(
                color: accent,
                fontSize: 11,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 13.5,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                if (subtitle.isNotEmpty) ...[
                  const SizedBox(height: 2),
                  Text(
                    subtitle,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: Tokens.dim, fontSize: 11.5),
                  ),
                ],
              ],
            ),
          ),
          const SizedBox(width: 6),
          const Icon(
            Icons.chevron_right_rounded,
            size: 18,
            color: Tokens.textTertiary,
          ),
        ],
      ),
    );
  }
}

/// The agent's live checklist: one row per tool call, then a "Thinking"
/// row while the model works on the next step.
class _LiveTurnCard extends StatelessWidget {
  const _LiveTurnCard({required this.turn, required this.accent});

  final LiveTurn turn;
  final Color accent;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Flexible(
          child: GlassSurface(
            radius: 18,
            elevated: false,
            borderColor: accent.withValues(alpha: 0.28),
            padding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (turn.interim.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 4),
                    child: Text(
                      turn.interim,
                      maxLines: 3,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Tokens.dim,
                        fontSize: 12.5,
                        fontStyle: FontStyle.italic,
                        height: 1.35,
                      ),
                    ),
                  ),
                for (final step in turn.steps)
                  _ToolRow(
                    label: step.label,
                    summary: step.summary,
                    state: step.state,
                    accent: accent,
                  ),
                if (turn.awaitingModel)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(
                          Icons.auto_awesome_rounded,
                          color: accent,
                          size: 15,
                        ),
                        const SizedBox(width: 8),
                        const Text(
                          'Thinking',
                          style: TextStyle(
                            color: Tokens.text,
                            fontSize: 13,
                            fontWeight: FontWeight.w500,
                          ),
                        ),
                        const SizedBox(width: 8),
                        _PulseDots(color: accent),
                      ],
                    ),
                  ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class _ToolRow extends StatelessWidget {
  const _ToolRow({
    required this.label,
    required this.summary,
    required this.state,
    required this.accent,
    this.dense = false,
  });

  final String label;
  final String summary;
  final ToolStepState state;
  final Color accent;
  final bool dense;

  @override
  Widget build(BuildContext context) {
    final Widget icon = switch (state) {
      ToolStepState.running => SizedBox(
        width: 13,
        height: 13,
        child: CircularProgressIndicator(strokeWidth: 1.8, color: accent),
      ),
      ToolStepState.done => Icon(
        Icons.check_circle_rounded,
        size: 15,
        color: accent.withValues(alpha: 0.85),
      ),
      ToolStepState.failed => const Icon(
        Icons.error_outline_rounded,
        size: 15,
        color: Tokens.accentRed,
      ),
    };
    final running = state == ToolStepState.running;
    return Padding(
      padding: EdgeInsets.symmetric(vertical: dense ? 2 : 3),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          SizedBox(width: 15, height: 18, child: Center(child: icon)),
          const SizedBox(width: 8),
          Flexible(
            child: Text.rich(
              TextSpan(
                text: label,
                style: TextStyle(
                  color: running || !dense ? Tokens.text : Tokens.dim,
                  fontSize: dense ? 12 : 13,
                  fontWeight: running ? FontWeight.w500 : FontWeight.w400,
                ),
                children: [
                  if (summary.isNotEmpty)
                    TextSpan(
                      text: '  ·  $summary',
                      style: TextStyle(
                        color: state == ToolStepState.failed
                            ? Tokens.accentRed
                            : Tokens.textTertiary,
                        fontSize: dense ? 11 : 12,
                      ),
                    ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// "Used N tools" on a finished agent reply; expands to the saved trail.
class _ToolTrail extends StatefulWidget {
  const _ToolTrail({required this.tools, required this.accent});

  final List<Map<String, Object?>> tools;
  final Color accent;

  @override
  State<_ToolTrail> createState() => _ToolTrailState();
}

class _ToolTrailState extends State<_ToolTrail> {
  bool _open = false;

  @override
  Widget build(BuildContext context) {
    final n = widget.tools.length;
    final failed = widget.tools.where((t) => t['ok'] == false).length;
    final label = n == 1 ? 'Used 1 tool' : 'Used $n tools';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        InkWell(
          borderRadius: BorderRadius.circular(8),
          onTap: () => setState(() => _open = !_open),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 2, vertical: 3),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(
                  Icons.build_circle_outlined,
                  size: 14,
                  color: Tokens.textTertiary,
                ),
                const SizedBox(width: 5),
                Text(
                  failed > 0 ? '$label · $failed failed' : label,
                  style: const TextStyle(
                    color: Tokens.textTertiary,
                    fontSize: 11.5,
                    fontWeight: FontWeight.w500,
                  ),
                ),
                Icon(
                  _open ? Icons.expand_less_rounded : Icons.expand_more_rounded,
                  size: 16,
                  color: Tokens.textTertiary,
                ),
              ],
            ),
          ),
        ),
        if (_open)
          Padding(
            padding: const EdgeInsets.only(left: 2, top: 2, bottom: 2),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                for (final t in widget.tools)
                  _ToolRow(
                    label: '${t['label'] ?? t['name'] ?? ''}',
                    summary: '${t['summary'] ?? ''}',
                    state: t['ok'] == false
                        ? ToolStepState.failed
                        : ToolStepState.done,
                    accent: widget.accent,
                    dense: true,
                  ),
              ],
            ),
          ),
      ],
    );
  }
}
