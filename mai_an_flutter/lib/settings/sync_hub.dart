import 'dart:async';
import 'dart:io';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../core/progress_notify.dart';
import '../core/py_num.dart';
import '../data/library_repository.dart' show kFeaturesVersion;
import '../sync/app_update.dart';
import '../sync/app_update_view.dart';
import '../sync/companion_client.dart'
    show CompanionAuthError, defaultCompanionPort, isNetworkError;
import '../sync/offload_library.dart';
import '../sync/problem_reports.dart';
import '../sync/state_bundles.dart';
import '../sync/sync_manager.dart';
import '../ui/app_switch.dart';
import '../ui/tokens.dart';
import '../ui/page_header.dart';
import 'settings_page.dart';

/// ui/views/sync_hub.py, the Android view: Settings → Companion & Sync.
/// The connection card (the switch, its status, Find Mac / Test
/// Connection, Unpair, Manual IP and the last diagnosis), Transfers from
/// Mac, DSP Acoustic Offload, and Library State Backup.
class SyncHubGroup extends StatefulWidget {
  const SyncHubGroup({
    super.key,
    required this.manager,
    this.bundles,
    this.library,
  });
  final CompanionSyncManager manager;

  /// The offload card's library; null leaves the card out (tests).
  final OffloadLibrary? library;

  /// The backup card's bundle; null leaves the card out (tests).
  final StateBundles? bundles;

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

  // The offload card.
  String _dspText = 'Checking library acoustic features...';
  int? _missing;
  bool _offloading = false;
  bool _cancelOffload = false;
  double _offloadProgress = 0;
  String _offloadDetail = '';

  /// Live at the last manager change: the feature count is a library
  /// query, re-run only when the link comes up (as Flet's
  /// `_was_connected`), not on every change.
  bool _wasLive = false;

  @override
  void initState() {
    super.initState();
    m.addListener(_changed);
    if (_live) unawaited(refreshPending());
    _wasLive = _live;
    if (_live) unawaited(refreshDspStats());
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
    if (_live && !_wasLive) unawaited(refreshDspStats());
    _wasLive = _live;
  }

  void _snack(String text, {SyncIcon? icon, bool red = false}) =>
      m.host.snack(text, icon: icon, red: red);

  Future<void> _reportProblem(ProblemReporter reporter) async {
    final text = await showDialog<String>(
      context: context,
      builder: (context) => const _ReportDialog(),
    );
    if (text == null || text.trim().isEmpty || !mounted) return;
    final sent = await reporter.report(text);
    if (!mounted) return;
    _snack(
      sent ? 'Sent to your Mac' : 'Saved: it goes to your Mac when it connects',
      icon: sent ? SyncIcon.check : SyncIcon.upload,
    );
  }

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

  /// `_refresh_dsp_stats`: how many tracks lack current features.
  Future<void> refreshDspStats() async {
    final library = widget.library;
    if (library == null) return;
    String text;
    int? missing;
    try {
      final paths = await library.tracksMissingFeatures();
      if (paths == null) return;
      missing = paths.length;
      text = missing == 0
          ? 'All tracks analyzed. Similarity walks and auto-play are ready.'
          : '$missing track${missing != 1 ? 's' : ''} require acoustic '
                'analysis.';
    } catch (e) {
      debugPrint('Failed to refresh DSP stats: $e');
      text = 'Status check unavailable: $e';
    }
    if (!mounted) return;
    setState(() {
      _dspText = text;
      _missing = missing;
    });
  }

  /// `_do_start_offload`: the tracks without current features sent to the
  /// Mac (its cache answers for those it has seen), their features stored
  /// as they come, then the similarity graph rebuilt. The `sync`
  /// notification carries the progress and its Cancel; the link is held
  /// awake throughout.
  Future<void> startOffload() async {
    final messenger = ScaffoldMessenger.of(context);
    final accent = Theme.of(context).colorScheme.primary;
    final client = m.client;
    final library = widget.library;
    if (client == null || !m.isConnected) {
      showSnackOn(
        messenger,
        'Connect and pair with your Mac first',
        color: Tokens.amber,
      );
      return;
    }
    if (library == null || _offloading) return;
    _offloading = true;
    final List<String> missing;
    try {
      missing = await library.tracksMissingFeatures() ?? const [];
    } catch (e) {
      _offloading = false;
      rethrow;
    }
    if (missing.isEmpty) {
      _offloading = false;
      showSnackOn(
        messenger,
        'All tracks already analysed!',
        icon: Icons.check_circle,
      );
      return;
    }
    if (mounted) {
      setState(() {
        _cancelOffload = false;
        _offloadProgress = 0;
        _offloadDetail = 'Initiating offload...';
      });
    }
    final notifier = ProgressNotifier(
      'sync',
      'DSP Offload to Mac',
      onCancel: () {
        _cancelOffload = true;
        debugPrint('DSP offload cancelled via system notification');
      },
    );
    var summary = '';

    void progress(int done, int total, String current, double rate) {
      final r = pyRound1(rate).toStringAsFixed(1);
      notifier.update(done, total, '$done/$total tracks ($r t/s) · $current');
      // The notification carries progress while hidden.
      if (m.host.inBackground || !mounted) return;
      setState(() {
        _offloadProgress = done / math.max(1, total);
        _offloadDetail = 'Processed $done/$total ($r tracks/s) • $current';
      });
    }

    try {
      await m.activity(() async {
        try {
          notifier.stage('Sending ${missing.length} tracks to Mac…');
          final res = await client.offloadDsp(
            missing,
            persist: library.storeFeatureRows,
            progress: progress,
            cancelled: () => _cancelOffload,
            featuresVersion: kFeaturesVersion,
          );
          if (_cancelOffload) {
            summary =
                'DSP offload cancelled (${res.analysed} of '
                '${missing.length} done).';
          } else if (res.failed > 0) {
            summary =
                'Offload finished: ${res.analysed} analysed, '
                '${res.failed} failed.';
          } else {
            summary = 'Offload complete: ${res.analysed} tracks analysed.';
          }
          showSnackOn(
            messenger,
            summary,
            icon: Icons.check_circle,
            color: accent,
          );
          if (res.analysed > 0) {
            notifier.stage('Rebuilding similarity graph…');
            await library.rebuildSimilarityGraph();
          }
        } on CompanionAuthError {
          summary =
              'DSP offload stopped: the Mac no longer accepts this phone. '
              'Pair again.';
          showSnackOn(messenger, summary, color: Tokens.accentRed);
          await m.unpair();
        } catch (e) {
          if (isNetworkError(e)) {
            summary =
                'DSP offload stopped: lost connection to the Mac. Progress '
                'so far was saved.';
            showSnackOn(messenger, summary, color: Tokens.accentRed);
          } else {
            debugPrint('DSP offload failed: $e');
            summary = 'DSP offload failed: $e';
            showSnackOn(
              messenger,
              'Offload error: $e',
              color: Tokens.accentRed,
            );
          }
        } finally {
          // Always finish: an unfinished notifier keeps the native task
          // service (and its wakelock) alive.
          notifier.finish(summary);
        }
      });
    } finally {
      _offloading = false;
      if (mounted) setState(() {});
      await refreshDspStats();
    }
  }

  void _cancelOffloadTap() {
    _cancelOffload = true;
    showSnackOn(
      ScaffoldMessenger.of(context),
      'Cancelling offload...',
      icon: Icons.cancel_rounded,
    );
  }

  bool _uploading = false;

  /// `_do_upload_bundle`: a fresh timestamped bundle, sent to the Mac with
  /// the link held awake, then deleted here (the Mac has it). The `sync`
  /// notification follows it. A second tap while one runs does nothing.
  Future<void> uploadBundle() async {
    final bundles = widget.bundles;
    final client = m.client;
    final messenger = ScaffoldMessenger.of(context);
    if (bundles == null || _uploading) return;
    if (client == null || !m.isConnected) {
      showSnackOn(messenger, 'Connect to Mac first', color: Tokens.amber);
      return;
    }
    _uploading = true;
    final notifier = ProgressNotifier('sync', 'State Backup Upload')
      ..stage('Creating snapshot bundle…');
    showSnackOn(
      messenger,
      'Creating snapshot bundle...',
      icon: Icons.ios_share_rounded,
    );
    var summary = '';
    try {
      await m.activity(() async {
        try {
          final path = await bundles.export();
          notifier.stage('Transmitting bundle to Mac…');
          showSnackOn(
            messenger,
            'Transmitting bundle to Mac...',
            icon: Icons.cloud_upload_rounded,
          );
          final res = await client.uploadStateBundle(path);
          final saved = '${res['saved_path'] ?? 'mai_an_lab_state.zip'}';
          summary = 'Backup saved on Mac: ${saved.split('/').last}';
          showSnackOn(messenger, summary, icon: Icons.check_circle);
          try {
            File(path).deleteSync();
          } on FileSystemException {
            // Already gone.
          }
        } catch (e) {
          summary = 'Backup upload failed: $e';
          showSnackOn(messenger, 'Backup failed: $e', color: Tokens.accentRed);
        } finally {
          notifier.finish(summary);
        }
      });
    } finally {
      _uploading = false;
    }
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
        AppSwitch(
          key: const ValueKey('sync-switch'),
          value: enabled,
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
          AppSwitch(
            key: const ValueKey('ask-switch'),
            value: m.askBeforeDownload,
            onChanged: (v) => unawaited(m.setAskBeforeDownload(v)),
          ),
        ],
      ),
      const Dim(
        'To send the other way, long-press a track or album and choose Send '
        'to your Mac.',
      ),
    ]);

    final offload = card([
      head(Icons.graphic_eq_rounded, Tokens.amber, 'DSP Acoustic Offload'),
      Text(_dspText, style: const TextStyle(color: Tokens.text, fontSize: 13)),
      if (_offloading) ...[
        ClipRRect(
          borderRadius: BorderRadius.circular(2),
          child: LinearProgressIndicator(
            value: _offloadProgress,
            minHeight: 4,
            color: accent,
            backgroundColor: Tokens.surface,
          ),
        ),
        Text(
          _offloadDetail,
          style: const TextStyle(color: Tokens.dim, fontSize: 12),
        ),
      ],
      Wrap(
        spacing: 8,
        runSpacing: 4,
        children: [
          FilledButton.icon(
            key: const ValueKey('offload'),
            style: pillButton,
            icon: const Icon(Icons.bolt_rounded, size: 18),
            label: const Text('Offload to Mac'),
            // Never while one runs (a second would race it), nor once the
            // count says every track is analysed.
            onPressed: linked && !_offloading && _missing != 0
                ? startOffload
                : null,
          ),
          if (_offloading)
            TextButton.icon(
              icon: const Icon(Icons.cancel_rounded, size: 18),
              label: const Text('Cancel'),
              onPressed: _cancelOffloadTap,
            ),
        ],
      ),
      const Dim(
        'Offload acoustic feature extraction (BPM, timbre, key) to your Mac '
        'for high-speed batch processing.',
      ),
    ]);

    final backup = card([
      head(Icons.archive_rounded, accent, 'Library State Backup'),
      const Dim(
        'Back up your library database, playback history, and similarity '
        'graph to your Mac.',
      ),
      Wrap(
        children: [
          TextButton.icon(
            icon: const Icon(Icons.cloud_upload_rounded, size: 18),
            label: const Text('Send Backup to Mac'),
            onPressed: linked ? uploadBundle : null,
          ),
        ],
      ),
    ]);

    final updater = m.updater;
    final appUpdate = updater == null
        ? null
        : ListenableBuilder(
            listenable: updater,
            builder: (context, _) {
              final u = updater;
              final hot =
                  u.state != AppUpdateState.none &&
                  u.state != AppUpdateState.upToDate;
              return card([
                head(
                  Icons.system_update_rounded,
                  hot ? Tokens.accent : Tokens.dim,
                  'App Updates',
                  hot
                      ? Container(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 8,
                            vertical: 2,
                          ),
                          decoration: BoxDecoration(
                            color: Tokens.accent,
                            borderRadius: BorderRadius.circular(99),
                          ),
                          child: Text(
                            u.offerLabel,
                            style: const TextStyle(
                              color: Tokens.bg,
                              fontSize: 11,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        )
                      : null,
                ),
                if (!linked && !hot)
                  Dim(
                    'Builds you make on your Mac (flutter build apk, or an APK '
                    'dropped on the Companion page) install from here. '
                    'This phone runs ${u.build?.label ?? 'an unknown build'}.',
                  )
                else
                  AppUpdatePanel(updater: u),
              ]);
            },
          );

    final reporter = m.reporter;
    final problems = reporter == null
        ? null
        : ListenableBuilder(
            listenable: reporter,
            builder: (context, _) {
              final waiting = reporter.pending.length;
              return card([
                head(Icons.bug_report_rounded, Tokens.dim, 'Report a Problem'),
                Dim(
                  'Describe what went wrong; the app\'s recent log goes with '
                  'it to the Companion page on your Mac. Errors the app '
                  'catches are sent there on their own.'
                  '${waiting == 0 ? '' : ' $waiting waiting for the Mac.'}',
                ),
                Wrap(
                  children: [
                    TextButton.icon(
                      icon: const Icon(Icons.edit_note_rounded, size: 18),
                      label: const Text('Report a Problem'),
                      onPressed: () => _reportProblem(reporter),
                    ),
                  ],
                ),
              ]);
            },
          );

    return ListView(
      padding: EdgeInsets.fromLTRB(28, HeaderInsets.topOf(context), 28, 100),
      children: [
        connection,
        if (appUpdate != null) ...[const SizedBox(height: 16), appUpdate],
        if (problems != null) ...[const SizedBox(height: 16), problems],
        const SizedBox(height: 16),
        transfers,
        if (widget.library != null) ...[const SizedBox(height: 16), offload],
        if (widget.bundles != null) ...[const SizedBox(height: 16), backup],
      ],
    );
  }
}

/// What went wrong, in the user's words.
class _ReportDialog extends StatefulWidget {
  const _ReportDialog();

  @override
  State<_ReportDialog> createState() => _ReportDialogState();
}

class _ReportDialogState extends State<_ReportDialog> {
  final _text = TextEditingController();

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text(
        'Report a Problem',
        style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
      ),
      content: SizedBox(
        width: math.min(380.0, MediaQuery.sizeOf(context).width - 48),
        child: TextField(
          key: const ValueKey('report-text'),
          controller: _text,
          autofocus: true,
          minLines: 4,
          maxLines: 8,
          textCapitalization: TextCapitalization.sentences,
          onChanged: (_) => setState(() {}),
          decoration: const InputDecoration(
            hintText: 'What happened, and what were you doing?',
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('Cancel'),
        ),
        FilledButton(
          style: FilledButton.styleFrom(
            backgroundColor: Tokens.accent,
            foregroundColor: Tokens.bg,
          ),
          onPressed: _text.text.trim().isEmpty
              ? null
              : () => Navigator.pop(context, _text.text),
          child: const Text('Send'),
        ),
      ],
    );
  }
}
