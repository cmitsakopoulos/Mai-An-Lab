import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:test/test.dart';

import '../src/home.dart';
import '../src/reports.dart';
import '../src/server.dart';
import '../src/ui_server.dart';
import 'updates_test.dart' show writeApk;

/// Building the phone app from the page (with a fake `flutter`), and the
/// background services the page switches on and off.

late Directory tmp;
late CompanionServer srv;
late UiServer ui;
late String project;
late String fakeBin;

String get uiBase => 'http://127.0.0.1:${ui.port}';

const h = {'x-maian': '1', 'content-type': 'application/json'};

Future<({int status, Map<String, Object?> json})> post(
  String url, [
  Object? body,
  Map<String, String> headers = h,
]) async {
  final c = HttpClient();
  try {
    final r = await c.postUrl(Uri.parse(url));
    headers.forEach(r.headers.set);
    r.write(jsonEncode(body ?? {}));
    final resp = await r.close();
    final text = await resp.transform(utf8.decoder).join();
    return (
      status: resp.statusCode,
      json: (text.isEmpty ? {} : jsonDecode(text)) as Map<String, Object?>,
    );
  } finally {
    c.close(force: true);
  }
}

/// A `flutter` that runs [script] (bash) in the project folder.
void fakeFlutter(String script) {
  final f = File(
    '$fakeBin/flutter',
  )..writeAsStringSync('#!/bin/bash\necho "\$@" > "\$PWD/args.txt"\n$script\n');
  Process.runSync('chmod', ['+x', f.path]);
}

Future<void> settle() async {
  for (var i = 0; i < 200 && srv.builder.running; i++) {
    await Future<void>.delayed(const Duration(milliseconds: 20));
  }
}

void main() {
  setUp(() async {
    tmp = Directory.systemTemp.createTempSync('maian_build_');
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
    srv.notifier = (_, _) {};
    await srv.start();
    ui = UiServer(srv, port: 0);
    await ui.start();
    project = '${tmp.path}/app';
    fakeBin = '${tmp.path}/bin';
    Directory(fakeBin).createSync(recursive: true);
    File('$project/pubspec.yaml')
      ..createSync(recursive: true)
      ..writeAsStringSync('name: app\nversion: 0.4.0+7\n');
    writeApk('${tmp.path}/built.apk', code: 8, name: '0.4.0');
    srv.builder.setProject(project);
    srv.builder.shellEnv = () async => {
      'PATH': '$fakeBin:/usr/bin:/bin',
      'BUILT': '${tmp.path}/built.apk',
    };
  });

  tearDown(() async {
    await ui.stop();
    await srv.stop();
    tmp.deleteSync(recursive: true);
  });

  group('build from the page', () {
    test('builds, raises the build number, and offers the APK', () async {
      fakeFlutter(r'''
echo "Running Gradle task 'assembleRelease'..."
mkdir -p build/app/outputs/flutter-apk
cp "$BUILT" build/app/outputs/flutter-apk/app-release.apk
echo "✓ Built build/app/outputs/flutter-apk/app-release.apk (9.1MB)"
''');
      await post('$uiBase/api/build/option', {'name': 'bump', 'on': true});
      final r = await post('$uiBase/api/build/start');
      expect(r.json['started'], true);
      await settle();
      final b = srv.builder;
      expect(b.phase, 'done', reason: b.error);
      expect(b.built, '0.4.0 (8)');
      expect(srv.updates.offer?['version_code'], 8);
      expect(srv.updates.current?['source'], 'built here');
      expect(
        File('$project/pubspec.yaml').readAsStringSync(),
        contains('version: 0.4.0+8'),
      );
      expect(
        File('$project/args.txt').readAsStringSync().trim(),
        'build apk --release --target-platform android-arm64',
      );
      expect(b.lines, contains(startsWith('✓ Built')));
      expect(srv.settings.get('build_last_ms'), isA<int>());
      final s = await srv.snapshot();
      expect((s['build'] as Map)['phase'], 'done');
      expect(srv.updates.hold, isFalse);
    });

    test('all architectures when 64-bit ARM only is off', () async {
      fakeFlutter(r'''
mkdir -p build/app/outputs/flutter-apk
cp "$BUILT" build/app/outputs/flutter-apk/app-release.apk
''');
      srv.builder.setOption('arm64', false);
      await srv.builder.build();
      expect(
        File('$project/args.txt').readAsStringSync().trim(),
        'build apk --release',
      );
      expect(
        File('$project/pubspec.yaml').readAsStringSync(),
        contains('0.4.0+7'),
      );
    });

    test('a failed build says what went wrong and offers nothing', () async {
      fakeFlutter(r'''
echo "FAILURE: Build failed with an exception." >&2
echo "* What went wrong:" >&2
echo "Execution failed for task ':app:minifyReleaseWithR8'." >&2
exit 1
''');
      await srv.builder.build();
      expect(srv.builder.phase, 'failed');
      expect(srv.builder.error, contains('minifyReleaseWithR8'));
      expect(srv.updates.current, isNull);
    });

    test('no new APK is a failure, not a stale offer', () async {
      fakeFlutter('echo ok');
      await srv.builder.build();
      expect(srv.builder.phase, 'failed');
      expect(srv.builder.error, contains('no new APK'));
    });

    test('no flutter on PATH', () async {
      srv.builder.shellEnv = () async => {'PATH': '/usr/bin:/bin'};
      await srv.builder.build();
      expect(srv.builder.phase, 'failed');
      expect(srv.builder.error, contains('PATH'));
    });

    test('cancel stops the build', () async {
      fakeFlutter('sleep 20');
      final done = srv.builder.build();
      for (var i = 0; i < 100 && !File('$project/args.txt').existsSync(); i++) {
        await Future<void>.delayed(const Duration(milliseconds: 20));
      }
      await post('$uiBase/api/build/cancel');
      await done.timeout(const Duration(seconds: 5));
      expect(srv.builder.phase, 'cancelled');
    });
  });

  group('background services', () {
    Future<int> ping() async {
      final c = HttpClient()..connectionTimeout = const Duration(seconds: 2);
      try {
        final r = await (await c.getUrl(
          Uri.parse('http://127.0.0.1:${srv.port}/api/v1/ping'),
        )).close();
        await r.drain<void>();
        return r.statusCode;
      } on SocketException {
        return -1;
      } finally {
        c.close(force: true);
      }
    }

    test('phone connection off closes the port; on reopens it', () async {
      expect(await ping(), 200);
      await post('$uiBase/api/service', {'name': 'phone', 'on': false});
      expect(await ping(), -1);
      final s = await srv.snapshot();
      expect((s['services'] as Map)['listening'], false);
      await post('$uiBase/api/service', {'name': 'phone', 'on': true});
      expect(await ping(), 200);
      expect(srv.settings.get('svc_phone'), true);
    });

    test('analysis off: the phone gets 503 with a reason', () async {
      final pair = await post(
        'http://127.0.0.1:${srv.port}/api/v1/pair',
        {'pin': '1234', 'device_name': 'Pixel'},
        const {'content-type': 'application/json'},
      );
      final token = pair.json['token'];
      await srv.setService('offload', false);
      final r = await post(
        'http://127.0.0.1:${srv.port}/api/v1/dsp/match',
        {
          'tracks': [
            {'path': '/a.flac'},
          ],
        },
        {'content-type': 'application/json', 'authorization': 'Bearer $token'},
      );
      expect(r.status, 503);
      expect(r.json['reason'], 'offload_off');
    });

    test('low-power analysis runs one job at a time', () async {
      await srv.setService('gentle', true);
      expect(srv.pool.size, 1);
      await srv.setService('gentle', false);
      expect(srv.pool.size, greaterThanOrEqualTo(1));
      expect(srv.settings.get('dsp_gentle'), false);
    });

    test('switches are remembered across restarts', () async {
      await srv.setService('awake', false);
      await srv.setService('beacon', false);
      final again = Settings.load(srv.home);
      expect(again.get('svc_awake'), false);
      expect(again.get('svc_beacon'), false);
      expect((await srv.snapshot())['services'], containsPair('awake', false));
    });

    test('an unknown service is refused', () async {
      final r = await post('$uiBase/api/service', {'name': 'x', 'on': true});
      expect(r.status, 400);
    });
  });

  group('release notes', () {
    void buildsOk() => fakeFlutter(r"""
mkdir -p build/app/outputs/flutter-apk
cp "$BUILT" build/app/outputs/flutter-apk/app-release.apk
""");

    void git(List<String> args) {
      final r = Process.runSync('git', [
        '-C',
        project,
        '-c',
        'user.name=t',
        '-c',
        'user.email=t@t',
        ...args,
      ]);
      expect(r.exitCode, 0, reason: '${r.stderr}');
    }

    test('the draft goes with the next build and is cleared', () async {
      buildsOk();
      await post('$uiBase/api/update/draft-notes', {
        'text': '- Fixed the queue\n',
      });
      expect(
        (await srv.snapshot())['update'],
        containsPair('draft_notes', '- Fixed the queue\n'),
      );
      await post('$uiBase/api/build/start');
      await settle();
      expect(srv.builder.phase, 'done', reason: srv.builder.error);
      expect(srv.updates.offer?['notes'], '- Fixed the queue');
      expect(srv.updates.draftNotes, '');
    });

    test('the offered build\'s notes can be edited', () async {
      final r0 = await post('$uiBase/api/update/notes', {'text': 'x'});
      expect(r0.status, 400); // nothing on offer
      await srv.updates.publish('${tmp.path}/built.apk');
      expect(srv.updates.offer?['notes'], '');
      await post('$uiBase/api/update/notes', {'text': 'Faster scans '});
      expect(srv.updates.offer?['notes'], 'Faster scans');
      expect(
        jsonDecode(
          File('${srv.updates.dir.path}/current.json').readAsStringSync(),
        )['notes'],
        'Faster scans',
      );
    });

    test('from git: commits since the last build made here', () async {
      git(['init', '-q']);
      git(['add', '.']);
      git(['commit', '-qm', 'First thing']);
      File('$project/second.txt').writeAsStringSync('x');
      git(['add', '.']);
      git(['commit', '-qm', 'Second thing']);
      var r = await post('$uiBase/api/update/git-notes');
      expect(r.json['text'], '- Second thing\n- First thing');
      buildsOk();
      await post('$uiBase/api/build/start');
      await settle();
      expect(srv.builder.phase, 'done', reason: srv.builder.error);
      r = await post('$uiBase/api/update/git-notes');
      expect(r.json['text'], '');
      File('$project/third.txt').writeAsStringSync('x');
      git(['add', '.']);
      git(['commit', '-qm', 'Third thing']);
      r = await post('$uiBase/api/update/git-notes');
      expect(r.json['text'], '- Third thing');
    });

    test('from git outside a repository says so', () async {
      final r = await post('$uiBase/api/update/git-notes');
      expect(r.status, 400);
      expect('${r.json['error']}', contains('git'));
    });
  });

  group('problem reports', () {
    String phone(String p) => 'http://127.0.0.1:${srv.port}$p';
    Future<String> pair() async =>
        (await post(
              phone('/api/v1/pair'),
              {'pin': '1234', 'device_name': 'Pixel'},
              const {'content-type': 'application/json'},
            )).json['token']
            as String;

    test('needs the pairing token', () async {
      final r = await post(
        phone('/api/v1/report'),
        {'kind': 'bug', 'text': 'x'},
        const {'content-type': 'application/json'},
      );
      expect(r.status, 401);
      expect(srv.reports.items, isEmpty);
    });

    test('kept with its log, listed, notified, copied, deleted', () async {
      final auth = {
        'content-type': 'application/json',
        'authorization': 'Bearer ${await pair()}',
        'x-device-name': 'Pixel',
      };
      final notes = <String>[];
      srv.notifier = (t, b) => notes.add('$t: $b');
      final r = await post(phone('/api/v1/report'), {
        'kind': 'bug',
        'text': 'Queue jumped to the start\nafter skipping',
        'app_version': '0.3.0+5',
        'logs': ['12:00:01 player: skip', '12:00:02 queue: reset'],
      }, auth);
      expect(r.status, 200);
      final id = r.json['id'] as String;
      final e = await post(phone('/api/v1/report'), {
        'kind': 'error',
        'error': 'Null check operator used on a null value',
        'stack': '#0 main (file.dart:1)',
        'count': 3,
      }, auth);
      expect(e.status, 200);
      expect(notes, hasLength(2));
      expect(notes.first, contains('Queue jumped to the start'));

      var s = (await srv.snapshot())['reports'] as Map;
      expect(s['unseen'], 2);
      final items = s['items'] as List;
      expect(items.first, containsPair('kind', 'error'));
      expect(items.last, containsPair('title', 'Queue jumped to the start'));
      expect(items.last, containsPair('log_lines', 2));

      final t = await post('$uiBase/api/report/text', {'id': id});
      expect(
        t.json['text'],
        allOf(contains('Pixel'), contains('0.3.0+5'), contains('queue: reset')),
      );
      expect(File('${srv.reports.dir.path}/$id.txt').existsSync(), isTrue);

      await post('$uiBase/api/report/seen');
      s = (await srv.snapshot())['reports'] as Map;
      expect(s['unseen'], 0);

      await post('$uiBase/api/report/delete', {'id': id});
      expect(srv.reports.items, hasLength(1));
      expect(File('${srv.reports.dir.path}/$id.txt').existsSync(), isFalse);
      // Survives a restart.
      expect(ProblemReports(srv.home, srv.settings).items, hasLength(1));
    });

    test('an empty report or a made-up id is refused', () async {
      final auth = {
        'content-type': 'application/json',
        'authorization': 'Bearer ${await pair()}',
      };
      final r = await post(phone('/api/v1/report'), {
        'kind': 'bug',
        'text': ' ',
      }, auth);
      expect(r.status, 400);
      final t = await post('$uiBase/api/report/text', {'id': '../../settings'});
      expect(t.status, 404);
    });
  });
}
