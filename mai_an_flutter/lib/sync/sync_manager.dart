import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../core/progress_notify.dart';
import 'companion_client.dart';
import 'discovery.dart';

/// utils/sync/manager.py: the phone's Companion & Sync coordinator.
/// Discovery (beacons, the USB tunnel and the saved address), PIN pairing,
/// the heartbeat, the idle auto-off, transfers offered by the Mac and the
/// Send to Mac worker. The dialogs and snackbars are the host's
/// ([SyncHost]), so this runs and is tested without widgets.

const _probeInterval = Duration(seconds: 4);
const _heartbeatInterval = Duration(seconds: 15);

/// In the background the heartbeat stands down (battery), but a transfer
/// sent from the Mac right after the phone is pocketed should still be
/// noticed: one cheap poll this often, until the idle auto-off.
const _bgTransferPoll = Duration(seconds: 60);

/// Missed heartbeats before the link is declared down: one miss is common
/// on Wi-Fi power-save wakeups; two in a row (~30 s) is a gone Mac.
const _missesBeforeDrop = 2;

/// After a cancelled PIN prompt (or an unpair) the same Mac is not prompted
/// for again for this long: the probe and the beacon would reopen it at once.
const _pinSnooze = Duration(seconds: 120);
const _loopback = {'127.0.0.1', '::1', 'localhost'};

/// Idle auto-off: no pairing, transfer, offload or user tap for this long
/// and discovery, probing and heartbeats stop until asked again.
const idleTimeout = Duration(minutes: 15);

/// Transfers handed to the download queue keep the link up, but a job the
/// queue dropped without reporting must not keep it up forever.
const _inFlightGrace = Duration(hours: 1);

/// How long Find Mac waits for a query reply before sweeping.
const _findQueryWait = Duration(seconds: 3);

/// Off (the default): tracks the Mac sends download without a prompt.
const askBeforeDownloadPref = 'companion_ask_before_download';

/// Failed downloads of one transfer before auto-accept leaves it to the
/// dialog.
const _autoAcceptMaxFailures = 2;

/// Consecutive network failures before a Send to Mac batch gives up.
const _maxSendNetFailures = 3;

const _qualityLabels = {
  'aac_vbr_max': 'AAC VBR',
  'aac_320k': 'AAC 320',
  'aac_256k': 'AAC 256',
  'aac_192k': 'AAC 192',
};

/// `transfer_quality_label`: the AAC profile the Mac encodes to, or for
/// "original" the source file's format (FLAC, MP3…).
String transferQualityLabel(Map<String, Object?> t) {
  final q = _pyStr(t['quality'], 'original');
  if (_qualityLabels[q] case final label?) return label;
  final ext = _pyStr(t['extension'], '').toUpperCase();
  return ext.isEmpty ? 'ORIGINAL' : ext;
}

/// Python's `str(x or default)`.
String _pyStr(Object? v, String fallback) {
  if (v == null || v == '' || v == false || v == 0) return fallback;
  return '$v';
}

String _plural(int n, String noun) => '$n $noun${n != 1 ? 's' : ''}';

// errno values: Darwin, then Linux/Android.
const _refused = {61, 111};
const _unreachable = {65, 51, 113, 101};

/// `_explain_connect_error`: why host:port didn't answer, in terms the user
/// can act on. Refused: the Mac is reachable and its server is off. Silence:
/// the network (or the Mac being away) is in the way.
String explainConnectError(Object ex, String host, int port) {
  final code = ex is SocketException ? ex.osError?.errorCode : null;
  if (code != null && _refused.contains(code)) {
    return '$host is reachable, but nothing answers on port $port. Turn on '
        'Companion Server on the Mac, or check the port it shows.';
  }
  if (ex is TimeoutException || (code != null && _unreachable.contains(code))) {
    return 'No answer from $host. Either the Mac is asleep or on another '
        'network, or this network keeps devices apart (office, campus, hotel '
        'and guest Wi-Fi often do). Then only your phone\'s hotspot or a USB '
        'cable will work.';
  }
  if (ex is CompanionHttpError || ex is _NotACompanion) {
    return "Something answered at $host:$port, but it isn't the Mai-An "
        'Companion Server.';
  }
  return 'Could not reach $host:$port ($ex).';
}

class _NotACompanion implements Exception {
  const _NotACompanion();
  @override
  String toString() => 'not a companion server';
}

/// Python 3.12 `ipaddress.IPv4Address.is_private`.
bool isPrivateIpv4(String ip) {
  final p = ip.split('.');
  if (p.length != 4) return false;
  final o = [for (final x in p) int.tryParse(x) ?? -1];
  if (o.any((x) => x < 0 || x > 255)) return false;
  final v = (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3];
  bool inNet(String net, int bits) {
    final n = net.split('.').map(int.parse).toList();
    final base = (n[0] << 24) | (n[1] << 16) | (n[2] << 8) | n[3];
    final mask = bits == 0 ? 0 : (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF;
    return (v & mask) == (base & mask);
  }

  return inNet('0.0.0.0', 8) ||
      inNet('10.0.0.0', 8) ||
      inNet('127.0.0.0', 8) ||
      inNet('169.254.0.0', 16) ||
      inNet('172.16.0.0', 12) ||
      inNet('192.0.0.0', 29) ||
      inNet('192.0.0.170', 31) ||
      inNet('192.0.2.0', 24) ||
      inNet('192.168.0.0', 16) ||
      inNet('198.18.0.0', 15) ||
      inNet('198.51.100.0', 24) ||
      inNet('203.0.113.0', 24) ||
      inNet('240.0.0.0', 4) ||
      ip == '255.255.255.255';
}

String? _deviceName;

/// `device_name`: this phone's model ("Pixel 9 Pro"), which the Mac shows
/// as "Send to Pixel 9 Pro".
Future<String> deviceName() async {
  if (_deviceName case final n?) return n;
  var name = '';
  try {
    final r = await Process.run('getprop', [
      'ro.product.model',
    ]).timeout(const Duration(seconds: 2));
    name = (r.stdout as String).trim();
  } catch (_) {}
  return _deviceName = name.isEmpty ? 'Android Phone' : name;
}

/// Snackbar icons the manager asks for (Flet's ft.Icons in manager.py,
/// send.py and sync_hub.py).
enum SyncIcon {
  linkOff,
  check,
  download,
  error,
  upload,
  sync,
  wifiOff,
  radar,
  stop,
}

/// The download queue's side of transfers (queue_controller.py
/// `enqueue_many`). The port's queue arrives with phase 4d.
abstract class TransferQueue {
  void enqueueMany(
    List<Map<String, Object?>> items, {
    String qualityTier = 'mp3',
  });
}

/// What the manager needs from the app.
abstract class SyncHost {
  Object? pref(String key);
  Future<void> savePref(String key, Object? value);
  bool get inBackground;
  void snack(String text, {SyncIcon? icon, bool red = false});

  /// Show [prompt]; when it goes away (paired, Not now, dismissed) the UI
  /// calls [CompanionSyncManager.pinPromptClosed].
  void showPinPrompt(PinPrompt prompt);
  void dismissPinPrompt(PinPrompt prompt);

  /// Show [prompt]; the UI calls [CompanionSyncManager.transferPromptClosed]
  /// when it goes away.
  void showTransferPrompt(TransferPrompt prompt);
  void dismissTransferPrompt(TransferPrompt prompt);

  TransferQueue? get queue;

  /// server.py `stored_features`: a track's features in wire form, or null.
  Future<Map<String, Object?>?> storedFeatures(String path);
}

/// The PIN dialog's contents and its Connect action.
class PinPrompt {
  PinPrompt._(this._manager, this.name, this.host, this.port);
  final CompanionSyncManager _manager;
  final String name, host;
  final int port;
  bool paired = false;

  /// "USB" for the tunnel, else the address.
  String get via => _loopback.contains(host) ? 'USB' : host;

  /// Connect: null on success (the prompt is dismissed), else the error to
  /// show under the field.
  Future<String?> submit(String pin) => _manager._pairWithPin(this, pin);
}

/// The transfer dialog's contents and its actions.
class TransferPrompt {
  TransferPrompt._(this._manager, this.macName, this.transfers);
  final CompanionSyncManager _manager;
  final String macName;
  final List<Map<String, Object?>> transfers;

  /// "3 tracks · AAC VBR · acoustic features included".
  String get summary {
    final count = transfers.length;
    final labels = {for (final t in transfers) transferQualityLabel(t)}.toList()
      ..sort();
    final withDsp = transfers.where((t) => _truthy(t['dsp_features'])).length;
    var s = '${_plural(count, 'track')} · ${labels.join(', ')}';
    if (withDsp > 0) {
      s += withDsp == count
          ? ' · acoustic features included'
          : ' · features for $withDsp';
    }
    return s;
  }

  /// Download: hand them to the queue.
  void download() {
    _manager.host.dismissTransferPrompt(this);
    _manager.transferPromptClosed(this);
    final n = _manager.downloadTransfers(transfers);
    _manager.host.snack(
      'Queued ${_plural(n, 'track')} in Download Dock',
      icon: SyncIcon.check,
    );
  }
}

bool _truthy(Object? v) =>
    v != null &&
    v != false &&
    v != 0 &&
    v != '' &&
    !(v is Map && v.isEmpty) &&
    !(v is List && v.isEmpty);

typedef ClientFactory =
    CompanionClient Function(
      String host,
      int port,
      String? token,
      String deviceName,
    );

/// A Python asyncio.Lock: [locked] lets a background attempt step aside.
class _Mutex {
  Completer<void>? _held;
  bool get locked => _held != null;
  Future<T> run<T>(Future<T> Function() body) async {
    while (_held != null) {
      await _held!.future;
    }
    final c = _held = Completer<void>();
    try {
      return await body();
    } finally {
      _held = null;
      c.complete();
    }
  }
}

class CompanionSyncManager extends ChangeNotifier {
  CompanionSyncManager(
    this.host, {
    ClientFactory? clientFactory,
    BeaconListener Function(DeviceFound onFound)? listenerFactory,
    Future<String> Function()? localIp,
    Future<List<String>> Function(String ip, int port)? sweep,
    Future<String> Function()? deviceName,
    Stopwatch? clock,
    this.usbPort = defaultCompanionPort,
  }) : _newClient =
           clientFactory ??
           ((h, p, t, n) =>
               CompanionClient(h, port: p, authToken: t, deviceName: n)),
       _newListener =
           listenerFactory ?? ((f) => BeaconListener(onDeviceFound: f)),
       _localIp = localIp ?? resolveLocalIp,
       _sweep = sweep ?? sweepSubnet,
       _deviceNameOf = deviceName ?? _defaultDeviceName,
       _clock = clock ?? (Stopwatch()..start()) {
    _enabled = host.pref('companion_sync_enabled') == true;
    _lastActivity = _clock.elapsed;
  }

  static Future<String> _defaultDeviceName() => deviceName();

  final SyncHost host;
  final ClientFactory _newClient;
  final BeaconListener Function(DeviceFound) _newListener;
  final Future<String> Function() _localIp;
  final Future<List<String>> Function(String, int) _sweep;
  final Future<String> Function() _deviceNameOf;
  final Stopwatch _clock;

  /// Where `adb reverse` puts the Mac (tests move it off the real port).
  final int usbPort;

  bool _enabled = false;
  CompanionClient? client;
  BeaconListener? listener;
  Map<String, Object?>? discoveredServer;
  bool isConnected = false;
  bool isSearching = false;
  String lastError = '';

  /// Idle auto-off. [enabled] (the user's preference) survives it; a
  /// sleeping manager wakes on Find Mac, the switch, or the next launch.
  bool isAsleep = false;
  late Duration _lastActivity;
  int _busy = 0;

  final _connectLock = _Mutex();
  int _misses = 0;
  PinPrompt? _pinPrompt;
  TransferPrompt? _transferPrompt;
  final _pinSnoozedUntil = <String, Duration>{};
  final _promptedTransferIds = <String>{};
  final _notifiedTransferIds = <String>{};
  Duration? _bgPolledAt;

  /// Handed to the download queue and not yet finished. Acked to the Mac
  /// only once the file is on disk, so a failed download stays queued there.
  final _inFlightIds = <String>{};
  final _unackedIds = <String>{};
  final _autoFailures = <String, int>{};

  /// Send to Mac: tracks waiting to be pushed, and whether the worker runs.
  final _sendQueue = <Map<String, Object?>>[];
  Future<void>? _sendTask;
  bool _sendCancelled = false;

  /// Bumped to end the probe and heartbeat loops (asyncio task.cancel).
  int _gen = 0;
  int? _probeGen, _heartbeatGen;

  bool get enabled => _enabled;
  bool get pinPromptOpen => _pinPrompt != null;
  bool get transferPromptOpen => _transferPrompt != null;
  Set<String> get inFlightIds => Set.unmodifiable(_inFlightIds);

  Object? _pref(String key, [Object? fallback]) => host.pref(key) ?? fallback;

  Future<void> _savePref(String key, Object? value) async {
    try {
      await host.savePref(key, value);
    } catch (e) {
      debugPrint('failed to save $key: $e');
    }
  }

  int _prefPort() {
    final v = _pref('companion_paired_port', defaultCompanionPort);
    final n = v is num ? v.toInt() : int.tryParse('$v') ?? 0;
    return n == 0 ? defaultCompanionPort : n;
  }

  // ── idle auto-off ──────────────────────────────────────────────────────────

  /// Mark real use: pairing, transfers, offload, a user tap. Heartbeats and
  /// probes don't count, or the link could never idle out.
  void touch() => _lastActivity = _clock.elapsed;

  /// Hold the link awake for a long job (offload, backup upload).
  Future<T> activity<T>(Future<T> Function() job) async {
    _busy++;
    touch();
    try {
      return await job();
    } finally {
      _busy--;
      touch();
    }
  }

  bool _idleExpired() {
    if (!_enabled || isAsleep || _busy > 0) return false;
    final idle = _clock.elapsed - _lastActivity;
    if (_inFlightIds.isNotEmpty && idle < _inFlightGrace) return false;
    return idle >= idleTimeout;
  }

  Future<bool> _maybeSleep() async {
    if (!_idleExpired()) return false;
    await _flushAcks();
    _sleep();
    return true;
  }

  void _sleep() {
    isAsleep = true;
    _stopListener();
    _gen++;
    isConnected = false;
    isSearching = false;
    client = null;
    lastError = '';
    _misses = 0;
    debugPrint('Companion Sync idle for ${idleTimeout.inMinutes} min; paused');
    notifyListeners();
  }

  // ── lifecycle ──────────────────────────────────────────────────────────────

  /// Turn Companion Sync on and start background discovery.
  Future<void> start() async {
    _enabled = true;
    isAsleep = false;
    touch();
    await _savePref('companion_sync_enabled', true);
    isSearching = !isConnected;
    lastError = '';
    notifyListeners();
    if (!isConnected) _startListener();
    if (_probeGen != _gen) unawaited(_probeLoop());
    if (_heartbeatGen != _gen) unawaited(_heartbeatLoop());
  }

  /// Turn it off: stop discovery and disconnect.
  Future<void> stop() async {
    _enabled = false;
    isAsleep = false;
    await _savePref('companion_sync_enabled', false);
    isSearching = false;
    isConnected = false;
    _stopListener();
    _gen++;
    if (_pinPrompt case final p?) host.dismissPinPrompt(p);
    if (_transferPrompt case final t?) host.dismissTransferPrompt(t);
    client = null;
    notifyListeners();
  }

  /// The UDP query broadcasts every 2 s: never with the screen off. The
  /// loops idle themselves while in the background.
  void handleAppBackground() => _stopListener();

  void handleAppResume() {
    if (!_enabled || isAsleep) return;
    if (_idleExpired()) {
      unawaited(_maybeSleep());
      return;
    }
    if (isConnected) {
      unawaited(_heartbeatOnce());
    } else {
      _startListener();
      unawaited(probeOnce());
    }
  }

  void _startListener() {
    if (listener != null || host.inBackground) return;
    final l = listener = _newListener(_onBeaconReceived);
    unawaited(l.start());
  }

  void _stopListener() {
    listener?.stop();
    listener = null;
  }

  /// Unpair: forget the saved credentials and resume searching.
  Future<void> unpair() async {
    final h = client?.host ?? _pref('companion_paired_host', '')! as String;
    await _savePref('companion_paired_token', '');
    await _savePref('companion_paired_host', '');
    await _savePref('companion_paired_port', defaultCompanionPort);
    await _savePref('companion_paired_name', '');
    if (h.isNotEmpty) _pinSnoozedUntil[h] = _clock.elapsed + _pinSnooze;
    _promptedTransferIds.clear();
    _setDisconnected();
    discoveredServer = null;
    notifyListeners();
    host.snack('Unpaired from Mac Companion.', icon: SyncIcon.linkOff);
  }

  // ── connection state ───────────────────────────────────────────────────────

  void _setConnected(CompanionClient c, Map<String, Object?> info) {
    client = c;
    isConnected = true;
    isSearching = false;
    lastError = '';
    _misses = 0;
    final name = _firstNonEmpty([
      info['name'],
      _pref('companion_paired_name', ''),
    ], 'Mac Companion');
    discoveredServer = {
      'name': name,
      'ip': c.host,
      'port': c.port,
      'features_version': info['features_version'],
    };
    // A USB session's 127.0.0.1 is useless for reconnecting over Wi-Fi
    // later; keep the last LAN address.
    if (!_loopback.contains(c.host)) {
      unawaited(_savePref('companion_paired_host', c.host));
      unawaited(_savePref('companion_paired_port', c.port));
    }
    unawaited(_savePref('companion_paired_name', name));
    _stopListener();
    debugPrint('Connected to $name at ${c.host}:${c.port}');
    notifyListeners();
    unawaited(checkPendingTransfers(promptUser: true));
  }

  void _setDisconnected([String reason = '']) {
    final was = isConnected;
    isConnected = false;
    client = null;
    isSearching = _enabled && !isAsleep;
    lastError = reason;
    _misses = 0;
    if (isSearching) _startListener();
    if (was) {
      final why = reason.isEmpty ? 'disconnected' : reason;
      debugPrint('Companion link down: $why');
    }
    notifyListeners();
  }

  // ── discovery and connection ───────────────────────────────────────────────

  void _onBeaconReceived(Map<String, Object?> server) {
    if (!_enabled || isAsleep || isConnected) return;
    unawaited(handleDiscoveredServer(server));
  }

  /// 127.0.0.1 first: with `adb reverse tcp:8945 tcp:8945` the USB cable is
  /// the most reliable path and needs no pairing.
  List<(String, int)> _candidates() {
    final out = [('127.0.0.1', usbPort)];
    final h = _pref('companion_paired_host', '')! as String;
    if (h.isNotEmpty) {
      final p = _prefPort();
      if (!out.contains((h, p))) out.add((h, p));
    }
    return out;
  }

  Future<void> probeOnce() async {
    if (isConnected || pinPromptOpen || host.inBackground) return;
    for (final (h, p) in _candidates()) {
      await handleDiscoveredServer({'ip': h, 'port': p});
      if (isConnected || pinPromptOpen) return;
    }
  }

  /// Probes the USB tunnel and the saved address every few seconds.
  Future<void> _probeLoop() async {
    final gen = _probeGen = _gen;
    while (_enabled && !isAsleep && gen == _gen) {
      try {
        if (await _maybeSleep()) break;
        await probeOnce();
      } catch (e) {
        debugPrint('probe failed: $e');
      }
      await Future<void>.delayed(_probeInterval);
    }
    if (_probeGen == gen) _probeGen = null;
  }

  /// Connect to a Mac found by beacon, probe or manual entry. Pings with the
  /// saved token; the server says whether it accepts it, and an
  /// unauthorized reply leads to the PIN prompt. [interactive] (a user tap)
  /// waits its turn instead of yielding to a background attempt, allows a
  /// slower answer, rethrows network errors and ignores the PIN snooze.
  Future<void> handleDiscoveredServer(
    Map<String, Object?> server, {
    bool interactive = false,
  }) async {
    final h = server['ip'] as String?;
    final p = _toPort(server['port']);
    if (h == null || h.isEmpty) return;
    // A beacon and a probe raced; one attempt is enough.
    if (_connectLock.locked && !interactive) return;
    final info = await _connectLock.run<Map<String, Object?>?>(() async {
      final c0 = client;
      if (isConnected && c0 != null && c0.host == h && c0.port == p) {
        return null;
      }
      final token = _pref('companion_paired_token', '') as String?;
      final c = _newClient(
        h,
        p,
        (token == null || token.isEmpty) ? null : token,
        await _deviceNameOf(),
      );
      final Map<String, Object?> info;
      try {
        info = await c.ping(
          timeout: interactive
              ? const Duration(seconds: 4)
              : const Duration(milliseconds: 1500),
        );
      } catch (e) {
        if (interactive) rethrow;
        return null;
      }
      if (info['status'] != 'ok') {
        if (interactive) throw const _NotACompanion();
        return null;
      }
      if (info['authorized'] == true) {
        _setConnected(c, info);
        return null;
      }
      return info;
    });
    if (info == null) return;

    final name = _firstNonEmpty([
      info['name'],
      server['name'],
    ], 'Mac Companion');
    if (!interactive &&
        _clock.elapsed < (_pinSnoozedUntil[h] ?? Duration.zero)) {
      return;
    }
    // Prompt when the user is looking; resume re-probes.
    if (host.inBackground) return;
    discoveredServer = {'name': name, 'ip': h, 'port': p};
    notifyListeners();
    _promptPin(name, h, p);
  }

  // ── Find Mac and manual connect (user taps) ────────────────────────────────

  String? _outcome([String how = '']) {
    final s = discoveredServer;
    if (s == null) return null;
    if (isConnected) return 'Connected to ${s['name']} (${s['ip']})$how.';
    if (pinPromptOpen) {
      return 'Found ${s['name']} (${s['ip']})$how. Enter the PIN shown on '
          'the Mac.';
    }
    return null;
  }

  /// The "Find Mac" / "Test connection" button: wake the link, then try each
  /// path in turn (USB and the saved address, the broadcast query, the /24
  /// sweep) and say what it learned.
  Future<String> findMac() async {
    if (!_enabled || isAsleep) await start();
    touch();
    _pinSnoozedUntil.clear();

    final c = client;
    if (isConnected && c != null) {
      final t0 = _clock.elapsed;
      try {
        await c.ping(timeout: const Duration(seconds: 3));
        final ms = (_clock.elapsed - t0).inMicroseconds / 1000;
        final o = _outcome()!;
        return '${o.substring(0, o.length - 1)} · answered in '
            '${ms.round()} ms.';
      } catch (e) {
        _setDisconnected('Mac stopped responding');
        debugPrint('test ping failed: $e');
      }
    }

    await probeOnce();
    if (_outcome() case final found?) return found;

    // A fresh listener re-reports at once, even a Mac it heard recently.
    _stopListener();
    _startListener();
    final deadline = _clock.elapsed + _findQueryWait;
    while (_clock.elapsed < deadline) {
      await Future<void>.delayed(const Duration(milliseconds: 250));
      if (_outcome() case final found?) return found;
    }

    final myIp = await _localIp();
    final ports = [defaultCompanionPort];
    final cached = _prefPort();
    if (!ports.contains(cached)) ports.add(cached);
    for (final port in ports) {
      for (final h in await _sweep(myIp, port)) {
        try {
          await handleDiscoveredServer({
            'ip': h,
            'port': port,
          }, interactive: true);
        } catch (_) {
          continue;
        }
        final found = _outcome(
          ' by scanning: this network blocks discovery broadcasts, so the '
          'address is saved for next time',
        );
        if (found != null) return found;
      }
    }

    final onLan = isPrivateIpv4(myIp) && !myIp.startsWith('127.');
    if (!onLan) {
      return "This phone isn't on a local network (its address is $myIp). "
          "Join the Mac's Wi-Fi, or use your phone's hotspot or a USB cable.";
    }
    return 'No Mac answered. This phone is $myIp. Check Companion Server is '
        'on; if the address the Mac shows starts differently, the two '
        "aren't on the same network. If it matches, type it under Manual IP: "
        'when that fails too, this network keeps devices apart, and only '
        "your phone's hotspot or a USB cable will work.";
  }

  Future<String> connectManual(String h, int port) async {
    if (!_enabled || isAsleep) await start();
    touch();
    _pinSnoozedUntil.remove(h);
    try {
      await handleDiscoveredServer({'ip': h, 'port': port}, interactive: true);
    } catch (e) {
      return explainConnectError(e, h, port);
    }
    return _outcome() ??
        "$h:$port answered, but the connection didn't complete.";
  }

  // ── PIN pairing ────────────────────────────────────────────────────────────

  void _promptPin(String name, String h, int port) {
    if (pinPromptOpen) return;
    final p = _pinPrompt = PinPrompt._(this, name, h, port);
    host.showPinPrompt(p);
  }

  /// The UI's PIN dialog went away. Not paired: the same Mac is not asked
  /// about again for two minutes.
  void pinPromptClosed(PinPrompt p) {
    if (!identical(_pinPrompt, p)) return;
    _pinPrompt = null;
    if (!p.paired) _pinSnoozedUntil[p.host] = _clock.elapsed + _pinSnooze;
    notifyListeners();
  }

  Future<String?> _pairWithPin(PinPrompt p, String pin) async {
    pin = pin.trim();
    if (pin.length != 4 || !RegExp(r'^[0-9]{4}$').hasMatch(pin)) {
      return 'Please enter the 4-digit PIN.';
    }
    final c = _newClient(p.host, p.port, null, await _deviceNameOf());
    try {
      final token = await c.pair(pin);
      await _savePref('companion_paired_token', token);
      final info = await c.ping(timeout: const Duration(seconds: 3));
      p.paired = true;
      touch();
      host.dismissPinPrompt(p);
      pinPromptClosed(p);
      _setConnected(c, info);
      host.snack('Connected to ${p.name}', icon: SyncIcon.check);
      return null;
    } on CompanionAuthError {
      return 'Incorrect PIN. Check the code on your Mac '
          '(it changes after 5 wrong tries).';
    } catch (e) {
      return 'Connection failed: $e';
    }
  }

  // ── heartbeat and transfers ────────────────────────────────────────────────

  Future<void> _heartbeatOnce() async {
    final c = client;
    if (!isConnected || c == null) return;
    final Map<String, Object?> info;
    try {
      info = await c.ping(timeout: const Duration(seconds: 3));
    } catch (e) {
      _misses++;
      if (_misses >= _missesBeforeDrop) {
        _setDisconnected('Mac stopped responding');
      }
      return;
    }
    _misses = 0;
    if (info['authorized'] != true) {
      // The Mac reset pairing (or lost its token): ours is dead.
      await _savePref('companion_paired_token', '');
      _setDisconnected('Pairing was revoked on the Mac');
      return;
    }
    await _flushAcks();
    await checkPendingTransfers(promptUser: true);
  }

  /// Liveness and the transfer poll. Without it a Mac that quit or slept
  /// stayed "Connected" forever and every action failed.
  Future<void> _heartbeatLoop() async {
    final gen = _heartbeatGen = _gen;
    while (_enabled && !isAsleep && gen == _gen) {
      await Future<void>.delayed(_heartbeatInterval);
      if (gen != _gen || !_enabled || isAsleep) break;
      // Before the background skip, so a pocketed phone stops too.
      if (await _maybeSleep()) break;
      if (host.inBackground) {
        final now = _clock.elapsed;
        final last = _bgPolledAt;
        if (last == null || now - last >= _bgTransferPoll) {
          _bgPolledAt = now;
          try {
            await _backgroundTransferCheck();
          } catch (e) {
            debugPrint('background transfer check failed: $e');
          }
        }
        continue;
      }
      try {
        await _heartbeatOnce();
      } catch (e) {
        debugPrint('heartbeat failed: $e');
      }
    }
    if (_heartbeatGen == gen) _heartbeatGen = null;
  }

  /// The Mac's pending transfers, minus those already handed to the queue;
  /// with [promptUser] in the foreground, auto-accepted or offered.
  Future<List<Map<String, Object?>>> checkPendingTransfers({
    bool promptUser = false,
  }) async {
    final c = client;
    if (c == null || !isConnected) return const [];
    List<Map<String, Object?>> transfers;
    try {
      transfers = await c.getPendingTransfers();
    } on CompanionAuthError {
      await _savePref('companion_paired_token', '');
      _setDisconnected('Pairing was revoked on the Mac');
      return const [];
    } catch (e) {
      debugPrint('Failed checking pending transfers: $e');
      return const [];
    }
    bool fresh(Map<String, Object?> t) => !_inFlightIds.contains('${t['id']}');
    transfers = transfers.where(fresh).toList();
    bool canPrompt() => promptUser && !transferPromptOpen && !host.inBackground;
    if (transfers.isNotEmpty && canPrompt()) {
      final n = _autoAccept(transfers);
      if (n > 0) {
        host.snack(
          'Receiving ${_plural(n, 'track')} from ${_macName()}',
          icon: SyncIcon.download,
        );
        transfers = transfers.where(fresh).toList();
      }
    }
    if (transfers.isNotEmpty && canPrompt()) {
      final ids = {
        for (final t in transfers)
          if (_truthy(t['id'])) '${t['id']}',
      };
      if (ids.difference(_promptedTransferIds).isNotEmpty) {
        touch(); // the Mac sent something new: that's use
        _promptedTransferIds.addAll(ids);
        _promptTransfers(transfers);
      }
    }
    return transfers;
  }

  /// In the background no dialog can show, so new transfers are announced
  /// by a notification that opens the Sync screen. Neither a miss on
  /// failure nor use.
  Future<void> _backgroundTransferCheck() async {
    var transfers = await checkPendingTransfers();
    if (transfers.isNotEmpty && _autoAccept(transfers) > 0) {
      // The download queue's own notification takes it from here.
      transfers = [
        for (final t in transfers)
          if (!_inFlightIds.contains('${t['id']}')) t,
      ];
    }
    final ids = {
      for (final t in transfers)
        if (_truthy(t['id'])) '${t['id']}',
    };
    final fresh = ids
        .difference(_promptedTransferIds)
        .difference(_notifiedTransferIds);
    if (fresh.isEmpty) return;
    _notifiedTransferIds.addAll(fresh);
    postAlert(
      'transfers',
      '${_macName('Your Mac')} has ${_plural(ids.length, 'track')} for you',
      'Tap to review and download them.',
      navigateTo: 'sync:transfers',
      urgent: true,
      icon: 'sync',
    );
  }

  /// Hand transfers to the download queue. Returns how many were queued.
  int downloadTransfers(List<Map<String, Object?>> transfers) {
    final c = client;
    final q = host.queue;
    if (c == null || q == null) return 0;
    final items = <Map<String, Object?>>[];
    for (final t in transfers) {
      final id = _pyStr(t['id'], '');
      if (id.isEmpty || _inFlightIds.contains(id)) continue;
      _inFlightIds.add(id);
      items.add({
        'source': 'companion',
        'media_type': 'track',
        'id': id,
        'companion_transfer_id': id,
        'url': c.streamUrl(t),
        'ui_title': t['title'],
        'ui_subtitle': t['artist'],
        'title': t['title'],
        'artist': t['artist'],
        'album': t['album'],
        'dsp_features': t['dsp_features'],
        'quality_label': transferQualityLabel(t),
      });
    }
    if (items.isNotEmpty) {
      touch();
      q.enqueueMany(items, qualityTier: 'original');
    }
    notifyListeners();
    return items.length;
  }

  /// The download queue reports how a companion job ended.
  void onTransferFinished(String transferId, {required bool ok}) {
    _inFlightIds.remove(transferId);
    touch();
    if (ok) {
      _autoFailures.remove(transferId);
      _unackedIds.add(transferId);
      unawaited(_flushAcks());
    } else {
      // Offered again on the next poll (asking, once auto-accept gave up).
      _autoFailures[transferId] = (_autoFailures[transferId] ?? 0) + 1;
      _promptedTransferIds.remove(transferId);
    }
    notifyListeners();
  }

  // ── auto-accept ────────────────────────────────────────────────────────────

  bool get askBeforeDownload => _pref(askBeforeDownloadPref, false) == true;

  Future<void> setAskBeforeDownload(bool ask) async {
    await _savePref(askBeforeDownloadPref, ask);
    notifyListeners();
  }

  String _macName([String fallback = 'your Mac']) => _firstNonEmpty([
    discoveredServer?['name'],
    _pref('companion_paired_name', ''),
  ], fallback);

  /// The name the menus use (send.py `companion_target`).
  String macName([String fallback = 'Mac']) => _macName(fallback);

  /// send.py `companion_target(...).paired`: a pairing token is saved.
  bool get isPaired => _truthy(_pref('companion_paired_token', ''));

  /// Download what the paired Mac sent without asking: sending it was the
  /// decision. A transfer that keeps failing drops back to the dialog.
  int _autoAccept(List<Map<String, Object?>> transfers) {
    if (askBeforeDownload) return 0;
    final auto = [
      for (final t in transfers)
        if ((_autoFailures['${t['id']}'] ?? 0) < _autoAcceptMaxFailures) t,
    ];
    return auto.isEmpty ? 0 : downloadTransfers(auto);
  }

  void _promptTransfers(List<Map<String, Object?>> transfers) {
    if (transferPromptOpen) return;
    // The dialog supersedes the background notification about them.
    cancelAlert('transfers');
    final p = _transferPrompt = TransferPrompt._(
      this,
      _pyStr(discoveredServer?['name'], 'Mac Companion'),
      transfers,
    );
    host.showTransferPrompt(p);
  }

  void transferPromptClosed(TransferPrompt p) {
    if (identical(_transferPrompt, p)) _transferPrompt = null;
  }

  // ── Send to Mac ────────────────────────────────────────────────────────────

  bool get isSending => _sendTask != null;

  /// Queue library tracks (maps with `path`, and `title`/`artist`/`album`)
  /// for the Mac. One worker drains the queue, so a second tap mid-send
  /// extends the batch. Returns how many were newly queued.
  int sendTracks(List<Map<String, Object?>> tracks) {
    if (client == null || !isConnected) return 0;
    final queued = {for (final t in _sendQueue) t['path']};
    final fresh = [
      for (final t in tracks)
        if (_truthy(t['path']) && !queued.contains(t['path'])) t,
    ];
    _sendQueue.addAll(fresh);
    if (fresh.isNotEmpty && !isSending) {
      _sendCancelled = false;
      _sendTask = _sendWorker().whenComplete(() => _sendTask = null);
    }
    return fresh.length;
  }

  void cancelSend() => _sendCancelled = true;

  Future<void> _sendWorker() async {
    final mac = _macName();
    final notifier = ProgressNotifier(
      'sync',
      'Sending to $mac',
      onCancel: cancelSend,
    );
    var sent = 0, dup = 0, failed = 0, netFailures = 0;
    var error = '';
    try {
      await activity(() async {
        while (_sendQueue.isNotEmpty && !_sendCancelled) {
          final c = client;
          if (c == null || !isConnected) {
            error = 'Lost connection to $mac';
            break;
          }
          final t = _sendQueue.removeAt(0);
          final path = t['path']! as String;
          final title = _firstNonEmpty([
            t['track_title'],
            t['title'],
          ], path.split('/').last);
          final done = sent + dup + failed;
          final total = done + _sendQueue.length + 1;
          notifier.update(done, total, '$title · ${done + 1} of $total');
          final meta = <String, Object?>{
            'title': title,
            'artist': _firstNonEmpty([t['artist_name'], t['artist']], ''),
            'album': _firstNonEmpty([t['album'], t['album_title']], ''),
            'dsp_features': await _storedFeatures(path),
            'remaining': _sendQueue.length,
          };
          try {
            final reply = await c.pushTrack(path, meta);
            if (_truthy(reply['duplicate'])) {
              dup++;
            } else {
              sent++;
            }
            netFailures = 0;
          } on CompanionAuthError {
            await _savePref('companion_paired_token', '');
            _setDisconnected('Pairing was revoked on the Mac');
            failed++;
            error = 'Pairing was revoked on the Mac';
            break;
          } catch (e) {
            failed++;
            debugPrint('Send to Mac failed for $path: $e');
            if (isNetworkError(e) && ++netFailures >= _maxSendNetFailures) {
              error = 'Lost connection to $mac';
              break;
            }
          }
          notifyListeners();
        }
      });
    } finally {
      failed += _sendQueue.length;
      _sendQueue.clear();
      final cancelled = _sendCancelled;
      _sendCancelled = false;
      final parts = [
        if (sent > 0) 'Sent ${_plural(sent, 'track')} to $mac',
        if (dup > 0) '$dup already there',
        if (failed > 0)
          cancelled && error.isEmpty ? 'cancelled' : '$failed failed',
      ];
      final summary = parts.join(' · ') + (error.isNotEmpty ? ' ($error)' : '');
      notifier.finish(summary);
      if (summary.isNotEmpty) {
        host.snack(
          summary,
          icon: failed == 0 ? SyncIcon.check : SyncIcon.error,
          red: failed > 0 && sent == 0 && dup == 0,
        );
      }
      notifyListeners();
    }
  }

  Future<Map<String, Object?>?> _storedFeatures(String path) async {
    try {
      return await host.storedFeatures(path);
    } catch (e) {
      debugPrint('db lookup failed: $e');
      return null;
    }
  }

  Future<void> _flushAcks() async {
    final c = client;
    if (c == null || _unackedIds.isEmpty) return;
    final ids = _unackedIds.toList();
    try {
      if (await c.ackTransfers(ids)) _unackedIds.removeAll(ids);
    } catch (e) {
      debugPrint('ack failed (will retry): $e');
    }
  }

  @override
  void dispose() {
    _gen++;
    _stopListener();
    super.dispose();
  }
}

/// Python's `a or b or default` over values that are strings or null.
String _firstNonEmpty(List<Object?> values, String fallback) {
  for (final v in values) {
    if (_truthy(v)) return '$v';
  }
  return fallback;
}

int _toPort(Object? v) {
  if (v is num && v != 0) return v.toInt();
  if (v is String) return int.tryParse(v) ?? defaultCompanionPort;
  return defaultCompanionPort;
}
