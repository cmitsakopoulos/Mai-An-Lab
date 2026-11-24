import 'dart:async';

import 'package:flutter/material.dart';

import '../data/library_repository.dart' show kFeaturesVersion;
import '../sync/companion_client.dart' show defaultCompanionPort;
import '../sync/sync_manager.dart';
import '../ui/tokens.dart';
import 'settings_page.dart';

/// ui/views/sync_hub.py, the Android view: Settings → Companion & Sync.
/// The connection card (the switch, its status, Find Mac / Test
/// Connection, Unpair, Manual IP and the last diagnosis) and Transfers
/// from Mac. The DSP offload and backup cards come with stages g and f.
class SyncHubGroup extends StatefulWidget {
  const SyncHubGroup({super.key, required this.manager});
  final CompanionSyncManager manager;

  @override
  State<SyncHubGroup> createState() => SyncHubGroupState();
}

@visibleForTesting
class SyncHubGroupState extends State<SyncHubGroup> {
  CompanionSyncManager get m => widget.manager;

  bool _finding = false;
  bool _manual = false;
  final _ip = TextEditingController();

  /// What the last Find Mac / manual connect learned, kept on screen: a
  /// snackbar is gone before anyone reads a network diagnosis.
  String _diag = '';
  bool _diagOk = false;

  String _transfersText = 'Connect to Mac to check pending transfers.';
  int _pending = 0;

  @override
  void initState() {
    super.initState();
    m.addListener(_changed);
    if (_live) unawaited(refreshPending());
  }

  @override
  void dispose() {
    m.removeListener(_changed);
    _ip.dispose();
    super.dispose();
  }

  bool get _live => m.enabled && !m.isAsleep && m.isConnected;

  /// `_on_manager_state_changed`: connected, the pending count is asked for
  /// again (as Flet does on every state change).
  void _changed() {
    if (!mounted) return;
    setState(() {});
    if (_live) unawaited(refreshPending());
  }

  void _snack(String text, {SyncIcon? icon, bool red = false}) =>
      m.host.snack(text, icon: icon, red: red);

  /// `_refresh_pending_transfers`.
  Future<void> refreshPending() async {
    if (!m.isConnected || m.client == null) {
      setState(() {
        _transfersText = 'Connect to Mac to check pending transfers.';
        _pending = 0;
      });
      return;
    }
    final transfers = await m.checkPendingTransfers();
    if (!mounted) return;
    final n = transfers.length;
    setState(() {
      _pending = n;
      _transfersText = n == 0
          ? 'No transfers waiting from Mac.'
          : '$n track${n != 1 ? 's' : ''} ready to download from Mac.';
    });
  }

  Future<void> _downloadTransfers() async {
    if (!m.isConnected) return;
    final transfers = await m.checkPendingTransfers();
    if (!mounted) return;
    if (transfers.isEmpty) {
      _snack('No pending transfers found on Mac.');
      return;
    }
    final n = m.downloadTransfers(transfers);
    final noun = '$n track${n != 1 ? 's' : ''}';
    _snack('Queued $noun in Download Dock', icon: SyncIcon.check);
    setState(() {
      _transfersText = 'Queued $noun to Download Dock.';
      _pending = 0;
    });
  }

  void _showDiagnosis(String text, {bool ok = false}) => setState(() {
    _diag = text;
    _diagOk = ok;
  });

  bool get _found => m.isConnected || m.pinPromptOpen;

  /// `_do_find`.
  Future<void> _find() async {
    if (_finding) return;
    setState(() => _finding = true);
    _showDiagnosis(
      'Trying USB, the last known address, a broadcast query, then a scan '
      'of this network…',
    );
    String result;
    try {
      result = await m.findMac();
    } catch (e) {
      debugPrint('Find Mac failed: $e');
      result = 'Search failed: $e';
    }
    if (!mounted) return;
    _finding = false;
    _showDiagnosis(result, ok: _found);
    if (!_found && !_manual) setState(() => _manual = true);
  }

  Future<void> _toggle(bool on) async {
    if (on) {
      await m.start();
      _snack('Searching for Mac Companion…', icon: SyncIcon.radar);
    } else {
      await m.stop();
      _snack('Companion Sync disconnected', icon: SyncIcon.stop);
    }
  }

  /// `_on_manual_connect_click` and `_do_manual_connect`.
  Future<void> _connectManual() async {
    final ip = _ip.text.trim();
    if (ip.isEmpty) {
      _snack('Please enter an IP address', red: true);
      return;
    }
    final i = ip.indexOf(':');
    final host = i < 0 ? ip : ip.substring(0, i);
    final portText = i < 0 ? '' : ip.substring(i + 1);
    final port = portText.isEmpty
        ? defaultCompanionPort
        : int.tryParse(portText.trim());
    if (port == null) {
      _snack('Invalid port in $ip', red: true);
      return;
    }
    _showDiagnosis('Connecting to $host:$port…');
    final result = await m.connectManual(host, port);
    if (!mounted) return;
    _showDiagnosis(result, ok: _found);
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final enabled = m.enabled;
    final asleep = m.isAsleep;
    final connected = m.isConnected;

    Color dot = Tokens.dim;
    var status = 'Offline · Inactive';
    var linked = false;
    if (enabled && asleep) {
      status = 'Paused after 15 min idle · tap Find Mac to resume';
    } else if (!enabled) {
      // Offline · Inactive
    } else if (connected) {
      linked = true;
      dot = Tokens.accentGreen;
      final s = m.discoveredServer;
      final name = '${s?['name'] ?? 'Mac Companion'}';
      final ip = '${s?['ip'] ?? ''}';
      final via = (ip == '127.0.0.1' || ip == '::1') ? 'USB' : ip;
      status = 'Connected · $name ($via)';
      final macFv = s?['features_version'];
      if (macFv != null && macFv != 0 && macFv != kFeaturesVersion) {
        status +=
            ' · Mac app is on analysis v$macFv, this phone '
            'v$kFeaturesVersion: update both';
      }
    } else if (m.isSearching) {
      dot = Tokens.amber;
      final err = m.lastError;
      status = err.isNotEmpty
          ? '$err · searching…'
          : 'Searching for Mac on network or USB…';
    }

    final findLabel = _finding
        ? 'Searching…'
        : (connected ? 'Test Connection' : 'Find Mac');

    Widget card(List<Widget> children) => SettingsCard(
      radius: Tokens.radiusCard,
      borderColor: Tokens.borderSubtle,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (final (i, c) in children.indexed) ...[
            if (i > 0) const SizedBox(height: 10),
            c,
          ],
        ],
      ),
    );

    Widget head(IconData icon, Color color, String title, [Widget? trailing]) =>
        Row(
          children: [
            Icon(icon, color: color, size: 22),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                title,
                style: const TextStyle(
                  color: Tokens.text,
                  fontSize: 15,
                  fontWeight: FontWeight.bold,
                ),
              ),
            ),
            ?trailing,
          ],
        );

    final pillButton = FilledButton.styleFrom(
      backgroundColor: accent,
      foregroundColor: Tokens.bg,
    );

    final connection = card([
      head(
        Icons.phonelink_setup_rounded,
        accent,
        'Companion Sync',
        Switch(
          key: const ValueKey('sync-switch'),
          value: enabled,
          activeTrackColor: accent,
          onChanged: _toggle,
        ),
      ),
      Row(
        children: [
          Container(
            width: 10,
            height: 10,
            decoration: BoxDecoration(color: dot, shape: BoxShape.circle),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              status,
              style: const TextStyle(color: Tokens.dim, fontSize: 13),
            ),
          ),
        ],
      ),
      const Dim(
        'Automatically discovers your Mac Companion over Wi-Fi, hotspot, or '
        'USB to sync tracks and offload acoustic analysis. Pauses after 15 '
        'minutes without use to save battery.',
      ),
      Wrap(
        spacing: 8,
        runSpacing: 4,
        children: [
          FilledButton.icon(
            style: pillButton,
            icon: const Icon(Icons.radar_rounded, size: 18),
            label: Text(findLabel),
            onPressed: _finding ? null : _find,
          ),
          if (linked)
            TextButton.icon(
              icon: const Icon(Icons.link_off_rounded, size: 18),
              label: const Text('Unpair Device'),
              onPressed: () => unawaited(m.unpair()),
            ),
          TextButton.icon(
            icon: const Icon(Icons.router_rounded, size: 18),
            label: Text(_manual ? 'Hide Manual IP' : 'Manual IP Connection…'),
            onPressed: () => setState(() => _manual = !_manual),
          ),
        ],
      ),
      if (_diag.isNotEmpty)
        SelectableText(
          _diag,
          style: TextStyle(
            color: _diagOk ? Tokens.accentGreen : Tokens.dim,
            fontSize: 12,
          ),
        ),
      if (_manual)
        Row(
          children: [
            Expanded(
              child: TextField(
                key: const ValueKey('manual-ip'),
                controller: _ip,
                keyboardType: TextInputType.url,
                onSubmitted: (_) => _connectManual(),
                decoration: InputDecoration(
                  labelText: 'Manual Mac IP',
                  hintText: 'e.g. 192.168.1.50 or 192.168.1.50:8946',
                  isDense: true,
                  filled: false,
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: const BorderSide(color: Tokens.border),
                  ),
                ),
              ),
            ),
            const SizedBox(width: 8),
            FilledButton.icon(
              style: pillButton,
              icon: const Icon(Icons.link_rounded, size: 18),
              label: const Text('Connect'),
              onPressed: _connectManual,
            ),
          ],
        ),
    ]);

    final transfers = card([
      head(
        Icons.phonelink_ring_rounded,
        Tokens.accentGreen,
        'Transfers from Mac',
      ),
      Text(
        _transfersText,
        style: const TextStyle(color: Tokens.dim, fontSize: 13),
      ),
      Wrap(
        spacing: 8,
        runSpacing: 4,
        children: [
          FilledButton.icon(
            style: FilledButton.styleFrom(
              backgroundColor: Tokens.accentGreen,
              foregroundColor: Tokens.bg,
            ),
            icon: const Icon(Icons.download_for_offline_rounded, size: 18),
            label: Text(
              _pending > 0
                  ? 'Download ($_pending) from Mac'
                  : 'Download from Mac',
            ),
            onPressed: linked && _pending > 0 ? _downloadTransfers : null,
          ),
          TextButton.icon(
            icon: const Icon(Icons.refresh_rounded, size: 18),
            label: const Text('Check Pending'),
            onPressed: linked ? refreshPending : null,
          ),
        ],
      ),
      Row(
        children: [
          const Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Ask before downloading',
                  style: TextStyle(
                    color: Tokens.text,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                SizedBox(height: 1),
                Dim(
                  'Off: tracks sent from the Mac download on their own',
                  size: 11,
                ),
              ],
            ),
          ),
          Switch(
            key: const ValueKey('ask-switch'),
            value: m.askBeforeDownload,
            activeTrackColor: accent,
            onChanged: (v) => unawaited(m.setAskBeforeDownload(v)),
          ),
        ],
      ),
      const Dim(
        'To send the other way, long-press a track or album and choose Send '
        'to your Mac.',
      ),
    ]);

    return ListView(
      padding: const EdgeInsets.fromLTRB(28, 0, 28, 100),
      children: [connection, const SizedBox(height: 16), transfers],
    );
  }
}
