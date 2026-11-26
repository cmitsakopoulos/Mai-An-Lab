import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:test/test.dart';

import '../src/apk.dart';
import '../src/home.dart';
import '../src/server.dart';
import '../src/ui_server.dart';

/// Phone app updates: the APK reader, the offer in /ping, the download and
/// the phone's status reports, and the build-folder watcher.

// ── a synthetic APK: binary AndroidManifest.xml in a stored zip ─────────────

/// A UTF-16 string pool, a resource map and the tags an APK manifest has.
Uint8List axml({
  required String package,
  required int versionCode,
  required String versionName,
  bool debuggable = false,
}) {
  // Indices 0-2 are attribute names with resource ids (as aapt2 writes
  // them, the names may be empty: the ids are what count).
  final strings = [
    '',
    '',
    'debuggable',
    'package',
    'manifest',
    'application',
    package,
    versionName,
  ];
  const resIds = [0x0101021b, 0x0101021c, 0x0101000f];

  final pool = BytesBuilder();
  final offsets = <int>[];
  final data = BytesBuilder();
  for (final s in strings) {
    offsets.add(data.length);
    final b = ByteData(2 + s.length * 2 + 2);
    b.setUint16(0, s.length, Endian.little);
    for (var i = 0; i < s.length; i++) {
      b.setUint16(2 + i * 2, s.codeUnitAt(i), Endian.little);
    }
    data.add(b.buffer.asUint8List());
  }
  while (data.length % 4 != 0) {
    data.addByte(0);
  }
  final headerSize = 28;
  final stringsStart = headerSize + strings.length * 4;
  final h = ByteData(stringsStart);
  h.setUint16(0, 0x0001, Endian.little);
  h.setUint16(2, headerSize, Endian.little);
  h.setUint32(4, stringsStart + data.length, Endian.little);
  h.setUint32(8, strings.length, Endian.little);
  h.setUint32(20, stringsStart, Endian.little);
  for (var i = 0; i < offsets.length; i++) {
    h.setUint32(28 + i * 4, offsets[i], Endian.little);
  }
  pool
    ..add(h.buffer.asUint8List())
    ..add(data.toBytes());

  final rm = ByteData(8 + resIds.length * 4);
  rm.setUint16(0, 0x0180, Endian.little);
  rm.setUint16(2, 8, Endian.little);
  rm.setUint32(4, rm.lengthInBytes, Endian.little);
  for (var i = 0; i < resIds.length; i++) {
    rm.setUint32(8 + i * 4, resIds[i], Endian.little);
  }

  Uint8List element(
    int nameIdx,
    List<(int name, int raw, int type, int data)> attrs,
  ) {
    final e = ByteData(36 + attrs.length * 20);
    e.setUint16(0, 0x0102, Endian.little);
    e.setUint16(2, 16, Endian.little);
    e.setUint32(4, e.lengthInBytes, Endian.little);
    e.setUint32(12, 0xffffffff, Endian.little); // comment
    e.setUint32(16, 0xffffffff, Endian.little); // ns
    e.setUint32(20, nameIdx, Endian.little);
    e.setUint16(24, 20, Endian.little); // attributeStart
    e.setUint16(26, 20, Endian.little); // attributeSize
    e.setUint16(28, attrs.length, Endian.little);
    for (var i = 0; i < attrs.length; i++) {
      final o = 36 + i * 20;
      final (name, raw, type, value) = attrs[i];
      e.setUint32(o, 0xffffffff, Endian.little);
      e.setUint32(o + 4, name, Endian.little);
      e.setUint32(o + 8, raw, Endian.little);
      e.setUint16(o + 12, 8, Endian.little);
      e.setUint8(o + 15, type);
      e.setUint32(o + 16, value, Endian.little);
    }
    return e.buffer.asUint8List();
  }

  final manifest = element(4, [
    (0, 0xffffffff, 0x10, versionCode),
    (1, 7, 0x03, 7),
    (3, 6, 0x03, 6),
  ]);
  final app = element(5, [(2, 0xffffffff, 0x12, debuggable ? 0xffffffff : 0)]);

  final body = BytesBuilder()
    ..add(pool.toBytes())
    ..add(rm.buffer.asUint8List())
    ..add(manifest)
    ..add(app);
  final top = ByteData(8);
  top.setUint16(0, 0x0003, Endian.little);
  top.setUint16(2, 8, Endian.little);
  top.setUint32(4, 8 + body.length, Endian.little);
  return (BytesBuilder()
        ..add(top.buffer.asUint8List())
        ..add(body.toBytes()))
      .toBytes();
}

/// A zip of [files], the manifest deflated as in a real APK.
Uint8List zip(Map<String, List<int>> files) {
  final out = BytesBuilder();
  final central = BytesBuilder();
  for (final MapEntry(key: name, value: raw) in files.entries) {
    final comp = ZLibEncoder(raw: true).convert(raw);
    final n = utf8.encode(name);
    final off = out.length;
    final lh = ByteData(30);
    lh.setUint32(0, 0x04034b50, Endian.little);
    lh.setUint16(8, 8, Endian.little);
    lh.setUint32(18, comp.length, Endian.little);
    lh.setUint32(22, raw.length, Endian.little);
    lh.setUint16(26, n.length, Endian.little);
    out
      ..add(lh.buffer.asUint8List())
      ..add(n)
      ..add(comp);
    final ch = ByteData(46);
    ch.setUint32(0, 0x02014b50, Endian.little);
    ch.setUint16(10, 8, Endian.little);
    ch.setUint32(20, comp.length, Endian.little);
    ch.setUint32(24, raw.length, Endian.little);
    ch.setUint16(28, n.length, Endian.little);
    ch.setUint32(42, off, Endian.little);
    central
      ..add(ch.buffer.asUint8List())
      ..add(n);
  }
  final cdOff = out.length;
  final cd = central.toBytes();
  out.add(cd);
  final e = ByteData(22);
  e.setUint32(0, 0x06054b50, Endian.little);
  e.setUint16(8, files.length, Endian.little);
  e.setUint16(10, files.length, Endian.little);
  e.setUint32(12, cd.length, Endian.little);
  e.setUint32(16, cdOff, Endian.little);
  out.add(e.buffer.asUint8List());
  return out.toBytes();
}

File writeApk(
  String path, {
  String package = 'lab.maian.test',
  int code = 5,
  String name = '0.4.0',
  bool debug = false,
  String filler = 'classes',
}) => File(path)
  ..parent.createSync(recursive: true)
  ..writeAsBytesSync(
    zip({
      'AndroidManifest.xml': axml(
        package: package,
        versionCode: code,
        versionName: name,
        debuggable: debug,
      ),
      'classes.dex': utf8.encode(filler * 500),
    }),
  );

// ── harness ─────────────────────────────────────────────────────────────────

late Directory tmp;
late CompanionServer srv;
late UiServer ui;
final notes = <String>[];

Future<({int status, List<int> bytes, HttpHeaders headers})> call(
  String method,
  String url, {
  Object? json,
  String? token,
  Map<String, String> headers = const {},
  List<int>? body,
}) async {
  final c = HttpClient();
  try {
    final r = await c.openUrl(method, Uri.parse(url));
    if (token != null) r.headers.set('authorization', 'Bearer $token');
    headers.forEach(r.headers.set);
    final data = json != null ? utf8.encode(jsonEncode(json)) : body;
    if (json != null) r.headers.contentType = ContentType.json;
    if (data != null) {
      r.contentLength = data.length;
      r.add(data);
    }
    final resp = await r.close();
    final bytes = await resp.fold<List<int>>([], (a, b) => a..addAll(b));
    return (status: resp.statusCode, bytes: bytes, headers: resp.headers);
  } finally {
    c.close(force: true);
  }
}

Map<String, Object?> j(List<int> b) =>
    jsonDecode(utf8.decode(b)) as Map<String, Object?>;

String get base => 'http://127.0.0.1:${srv.port}';
String get uiBase => 'http://127.0.0.1:${ui.port}';

Future<String> pair() async {
  final r = await call(
    'POST',
    '$base/api/v1/pair',
    json: {'pin': srv.pin, 'device_name': 'Pixel 8'},
  );
  return j(r.bytes)['token']! as String;
}

void main() {
  setUp(() async {
    tmp = Directory.systemTemp.createTempSync('maian_upd_');
    final home = MaiAnHome('${tmp.path}/home');
    srv = CompanionServer(
      home: home,
      settings: Settings.load(home),
      port: 0,
      pin: '1234',
      trustLoopback: false,
      beaconUdpPort: 0,
      beaconTargets: const [],
    );
    notes.clear();
    srv.notifier = (t, b) => notes.add('$t: $b');
    await srv.start();
    ui = UiServer(srv, port: 0);
    await ui.start();
  });

  tearDown(() async {
    await ui.stop();
    await srv.stop();
    tmp.deleteSync(recursive: true);
  });

  group('APK reader', () {
    test('package, version and debuggable from the binary manifest', () {
      final f = writeApk(
        '${tmp.path}/a.apk',
        code: 12,
        name: '1.2.0',
        debug: true,
      );
      final i = ApkInfo.read(f.path);
      expect(i.package, 'lab.maian.test');
      expect(i.versionCode, 12);
      expect(i.versionName, '1.2.0');
      expect(i.debuggable, isTrue);
      expect(i.label, '1.2.0 (12)');
    });

    test('anything else is a FormatException', () {
      final f = File('${tmp.path}/x.apk')
        ..writeAsStringSync('not a zip at all');
      expect(() => ApkInfo.read(f.path), throwsFormatException);
      final z = File('${tmp.path}/z.apk')
        ..writeAsBytesSync(
          zip({
            'a.txt': [1, 2, 3],
          }),
        );
      expect(() => ApkInfo.read(z.path), throwsFormatException);
    });

    final real =
        '${Directory.current.path}/../mai_an_flutter/build/app/outputs/flutter-apk/app-release.apk';
    test(
      'reads the real Flutter build',
      skip: File(real).existsSync() ? null : 'no build',
      () {
        final i = ApkInfo.read(real);
        expect(i.package, startsWith('com.mitsakopoulos.maianlab'));
        expect(i.versionCode, greaterThan(0));
        expect(i.debuggable, isFalse);
      },
    );
  });

  group('offer to the phone', () {
    test(
      'only a paired phone sees the offer, and downloads the exact bytes',
      () async {
        final f = writeApk('${tmp.path}/b.apk');
        await srv.updates.publish(f.path);
        final digest = '${sha256.convert(f.readAsBytesSync())}';

        var r = await call('GET', '$base/api/v1/ping');
        expect(j(r.bytes).containsKey('app_update'), isFalse);
        expect((await call('GET', '$base/api/v1/app/apk')).status, 401);

        final token = await pair();
        r = await call('GET', '$base/api/v1/ping', token: token);
        final offer = j(r.bytes)['app_update']! as Map<String, Object?>;
        expect(offer['package'], 'lab.maian.test');
        expect(offer['version_code'], 5);
        expect(offer['sha256'], digest);
        expect(offer['size'], f.lengthSync());

        // The download URL works with the token as a query parameter too.
        r = await call('GET', '$base${offer['path']}?token=$token');
        expect(r.status, 200);
        expect(
          r.headers.contentType?.mimeType,
          'application/vnd.android.package-archive',
        );
        expect('${sha256.convert(r.bytes)}', digest);
        expect(srv.updates.phoneStatus?['state'], 'downloading');
      },
    );

    test(
      'the offered copy does not change when the build is overwritten',
      () async {
        final f = writeApk('${tmp.path}/c.apk', filler: 'one');
        await srv.updates.publish(f.path);
        writeApk(f.path, filler: 'two');
        final token = await pair();
        final r = await call('GET', '$base/api/v1/app/apk', token: token);
        expect(
          utf8.decode(r.bytes, allowMalformed: true),
          isNot(contains('two')),
        );
      },
    );

    test('status reports reach the page; installed notifies once', () async {
      await srv.updates.publish(writeApk('${tmp.path}/d.apk').path);
      final token = await pair();
      notes.clear();
      var r = await call(
        'POST',
        '$base/api/v1/app/status',
        token: token,
        json: {'state': 'installing'},
      );
      expect(r.status, 200);
      expect(
        ((await srv.snapshot())['update'] as Map)['phone'],
        containsPair('state', 'installing'),
      );
      await call(
        'POST',
        '$base/api/v1/app/status',
        token: token,
        json: {'state': 'installed'},
      );
      await call(
        'POST',
        '$base/api/v1/app/status',
        token: token,
        json: {'state': 'installed'},
      );
      expect(notes.where((n) => n.contains('updated')), hasLength(1));
      r = await call(
        'POST',
        '$base/api/v1/app/status',
        token: token,
        json: {'state': 'pwned'},
      );
      expect(r.status, 400);
      expect(
        (await call(
          'POST',
          '$base/api/v1/app/status',
          json: {'state': 'failed'},
        )).status,
        401,
      );
    });

    test(
      'the phone coming back on the offered version counts as installed',
      () async {
        await srv.updates.publish(
          writeApk('${tmp.path}/e.apk', code: 9, name: '0.9.0').path,
        );
        final token = await pair();
        Future<void> ping(String v) => call(
          'GET',
          '$base/api/v1/ping',
          token: token,
          headers: {
            'x-device-name': 'Pixel 8',
            'x-app-version': v,
            'x-app-package': 'lab.maian.test',
          },
        );
        await ping('0.8.0+8');
        expect(srv.updates.phoneStatus, isNull);
        await ping('0.9.0+9');
        expect(srv.updates.phoneStatus?['state'], 'installed');
        final c = ((await srv.snapshot())['clients'] as List).first as Map;
        expect(c['app_version'], '0.9.0+9');
      },
    );

    test('nothing offered: 404, and withdraw takes it back', () async {
      final token = await pair();
      expect(
        (await call('GET', '$base/api/v1/app/apk', token: token)).status,
        404,
      );
      await srv.updates.publish(writeApk('${tmp.path}/f.apk').path);
      srv.updates.withdraw();
      expect(
        (await call('GET', '$base/api/v1/app/apk', token: token)).status,
        404,
      );
      final r = await call('GET', '$base/api/v1/ping', token: token);
      expect(j(r.bytes).containsKey('app_update'), isFalse);
    });

    test('the offer survives a restart', () async {
      await srv.updates.publish(writeApk('${tmp.path}/g.apk', code: 7).path);
      await ui.stop();
      await srv.stop();
      final home = MaiAnHome('${tmp.path}/home');
      srv = CompanionServer(
        home: home,
        settings: Settings.load(home),
        port: 0,
        beaconUdpPort: 0,
        beaconTargets: const [],
      );
      srv.notifier = (_, _) {};
      await srv.start();
      ui = UiServer(srv, port: 0);
      await ui.start();
      expect(srv.updates.offer?['version_code'], 7);
    });
  });

  group('control page', () {
    const h = {'x-maian': '1'};
    test('a dropped APK is offered; a non-APK is refused', () async {
      final bytes = writeApk('${tmp.path}/h.apk', code: 21).readAsBytesSync();
      var r = await call(
        'POST',
        '$uiBase/api/update/upload?name=app.apk',
        headers: h,
        body: bytes,
      );
      expect(r.status, 200);
      expect((j(r.bytes)['update'] as Map)['version_code'], 21);
      r = await call(
        'POST',
        '$uiBase/api/update/upload?name=x.apk',
        headers: h,
        body: utf8.encode('nope'),
      );
      expect(r.status, 400);
      expect(srv.updates.offer?['version_code'], 21);
    });

    test('maian publish: a path on this Mac', () async {
      final f = writeApk('${tmp.path}/i.apk', code: 30);
      final r = await call(
        'POST',
        '$uiBase/api/update/publish',
        headers: h,
        json: {'path': f.path},
      );
      expect(r.status, 200);
      expect(srv.updates.offer?['version_code'], 30);
      final bad = await call(
        'POST',
        '$uiBase/api/update/publish',
        headers: h,
        json: {'path': 'relative.apk'},
      );
      expect(bad.status, 400);
    });

    test('state carries the update and the activity log', () async {
      await srv.updates.publish(writeApk('${tmp.path}/j.apk').path);
      final s = await srv.snapshot();
      expect((s['update'] as Map)['current'], isNotNull);
      expect(
        (s['log'] as List).map((l) => (l as Map)['text']),
        contains(contains('Offering app')),
      );
    });
  });

  group('build folder watcher', () {
    test(
      'offers a new build once it has settled, and never an older one',
      () async {
        final dir = '${tmp.path}/build';
        srv.updates.stop();
        srv.updates.setWatchDir(dir);
        writeApk('$dir/app-release.apk', code: 4);
        await srv.updates.poll(); // first sight: may still be writing
        expect(srv.updates.current, isNull);
        await srv.updates.poll();
        expect(srv.updates.offer?['version_code'], 4);

        writeApk('$dir/app-release.apk', code: 6, filler: 'newer');
        await srv.updates.poll();
        await srv.updates.poll();
        expect(srv.updates.offer?['version_code'], 6);

        writeApk('$dir/app-release.apk', code: 3, filler: 'older');
        await srv.updates.poll();
        await srv.updates.poll();
        expect(srv.updates.offer?['version_code'], 6);
      },
    );

    test(
      'auto off: the folder is left alone; "" survives the default',
      () async {
        final dir = '${tmp.path}/build2';
        srv.updates.stop();
        srv.updates.setWatchDir(dir);
        srv.updates.setAuto(false);
        writeApk('$dir/app-release.apk');
        await srv.updates.poll();
        await srv.updates.poll();
        expect(srv.updates.current, isNull);
        srv.updates.setWatchDir(null);
        expect(srv.settings.get('apk_watch_dir'), '');
        expect(srv.updates.watchDir, isNull);
      },
    );
  });

  test('pubspec and appVersion agree', () {
    final v = RegExp(
      r'^version: *(\S+)',
      multiLine: true,
    ).firstMatch(File('pubspec.yaml').readAsStringSync())!.group(1)!;
    expect(v.split('+').first, appVersion);
  });
}
