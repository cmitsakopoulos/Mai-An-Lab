// BeaconListener's datagram handling (discovery.py `_listen_loop`), fed by a
// local UDP socket: what it accepts, ignores and re-reports.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/sync/discovery.dart';

void main() {
  test(
    'accepts beacons, ignores the rest, re-reports after 10 s only',
    () async {
      final found = <Map<String, Object?>>[];
      final queries = <String>[];
      // Stands in for the Mac's responder: records queries, sends nothing.
      final mac = await RawDatagramSocket.bind(InternetAddress.loopbackIPv4, 0);
      mac.listen((e) {
        final d = mac.receive();
        if (d != null) queries.add(utf8.decode(d.data));
      });
      final l = BeaconListener(
        port: 0,
        queryPort: mac.port,
        targets: const ['127.0.0.1'],
        holdMulticastLock: false,
        onDeviceFound: found.add,
      );
      await l.start();
      final to = InternetAddress.loopbackIPv4;
      void send(Object payload) => mac.send(
        payload is String
            ? utf8.encode(payload)
            : utf8.encode(jsonEncode(payload)),
        to,
        l.boundPort!,
      );

      send('not json');
      send([1, 2]);
      send({'service': 'other', 'type': 'beacon', 'port': 1});
      send({'service': beaconMagic, 'type': 'query'});
      send({'service': beaconMagic, 'type': 'beacon', 'port': null});
      send({'service': beaconMagic, 'type': 'beacon', 'port': 'x'});
      send({
        'service': beaconMagic,
        'type': 'beacon',
        'name': 'Mac',
        'ip': '10.9.9.9',
        'port': '8946',
      });
      send({
        'service': beaconMagic,
        'type': 'beacon',
        'name': 'Mac',
        'port': 8946,
      });
      send({'service': beaconMagic, 'type': 'beacon', 'name': 'Mac 2'});
      await Future<void>.delayed(const Duration(milliseconds: 300));
      l.stop();
      mac.close();

      // A string port is int()-ed; the sender's address replaces the
      // advertised one; the repeat within 10 s is recorded, not reported; no
      // port means 8945.
      expect(found, [
        {
          'service': beaconMagic,
          'type': 'beacon',
          'name': 'Mac',
          'ip': '127.0.0.1',
          'port': 8946,
        },
        {
          'service': beaconMagic,
          'type': 'beacon',
          'name': 'Mac 2',
          'ip': '127.0.0.1',
          'port': 8945,
        },
      ]);
      expect(l.discovered.keys, ['127.0.0.1:8946', '127.0.0.1:8945']);
      expect(queries, isNotEmpty);
      expect(jsonDecode(queries.first), {
        'service': beaconMagic,
        'version': 1,
        'type': 'query',
      });
    },
  );
}
