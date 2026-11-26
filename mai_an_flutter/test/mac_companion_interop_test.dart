// The phone's own Companion code (CompanionClient, the download pipeline,
// BeaconListener) against the Mac program in mai_an_mac, running as the real
// `maian serve` process: the contract between the two halves.
//
// Needs `dart` (always there for flutter test) and ffmpeg; skipped without
// ffmpeg or the mai_an_mac folder.
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:crypto/crypto.dart';
import 'package:mai_an_lab/downloads/companion_download.dart';
import 'package:mai_an_lab/sync/app_update.dart';
import 'package:mai_an_lab/sync/companion_client.dart';
import 'package:mai_an_lab/sync/discovery.dart';
import 'package:mai_an_lab/sync/problem_reports.dart';

bool get _canRun =>
    Directory('../mai_an_mac').existsSync() &&
    [
      '/opt/homebrew/bin/ffmpeg',
      '/usr/local/bin/ffmpeg',
      '/usr/bin/ffmpeg',
    ].any((p) => File(p).existsSync());

/// A mono 22.05 kHz WAV: a tone with a click every half second.
Uint8List _wav(int secs, {double hz = 220}) {
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
    var v = 0.3 * math.sin(2 * math.pi * hz * i / sr);
    if (i % (sr ~/ 2) < 200) v += 0.6 * (1 - (i % (sr ~/ 2)) / 200);
    b.setInt16(44 + i * 2, (v.clamp(-1.0, 1.0) * 32000).round(), Endian.little);
  }
  return b.buffer.asUint8List();
}

class _Mac {
  _Mac(
    this.proc,
    this.home,
    this.recv,
    this.port,
    this.uiPort,
    this.udp,
    this.out,
  );
  final Process proc;
  final StringBuffer out;
  final String home, recv;
  final int port, uiPort, udp;

  static Future<_Mac> start(String root) async {
    final home = '$root/home', recv = '$root/recv';
    final proc = await Process.start('dart', [
      'run',
      'cli/maian.dart',
      'serve',
      '--no-open',
      '--no-notify',
      '--no-announce',
      '--no-trust-loopback',
      '--port',
      '0',
      '--ui-port',
      '0',
      '--udp-port',
      '0',
      '--pin',
      '4821',
      '--home',
      home,
      '--receive-dir',
      recv,
    ], workingDirectory: '../mai_an_mac');
    final out = StringBuffer();
    proc.stdout.transform(utf8.decoder).listen(out.write);
    proc.stderr.transform(utf8.decoder).listen(out.write);
    final started = Completer<void>();
    final sw = Stopwatch()..start();
    unawaited(() async {
      while (sw.elapsed < const Duration(seconds: 60)) {
        if (out.toString().contains('Waiting for the phone')) {
          started.complete();
          return;
        }
        await Future<void>.delayed(const Duration(milliseconds: 200));
      }
      started.completeError(StateError('maian did not start:\n$out'));
    }());
    await started.future;
    final uiPort = int.parse(File('$home/ui.port').readAsStringSync());
    final http = HttpClient();
    final req = await http.getUrl(
      Uri.parse('http://127.0.0.1:$uiPort/api/state'),
    );
    final state =
        jsonDecode(await utf8.decodeStream(await req.close()))
            as Map<String, Object?>;
    http.close();
    final udp = RegExp(r'UDP (\d+)').firstMatch(out.toString());
    return _Mac(
      proc,
      home,
      recv,
      state['port']! as int,
      uiPort,
      int.parse(udp?.group(1) ?? '0'),
      out,
    );
  }

  /// `maian send ...` as a user would type it.
  Future<ProcessResult> send(List<String> files) => Process.run('dart', [
    'run',
    'cli/maian.dart',
    'send',
    '--home',
    home,
    '--no-dsp',
    '--quality',
    'original',
    ...files,
  ], workingDirectory: '../mai_an_mac');

  Future<void> stop() async {
    proc.kill(ProcessSignal.sigint);
    await proc.exitCode.timeout(
      const Duration(seconds: 10),
      onTimeout: () {
        proc.kill(ProcessSignal.sigkill);
        return -1;
      },
    );
  }
}

/// Android's half of an app update, minus Android: installs are recorded.
class _Platform implements AppUpdatePlatform {
  _Platform(this.dir, this.mine);
  final Directory dir;
  final AppBuild mine;
  final installs = <String>[];
  @override
  Stream<(String, String?)> get results => const Stream.empty();
  @override
  Future<AppBuild?> info() async => mine;
  @override
  Future<String> sha256Of(String path) async =>
      '${await sha256.bind(File(path).openRead()).first}';
  @override
  Future<bool> canInstall() async => true;
  @override
  Future<void> openInstallSettings() async {}
  @override
  Future<void> install(String path) async => installs.add(path);
  @override
  Future<Directory> downloadDir() async =>
      Directory('${dir.path}/apk_dl')..createSync(recursive: true);
}

const _releaseApk = 'build/app/outputs/flutter-apk/app-release.apk';

void main() {
  late Directory tmp;
  late _Mac mac;

  setUpAll(() async {
    if (!_canRun) return;
    tmp = Directory.systemTemp.createTempSync('maian_interop_');
    mac = await _Mac.start(tmp.path);
  });

  tearDownAll(() async {
    if (!_canRun) return;
    await mac.stop();
    if (Platform.environment['MAIAN_DEBUG'] != null) {
      // ignore: avoid_print
      print(mac.out);
    }
    tmp.deleteSync(recursive: true);
  });

  final skip = _canRun ? null : 'needs ../mai_an_mac and ffmpeg';

  test(
    'pairing: wrong PIN, right PIN, then the token works',
    skip: skip,
    () async {
      final c = CompanionClient(
        '127.0.0.1',
        port: mac.port,
        deviceName: 'Pixel 8',
      );
      final info = await c.ping();
      expect(info['status'], 'ok');
      expect(info['device'], 'macOS');
      expect(info['authorized'], false);
      await expectLater(c.pair('0000'), throwsA(isA<CompanionAuthError>()));
      final token = await c.pair('4821');
      expect(token, isNotEmpty);
      expect((await c.ping())['authorized'], true);
      // A fresh client with only the saved token is accepted too.
      final c2 = CompanionClient('127.0.0.1', port: mac.port, authToken: token);
      expect((await c2.ping())['authorized'], true);
    },
  );

  test(
    'discovery: a phone query is answered with the Mac\'s address',
    skip: skip,
    () async {
      final l = BeaconListener(
        port: 0,
        queryPort: mac.udp,
        targets: ['127.0.0.1'],
        localIp: () async => '127.0.0.1',
        holdMulticastLock: false,
      );
      await l.start();
      final sw = Stopwatch()..start();
      while (l.discovered.isEmpty && sw.elapsed < const Duration(seconds: 6)) {
        await Future<void>.delayed(const Duration(milliseconds: 100));
      }
      l.stop();
      expect(l.discovered, isNotEmpty);
      final s = l.discovered.values.single;
      expect(s['port'], mac.port);
      expect(s['ip'], '127.0.0.1');
    },
  );

  test(
    'offload switched off on the Mac: the run stops with its message',
    skip: skip,
    () async {
      Future<void> setOffload(bool on) async {
        final h = HttpClient();
        try {
          final r = await h.postUrl(
            Uri.parse('http://127.0.0.1:${mac.uiPort}/api/service'),
          );
          r.headers.set('x-maian', '1');
          r.headers.contentType = ContentType.json;
          r.write(jsonEncode({'name': 'offload', 'on': on}));
          expect((await r.close()).statusCode, 200);
        } finally {
          h.close(force: true);
        }
      }

      final c = CompanionClient('127.0.0.1', port: mac.port);
      await c.pair('4821');
      final f = File('${tmp.path}/off.wav')..writeAsBytesSync(_wav(5));
      await setOffload(false);
      try {
        await expectLater(
          c.offloadDsp([f.path], persist: (_) async {}),
          throwsA(
            isA<CompanionHttpError>()
                .having((e) => e.status, 'status', 503)
                .having((e) => e.message, 'message', contains('turned off')),
          ),
        );
      } finally {
        await setOffload(true);
      }
    },
  );

  Future<Map<String, Object?>> uiPost(String path, [Object? body]) async {
    final h = HttpClient();
    try {
      final r = await h.postUrl(
        Uri.parse('http://127.0.0.1:${mac.uiPort}$path'),
      );
      r.headers.set('x-maian', '1');
      r.headers.contentType = ContentType.json;
      r.write(jsonEncode(body ?? {}));
      final resp = await r.close();
      return jsonDecode(await utf8.decodeStream(resp)) as Map<String, Object?>;
    } finally {
      h.close(force: true);
    }
  }

  test(
    'problem reports: queued while away, sent with the log on connecting',
    skip: skip,
    () async {
      final reporter = ProblemReporter(maxLog: 5);
      reporter.log('player: skip');
      reporter.log('queue: reset to 0');
      reporter.recordError(
        StateError('queue index out of range'),
        StackTrace.current,
      );
      reporter.recordError(
        StateError('queue index out of range'),
        StackTrace.current,
      );
      expect(reporter.pending, hasLength(2), reason: 'two frames differ');
      expect(await reporter.report('Queue jumped to the start'), isFalse);
      expect(reporter.pending, hasLength(3));

      final c = CompanionClient(
        '127.0.0.1',
        port: mac.port,
        deviceName: 'Pixel 8',
      );
      await c.pair('4821');
      reporter.connected = () => c;
      await reporter.flush(c);
      expect(reporter.pending, isEmpty);
      expect(await reporter.report('Art missing on the lock screen'), isTrue);

      final h = HttpClient();
      final req = await h.getUrl(
        Uri.parse('http://127.0.0.1:${mac.uiPort}/api/state'),
      );
      final state =
          jsonDecode(await utf8.decodeStream(await req.close()))
              as Map<String, Object?>;
      h.close();
      final reports = state['reports'] as Map;
      expect(reports['total'], 4);
      final items = (reports['items'] as List).cast<Map<String, Object?>>();
      expect(items.first['title'], 'Art missing on the lock screen');
      expect(items.first['device'], 'Pixel 8');
      final bug = items.firstWhere(
        (r) => r['title'] == 'Queue jumped to the start',
      );
      final text = await uiPost('/api/report/text', {'id': bug['id']});
      expect(
        text['text'],
        allOf(contains('queue: reset to 0'), contains('player: skip')),
      );
      await uiPost('/api/report/clear');
    },
  );

  test(
    'release notes: in the offer, and edits reach the phone',
    skip: skip ?? (File(_releaseApk).existsSync() ? null : 'no release APK'),
    () async {
      final c = CompanionClient('127.0.0.1', port: mac.port);
      await c.pair('4821');
      await uiPost('/api/update/draft-notes', {'text': '- Faster scans'});
      final apk = File(_releaseApk);
      await uiPost('/api/update/notes', {
        'text': 'x',
      }); // nothing offered yet: refused
      final h = HttpClient();
      final r = await h.postUrl(
        Uri.parse(
          'http://127.0.0.1:${mac.uiPort}/api/update/upload?name=notes.apk',
        ),
      );
      r.headers.set('x-maian', '1');
      r.add(apk.readAsBytesSync());
      expect((await r.close()).statusCode, 200);
      h.close(force: true);

      var offer = (await c.ping())['app_update'] as Map;
      expect(offer['notes'], '- Faster scans');
      await uiPost('/api/update/notes', {
        'text': '- Faster scans\n- Fixed the queue',
      });
      offer = (await c.ping())['app_update'] as Map;
      expect(offer['notes'], '- Faster scans\n- Fixed the queue');
      await uiPost('/api/update/withdraw');
    },
  );

  test(
    'offload: analyse on the Mac, then answered from its cache',
    skip: skip,
    () async {
      final c = CompanionClient('127.0.0.1', port: mac.port);
      await c.pair('4821');
      final a = File('${tmp.path}/Ωmega - one.wav')..writeAsBytesSync(_wav(25));
      final b = File('${tmp.path}/two.wav')
        ..writeAsBytesSync(_wav(25, hz: 330));
      final rows = <FeatureRow>[];
      final progress = <String>[];
      final r = await c.offloadDsp(
        [a.path, b.path],
        persist: (batch) async => rows.addAll(batch),
        progress: (d, t, cur, rate) => progress.add('$d/$t $cur'),
      );
      expect(
        (analysed: r.analysed, failed: r.failed, total: r.total),
        (analysed: 2, failed: 0, total: 2),
      );
      expect(rows.map((x) => x.path), [a.path, b.path]);
      for (final x in rows) {
        expect(x.timbre.length, 272);
        expect(x.featuresVersion, 5);
        expect(x.keyIndex, inInclusiveRange(0, 23));
      }
      expect(
        rows[0].timbre,
        isNot(rows[1].timbre),
        reason: 'different audio, different features',
      );

      // Same tracks again: nothing is uploaded, the Mac cache answers.
      final again = <FeatureRow>[];
      final progress2 = <String>[];
      final r2 = await c.offloadDsp(
        [a.path, b.path],
        persist: (batch) async => again.addAll(batch),
        progress: (d, t, cur, rate) => progress2.add(cur),
      );
      expect(r2.analysed, 2);
      expect(progress2, contains('Loaded from Mac cache'));
      expect(again.map((x) => x.bpm), rows.map((x) => x.bpm));
      expect(again[0].timbre, rows[0].timbre);
    },
  );

  test(
    'send to Mac files the track under Artist/Album and a state bundle is kept',
    skip: skip,
    () async {
      final c = CompanionClient(
        '127.0.0.1',
        port: mac.port,
        deviceName: 'Pixel 8',
      );
      await c.pair('4821');
      final t = File('${tmp.path}/Ο Δρόμος.flac')
        ..writeAsBytesSync(List.generate(4000, (i) => i % 251));
      final res = await c.pushTrack(t.path, {
        'artist': 'Σωκράτης',
        'album': 'Άλμπουμ',
        'remaining': 0,
      });
      expect(res['duplicate'], false);
      final dest = File('${mac.recv}/Σωκράτης/Άλμπουμ/Ο Δρόμος.flac');
      expect(dest.existsSync(), true);
      expect(dest.readAsBytesSync(), t.readAsBytesSync());
      expect(
        (await c.pushTrack(t.path, {
          'artist': 'Σωκράτης',
          'album': 'Άλμπουμ',
        }))['duplicate'],
        true,
      );

      final zip = File('${tmp.path}/mai_an_lab_state_latest.zip')
        ..writeAsBytesSync([80, 75, 3, 4, 1, 2, 3]);
      final up = await c.uploadStateBundle(zip.path);
      expect(
        File(up['saved_path']! as String).readAsBytesSync(),
        zip.readAsBytesSync(),
      );
    },
  );

  test(
    'Mac to phone: `maian send`, pending list, the phone\'s own downloader, ack',
    skip: skip,
    () async {
      final c = CompanionClient('127.0.0.1', port: mac.port);
      await c.pair('4821');
      final src = File('${tmp.path}/Άλφα - Song.flac')
        ..writeAsBytesSync(List.generate(6000, (i) => (i * 7) % 256));
      final r = await mac.send([src.path]);
      expect(r.exitCode, 0, reason: '${r.stdout}${r.stderr}');
      expect('${r.stdout}', contains('Queued 1'));

      final pending = await c.getPendingTransfers();
      expect(pending, hasLength(1));
      final t = pending.single;
      expect(t['title'], 'Άλφα - Song');
      expect(t['extension'], 'flac');

      final dir = Directory('${tmp.path}/phone_downloads')..createSync();
      final saved = await companionDownload(c.streamUrl(t), dir.path);
      expect(saved, hasLength(1));
      expect(File(saved.single).readAsBytesSync(), src.readAsBytesSync());
      expect(saved.single, endsWith('Άλφα - Song.flac'));

      expect(await c.ackTransfers(['${t['id']}']), true);
      expect(await c.getPendingTransfers(), isEmpty);
    },
  );

  test(
    'app update: maian publish, then the phone downloads, verifies, installs',
    skip: skip ?? (File(_releaseApk).existsSync() ? null : 'no release APK'),
    () async {
      final apk = File(_releaseApk).absolute;
      final pub = await Process.run('dart', [
        'run',
        'cli/maian.dart',
        'publish',
        '--home',
        mac.home,
        apk.path,
      ], workingDirectory: '../mai_an_mac');
      expect(pub.exitCode, 0, reason: '${pub.stdout}${pub.stderr}');
      expect('${pub.stdout}', contains('Offering Mai An'));

      final c = CompanionClient(
        '127.0.0.1',
        port: mac.port,
        deviceName: 'Pixel 8',
      );
      await c.pair('4821');
      final offer = (await c.ping())['app_update']! as Map<String, Object?>;
      final code = (offer['version_code']! as num).toInt();

      // A phone one build behind, on the same package.
      final platform = _Platform(
        tmp,
        AppBuild(
          package: '${offer['package']}',
          versionCode: code - 1,
          versionName: 'old',
          apkPath: '/nonexistent',
        ),
      );
      final u = AppUpdater(
        pref: (_) => null,
        savePref: (_, _) async {},
        platform: platform,
      );
      await u.init();
      addTearDown(() => CompanionClient.appHeaders = const {});
      await u.handleOffer(c, offer);
      expect(u.state, AppUpdateState.available);
      await u.update();
      expect(u.state, AppUpdateState.installing);
      expect(
        '${sha256.convert(File(platform.installs.single).readAsBytesSync())}',
        '${sha256.convert(apk.readAsBytesSync())}',
      );

      // The Mac's page saw it all: who, which version, how far.
      await Future<void>.delayed(const Duration(milliseconds: 300));
      final http = HttpClient();
      final req = await http.getUrl(
        Uri.parse('http://127.0.0.1:${mac.uiPort}/api/state'),
      );
      final st =
          jsonDecode(await utf8.decodeStream(await req.close()))
              as Map<String, Object?>;
      http.close();
      final up = st['update']! as Map<String, Object?>;
      expect((up['phone']! as Map)['state'], 'installing');
      expect(up['downloads'], 1);
      final clients = (st['clients']! as List).cast<Map<String, Object?>>();
      expect(clients.map((c) => c['app_version']), contains('old+${code - 1}'));
    },
  );

  test(
    'a phone that was never paired gets 401, not data',
    skip: skip,
    () async {
      final stranger = CompanionClient('127.0.0.1', port: mac.port);
      await expectLater(
        stranger.getPendingTransfers(),
        throwsA(isA<CompanionAuthError>()),
      );
    },
  );
}
