import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'net.dart';

const udpPort = 54545;
const beaconMagic = 'mai_an_sync';

/// utils/sync/discovery.py `BeaconBroadcaster`: tells phones where we are.
///
///  * a broadcast beacon every 2 s (255.255.255.255 and each real
///    interface's own broadcast address), as the Flet server does;
///  * the same beacon by UNICAST to the default gateway. When this Mac is on
///    a phone's hotspot the phone IS that gateway (a Pixel's hotspot sits at
///    an odd address like 10.x.x.163, not .1), and unicast is always
///    delivered, where Android may drop broadcasts;
///  * an answer, by unicast, to every query a phone broadcasts.
class Beacon {
  Beacon({
    required this.name,
    required this.port,
    this.udp = udpPort,
    this.announcePort,
    this.interval = const Duration(seconds: 2),
    this.targets,
    this.log,
  });

  final String name;
  final int port;

  /// The port we listen on for queries.
  final int udp;

  /// The port phones listen on for beacons (tests point it elsewhere).
  final int? announcePort;
  final Duration interval;

  /// Fixed destinations (tests); null = work them out from the interfaces.
  final List<String>? targets;
  final void Function(String)? log;

  RawDatagramSocket? _sock;
  Timer? _timer;
  bool _warned = false;
  int get boundPort => _sock?.port ?? 0;

  Future<void> start() async {
    if (_sock != null) return;
    RawDatagramSocket? s;
    Future<RawDatagramSocket> bind(int p, {required bool reusePort}) =>
        RawDatagramSocket.bind(
          InternetAddress.anyIPv4,
          p,
          reuseAddress: true,
          reusePort: reusePort,
        );
    try {
      try {
        s = await bind(udp, reusePort: true);
      } catch (_) {
        s = await bind(udp, reusePort: false);
      }
    } catch (e) {
      // Another program holds the port: we can still announce, but can't
      // hear queries.
      log?.call(
        'UDP $udp is busy ($e): announcing only, not answering queries',
      );
      s = await bind(0, reusePort: false);
    }
    s.broadcastEnabled = true;
    _sock = s;
    s.listen(
      (e) {
        if (e != RawSocketEvent.read) return;
        Datagram? d;
        while ((d = s!.receive()) != null) {
          _onDatagram(d!);
        }
      },
      // A failed send can be reported here, not at the call.
      onError: (Object e) => _warn(e),
    );
    unawaited(_announce());
    _timer = Timer.periodic(interval, (_) => _announce());
  }

  void stop() {
    _timer?.cancel();
    _timer = null;
    _sock?.close();
    _sock = null;
  }

  Future<List<int>> _payload() async => utf8.encode(
    jsonEncode({
      'service': beaconMagic,
      'version': 1,
      'type': 'beacon',
      'name': name,
      'ip': await lanIp(),
      'port': port,
    }),
  );

  void _onDatagram(Datagram d) {
    try {
      final v = jsonDecode(utf8.decode(d.data));
      if (v is! Map || v['service'] != beaconMagic || v['type'] != 'query') {
        return; // our own beacons echo back here
      }
    } catch (_) {
      return;
    }
    unawaited(() async {
      _send(await _payload(), d.address, d.port);
    }());
  }

  void _send(List<int> bytes, InternetAddress to, int toPort) {
    try {
      _sock?.send(bytes, to, toPort);
    } catch (e) {
      _warn(e, to.address);
    }
  }

  // EHOSTUNREACH on macOS 15+ usually means "Local Network" access was
  // denied to this terminal (System Settings > Privacy & Security).
  void _warn(Object e, [String? to]) {
    if (_warned) return;
    _warned = true;
    log?.call(
      'Could not send discovery beacon${to == null ? '' : ' to $to'}: $e. If this '
      'persists, allow Mai An Companion (or your terminal, if you run maian '
      'there) under System Settings > Privacy & '
      'Security > Local Network.',
    );
  }

  Future<Set<String>> destinations() async {
    if (targets != null) return targets!.toSet();
    final out = <String>{'255.255.255.255'};
    for (final i in await localInterfaces()) {
      if (i.vpn) continue;
      if (i.broadcast != null) out.add(i.broadcast!);
    }
    final gw = (await defaultRoute()).gateway;
    if (gw != null && !gw.contains(':')) out.add(gw);
    return out;
  }

  Future<void> _announce() async {
    if (_sock == null) return;
    final bytes = await _payload();
    for (final t in await destinations()) {
      _send(bytes, InternetAddress(t), announcePort ?? udp);
    }
  }
}
