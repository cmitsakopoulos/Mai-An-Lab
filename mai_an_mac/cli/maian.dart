import 'dart:async';
import 'dart:convert';
import 'dart:io';

import '../src/beacon.dart' show udpPort;
import '../src/builder.dart' show guessFlutterProject;
import '../src/ffmpeg.dart';
import '../src/home.dart';
import '../src/net.dart';
import '../src/server.dart';
import '../src/ui_server.dart';

const _usage = '''
maian: Mai An Companion for the Mac

  maian [serve]            Pair the phone, offload analysis, move files.
                           Opens the control page in your browser.
  maian send FILE...       Queue audio files for the phone (while serve runs).
  maian status             What serve is doing right now.
  maian publish APP.apk    Offer this build of the phone app to the phone.
  maian quit               Stop a running serve.

serve options
  --port N          Phone-facing port (default 8945)
  --ui-port N       Control page port (default 8946)
  --pin NNNN        Use this pairing PIN instead of a random one
  --receive-dir D   Where tracks sent from the phone are saved
  --home D          Where maian keeps its files
  --no-open         Don't open the browser
  --no-announce     Don't broadcast to the network (still answers queries)
  --no-notify       No macOS notifications this run
  --udp-port N      Discovery port to listen on (default 54545)
  --watch-apk D     Offer each new D/app-release.apk to the phone
  --default-watch-apk D
                    The same, unless a folder (or none) was chosen before
  --flutter-project D
                    Build the phone app from D (the page's Build button)
  --default-flutter-project D
                    The same, unless a project was chosen before

send options
  --quality Q       original | aac_256k | aac_vbr_max   (default: the page's)
  --no-dsp          Skip analysing the tracks first
''';

Future<void> main(List<String> args) async {
  final cmd = args.isNotEmpty && !args.first.startsWith('-')
      ? args.first
      : 'serve';
  final rest = args.isNotEmpty && !args.first.startsWith('-')
      ? args.sublist(1)
      : args;
  switch (cmd) {
    case 'serve':
      exit(await _serve(_Args(rest)));
    case 'send':
      exit(await _send(_Args(rest)));
    case 'status':
      exit(await _status(_Args(rest)));
    case 'publish':
      exit(await _publish(_Args(rest)));
    case 'quit':
      exit(await _quit(_Args(rest)));
    case 'help' || '--help' || '-h':
      stdout.write(_usage);
    default:
      stderr.write('Unknown command "$cmd".\n$_usage');
      exit(64);
  }
}

class _Args {
  _Args(List<String> raw) {
    for (var i = 0; i < raw.length; i++) {
      final a = raw[i];
      if (a.startsWith('--')) {
        final eq = a.indexOf('=');
        final key = eq < 0 ? a.substring(2) : a.substring(2, eq);
        if (eq >= 0) {
          opts[key] = a.substring(eq + 1);
        } else if (_flags.contains(key)) {
          flags.add(key);
        } else if (i + 1 < raw.length) {
          opts[key] = raw[++i];
        } else {
          opts[key] = '';
        }
      } else {
        positional.add(a);
      }
    }
  }
  static const _flags = {
    'no-open',
    'no-dsp',
    'help',
    'no-announce',
    'no-trust-loopback',
    'no-notify',
  };
  final opts = <String, String>{};
  final flags = <String>{};
  final positional = <String>[];
  int? intOpt(String k) => opts[k] == null ? null : int.tryParse(opts[k]!);
}

String _stamp() {
  final t = DateTime.now();
  String two(int n) => n.toString().padLeft(2, '0');
  return '${two(t.hour)}:${two(t.minute)}:${two(t.second)}';
}

void _log(String m) => stdout.writeln('[${_stamp()}] $m');

Future<String> _computerName() async {
  if (Platform.isMacOS) {
    try {
      final r = await Process.run('scutil', ['--get', 'ComputerName']);
      final n = '${r.stdout}'.trim();
      if (r.exitCode == 0 && n.isNotEmpty) return n;
    } catch (_) {}
  }
  return Platform.localHostname;
}

Future<int> _serve(_Args a) async {
  final home = MaiAnHome.resolve(a.opts['home']);
  final settings = Settings.load(home);
  if (a.opts['receive-dir'] case final d? when d.isNotEmpty) {
    settings.set('receive_dir', d);
  }
  final ffmpeg = await Ffmpeg.find();
  final server = CompanionServer(
    home: home,
    settings: settings,
    ffmpeg: ffmpeg,
    port: a.intOpt('port') ?? defaultPort,
    pin: a.opts['pin'],
    deviceName: await _computerName(),
    // Hidden, for tests: pairing by PIN even over loopback.
    trustLoopback: !a.flags.contains('no-trust-loopback'),
    beaconUdpPort: a.intOpt('udp-port') ?? udpPort,
    beaconTargets: a.flags.contains('no-announce') ? const [] : null,
    log: _log,
  );
  if (a.flags.contains('no-notify')) server.notifier = (_, _) {};
  final int port;
  try {
    port = await server.start();
  } catch (e) {
    stderr.writeln('Could not start: $e');
    return 1;
  }
  if (a.opts['watch-apk'] case final d? when d.isNotEmpty) {
    server.updates.setWatchDir(d);
  } else if (a.opts['default-watch-apk'] case final d?
      when d.isNotEmpty && settings.get('apk_watch_dir') == null) {
    server.updates.setWatchDir(d);
  }
  if (a.opts['flutter-project'] case final d? when d.isNotEmpty) {
    server.builder.setProject(d);
  } else if (settings.get('flutter_project') == null) {
    final d =
        a.opts['default-flutter-project'] ??
        guessFlutterProject(Directory.current.path);
    if (d != null && File('$d/pubspec.yaml').existsSync()) {
      server.builder.setProject(d);
    }
  }
  final quit = Completer<void>();
  final ui = UiServer(
    server,
    port: a.intOpt('ui-port') ?? defaultUiPort,
    log: _log,
    onQuit: () {
      if (!quit.isCompleted) quit.complete();
    },
  );
  await ui.start();
  home.uiPortFile.writeAsStringSync('${ui.port}');

  final ips = [
    for (final i in await localInterfaces())
      if (!i.vpn) i.ip,
  ];
  stdout.writeln('''

  Mai An Companion ${appVersion}

  Phone address   ${ips.isEmpty ? 'no network' : ips.map((i) => '$i:$port').join('  ')}
  Pairing PIN     ${server.pin}
  Control page    ${ui.url}
  Received into   ${server.receiveDir}
  Files kept in   ${home.root.path}
  ffmpeg          ${ffmpeg?.path ?? 'NOT FOUND (brew install ffmpeg): offload and AAC need it'}
  Discovery       UDP ${server.beaconPort}
  Phone app from  ${server.builder.project ?? 'no Flutter project chosen'}
  App updates     ${server.updates.watchDir == null ? 'drop an APK on the control page' : 'watching ${server.updates.watchDir}'}
''');
  final wanted = a.intOpt('port') ?? defaultPort;
  if (wanted != 0 && port != wanted) {
    _log('Port $wanted was busy: the phone needs $port');
  }
  _log('Waiting for the phone. Ctrl-C to stop.');
  if (!a.flags.contains('no-open')) unawaited(openBrowser(ui.url));

  final done = Completer<void>();
  Future<void> shutdown() async {
    if (done.isCompleted) return;
    _log('Stopping');
    try {
      home.uiPortFile.deleteSync();
    } catch (_) {}
    await ui.stop();
    await server.stop();
    done.complete();
  }

  unawaited(quit.future.then((_) => shutdown()));
  ProcessSignal.sigint.watch().listen((_) => shutdown());
  if (!Platform.isWindows) {
    ProcessSignal.sigterm.watch().listen((_) => shutdown());
  }
  await done.future;
  return 0;
}

/// Talk to the running `maian serve` through its control port.
Future<Map<String, Object?>> _call(
  _Args a,
  String method,
  String path, [
  Object? body,
]) async {
  final home = MaiAnHome.resolve(a.opts['home']);
  var port = a.intOpt('ui-port');
  if (port == null) {
    try {
      port = int.parse(home.uiPortFile.readAsStringSync().trim());
    } catch (_) {
      throw StateError('maian serve is not running. Start it first.');
    }
  }
  final http = HttpClient()..connectionTimeout = const Duration(seconds: 3);
  try {
    final req = await http.openUrl(
      method,
      Uri.parse('http://127.0.0.1:$port$path'),
    );
    req.headers.set('x-maian', '1');
    if (body != null) {
      final bytes = utf8.encode(jsonEncode(body));
      req.headers.contentType = ContentType.json;
      req.contentLength = bytes.length;
      req.add(bytes);
    }
    final resp = await req.close();
    final text = await utf8.decodeStream(resp);
    final v = jsonDecode(text);
    if (resp.statusCode != 200) {
      throw StateError(v is Map ? '${v['error']}' : 'HTTP ${resp.statusCode}');
    }
    return v as Map<String, Object?>;
  } on SocketException {
    throw StateError('maian serve is not running. Start it first.');
  } finally {
    http.close(force: true);
  }
}

Future<int> _send(_Args a) async {
  if (a.positional.isEmpty) {
    stderr.write('Give at least one file.\n$_usage');
    return 64;
  }
  final paths = <String>[];
  for (final p in a.positional) {
    final f = File(p);
    if (!f.existsSync()) {
      stderr.writeln('Not found: $p');
      return 66;
    }
    paths.add(f.absolute.path);
  }
  try {
    stdout.writeln(
      'Preparing ${paths.length} file${paths.length == 1 ? '' : 's'}…',
    );
    final r = await _call(a, 'POST', '/api/send', {
      'paths': paths,
      'wait': true,
      'dsp': !a.flags.contains('no-dsp'),
      if (a.opts['quality'] != null) 'quality': a.opts['quality'],
    });
    stdout.writeln(
      'Queued ${r['queued']}. The phone picks them up when connected.',
    );
    return 0;
  } on StateError catch (e) {
    stderr.writeln(e.message);
    return 1;
  }
}

Future<int> _publish(_Args a) async {
  if (a.positional.length != 1) {
    stderr.write('Give one .apk file.\n$_usage');
    return 64;
  }
  final f = File(a.positional.first);
  if (!f.existsSync()) {
    stderr.writeln('Not found: ${f.path}');
    return 66;
  }
  try {
    final r = await _call(a, 'POST', '/api/update/publish', {
      'path': f.absolute.path,
    });
    final u = r['update'] as Map<String, Object?>;
    stdout.writeln(
      'Offering Mai An ${u['version_name']} (${u['version_code']}) to the phone.',
    );
    return 0;
  } on StateError catch (e) {
    stderr.writeln(e.message);
    return 1;
  }
}

Future<int> _quit(_Args a) async {
  try {
    await _call(a, 'POST', '/api/quit');
    stdout.writeln('Stopped.');
    return 0;
  } on StateError catch (e) {
    stderr.writeln(e.message);
    return 1;
  }
}

Future<int> _status(_Args a) async {
  try {
    final s = await _call(a, 'GET', '/api/state');
    final clients = (s['clients'] as List).cast<Map<String, Object?>>();
    final live = clients.where((c) => (c['ago'] as int) < 45).toList();
    stdout.writeln(
      live.isEmpty
          ? 'Phone: not connected (PIN ${s['pin']}, port ${s['port']})'
          : 'Phone: ${live.first['name']} at ${live.first['ip']}',
    );
    final o = s['offload'] as Map<String, Object?>;
    stdout.writeln(
      (o['total'] as int) == 0
          ? 'Offload: idle (${s['cached_tracks']} tracks cached)'
          : 'Offload: ${o['done']}/${o['total']}'
                '${o['active'] == true ? ' (running)' : ''}, ${o['failed']} failed',
    );
    stdout.writeln(
      'Waiting for the phone: ${(s['pending'] as List).length} tracks',
    );
    final up = (s['update'] as Map<String, Object?>)['current'] as Map?;
    stdout.writeln(
      up == null
          ? 'App update: none offered'
          : 'App update: offering ${up['version_name']} (${up['version_code']})',
    );
    stdout.writeln(
      'Received: ${(s['received'] as List).length} recent tracks in ${s['receive_dir']}',
    );
    return 0;
  } on StateError catch (e) {
    stderr.writeln(e.message);
    return 1;
  }
}
