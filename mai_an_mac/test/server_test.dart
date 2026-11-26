import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:test/test.dart';

import '../src/ffmpeg.dart';
import '../src/home.dart';
import '../src/server.dart';
import '../src/ui_server.dart';

/// The Companion endpoints, driven over real HTTP against a real server and
/// real ffmpeg. (The phone's own client is exercised against the same
/// server in mai_an_flutter/test/mac_companion_interop_test.dart.)

late Directory tmp;
late CompanionServer srv;
late UiServer ui;
late String base;
Ffmpeg? ffmpeg;

/// A mono 22.05 kHz WAV: a tone with a click every half second, [secs] long.
Uint8List wav(int secs) {
  const sr = 22050;
  final n = sr * secs;
  final b = ByteData(44 + n * 2);
  void tag(int o, String s) {
    for (var i = 0; i < s.length; i++) {
      b.setUint8(o + i, s.codeUnitAt(i));
    }
  }

  tag(0, 'RIFF');
  b.setUint32(4, 36 + n * 2, Endian.little);
  tag(8, 'WAVEfmt ');
  b.setUint32(16, 16, Endian.little);
  b.setUint16(20, 1, Endian.little);
  b.setUint16(22, 1, Endian.little);
  b.setUint32(24, sr, Endian.little);
  b.setUint32(28, sr * 2, Endian.little);
  b.setUint16(32, 2, Endian.little);
  b.setUint16(34, 16, Endian.little);
  tag(36, 'data');
  b.setUint32(40, n * 2, Endian.little);
  for (var i = 0; i < n; i++) {
    final t = i / sr;
    var v = 0.3 * _sin(2 * 3.14159265 * 220 * t);
    if (i % (sr ~/ 2) < 200) v += 0.6 * (1 - (i % (sr ~/ 2)) / 200);
    b.setInt16(44 + i * 2, (v.clamp(-1.0, 1.0) * 32000).round(), Endian.little);
  }
  return b.buffer.asUint8List();
}

double _sin(double x) {
  // dart:math without the import noise
  x = x % (2 * 3.14159265358979);
  var term = x, sum = x;
  for (var k = 1; k < 12; k++) {
    term *= -x * x / ((2 * k) * (2 * k + 1));
    sum += term;
  }
  return sum;
}

class Resp {
  Resp(this.status, this.bytes, this.headers);
  final int status;
  final List<int> bytes;
  final HttpHeaders headers;
  String get text => utf8.decode(bytes);
  Map<String, Object?> get json => jsonDecode(text) as Map<String, Object?>;
}

Future<Resp> call(
  String method,
  String path, {
  Object? json,
  String? token,
  List<int>? body,
  String? contentType,
  Map<String, String> headers = const {},
  String? baseUrl,
}) async {
  final c = HttpClient();
  try {
    final r = await c.openUrl(method, Uri.parse('${baseUrl ?? base}$path'));
    if (token != null) r.headers.set('authorization', 'Bearer $token');
    headers.forEach(r.headers.set);
    List<int>? data = body;
    if (json != null) {
      data = utf8.encode(jsonEncode(json));
      r.headers.contentType = ContentType.json;
    }
    if (contentType != null) r.headers.set('content-type', contentType);
    if (data != null) {
      r.contentLength = data.length;
      r.add(data);
    }
    final resp = await r.close();
    final bytes = await resp.fold<List<int>>([], (a, b) => a..addAll(b));
    return Resp(resp.statusCode, bytes, resp.headers);
  } finally {
    c.close(force: true);
  }
}

/// multipart/form-data with RFC 5987 file names, as the phone writes them.
({List<int> body, String type}) multipart(
  List<({String name, String? file, List<int> data, String? type})> parts,
) {
  const b = '----maiantest';
  final out = BytesBuilder();
  for (final p in parts) {
    out.add(utf8.encode('--$b\r\n'));
    final disp = p.file == null
        ? 'form-data; name="${p.name}"'
        : "form-data; name=\"${p.name}\"; filename*=UTF-8''${Uri.encodeComponent(p.file!)}";
    out.add(utf8.encode('Content-Disposition: $disp\r\n'));
    if (p.type != null) out.add(utf8.encode('Content-Type: ${p.type}\r\n'));
    out.add(utf8.encode('\r\n'));
    out.add(p.data);
    out.add(utf8.encode('\r\n'));
  }
  out.add(utf8.encode('--$b--\r\n'));
  return (body: out.toBytes(), type: 'multipart/form-data; boundary=$b');
}

Future<String> pair() async {
  final r = await call('POST', '/api/v1/pair', json: {'pin': srv.pin, 'device_name': 'Pixel 8'});
  expect(r.status, 200);
  return r.json['token']! as String;
}

void main() {
  setUpAll(() async => ffmpeg = await Ffmpeg.find());

  setUp(() async {
    tmp = Directory.systemTemp.createTempSync('maian_test_');
    final home = MaiAnHome('${tmp.path}/home');
    final settings = Settings.load(home)..set('receive_dir', '${tmp.path}/recv');
    srv = CompanionServer(
      home: home,
      settings: settings,
      ffmpeg: ffmpeg,
      port: 0,
      pin: '4821',
      deviceName: 'Chris’s Mac Ω',
      trustLoopback: false,
      beaconUdpPort: 0,
      beaconTargets: const [],
    );
    await srv.start();
    ui = UiServer(srv, port: 0);
    await ui.start();
    base = 'http://127.0.0.1:${srv.port}';
  });

  tearDown(() async {
    await ui.stop();
    await srv.stop();
    tmp.deleteSync(recursive: true);
  });

  group('pairing', () {
    test('ping is open and says whether the token works', () async {
      var r = await call('GET', '/api/v1/ping');
      expect(r.status, 200);
      expect(r.json['status'], 'ok');
      expect(r.json['device'], 'macOS');
      expect(r.json['name'], 'Chris’s Mac Ω');
      expect(r.json['features_version'], 5);
      expect(r.json['authorized'], false);
      final token = await pair();
      r = await call('GET', '/api/v1/ping', token: token, headers: {'x-device-name': 'Pixel%208'});
      expect(r.json['authorized'], true);
      expect((await srv.snapshot())['clients'], isNotEmpty);
    });

    test('wrong PIN is 403, and five of them rotate the PIN', () async {
      for (var i = 0; i < 4; i++) {
        final r = await call('POST', '/api/v1/pair', json: {'pin': '0000'});
        expect(r.status, 403);
      }
      expect(srv.pin, '4821');
      await call('POST', '/api/v1/pair', json: {'pin': '0000'});
      expect(srv.pin, isNot('4821'));
    });

    test('every other endpoint is 401 without the token', () async {
      for (final (m, p) in [
        ('POST', '/api/v1/dsp/match'),
        ('POST', '/api/v1/dsp/extract'),
        ('GET', '/api/v1/tracks/stream'),
        ('POST', '/api/v1/bundle/upload'),
        ('POST', '/api/v1/tracks/receive'),
        ('GET', '/api/v1/transfers/pending'),
        ('POST', '/api/v1/transfers/queue'),
        ('POST', '/api/v1/transfers/ack'),
      ]) {
        expect((await call(m, p, json: {})).status, 401, reason: '$m $p');
      }
      expect((await call('GET', '/api/v1/ping', token: 'nope')).json['authorized'], false);
    });

    test('the token survives a restart, so phones stay paired', () async {
      final token = await pair();
      final again = Settings.load(MaiAnHome('${tmp.path}/home'));
      expect(again.token, token);
    });

    test('loopback (adb reverse) needs no PIN when trusted', () async {
      final home = MaiAnHome('${tmp.path}/home2');
      final s = CompanionServer(
        home: home,
        settings: Settings.load(home),
        port: 0,
        beaconUdpPort: 0,
        beaconTargets: const [],
      );
      await s.start();
      final r = await call('GET', '/api/v1/ping', baseUrl: 'http://127.0.0.1:${s.port}');
      expect(r.json['authorized'], true);
      await s.stop();
    });

    test('unpair revokes the old token', () async {
      final token = await pair();
      srv.resetPairing();
      expect((await call('GET', '/api/v1/transfers/pending', token: token)).status, 401);
    });
  });

  group('offload', () {
    test('extracts, then answers from the cache without the audio',
        skip: Ffmpeg.which('ffmpeg') == null ? 'ffmpeg missing' : null, () async {
      final token = await pair();
      const phonePath = '/storage/emulated/0/Music/Ωmega/tone.wav';
      var m = await call('POST', '/api/v1/dsp/match', token: token, json: {
        'tracks': [{'path': phonePath}],
      });
      expect(m.json['cached'], isEmpty);
      expect(m.json['missing'], [phonePath]);

      final mp = multipart([
        (name: 'path', file: null, data: utf8.encode(phonePath), type: null),
        (name: 'file', file: 'tone.wav', data: wav(25), type: 'application/octet-stream'),
      ]);
      final e = await call('POST', '/api/v1/dsp/extract', token: token, body: mp.body, contentType: mp.type);
      expect(e.status, 200, reason: e.text);
      final f = e.json;
      expect(f['status'], 'ok');
      expect(f['path'], phonePath);
      expect(f['features_version'], 5);
      for (final k in ['bpm', 'energy', 'brightness', 'rolloff', 'beat_strength', 'spectral_flatness', 'spectral_contrast']) {
        expect(f[k], isA<num>(), reason: k);
      }
      expect(f['key_index'], inInclusiveRange(0, 23));
      expect(base64.decode(f['timbre_b64']! as String).length, 272);
      expect(srv.offload.extracted, 1);
      expect(srv.offload.done, srv.offload.total, reason: 'the one-track run is finished');

      m = await call('POST', '/api/v1/dsp/match', token: token, json: {
        'tracks': [{'path': phonePath}],
      });
      expect(m.json['missing'], isEmpty);
      final cached = (m.json['cached']! as List).single as Map;
      expect(cached['path'], phonePath);
      expect(cached['bpm'], f['bpm']);
      expect(cached['timbre_b64'], f['timbre_b64']);
      // A new run that the cache answered entirely.
      expect(srv.offload.cached, 1);
      expect(srv.offload.extracted, 0);
    });

    test('the cache survives a restart', skip: Ffmpeg.which('ffmpeg') == null ? 'ffmpeg missing' : null, () async {
      final token = await pair();
      final mp = multipart([
        (name: 'path', file: null, data: utf8.encode('/p/a.wav'), type: null),
        (name: 'file', file: 'a.wav', data: wav(25), type: null),
      ]);
      await call('POST', '/api/v1/dsp/extract', token: token, body: mp.body, contentType: mp.type);
      await srv.cache.close();
      final c2 = srv.cache.file;
      final reloaded = CompanionServer(
        home: MaiAnHome('${tmp.path}/home'),
        settings: Settings.load(MaiAnHome('${tmp.path}/home')),
        port: 0,
        beaconUdpPort: 0,
      );
      reloaded.cache.load();
      expect(reloaded.cache.get('/p/a.wav'), isNotNull);
      expect(c2.existsSync(), true);
    });

    test('garbage audio is a 500 and counted as failed', skip: Ffmpeg.which('ffmpeg') == null ? 'ffmpeg missing' : null, () async {
      final token = await pair();
      final mp = multipart([
        (name: 'path', file: null, data: utf8.encode('/p/bad.flac'), type: null),
        (name: 'file', file: 'bad.flac', data: utf8.encode('not audio at all'), type: null),
      ]);
      final e = await call('POST', '/api/v1/dsp/extract', token: token, body: mp.body, contentType: mp.type);
      expect(e.status, 500);
      expect(srv.offload.failed, 1);
    });
  });

  group('files from the phone', () {
    Future<Resp> push(String file, Map<String, Object?> meta, List<int> data, String token) {
      final mp = multipart([
        (name: 'meta', file: null, data: utf8.encode(jsonEncode(meta)), type: 'application/json'),
        (name: 'file', file: file, data: data, type: null),
      ]);
      return call('POST', '/api/v1/tracks/receive', token: token, body: mp.body, contentType: mp.type, headers: {'x-device-name': 'Pixel%208'});
    }

    test('lands under Artist/Album, Greek names intact, duplicates noticed', () async {
      final token = await pair();
      final data = List<int>.generate(5000, (i) => i % 251);
      var r = await push('Ο Δρόμος.flac', {'artist': 'Σωκράτης', 'album': 'Άλμπουμ', 'remaining': 0}, data, token);
      expect(r.status, 200, reason: r.text);
      final dest = r.json['path']! as String;
      expect(dest, '${tmp.path}/recv/Σωκράτης/Άλμπουμ/Ο Δρόμος.flac');
      expect(File(dest).readAsBytesSync(), data);
      expect(r.json['duplicate'], false);

      r = await push('Ο Δρόμος.flac', {'artist': 'Σωκράτης', 'album': 'Άλμπουμ'}, data, token);
      expect(r.json['duplicate'], true);

      // Same name, different bytes: never overwritten.
      r = await push('Ο Δρόμος.flac', {'artist': 'Σωκράτης', 'album': 'Άλμπουμ'}, [...data, 1], token);
      expect(r.json['path'], endsWith('Ο Δρόμος (2).flac'));
      expect(Directory('${tmp.path}/recv/Σωκράτης/Άλμπουμ').listSync().whereType<File>().length, 2);
      expect((await srv.snapshot())['received'], hasLength(2));
    });

    test('refuses non-audio, and cannot climb out of the folder', () async {
      final token = await pair();
      var r = await push('evil.sh', {}, [1, 2, 3], token);
      expect(r.status, 415);
      r = await push('x.flac', {'artist': '..', 'album': '..'}, [1, 2, 3], token);
      expect(r.status, 200);
      expect(r.json['path'], '${tmp.path}/recv/x.flac');
      r = await push('../../etc/x.flac', {}, [1, 2, 3], token);
      expect((r.json['path']! as String).startsWith('${tmp.path}/recv/'), true);
      expect(File('${tmp.path}/recv/x.flac').existsSync(), true);
      r = await push('empty.flac', {}, [], token);
      expect(r.status, 400);
    });

    test('state bundles are saved to the backups folder', () async {
      final token = await pair();
      final mp = multipart([(name: 'file', file: 'b.zip', data: [80, 75, 3, 4, 9, 9], type: 'application/zip')]);
      final r = await call('POST', '/api/v1/bundle/upload', token: token, body: mp.body, contentType: mp.type);
      expect(r.status, 200);
      final saved = File(r.json['saved_path']! as String);
      expect(saved.path, contains('state_backups/mai_an_lab_state_'));
      expect(saved.readAsBytesSync(), [80, 75, 3, 4, 9, 9]);
    });
  });

  group('files to the phone', () {
    late File track;
    setUp(() {
      track = File('${tmp.path}/Song Ω.flac')..writeAsBytesSync(List.generate(3000, (i) => i % 200));
    });

    test('queue, list, download, ack', () async {
      final token = await pair();
      final q = await call('POST', '/api/v1/transfers/queue', token: token, json: {
        'tracks': [{'path': track.path, 'title': 'Song', 'artist': 'A', 'album': 'B'}],
        'quality': 'original',
        'precompute_dsp': false,
      });
      expect(q.json['queued'], 1);

      final p = await call('GET', '/api/v1/transfers/pending', token: token);
      final t = (p.json['transfers']! as List).single as Map;
      expect(p.json['count'], 1);
      expect(t['title'], 'Song');
      expect(t['artist'], 'A');
      expect(t['extension'], 'flac');
      expect(t['stream_path'], startsWith('/api/v1/tracks/stream?path='));

      final d = await call('GET', '${t['stream_path']}&token=$token');
      expect(d.status, 200);
      expect(d.bytes, track.readAsBytesSync());
      expect(d.headers.value('content-disposition'), "attachment; filename*=UTF-8''Song%20%CE%A9.flac");

      expect((await call('GET', '${t['stream_path']}')).status, 401);
      final other = File('${tmp.path}/secret.flac')..writeAsBytesSync([1]);
      expect(
        (await call('GET', '/api/v1/tracks/stream?path=${Uri.encodeQueryComponent(other.path)}&quality=original&token=$token')).status,
        404,
        reason: 'only queued files are served',
      );

      final a = await call('POST', '/api/v1/transfers/ack', token: token, json: {'ids': [t['id']]});
      expect(a.json['remaining'], 0);
      expect((await call('GET', '${t['stream_path']}&token=$token')).status, 404);
    });

    test('re-queueing a track replaces its entry (stable id)', () async {
      await srv.queueTracks([{'path': track.path}], quality: 'original', precomputeDsp: false);
      await srv.queueTracks([{'path': track.path}], quality: 'original', precomputeDsp: false);
      expect(srv.queue.items, hasLength(1));
    });

    test('the queue survives a restart', () async {
      await srv.queueTracks([{'path': track.path}], quality: 'original', precomputeDsp: false);
      final again = CompanionServer(home: MaiAnHome('${tmp.path}/home'), settings: Settings.load(MaiAnHome('${tmp.path}/home')), port: 0, beaconUdpPort: 0);
      again.queue.load();
      expect(again.queue.items, hasLength(1));
    });

    test('AAC profile is converted and served as m4a', skip: Ffmpeg.which('ffmpeg') == null ? 'ffmpeg missing' : null, () async {
      final w = File('${tmp.path}/tone.wav')..writeAsBytesSync(wav(8));
      final token = await pair();
      final q = await srv.queueTracks([{'path': w.path}], quality: 'aac_256k', precomputeDsp: false);
      expect(q.single['extension'], 'm4a');
      final path = q.single['stream_path']! as String;
      final d = await call('GET', '$path&token=$token');
      expect(d.status, 200);
      // MP4 container: "ftyp" at byte 4.
      expect(utf8.decode(d.bytes.sublist(4, 8)), 'ftyp');
      expect(d.bytes.length, lessThan(w.lengthSync()));
    });

    test('tracks sent with analysis carry their features', skip: Ffmpeg.which('ffmpeg') == null ? 'ffmpeg missing' : null, () async {
      final w = File('${tmp.path}/tone2.wav')..writeAsBytesSync(wav(25));
      final q = await srv.queueTracks([{'path': w.path}], quality: 'original', precomputeDsp: true);
      final f = q.single['dsp_features']! as Map;
      expect(f['features_version'], 5);
      expect(base64.decode(f['timbre_b64']! as String).length, 272);
    });
  });

  group('beacon', () {
    test('answers a phone\'s query by unicast with where we are', () async {
      final sock = await RawDatagramSocket.bind(InternetAddress.loopbackIPv4, 0);
      sock.send(
        utf8.encode(jsonEncode({'service': 'mai_an_sync', 'version': 1, 'type': 'query'})),
        InternetAddress.loopbackIPv4,
        srv.beaconPort,
      );
      final reply = await sock.where((e) => e == RawSocketEvent.read).map((_) => sock.receive()).where((d) => d != null).first.timeout(const Duration(seconds: 3));
      final j = jsonDecode(utf8.decode(reply!.data)) as Map;
      expect(j['service'], 'mai_an_sync');
      expect(j['type'], 'beacon');
      expect(j['name'], 'Chris’s Mac Ω');
      expect(j['port'], srv.port);
      sock.close();
    });
  });

  group('control page', () {
    String ubase() => 'http://127.0.0.1:${ui.port}';

    test('serves the page and state, to localhost only', () async {
      var r = await call('GET', '/', baseUrl: ubase());
      expect(r.status, 200);
      expect(r.text, contains('Mai An Companion'));
      r = await call('GET', '/api/state', baseUrl: ubase());
      expect(r.json['pin'], '4821');
      expect(r.json['port'], srv.port);
      // DNS rebinding: a foreign Host header is refused.
      r = await call('GET', '/api/state', baseUrl: ubase(), headers: {'host': 'evil.example:${ui.port}'});
      expect(r.status, 403);
    });

    test('state-changing requests need the custom header', () async {
      var r = await call('POST', '/api/rotate-pin', baseUrl: ubase());
      expect(r.status, 403);
      expect(srv.pin, '4821');
      r = await call('POST', '/api/rotate-pin', baseUrl: ubase(), headers: {'x-maian': '1'});
      expect(r.status, 200);
      expect(srv.pin, isNot('4821'));
      r = await call('POST', '/api/rotate-pin', baseUrl: ubase(), headers: {'x-maian': '1', 'origin': 'https://evil.example'});
      expect(r.status, 403);
    });

    test('a dropped file is kept in the outbox, queued, and removed once fetched', () async {
      final h = {'x-maian': '1'};
      final up = await call('POST', '/api/upload?name=${Uri.encodeQueryComponent('Δοκιμή.flac')}', baseUrl: ubase(), headers: h, body: List.generate(2000, (i) => i % 100));
      expect(up.status, 200);
      final path = up.json['path']! as String;
      expect(path, startsWith(srv.home.outbox.path));
      expect(path, endsWith('Δοκιμή.flac'));
      final s = await call('POST', '/api/send', baseUrl: ubase(), headers: h, json: {'paths': [path], 'quality': 'original', 'dsp': false, 'wait': true});
      expect(s.json['queued'], 1);
      expect(srv.queue.items, hasLength(1));
      final id = '${srv.queue.items.single['id']}';
      srv.removeTransfers([id]);
      expect(File(path).existsSync(), false, reason: 'outbox copy removed');
    });

    test('uploads cannot escape the outbox', () async {
      final up = await call('POST', '/api/upload?name=${Uri.encodeQueryComponent('../../evil.flac')}', baseUrl: ubase(), headers: {'x-maian': '1'}, body: [1, 2, 3]);
      expect((up.json['path']! as String).startsWith(srv.home.outbox.path), true);
    });

    test('"show in Finder" only opens files Mac handed out', () async {
      final r = await call('POST', '/api/reveal', baseUrl: ubase(), headers: {'x-maian': '1'}, json: {'path': '/etc/hosts'});
      expect(r.status, 404);
    });

    test('event stream pushes state when something changes', () async {
      final c = HttpClient();
      final req = await c.getUrl(Uri.parse('${ubase()}/api/events'));
      final resp = await req.close();
      final lines = resp.transform(utf8.decoder).transform(const LineSplitter());
      final it = StreamIterator(lines.where((l) => l.startsWith('data: ')));
      expect(await it.moveNext().timeout(const Duration(seconds: 3)), true);
      expect((jsonDecode(it.current.substring(6)) as Map)['pin'], '4821');
      srv.rotatePin();
      expect(await it.moveNext().timeout(const Duration(seconds: 3)), true);
      expect((jsonDecode(it.current.substring(6)) as Map)['pin'], srv.pin);
      await it.cancel();
      c.close(force: true);
    });
  });
}
