import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

/// utils/sync/discovery.py, the listener's half (the broadcaster runs on the
/// Mac, in Flet until phase 8).

const udpPort = 54545;
const beaconMagic = 'mai_an_sync';

/// A listener re-reports a server it already knows at most this often, so a
/// consumer that ignored one beacon (a dismissed PIN prompt) hears it again.
const _reportInterval = Duration(seconds: 10);

/// How often the listener asks out loud. The Mac answers the query by
/// unicast, which Android always delivers; its periodic broadcast beacons
/// arrive only while a WifiManager.MulticastLock is held (the port holds
/// one, which Flet couldn't: pyjnius isn't in its build).
const _queryInterval = Duration(seconds: 2);

/// Interface names of tunnels: never used to reach a Mac on the LAN.
const _vpnPrefixes = ['utun', 'ipsec', 'ppp', 'tun', 'tap', 'wg', 'gpd'];

const _app = MethodChannel('mai_an/app');

/// The real OS (widget tests report `defaultTargetPlatform` as Android).
bool get _android => !kIsWeb && Platform.isAndroid;

/// `resolve_local_ip`: the IPv4 address of the default route. Python asks
/// the kernel by "connecting" a UDP socket to 8.8.8.8, which dart:io can't
/// do; on Android the active network's link address is the same answer
/// (MainActivity `defaultRouteIpv4`). Elsewhere: the first non-tunnel,
/// non-link-local IPv4 address. Falls back to 127.0.0.1, as Python does.
Future<String> resolveLocalIp() async {
  if (_android) {
    try {
      final ip = await _app.invokeMethod<String>('defaultRouteIpv4');
      if (ip != null && ip.isNotEmpty) return ip;
    } catch (_) {}
  }
  try {
    final ifaces = await NetworkInterface.list(type: InternetAddressType.IPv4);
    String? any;
    for (final i in ifaces) {
      for (final a in i.addresses) {
        if (a.isLoopback || a.address.startsWith('169.254.')) continue;
        any ??= a.address;
        if (!_vpnPrefixes.any(i.name.startsWith)) return a.address;
      }
    }
    if (any != null) return any;
  } catch (_) {}
  return '127.0.0.1';
}

/// `_slash24_broadcast`.
String? slash24Broadcast(String ip) {
  final p = ip.split('.');
  if (p.length == 4 && p[0] != '127' && p[0] != '0') {
    return '${p[0]}.${p[1]}.${p[2]}.255';
  }
  return null;
}

/// `_broadcast_targets` (the listener's: the limited broadcast plus the /24
/// guess; Android has no netmask to read).
List<String> broadcastTargets(String localIp) => [
  '255.255.255.255',
  ?slash24Broadcast(localIp),
];

/// `sweep_subnet`: hosts in [localIp]'s /24 with [port] open, by TCP
/// connect. The fallback for networks that drop broadcasts but still route
/// unicast. User-triggered only: on a managed network a sweep looks like a
/// port scan.
Future<List<String>> sweepSubnet(
  String localIp,
  int port, {
  Duration timeout = const Duration(milliseconds: 700),
  int concurrency = 64,
}) async {
  final p = localIp.split('.');
  if (p.length != 4) return const [];
  final base = p.take(3).join('.');
  final hosts = [for (var i = 1; i < 255; i++) '$base.$i'];
  final found = List<String?>.filled(hosts.length, null);
  var next = 0;
  Future<void> worker() async {
    while (next < hosts.length) {
      final i = next++;
      try {
        final s = await Socket.connect(hosts[i], port, timeout: timeout);
        s.destroy();
        found[i] = hosts[i];
      } catch (_) {}
    }
  }

  await Future.wait([for (var w = 0; w < concurrency; w++) worker()]);
  return [for (final h in found) ?h];
}

/// Holds Android's WifiManager.MulticastLock while discovery runs, so the
/// Mac's broadcast beacons are delivered too. No-op elsewhere.
abstract final class MulticastLock {
  static Future<void> acquire() async {
    if (!_android) return;
    try {
      await _app.invokeMethod<void>('acquireMulticastLock');
    } catch (e) {
      debugPrint('MulticastLock not available: $e');
    }
  }

  static Future<void> release() async {
    if (!_android) return;
    try {
      await _app.invokeMethod<void>('releaseMulticastLock');
    } catch (_) {}
  }
}

typedef DeviceFound = void Function(Map<String, Object?> server);

/// `BeaconListener`: finds Mac Companion Servers on the LAN. One socket on
/// [port] hears both the Mac's beacons and the unicast replies to the query
/// it broadcasts every two seconds. Run on demand; stop it once connected.
class BeaconListener {
  BeaconListener({
    this.onDeviceFound,
    this.port = udpPort,
    this.queryPort = udpPort,
    this.targets,
    this.localIp = resolveLocalIp,
    this.holdMulticastLock = true,
  });

  final DeviceFound? onDeviceFound;

  /// The port to listen on, and the one queries go to (the Mac's responder).
  final int port, queryPort;

  /// Where queries go; by default [broadcastTargets] of the local address
  /// (tests point it at loopback).
  final List<String>? targets;
  final Future<String> Function() localIp;
  final bool holdMulticastLock;

  final Map<String, Map<String, Object?>> discovered = {};
  final Map<String, Stopwatch> _lastReported = {};
  RawDatagramSocket? _sock;
  Timer? _queryTimer;
  bool _running = false;
  bool _locked = false;

  bool get isRunning => _running;
  int? get boundPort => _sock?.port;

  Future<void> start() async {
    if (_running) return;
    _running = true;
    discovered.clear();
    _lastReported.clear();
    if (holdMulticastLock) {
      _locked = true;
      await MulticastLock.acquire();
    }
    RawDatagramSocket? sock;
    try {
      sock = await _bind(port);
    } catch (e) {
      // The port is taken (another app instance): an ephemeral port still
      // receives unicast query replies.
      debugPrint('UDP $port unavailable ($e); listening on an ephemeral port');
      try {
        sock = await _bind(0);
      } catch (e2) {
        debugPrint('Failed to start BeaconListener: $e2');
        stop();
        return;
      }
    }
    if (!_running) {
      sock.close(); // stopped while binding
      return;
    }
    _sock = sock;
    sock.listen((event) {
      if (event == RawSocketEvent.read) _drain(sock!);
    });
    unawaited(_query());
    _queryTimer = Timer.periodic(_queryInterval, (_) => _query());
  }

  void stop() {
    _running = false;
    _queryTimer?.cancel();
    _queryTimer = null;
    _sock?.close();
    _sock = null;
    if (_locked) {
      _locked = false;
      unawaited(MulticastLock.release());
    }
  }

  static Future<RawDatagramSocket> _bind(int port) async {
    Future<RawDatagramSocket> bind({required bool reusePort}) =>
        RawDatagramSocket.bind(
          InternetAddress.anyIPv4,
          port,
          reuseAddress: true,
          reusePort: reusePort,
        );
    RawDatagramSocket s;
    try {
      s = await bind(reusePort: true);
    } catch (_) {
      s = await bind(reusePort: false); // as Python: SO_REUSEPORT if it can
    }
    return s..broadcastEnabled = true;
  }

  static final _queryBytes = utf8.encode(
    jsonEncode({'service': beaconMagic, 'version': 1, 'type': 'query'}),
  );

  Future<void> _query() async {
    final sock = _sock;
    if (!_running || sock == null) return;
    final to = targets ?? broadcastTargets(await localIp());
    for (final t in to) {
      try {
        sock.send(_queryBytes, InternetAddress(t), queryPort);
      } catch (e) {
        debugPrint('Discovery query to $t failed: $e');
      }
    }
  }

  void _drain(RawDatagramSocket sock) {
    Datagram? d;
    while ((d = sock.receive()) != null) {
      _handle(d!);
    }
  }

  void _handle(Datagram d) {
    final Map<String, Object?> parsed;
    try {
      final v = jsonDecode(utf8.decode(d.data));
      if (v is! Map<String, Object?>) return;
      parsed = v;
    } catch (_) {
      return;
    }
    if (parsed['service'] != beaconMagic || parsed['type'] == 'query') return;
    // The sender's address is the one that provably routes back to us; the
    // advertised IP can name another interface on a multi-homed Mac.
    final host = d.address.address.isNotEmpty
        ? d.address.address
        : parsed['ip'] as String?;
    final int port;
    try {
      port = _pyInt(parsed.containsKey('port') ? parsed['port'] : 8945);
    } catch (_) {
      return;
    }
    final key = '$host:$port';
    parsed['ip'] = host;
    parsed['port'] = port;
    discovered[key] = parsed;
    final last = _lastReported[key];
    if (last != null && last.elapsed < _reportInterval) return;
    _lastReported[key] = Stopwatch()..start();
    try {
      onDeviceFound?.call(parsed);
    } catch (e) {
      debugPrint('on_device_found callback raised $e');
    }
  }

  /// `scan_once`: listen for [timeout] and return what answered.
  static Future<List<Map<String, Object?>>> scanOnce({
    Duration timeout = const Duration(milliseconds: 4500),
  }) async {
    final l = BeaconListener();
    await l.start();
    try {
      await Future<void>.delayed(timeout);
      return l.discovered.values.toList();
    } finally {
      l.stop();
    }
  }
}

/// Python's `int()` on a JSON value: ints, integral floats truncated, and
/// numeric strings.
int _pyInt(Object? v) => switch (v) {
  int() => v,
  double() => v.truncate(),
  String() => int.parse(v.trim()),
  _ => throw FormatException('not an int: $v'),
};
