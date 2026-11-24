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
import 'package:mai_an_lab/downloads/companion_download.dart';
import 'package:mai_an_lab/sync/companion_client.dart';
import 'package:mai_an_lab/sync/discovery.dart';
import 'package:mai_an_lab/sync/sync_manager.dart';

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

class _ManagerHost implements SyncHost {
  final prefs = <String, Object?>{};
  final snacks = <String>[];
  final pins = <PinPrompt>[];
  final queued = <Map<String, Object?>>[];
  late CompanionSyncManager manager;

  @override
  Object? pref(String key) => prefs[key];
  @override
  Future<void> savePref(String key, Object? value) async => prefs[key] = value;
  @override
  bool get inBackground => false;
  @override
  void snack(String text, {SyncIcon? icon, bool red = false}) =>
      snacks.add(text);
  @override
  void showPinPrompt(PinPrompt prompt) => pins.add(prompt);
  @override
  void dismissPinPrompt(PinPrompt prompt) => manager.pinPromptClosed(prompt);
  @override
  void showTransferPrompt(TransferPrompt prompt) {}
  @override
  void dismissTransferPrompt(TransferPrompt prompt) {}
  @override
  TransferQueue? get queue => _Q(queued);
  @override
  Future<Map<String, Object?>?> storedFeatures(String path) async => null;
}

class _Q implements TransferQueue {
  _Q(this.items);
  final List<Map<String, Object?>> items;
  @override
  void enqueueMany(
    List<Map<String, Object?>> items, {
    String qualityTier = 'mp3',
  }) => this.items.addAll(items);
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

  test('the manager against the Mac: PIN, transfers, ack, Send', () async {
    // Last: it takes the transfer the earlier tests left pending.
    final host = _ManagerHost();
    final usb = await ServerSocket.bind('127.0.0.1', 0); // not the real 8945
    final usbPort = usb.port;
    await usb.close();
    final m = host.manager = CompanionSyncManager(
      host,
      listenerFactory: (f) => BeaconListener(
        onDeviceFound: f,
        port: 0,
        queryPort: server.udpPort,
        targets: const ['127.0.0.1'],
        holdMulticastLock: false,
      ),
      deviceName: () async => 'Pixel 9 Pro',
      usbPort: usbPort,
    );
    final out = await m.connectManual('127.0.0.1', server.port);
    expect(out, 'Found Test Mac (127.0.0.1). Enter the PIN shown on the Mac.');
    final pin = host.pins.single;
    expect(pin.via, 'USB');
    expect(await pin.submit('0000'), startsWith('Incorrect PIN.'));
    expect(await pin.submit(server.pin), isNull);
    expect(m.isConnected, isTrue);
    expect(host.prefs['companion_paired_token'], server.token);
    expect(host.prefs.containsKey('companion_paired_host'), isFalse);

    // Auto-accepted on connect.
    for (var i = 0; i < 50 && host.queued.isEmpty; i++) {
      await Future<void>.delayed(const Duration(milliseconds: 20));
    }
    final item = host.queued.single;
    final pending = (await server.cmd({'cmd': 'state'}))['pending']! as List;
    expect(item['id'], pending.single);
    // The URL serves the file.
    final http = HttpClient();
    final resp = await (await http.getUrl(
      Uri.parse(item['url']! as String),
    )).close();
    await resp.drain<void>();
    http.close();
    expect(resp.statusCode, 200);

    m.onTransferFinished(item['id']! as String, ok: true);
    for (var i = 0; i < 50; i++) {
      final st = await server.cmd({'cmd': 'state'});
      if ((st['pending']! as List).isEmpty) break;
      await Future<void>.delayed(const Duration(milliseconds: 20));
    }
    expect((await server.cmd({'cmd': 'state'}))['pending'], isEmpty);

    final f = File('${phone.path}/Δέλτα.flac')
      ..writeAsBytesSync(List.filled(77, 3));
    expect(
      m.sendTracks([
        {'path': f.path, 'title': 'Δέλτα', 'artist': 'Β', 'album': 'Γ'},
      ]),
      1,
    );
    for (var i = 0; i < 100 && m.isSending; i++) {
      await Future<void>.delayed(const Duration(milliseconds: 20));
    }
    expect(host.snacks.last, 'Sent 1 track to Test Mac');
    final st = await server.cmd({'cmd': 'state'});
    expect(st['recv_files'], contains('Β/Γ/Δέλτα.flac'));
    await m.stop();
    m.dispose();
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

  // Last: it queues its own transfers, which the tests above don't expect.
  test('a transfer downloads exactly as streamrip_api.download does', () async {
    final nfd = 'Άλφα'; // "Άλφα" as macOS spells it (NFD)
    final queued = await server.cmd({
      'cmd': 'queue_files',
      'files': [
        {'name': '$nfd.flac', 'size': 3000, 'fill': 1},
        {'name': 'AC:DC?.mp3', 'size': 1500, 'fill': 2},
        {'name': 'dup.flac', 'size': 2000, 'fill': 3},
      ],
    });
    final ids = (queued['queued']! as List).cast<String>();
    final c = client(token: server.token);
    final pending = {for (final t in await c.getPendingTransfers()) t['id']: t};
    String url(String id) => c.streamUrl(pending[id]!);

    final pyDir = Directory('${scratch.path}/dl_py');
    final dartDir = Directory('${scratch.path}/dl_dart');

    // Both downloads of [u] into their folders: the same result, and the
    // same files on disk afterwards.
    Future<void> same(String u, {String? reason}) async {
      final py = await server.cmd({
        'cmd': 'py_download',
        'url': u,
        'dir': pyDir.path,
      });
      final events = <List<Object?>>[];
      Object? mine;
      try {
        final paths = await companionDownload(
          u,
          dartDir.path,
          progress: (s, p, m) => events.add([s, p, m]),
        );
        mine = [for (final p in paths) p.substring(dartDir.path.length)];
      } on DownloadError catch (e) {
        mine = [e.pyType, e.message];
      }
      final want = py['paths'] != null
          ? [
              for (final p in (py['paths']! as List).cast<String>())
                p.substring(pyDir.path.length),
            ]
          : [py['exc_type'], py['exc']];
      expect(mine, want, reason: reason);
      Map<String, List<int>> files(Directory d) => {
        for (final f in d.listSync().whereType<File>())
          f.path.substring(d.path.length): f.readAsBytesSync(),
      };
      expect(files(dartDir), files(pyDir), reason: reason);
      // The first and last progress reports agree (chunking may differ).
      final pyEvents = [
        for (final e in (py['events']! as List).cast<Map<String, Object?>>())
          [e['status'], e['percent'], e['message']],
      ];
      if (pyEvents.isNotEmpty) {
        expect(events.first, pyEvents.first, reason: reason);
        expect(events.last, pyEvents.last, reason: reason);
      }
    }

    for (final id in ids) {
      await same(url(id), reason: 'fresh $id');
    }
    // The NFD name was saved composed.
    expect(File('${dartDir.path}/Άλφα.flac').existsSync(), isTrue);
    // Sent again: already on this device, nothing new written.
    await same(url(ids[2]), reason: 'again');
    // A different file holds the name (and the next one): numbered.
    for (final d in [pyDir, dartDir]) {
      File('${d.path}/dup.flac').writeAsBytesSync([9]);
      File('${d.path}/dup (2).flac').writeAsBytesSync([9]);
    }
    await same(url(ids[2]), reason: 'taken name');
    expect(File('${dartDir.path}/dup (3).flac').existsSync(), isTrue);
    // Not paired, and a file the Mac never queued.
    await same(url(ids[0]).split('&token=').first, reason: 'no token');
    await same(
      c.streamUrl({
        'stream_path':
            '/api/v1/tracks/stream?path=%2Fetc%2Fhosts&quality=original',
      }),
      reason: 'not queued',
    );
  });
}
