import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:url_launcher/url_launcher.dart';

import '../settings/settings_page.dart';
import '../ui/tokens.dart';
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
  final _scroll = ScrollController();
  int _lastCount = 0;
  bool _lastThinking = false;
  bool _lastVoiceActive = false;

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
    _scroll.dispose();
    super.dispose();
  }

  void _onChanged() {
    final c = widget.controller;
    final voiceActive = c.micListening || c.micProcessing;
    final grew =
        c.messages.length != _lastCount ||
        c.thinking != _lastThinking ||
        voiceActive != _lastVoiceActive;
    _lastCount = c.messages.length;
    _lastThinking = c.thinking;
    _lastVoiceActive = voiceActive;
    if (grew) _scrollToBottom();
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_scroll.hasClients) return;
      _scroll
          .animateTo(
            _scroll.position.maxScrollExtent,
            duration: const Duration(milliseconds: 200),
            curve: Curves.easeOut,
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

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return SafeArea(
      bottom: false,
      child: ListenableBuilder(
        listenable: widget.controller,
        builder: (context, _) {
          final c = widget.controller;
          final msgs = c.messages;
          final voiceBubble = c.micListening || c.micProcessing;
          final showEmpty = msgs.isEmpty && !c.thinking && !voiceBubble;
          final itemCount =
              msgs.length + (voiceBubble ? 1 : 0) + (c.thinking ? 1 : 0);

          return Column(
            children: [
              // Header
              Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 8,
                ),
                child: Row(
                  children: [
                    const Text(
                      'JARVIS',
                      style: TextStyle(
                        color: Tokens.text,
                        fontSize: 13,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const Spacer(),
                    if (widget.onOpenSettings != null)
                      IconButton(
                        icon: const Icon(Icons.settings_outlined),
                        color: accent,
                        tooltip: 'Settings',
                        onPressed: () => widget.onOpenSettings!(),
                      ),
                    IconButton(
                      icon: const Icon(Icons.delete_outline_rounded),
                      color: accent,
                      tooltip: 'Clear conversation history',
                      onPressed: c.clearChat,
                    ),
                    IconButton(
                      icon: Icon(
                        c.ttsEnabled
                            ? Icons.volume_up_rounded
                            : Icons.volume_off_rounded,
                      ),
                      color: accent,
                      tooltip: 'Toggle voice replies',
                      onPressed: c.toggleTts,
                    ),
                  ],
                ),
              ),
              const Divider(color: Tokens.border, height: 1),
              // Messages
              Expanded(
                child: showEmpty
                    ? _EmptyState(
                        accent: accent,
                        onPrompt: (q) => unawaited(c.sendUserText(q)),
                      )
                    : Scrollbar(
                        controller: _scroll,
                        child: ListView.separated(
                          controller: _scroll,
                          padding: const EdgeInsets.symmetric(
                            horizontal: 12,
                            vertical: 8,
                          ),
                          itemCount: itemCount,
                          separatorBuilder: (_, _) => const SizedBox(height: 8),
                          itemBuilder: (context, index) {
                            if (index < msgs.length) {
                              return _BubbleRow(
                                msg: msgs[index],
                                accent: accent,
                                onOptionTap: (id) =>
                                    unawaited(c.sendUserText(id)),
                                onLinkTap: _openLink,
                              );
                            }
                            if (voiceBubble && index == msgs.length) {
                              return _StatusBubble(
                                icon: c.micListening
                                    ? Icons.mic_rounded
                                    : Icons.auto_awesome_rounded,
                                iconColor: c.micListening
                                    ? const Color(0xFFFF4444)
                                    : accent,
                                label: c.micListening
                                    ? 'Listening, sir...'
                                    : 'Thinking...',
                                accent: accent,
                              );
                            }
                            return _StatusBubble(
                              icon: Icons.auto_awesome_rounded,
                              iconColor: accent,
                              label: 'Thinking...',
                              accent: accent,
                            );
                          },
                        ),
                      ),
              ),
              const Divider(color: Tokens.border, height: 1),
              // Footer input
              Padding(
                padding: const EdgeInsets.fromLTRB(12, 8, 8, 12),
                child: Row(
                  children: [
                    if (c.hasVoice) ...[
                      Tooltip(
                        message: c.micListening
                            ? 'Release to Send'
                            : 'Hold to Speak',
                        child: GestureDetector(
                          onTapDown: (_) => c.onMicDown(),
                          onTapUp: (_) => c.onMicUp(),
                          onLongPressEnd: (_) => c.onMicUp(),
                          child: Container(
                            padding: const EdgeInsets.all(10),
                            decoration: BoxDecoration(
                              borderRadius: BorderRadius.circular(20),
                            ),
                            child: Icon(
                              Icons.mic_rounded,
                              color: c.micListening
                                  ? const Color(0xFFFF4444)
                                  : accent,
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(width: 6),
                    ],
                    Expanded(
                      child: TextField(
                        controller: _input,
                        style: const TextStyle(
                          color: Tokens.text,
                          fontSize: 14,
                        ),
                        textInputAction: TextInputAction.send,
                        onSubmitted: (_) => _send(),
                        decoration: InputDecoration(
                          hintText: 'Ask me anything, sir…',
                          hintStyle: const TextStyle(
                            color: Tokens.dim,
                            fontSize: 14,
                          ),
                          filled: false,
                          isDense: true,
                          contentPadding: const EdgeInsets.symmetric(
                            horizontal: 14,
                            vertical: 12,
                          ),
                          enabledBorder: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(22),
                            borderSide: const BorderSide(color: Tokens.border),
                          ),
                          focusedBorder: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(22),
                            borderSide: BorderSide(color: accent),
                          ),
                          border: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(22),
                            borderSide: const BorderSide(color: Tokens.border),
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(width: 6),
                    IconButton(
                      icon: const Icon(Icons.arrow_upward_rounded),
                      color: accent,
                      tooltip: 'Send',
                      onPressed: _send,
                    ),
                  ],
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({required this.accent, required this.onPrompt});

  final Color accent;
  final ValueChanged<String> onPrompt;

  static const _prompts = [
    ('[>] Play something chill', 'play something chill'),
    ('[*] Play similar tracks', 'play similar'),
    ('[#] Library stats', 'how big is my library?'),
    ('[?] What can you do?', 'help'),
  ];

  @override
  Widget build(BuildContext context) {
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const SizedBox(height: 16),
            Icon(Icons.auto_awesome_rounded, color: accent, size: 38),
            const SizedBox(height: 8),
            const Text(
              'JARVIS',
              textAlign: TextAlign.center,
              style: TextStyle(
                color: Tokens.text,
                fontSize: 18,
                fontWeight: FontWeight.w900,
              ),
            ),
            const SizedBox(height: 8),
            const Text(
              'At your service, sir. Select a suggestion or speak a directive:',
              textAlign: TextAlign.center,
              style: TextStyle(color: Tokens.dim, fontSize: 12),
            ),
            const SizedBox(height: 14),
            Wrap(
              alignment: WrapAlignment.center,
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final (label, query) in _prompts)
                  Material(
                    color: Tokens.surface2,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(20),
                      side: BorderSide(color: accent.withValues(alpha: 0.3)),
                    ),
                    clipBehavior: Clip.antiAlias,
                    child: InkWell(
                      onTap: () => onPrompt(query),
                      child: Padding(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 14,
                          vertical: 8,
                        ),
                        child: Text(
                          label,
                          style: TextStyle(
                            color: accent,
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                    ),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _StatusBubble extends StatelessWidget {
  const _StatusBubble({
    required this.icon,
    required this.iconColor,
    required this.label,
    required this.accent,
  });

  final IconData icon;
  final Color iconColor;
  final String label;
  final Color accent;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.start,
      children: [
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            color: Tokens.surface2,
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: accent.withValues(alpha: 0.1)),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, color: iconColor, size: 18),
              const SizedBox(width: 8),
              Text(
                label,
                style: const TextStyle(
                  color: Tokens.dim,
                  fontSize: 13,
                  fontStyle: FontStyle.italic,
                ),
              ),
              const SizedBox(width: 8),
              SizedBox(
                width: 12,
                height: 12,
                child: CircularProgressIndicator(
                  strokeWidth: 1.5,
                  color: accent,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

class _BubbleRow extends StatelessWidget {
  const _BubbleRow({
    required this.msg,
    required this.accent,
    required this.onOptionTap,
    required this.onLinkTap,
  });

  final Map<String, Object?> msg;
  final Color accent;
  final ValueChanged<String> onOptionTap;
  final ValueChanged<String?> onLinkTap;

  @override
  Widget build(BuildContext context) {
    final sender = '${msg['sender'] ?? 'assistant'}';
    final text = '${msg['text'] ?? ''}';
    final isUser = sender == 'user';

    List<Map<String, Object?>>? options;
    if (!isUser) {
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
    }

    Widget content;
    if (isUser) {
      content = SelectableText(
        text,
        style: const TextStyle(color: Colors.white, fontSize: 13),
      );
    } else {
      content = MarkdownBody(
        data: text,
        selectable: true,
        onTapLink: (_, href, _) => onLinkTap(href),
        styleSheet: MarkdownStyleSheet(
          p: const TextStyle(color: Tokens.text, fontSize: 13),
          strong: const TextStyle(
            color: Tokens.text,
            fontSize: 13,
            fontWeight: FontWeight.w700,
          ),
          em: const TextStyle(
            color: Tokens.text,
            fontSize: 13,
            fontStyle: FontStyle.italic,
          ),
          h1: const TextStyle(
            color: Tokens.text,
            fontSize: 18,
            fontWeight: FontWeight.w700,
          ),
          h2: const TextStyle(
            color: Tokens.text,
            fontSize: 16,
            fontWeight: FontWeight.w700,
          ),
          h3: const TextStyle(
            color: Tokens.text,
            fontSize: 14,
            fontWeight: FontWeight.w700,
          ),
          a: TextStyle(color: accent, decoration: TextDecoration.underline),
          code: const TextStyle(
            color: Tokens.text,
            backgroundColor: Tokens.surface,
            fontFamily: 'monospace',
            fontSize: 12,
          ),
          codeblockDecoration: BoxDecoration(
            color: Tokens.surface,
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: Tokens.border),
          ),
          listBullet: const TextStyle(color: Tokens.text, fontSize: 13),
        ),
      );
    }

    if (options != null) {
      content = Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          content,
          Container(
            height: 1,
            color: const Color(0xFF262626),
            margin: const EdgeInsets.symmetric(vertical: 6),
          ),
          for (final (i, opt) in options.indexed) ...[
            if (i > 0) const SizedBox(height: 4),
            _OptionButton(
              id: '${opt['id'] ?? ''}',
              title: '${opt['title'] ?? ''}',
              subtitle: '${opt['subtitle'] ?? ''}',
              accent: accent,
              onTap: () => onOptionTap('${opt['id'] ?? ''}'),
            ),
          ],
        ],
      );
    }

    final wide = text.length > 25 || options != null;
    final bubble = Container(
      constraints: BoxConstraints(maxWidth: 290, minWidth: wide ? 290 : 0),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: isUser ? accent : Tokens.surface2,
        borderRadius: BorderRadius.circular(14),
      ),
      child: content,
    );

    return Row(
      mainAxisAlignment: isUser
          ? MainAxisAlignment.end
          : MainAxisAlignment.start,
      children: [Flexible(child: bubble)],
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
    final btn = OutlinedButton(
      style: OutlinedButton.styleFrom(
        foregroundColor: accent,
        side: BorderSide(color: accent),
      ),
      onPressed: onTap,
      child: Text('$id. $title'),
    );
    return subtitle.isNotEmpty ? Tooltip(message: subtitle, child: btn) : btn;
  }
}
