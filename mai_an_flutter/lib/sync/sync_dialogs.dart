import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../ui/tokens.dart';
import 'sync_manager.dart';

/// manager.py's dialog width: `min(380, max(280, page_w - 48))`.
double _dialogWidth(BuildContext context) =>
    math.min(380, math.max(280, MediaQuery.sizeOf(context).width - 48));

Widget _title(IconData icon, Color color, String text) => Row(
  children: [
    Icon(icon, color: color, size: 24),
    const SizedBox(width: 8),
    Expanded(
      child: Text(
        text,
        style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
      ),
    ),
  ],
);

/// manager.py `prompt_pin_dialog`: the Mac's 4-digit PIN. Connect pairs
/// (the manager closes the dialog on success); Not now, back or a tap
/// outside snooze this Mac for two minutes.
class PinDialog extends StatefulWidget {
  const PinDialog({super.key, required this.prompt});
  final PinPrompt prompt;

  @override
  State<PinDialog> createState() => _PinDialogState();
}

class _PinDialogState extends State<PinDialog> {
  final _pin = TextEditingController();
  bool _hidden = true;
  bool _pairing = false;
  String? _error;

  @override
  void dispose() {
    _pin.dispose();
    super.dispose();
  }

  Future<void> _pair() async {
    if (_pairing) return;
    final pin = _pin.text.trim();
    if (pin.length != 4 || !RegExp(r'^[0-9]{4}$').hasMatch(pin)) {
      setState(() => _error = 'Please enter the 4-digit PIN.');
      return;
    }
    setState(() {
      _error = null;
      _pairing = true;
    });
    final err = await widget.prompt.submit(pin);
    if (!mounted) return;
    setState(() {
      _error = err;
      _pairing = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final p = widget.prompt;
    return AlertDialog(
      title: _title(
        Icons.phonelink_setup_rounded,
        accent,
        'Pair with ${p.name}',
      ),
      content: SizedBox(
        width: _dialogWidth(context),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              'Found ${p.name} (${p.via}). Enter the 4-digit PIN shown in '
              'Settings → Companion & Sync on your Mac.',
              style: const TextStyle(color: Tokens.dim, fontSize: 13),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _pin,
              autofocus: true,
              obscureText: _hidden,
              keyboardType: TextInputType.number,
              textAlign: TextAlign.center,
              maxLength: 4,
              style: const TextStyle(fontSize: 20),
              onSubmitted: (_) => _pair(),
              decoration: InputDecoration(
                labelText: '4-Digit PIN',
                hintText: '0000',
                filled: false,
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(8),
                  borderSide: const BorderSide(color: Tokens.border),
                ),
                suffixIcon: IconButton(
                  icon: Icon(
                    _hidden
                        ? Icons.visibility_outlined
                        : Icons.visibility_off_outlined,
                  ),
                  onPressed: () => setState(() => _hidden = !_hidden),
                ),
              ),
            ),
            if (_error case final e?) ...[
              const SizedBox(height: 12),
              Text(
                e,
                style: const TextStyle(color: Tokens.accentRed, fontSize: 12),
              ),
            ],
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('Not now'),
        ),
        FilledButton(
          style: FilledButton.styleFrom(
            backgroundColor: accent,
            foregroundColor: Tokens.bg,
          ),
          onPressed: _pairing ? null : _pair,
          child: Text(_pairing ? 'Pairing…' : 'Connect'),
        ),
      ],
    );
  }
}

/// manager.py `prompt_transfer_dialog`: what the Mac sent, and Download
/// (to the dock) or Later.
class TransferDialog extends StatelessWidget {
  const TransferDialog({super.key, required this.prompt});
  final TransferPrompt prompt;

  @override
  Widget build(BuildContext context) {
    final p = prompt;
    final count = p.transfers.length;
    String text(Object? v, String fallback) =>
        (v == null || v == '') ? fallback : '$v';
    return AlertDialog(
      title: _title(
        Icons.phonelink_ring_rounded,
        Tokens.accentGreen,
        'Transfers from ${p.macName}',
      ),
      content: SizedBox(
        width: _dialogWidth(context),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              p.summary,
              style: const TextStyle(color: Tokens.dim, fontSize: 13),
            ),
            const SizedBox(height: 10),
            Container(
              height: math.min(260, 44.0 * count + 16),
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              decoration: BoxDecoration(
                border: Border.all(color: Tokens.border),
                borderRadius: BorderRadius.circular(10),
              ),
              child: ListView.separated(
                itemCount: count,
                separatorBuilder: (_, _) => const SizedBox(height: 8),
                itemBuilder: (_, i) {
                  final t = p.transfers[i];
                  return Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text(
                        text(t['title'], 'Unknown'),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 13),
                      ),
                      Text(
                        text(t['artist'], ''),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 11, color: Tokens.dim),
                      ),
                    ],
                  );
                },
              ),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('Later'),
        ),
        FilledButton(
          style: FilledButton.styleFrom(
            backgroundColor: Tokens.accentGreen,
            foregroundColor: Tokens.bg,
          ),
          onPressed: p.download,
          child: Text('Download ($count)'),
        ),
      ],
    );
  }
}
