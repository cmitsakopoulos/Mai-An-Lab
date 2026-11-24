// The Companion protocol against Flet's own CompanionServer and Python
// client (PORT_MAP §0 rule 3; §4, phase 4). tool/companion_test_server.py
// runs utils/sync/server.py hermetically; each test talks to it with the
// Dart client and, where Python has the same call, compares with what
// utils/sync/client.py does against the same server.
//
// Needs ~/miniconda3/bin/python with aiohttp and numpy (the Flet dev env);
// skipped without it.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/py_text.dart';
import 'package:mai_an_lab/sync/companion_client.dart';
import 'package:mai_an_lab/sync/discovery.dart';

final _python = '${Platform.environment['HOME']}/miniconda3/bin/python';

class _Server {
  _Server(this.proc, this.info, this._lines);
  final Process proc;
  final Map<String, Object?> info;
  final StreamIterator<String> _lines;

  int get port => info['port']! as int;
  int get udpPort => info['udp_port']! as int;
  String get pin => info['pin']! as String;
  String get token => info['token']! as String;
  String get scratch => info['scratch']! as String;

  static Future<_Server> start(String scratch) async {
    final proc = await Process.start(_python, [
      'tool/companion_test_server.py',
      scratch,
    ]);
    proc.stderr.transform(utf8.decoder).listen((_) {});
    final lines = StreamIterator(
      proc.stdout.transform(utf8.decoder).transform(const LineSplitter()),
    );
    if (!await lines.moveNext().timeout(const Duration(seconds: 30))) {
      throw StateError('test server did not start');
    }
    final info = jsonDecode(lines.current) as Map<String, Object?>;
    return _Server(proc, info, lines);
  }

  Future<Map<String, Object?>> cmd(Map<String, Object?> msg) async {
    proc.stdin.writeln(jsonEncode(msg));
    await proc.stdin.flush();
    if (!await _lines.moveNext().timeout(const Duration(seconds: 60))) {
      throw StateError('test server exited');
    }
    final out = jsonDecode(_lines.current) as Map<String, Object?>;
    if (out['error'] != null) throw StateError('python: ${out['error']}');
    return out;
  }

  Future<void> stop() async {
    proc.stdin.writeln(jsonEncode({'cmd': 'quit'}));
    await proc.exitCode.timeout(
      const Duration(seconds: 10),
      onTimeout: () {
        proc.kill();
        return -1;
      },
    );
  }
}

/// A FeatureRow as Python's FakeDb records it (timbre base64).
List<Object?> _wire(FeatureRow r) => [
  r.path,
  r.bpm,
  r.energy,
  r.brightness,
  r.rolloff,
  r.beatStrength,
  r.spectralFlatness,
  r.spectralContrast,
  r.keyIndex,
  base64.encode(r.timbre),
  r.featuresVersion,
];

void main() {
  final ok =
      File(_python).existsSync() &&
      File('../StreamripApp/utils/sync/server.py').existsSync() &&
      Process.runSync(_python, ['-c', 'import aiohttp, numpy']).exitCode == 0;
  if (!ok) {
    test('companion protocol', () {}, skip: 'no Flet Python environment');
    return;
  }

  late Directory scratch;
  late _Server server;
  late Directory phone; // the phone's music folder

  setUpAll(() async {
    scratch = await Directory.systemTemp.createTemp('companion_');
    server = await _Server.start('${scratch.path}/server');
    phone = Directory('${scratch.path}/phone')..createSync();
  });
  tearDownAll(() async {
    await server.stop();
    await scratch.delete(recursive: true);
  });

  CompanionClient client({String? token, String name = 'Pixel 9 Pro'}) =>
      CompanionClient(
        '127.0.0.1',
        port: server.port,
        authToken: token,
        deviceName: name,
      );

  test('pyQuote matches urllib.parse.quote', () async {
    const values = [
      'Pixel 9 Pro',
      'Μαίρη\'s phone (2)!*~',
      'a/b?c=d&e#f',
      'tok+en/==',
      '',
      '🎵 €',
    ];
    final py = await server.cmd({'cmd': 'py_quote', 'values': values});
    expect([for (final v in values) pyQuote(v)], py['quoted']);
  });

  test('ping is open and says whether the token works', () async {
    final info = await client().ping();
    expect(info['status'], 'ok');
    expect(info['name'], 'Test Mac');
    expect(info['total_tracks'], 7);
    expect(info['authorized'], false);
    expect((await client(token: server.token).ping())['authorized'], true);
  });

  test('pairing: a wrong PIN, then the right one', () async {
    final c = client(name: 'Pixel 9 Pro (Μαίρη)');
    await expectLater(
      c.pair('0000'),
      throwsA(
        isA<CompanionAuthError>().having(
          (e) => e.message,
          'message',
          'Incorrect PIN',
        ),
      ),
    );
    expect(await c.pair(server.pin), server.token);
    expect(c.authToken, server.token);
    expect((await c.ping())['authorized'], true);
    // X-Device-Name arrives as sent (percent-encoded, decoded by the Mac).
    final state = await server.cmd({'cmd': 'state'});
    expect((state['last_client']! as List).first, 'Pixel 9 Pro (Μαίρη)');
  });

  test('unpaired calls fail as PermissionError does', () async {
    final c = client(token: 'wrong');
    await expectLater(
      c.getPendingTransfers(),
      throwsA(
        isA<CompanionAuthError>().having(
          (e) => e.message,
          'message',
          'Pending transfers: not paired (HTTP 401)',
        ),
      ),
    );
    await expectLater(
      c.offloadDsp(['/x.flac'], persist: (_) async {}),
      throwsA(isA<CompanionAuthError>()),
    );
  });

  test('a closed port is a network error', () async {
    final s = await ServerSocket.bind('127.0.0.1', 0);
    final port = s.port;
    await s.close();
    Object? err;
    try {
      await CompanionClient('127.0.0.1', port: port).ping();
    } catch (e) {
      err = e;
    }
    expect(err, isNotNull);
    expect(isNetworkError(err!), isTrue);
  });

  test('pending transfers, stream URLs and the download', () async {
    final c = client(token: server.token);
    final mine = await c.getPendingTransfers();
    final py = await server.cmd({'cmd': 'py_pending'});
    expect(mine, py['transfers']);
    expect(mine, hasLength(2));

    // stream_url as Python builds it, for every shape of transfer.
    final shapes = <Map<String, Object?>>[
      mine.first,
      {'stream_url': 'http://10.0.0.5:8945/api/v1/tracks/stream?path=%2Fa'},
      {'stream_url': 'nothing here'},
      {},
      {'stream_path': '/api/v1/x'},
    ];
    for (final token in [null, '', 'a b/+c']) {
      for (final t in shapes) {
        final cc = CompanionClient('192.168.1.4', port: 8946, authToken: token);
        final want = await server.cmd({
          'cmd': 'py_stream_url',
          'transfer': t,
          'token': token,
          'host': '192.168.1.4',
          'port': 8946,
        });
        expect(cc.streamUrl(t), want['url'], reason: '$t, token $token');
      }
    }

    // The URL serves the queued file (token in the query, as the download
    // pipeline sends no headers).
    final t = mine.first;
    final http = HttpClient();
    final resp = await (await http.getUrl(Uri.parse(c.streamUrl(t)))).close();
    final bytes = await resp.fold<List<int>>([], (a, b) => a..addAll(b));
    http.close();
    expect(resp.statusCode, 200);
    expect(bytes, File(t['path']! as String).readAsBytesSync());

    expect(await c.ackTransfers([t['id']! as String]), isTrue);
    final state = await server.cmd({'cmd': 'state'});
    expect(state['pending'], [mine[1]['id']]);
  });

  test('Send to Mac: file, features, duplicate, batch announce', () async {
    final c = client(token: server.token);
    final f = File('${phone.path}/Άλφα βήτα.flac')
      ..writeAsBytesSync(List.filled(321, 7));
    final dsp = {
      'bpm': 120.0,
      'energy': 0.5,
      'brightness': 1500.0,
      'rolloff': 0.2,
      'beat_strength': 0.6,
      'spectral_flatness': 0.1,
      'spectral_contrast': 3.0,
      'key_index': 4,
      'timbre_b64': base64.encode([1, 2, 3]),
      'features_version': 5,
    };
    final r1 = await c.pushTrack(f.path, {
      'title': 'Άλφα',
      'artist': 'Καλλιτέχνης',
      'album': 'Δίσκος',
      'dsp_features': dsp,
      'remaining': 0,
    });
    expect(r1['duplicate'], false);
    expect(
      r1['path'],
      '${server.scratch}/recv/Καλλιτέχνης/Δίσκος/Άλφα βήτα.flac',
    );
    final r2 = await c.pushTrack(f.path, {
      'artist': 'Καλλιτέχνης',
      'album': 'Δίσκος',
      'remaining': 0,
    });
    expect(r2['duplicate'], true);

    final state = await server.cmd({'cmd': 'state'});
    expect(state['recv_files'], ['Καλλιτέχνης/Δίσκος/Άλφα βήτα.flac']);
    expect(state['received'], [
      [
        [r1['path']],
        'Pixel 9 Pro',
      ],
    ]);
    final feats = (state['features']! as List).single as Map<String, Object?>;
    expect(feats['path'], r1['path']);
    expect(feats['bpm'], 120.0);
    expect(feats['key_index'], 4);
    expect(feats['timbre_blob'], base64.encode([1, 2, 3]));

    // Flet's client sends the same name percent-encoded, and the Mac keeps
    // it that way (PORT_MAP §7): the reason the port sends filename*.
    final g = File('${phone.path}/Γάμμα.flac')
      ..writeAsBytesSync(List.filled(99, 1));
    final py = await server.cmd({
      'cmd': 'py_push',
      'path': g.path,
      'meta': {'artist': 'X', 'album': 'Y', 'remaining': 0},
    });
    expect(
      (py['reply']! as Map)['path'],
      '${server.scratch}/recv/X/Y/%CE%93%CE%AC%CE%BC%CE%BC%CE%B1.flac',
    );

    await expectLater(
      c.pushTrack('${phone.path}/missing.flac', {}),
      throwsA(isA<PathNotFoundException>()),
    );
  });

  test('DSP offload stores exactly what Python stores', () async {
    // 11 analysable files (a batch of 10, then the rest), one the Mac can't
    // decode, one missing on the phone, one only the Mac's cache knows, one
    // whose cache row is from an older features_version.
    final paths = <String>[
      '/phone/Music/cached.flac',
      '/phone/Music/stale.flac',
    ];
    for (var i = 0; i < 11; i++) {
      final f = File('${phone.path}/t$i ü.flac')
        ..writeAsBytesSync(List.filled(500 + i * 37, i));
      paths.add(f.path);
      if (i == 4) {
        paths.add(
          (File('${phone.path}/bad.flac')..writeAsStringSync('BAD')).path,
        );
      }
    }
    paths.add('${phone.path}/gone.flac');

    // Each run starts from the same Mac cache (extract adds to it).
    await server.cmd({'cmd': 'reset_cache'});
    final batches = <List<Object?>>[];
    final progress = <int>[];
    final res = await client(token: server.token).offloadDsp(
      paths,
      persist: (rows) async => batches.add([for (final r in rows) _wire(r)]),
      progress: (done, total, _, _) => progress.add(done),
    );
    await server.cmd({'cmd': 'reset_cache'});
    final py = await server.cmd({'cmd': 'py_offload', 'paths': paths});
    final pyRes = py['result']! as Map<String, Object?>;
    expect(res.analysed, pyRes['analysed']);
    expect(res.failed, pyRes['failed']);
    expect(res.total, pyRes['total']);
    expect(batches, py['batches']);
    expect(res, (analysed: 12, failed: 3, total: 15));
    expect(batches.map((b) => b.length), [1, 10, 1]);
    expect(progress.last, 15);
  });

  test('offload: cancelled before it starts stores only the cache', () async {
    await server.cmd({'cmd': 'reset_cache'});
    final batches = <int>[];
    final res = await client(token: server.token).offloadDsp(
      ['/phone/Music/cached.flac', '${phone.path}/t0 ü.flac'],
      persist: (rows) async => batches.add(rows.length),
      cancelled: () => true,
    );
    expect(res, (analysed: 1, failed: 0, total: 2));
    expect(batches, [1]);
  });

  test('state bundle upload', () async {
    final zip = File('${phone.path}/mai_an_lab_state_latest.zip')
      ..writeAsBytesSync(List.generate(4096, (i) => i % 251));
    final r = await client(token: server.token).uploadStateBundle(zip.path);
    expect(r['status'], 'ok');
    final saved = File(r['saved_path']! as String);
    expect(saved.readAsBytesSync(), zip.readAsBytesSync());
    final state = await server.cmd({'cmd': 'state'});
    expect(state['bundles'], hasLength(1));
  });

  test('discovery: the query is answered by unicast', () async {
    final found = Completer<Map<String, Object?>>();
    final l = BeaconListener(
      port: 0,
      queryPort: server.udpPort,
      targets: const ['127.0.0.1'],
      holdMulticastLock: false,
      onDeviceFound: (s) {
        if (!found.isCompleted) found.complete(s);
      },
    );
    await l.start();
    try {
      final s = await found.future.timeout(const Duration(seconds: 8));
      expect(s['service'], beaconMagic);
      expect(s['type'], 'beacon');
      expect(s['name'], 'Test Mac');
      // The sender's address, not the advertised LAN IP.
      expect(s['ip'], '127.0.0.1');
      expect(s['port'], server.port);
      expect(l.discovered.keys, ['127.0.0.1:${server.port}']);
    } finally {
      l.stop();
    }
  });

  test('sweep finds the server by TCP connect', () async {
    expect(await sweepSubnet('127.0.0.9', server.port), ['127.0.0.1']);
    expect(await sweepSubnet('not an ip', server.port), isEmpty);
  });

  test('broadcast targets', () {
    expect(broadcastTargets('192.168.4.23'), [
      '255.255.255.255',
      '192.168.4.255',
    ]);
    expect(broadcastTargets('127.0.0.1'), ['255.255.255.255']);
    expect(slash24Broadcast('0.1.2.3'), isNull);
    expect(slash24Broadcast('fe80::1'), isNull);
  });
}
