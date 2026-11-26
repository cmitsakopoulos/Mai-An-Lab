import 'dart:io';

/// One IPv4 interface of this Mac.
class Iface {
  const Iface(this.name, this.ip, this.prefix, this.broadcast, this.vpn);
  final String name;
  final String ip;
  final int prefix;
  final String? broadcast;
  final bool vpn;
}

/// Interface names of tunnels: a phone can't reach us through them.
const _vpnPrefixes = ['utun', 'ipsec', 'ppp', 'tun', 'tap', 'wg', 'gpd'];

/// The slash-24 broadcast address of [ip], or null for loopback/unspecified.
String? slash24Broadcast(String ip) {
  final p = ip.split('.');
  if (p.length == 4 && p[0] != '127' && p[0] != '0') {
    return '${p[0]}.${p[1]}.${p[2]}.255';
  }
  return null;
}

/// The default route's interface and gateway, from `route -n get default`.
/// On a phone's hotspot the gateway IS the phone.
Future<({String? iface, String? gateway})> defaultRoute() async {
  if (!Platform.isMacOS) return (iface: null, gateway: null);
  try {
    final r = await Process.run('/sbin/route', ['-n', 'get', 'default']);
    String? iface, gw;
    for (final line in '${r.stdout}'.split('\n')) {
      final t = line.trim();
      if (t.startsWith('interface:')) iface = t.substring(10).trim();
      if (t.startsWith('gateway:')) gw = t.substring(8).trim();
    }
    return (iface: iface, gateway: gw);
  } catch (_) {
    return (iface: null, gateway: null);
  }
}

DateTime _cacheAt = DateTime.fromMillisecondsSinceEpoch(0);
List<Iface> _cache = const [];

/// This Mac's IPv4 interfaces, the default-route one first. macOS: parsed
/// from ifconfig (the only place the real netmask and broadcast live);
/// elsewhere: dart:io's list with an assumed /24. Cached for 10 s.
Future<List<Iface>> localInterfaces({bool fresh = false}) async {
  if (!fresh && DateTime.now().difference(_cacheAt).inSeconds < 10) {
    return _cache;
  }
  final out = <Iface>[];
  if (Platform.isMacOS) {
    try {
      final r = await Process.run('/sbin/ifconfig', const []);
      var name = '';
      for (final line in '${r.stdout}'.split('\n')) {
        if (line.isNotEmpty && line[0] != ' ' && line[0] != '\t') {
          name = line.split(':').first;
          continue;
        }
        final parts = line.trim().split(RegExp(r'\s+'));
        if (parts.length < 2 || parts[0] != 'inet') continue;
        final ip = parts[1];
        if (ip.startsWith('127.') || ip.startsWith('169.254.')) continue;
        var prefix = 24;
        String? bcast;
        final ni = parts.indexOf('netmask');
        if (ni >= 0 && ni + 1 < parts.length) {
          final m = int.tryParse(parts[ni + 1].replaceFirst('0x', ''), radix: 16);
          if (m != null) prefix = m.toRadixString(2).replaceAll('0', '').length;
        }
        final bi = parts.indexOf('broadcast');
        if (bi >= 0 && bi + 1 < parts.length) bcast = parts[bi + 1];
        out.add(Iface(name, ip, prefix, bcast, _vpnPrefixes.any(name.startsWith)));
      }
    } catch (_) {}
  }
  if (out.isEmpty) {
    try {
      for (final i in await NetworkInterface.list(type: InternetAddressType.IPv4)) {
        for (final a in i.addresses) {
          if (a.isLoopback || a.address.startsWith('169.254.')) continue;
          out.add(Iface(
            i.name,
            a.address,
            24,
            slash24Broadcast(a.address),
            _vpnPrefixes.any(i.name.startsWith),
          ));
        }
      }
    } catch (_) {}
  }
  final route = await defaultRoute();
  out.sort((a, b) {
    int rank(Iface i) => (i.name == route.iface ? 0 : 1) + (i.vpn ? 2 : 0);
    return rank(a).compareTo(rank(b));
  });
  _cache = out;
  _cacheAt = DateTime.now();
  return out;
}

/// The address a phone on the same network should use: the default-route
/// address unless that is a tunnel.
Future<String> lanIp() async {
  for (final i in await localInterfaces()) {
    if (!i.vpn) return i.ip;
  }
  return '127.0.0.1';
}
