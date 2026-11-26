import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/sync/app_update.dart';
import 'package:mai_an_lab/sync/companion_client.dart';

/// The phone's half of app updates: judging the Mac's offer, the download
/// and its checksum, the install hand-off and what is reported back.

class FakePlatform implements AppUpdatePlatform {
  FakePlatform(this.dir, this.mine);
  final Directory dir;
  AppBuild mine;
  bool allowed = true;
  final installs = <String>[];
  final _results = StreamController<(String, String?)>.broadcast();

  void emit(String status, [String? msg]) => _results.add((status, msg));

  @override
  Stream<(String, String?)> get results => _results.stream;
  @override
  Future<AppBuild?> info() async => mine;
  @override
  Future<String> sha256Of(String path) async =>
      '${sha256.convert(File(path).readAsBytesSync())}';
  @override
  Future<bool> canInstall() async => allowed;
  @override
  Future<void> openInstallSettings() async {}
  @override
  Future<void> install(String path) async => installs.add(path);
  @override
  Future<Directory> downloadDir() async =>
      Directory('${dir.path}/dl')..createSync(recursive: true);
}

late Directory tmp;
late HttpServer mac;
late List<int> served;
final statuses = <String>[];
final seenHeaders = <String, String>{};

Map<String, Object?> offerFor(
  List<int> apk, {
  int code = 5,
  String name = '0.4.0',
  String package = 'lab.maian.port',
}) => {
  'package': package,
  'version_code': code,
  'version_name': name,
  'sha256': '${sha256.convert(apk)}',
  'size': apk.length,
  'debuggable': false,
  'path': '/api/v1/app/apk',
};

void main() {
  late FakePlatform platform;
  late AppUpdater u;
  late CompanionClient client;
  late Map<String, Object?> prefs;
  late List<int> installed;
  var prompts = 0;

  setUp(() async {
    tmp = Directory.systemTemp.createTempSync('app_update_');
    installed = utf8.encode('installed apk bytes' * 100);
    final mineFile = File('${tmp.path}/base.apk')..writeAsBytesSync(installed);
    platform = FakePlatform(
      tmp,
      AppBuild(
        package: 'lab.maian.port',
        versionCode: 4,
        versionName: '0.3.0',
        apkPath: mineFile.path,
      ),
    );
    prefs = {};
    prompts = 0;
    statuses.clear();
    seenHeaders.clear();
    served = utf8.encode('new apk bytes' * 1000);
    mac = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    mac.listen((req) async {
      seenHeaders['x-app-version'] = req.headers.value('x-app-version') ?? '';
      if (req.uri.path == '/api/v1/app/apk') {
        req.response
          ..statusCode = 200
          ..contentLength = served.length
          ..add(served);
      } else if (req.uri.path == '/api/v1/app/status') {
        final b = jsonDecode(await utf8.decodeStream(req)) as Map;
        statuses.add('${b['state']}');
        req.response.write('{"status":"ok"}');
      } else {
        req.response.statusCode = 404;
      }
      await req.response.close();
    });
    u = AppUpdater(
      pref: (k) => prefs[k],
      savePref: (k, v) async => prefs[k] = v,
      platform: platform,
    )..onAvailable = (_) => prompts++;
    await u.init();
    client = CompanionClient('127.0.0.1', port: mac.port, authToken: 't');
  });

  tearDown(() async {
    CompanionClient.appHeaders = const {};
    await mac.close(force: true);
    tmp.deleteSync(recursive: true);
  });

  test('sends its version to the Mac with every request', () async {
    await client.postAppStatus('downloaded');
    expect(seenHeaders['x-app-version'], '0.3.0+4');
  });

  test('a newer build: prompt once, download, verify, install', () async {
    final offer = offerFor(served);
    await u.handleOffer(client, offer);
    await u.handleOffer(client, offer);
    expect(u.state, AppUpdateState.available);
    expect(prompts, 1);
    expect(u.offerLabel, '0.4.0 (5)');

    await u.update();
    expect(u.state, AppUpdateState.installing);
    expect(platform.installs, hasLength(1));
    expect(File(platform.installs.single).readAsBytesSync(), served);
    await pumpEventQueue();
    expect(statuses, ['downloaded', 'installing']);

    platform.emit('declined');
    await pumpEventQueue();
    expect(u.state, AppUpdateState.available);
    expect(statuses.last, 'declined');
  });

  test('the build we run: up to date, reported as installed once', () async {
    final offer = offerFor(installed, code: 4, name: '0.3.0');
    await u.handleOffer(client, offer);
    await u.handleOffer(client, offer);
    await pumpEventQueue();
    expect(u.state, AppUpdateState.upToDate);
    expect(statuses, ['installed']);
    expect(prompts, 0);
  });

  test('same version, different bytes: a rebuild is offered', () async {
    await u.handleOffer(client, offerFor(served, code: 4, name: '0.3.0'));
    expect(u.state, AppUpdateState.available);
    expect(u.isRebuild, isTrue);
  });

  test('older builds and other apps are ignored', () async {
    await u.handleOffer(client, offerFor(served, code: 3));
    expect(u.state, AppUpdateState.none);
    await u.handleOffer(client, offerFor(served, package: 'com.other'));
    expect(u.state, AppUpdateState.none);
    await u.handleOffer(client, null);
    expect(u.state, AppUpdateState.none);
    expect(prompts, 0);
  });

  test('a damaged download is refused and never installed', () async {
    final offer = offerFor(served);
    served = utf8.encode('tampered' * 1000);
    await u.handleOffer(client, offer);
    await u.update();
    expect(u.state, AppUpdateState.failed);
    expect(u.error, contains('checksum'));
    expect(platform.installs, isEmpty);
    await pumpEventQueue();
    expect(statuses, contains('failed'));
  });

  test('installs not allowed yet: ask, then carry on when back', () async {
    platform.allowed = false;
    await u.handleOffer(client, offerFor(served));
    await u.update();
    expect(u.state, AppUpdateState.needsPermission);
    expect(platform.installs, isEmpty);
    platform.allowed = true;
    await u.resumed();
    expect(u.state, AppUpdateState.installing);
    expect(platform.installs, hasLength(1));
  });

  test('"Not now" stops the prompt for that build only', () async {
    await u.handleOffer(client, offerFor(served));
    expect(prompts, 1);
    await u.decline();
    u = AppUpdater(
      pref: (k) => prefs[k],
      savePref: (k, v) async => prefs[k] = v,
      platform: platform,
    )..onAvailable = (_) => prompts++;
    await u.init();
    await u.handleOffer(client, offerFor(served));
    expect(u.state, AppUpdateState.available);
    expect(prompts, 1);
    served = utf8.encode('newer still' * 1000);
    await u.handleOffer(client, offerFor(served, code: 6));
    expect(prompts, 2);
  });

  test('an install failure is shown and reported', () async {
    await u.handleOffer(client, offerFor(served));
    await u.update();
    platform.emit('failed', 'it is signed with a different key');
    await pumpEventQueue();
    expect(u.state, AppUpdateState.failed);
    expect(u.error, contains('different key'));
    expect(statuses.last, 'failed');
  });
}
