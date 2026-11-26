import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'beacon.dart';
import 'features.dart';
import 'ffmpeg.dart';
import 'home.dart';
import 'mac.dart';
import 'multipart.dart';
import 'net.dart';
import 'reports.dart';
import 'shared.dart';
import 'transfers.dart';
import 'builder.dart';
import 'updates.dart';

const appVersion = '0.3.2';
const defaultPort = 8945;

/// Wrong PINs tolerated before the PIN rotates: a 4-digit PIN falls to a
/// scripted loop in seconds otherwise.
const _maxPinFailures = 5;

/// Tracks the phone may push to us; anything else is refused, so a token
/// never becomes write access for arbitrary files.
const _receivable = {
  '.flac',
  '.mp3',
  '.m4a',
  '.aac',
  '.alac',
  '.ogg',
  '.opus',
  '.wav',
  '.aif',
  '.aiff',
  '.wv',
  '.ape',
};

/// A phone that has talked to us.
class ClientInfo {
  ClientInfo(this.name, this.ip) : lastSeen = DateTime.now();
  String name;
  String ip;
  DateTime lastSeen;

  /// The phone app's own build, from its X-App-Version / X-App-Package
  /// headers ("0.3.0+4"); empty for phones too old to send them.
  String appVersion = '';
  String appPackage = '';
}

/// What the current (or last) offload from a phone looks like from here.
class OffloadStats {
  int total = 0, cached = 0, extracted = 0, failed = 0;
  String current = '';
  DateTime? started, last;

  int get done => cached + extracted + failed;
  bool get active =>
      last != null &&
      done < total &&
      DateTime.now().difference(last!).inSeconds < 45;
  double get rate {
    final s = started, l = last;
    if (s == null || l == null || extracted == 0) return 0;
    final secs = l.difference(s).inMilliseconds / 1000;
    return secs <= 0 ? 0 : extracted / secs;
  }

  Map<String, Object?> toJson() => {
    'total': total,
    'done': done,
    'cached': cached,
    'extracted': extracted,
    'failed': failed,
    'current': current,
    'active': active,
    'rate': rate,
    'ago': last == null ? null : DateTime.now().difference(last!).inSeconds,
  };
}

/// The Mac's half of the Companion protocol: byte-for-byte the endpoints of
/// StreamripApp/utils/sync/server.py, so the phone's client (and the Flet
/// app's) talk to it unchanged.
class CompanionServer {
  CompanionServer({
    required this.home,
    required this.settings,
    this.ffmpeg,
    this.port = defaultPort,
    String? pin,
    String? deviceName,
    this.trustLoopback = true,
    this.beaconUdpPort = udpPort,
    this.beaconAnnouncePort,
    this.beaconTargets,
    this.beaconInterval = const Duration(seconds: 2),
    this.dspJobs,
    this.log,
  }) : deviceName = deviceName ?? Platform.localHostname,
       pin = pin ?? _newPin(),
       token = settings.token ?? _newToken(),
       cache = FeatureCache(home.featuresFile),
       queue = TransferQueue(home.transfersFile) {
    if (settings.token == null) settings.set('token', token);
    pool = Pool(_workers);
    updates = AppUpdates(home, settings, log: _note, onChange: _changed);
    builder = AppBuilder(settings, updates, log: _note, onChange: _changed);
    reports = ProblemReports(home, settings);
  }

  /// Problem reports the phone sent (a described bug, or a caught error).
  late final ProblemReports reports;

  final MaiAnHome home;
  final Settings settings;
  final Ffmpeg? ffmpeg;
  final String deviceName;
  final bool trustLoopback;
  final int beaconUdpPort;
  final int? beaconAnnouncePort;
  final List<String>? beaconTargets;
  final Duration beaconInterval;
  final int? dspJobs;
  final void Function(String)? log;

  int port;
  String pin;
  String token;
  final FeatureCache cache;
  final TransferQueue queue;
  late final Pool pool;
  late final AppUpdates updates;
  late final AppBuilder builder;
  final _prepPool = Pool(3);

  /// The last lines logged, for the control page's Activity list.
  final logLines = <Map<String, Object?>>[];

  /// Log [m]: to [log] (the terminal / log file) and to the page.
  void note(String m) => _note(m);
  void _note(String m) {
    logLines.add({'at': DateTime.now().millisecondsSinceEpoch, 'text': m});
    if (logLines.length > 300) logLines.removeRange(0, logLines.length - 300);
    log?.call(m);
    _changed();
  }

  /// A macOS notification ([notifier] in tests); off with the "notify"
  /// setting.
  void Function(String title, String body)? notifier;
  void notify(String title, String body) => _notify(title, body);

  void _notify(String title, String body) {
    if (!settings.notify) return;
    (notifier ?? macNotify)(title, body);
  }

  final clients = <String, ClientInfo>{};
  final offload = OffloadStats();
  List<Map<String, Object?>> received = [];
  int _prepDone = 0, _prepTotal = 0;
  String _prepTitle = '';
  final _prepCancels = <_Cancel>{};

  HttpServer? _http;
  Beacon? _beacon;
  int _pinFailures = 0;
  int _batchReceived = 0;
  int _seq = 0;
  int _sleepHolders = 0;
  Process? _caffeinate;
  Timer? _tick;

  /// Fires when anything the UI shows has changed.
  final changes = StreamController<void>.broadcast();
  void changed() => _changed();

  void _changed() {
    if (!changes.isClosed) changes.add(null);
  }

  String get receiveDir => settings.receiveDir ?? home.defaultReceiveDir;
  bool get running => _http != null;

  /// The UDP port the discovery beacon is bound to (tests).
  int get beaconPort => _beacon?.boundPort ?? 0;

  static String _newPin() => '${Random.secure().nextInt(9000) + 1000}';
  static String _newToken() => List.generate(
    16,
    (_) => Random.secure().nextInt(256).toRadixString(16).padLeft(2, '0'),
  ).join();

  // ── lifecycle ────────────────────────────────────────────────────────────

  Future<int> start() async {
    cache.load();
    queue.load();
    _loadReceived();
    if (phoneOn) await _listen();
    updates.start();
    // Keeps the UI's "seen 12 s ago" honest and ends a finished offload;
    // only while a control page is open.
    _tick = Timer.periodic(const Duration(seconds: 5), (_) {
      if (changes.hasListener) _changed();
    });
    return port;
  }

  /// Open the phone-facing port (and the beacon, if on).
  Future<void> _listen() async {
    if (_http != null) return;
    HttpServer? s;
    var p = port;
    for (var i = 0; i < 5 && s == null; i++, p++) {
      try {
        s = await HttpServer.bind(InternetAddress.anyIPv4, p);
      } on SocketException {
        _note('Port $p is busy, trying ${p + 1}');
      }
    }
    if (s == null) throw StateError('No free port from $port to ${p - 1}');
    s.autoCompress = false;
    _http = s;
    port = s.port;
    s.listen(_dispatch);
    if (beaconOn) await _startBeacon();
  }

  Future<void> _unlisten() async {
    _beacon?.stop();
    _beacon = null;
    await _http?.close(force: true);
    _http = null;
    clients.clear();
  }

  Future<void> _startBeacon() async {
    if (_beacon != null) return;
    final b = Beacon(
      name: deviceName,
      port: port,
      udp: beaconUdpPort,
      announcePort: beaconAnnouncePort,
      interval: beaconInterval,
      targets: beaconTargets,
      log: log,
    );
    _beacon = b;
    await b.start();
  }

  Future<void> stop() async {
    _tick?.cancel();
    updates.stop();
    builder.dispose();
    await _unlisten();
    _sleepHolders = 0;
    _caffeinate?.kill();
    _caffeinate = null;
    await cache.close();
    await changes.close();
  }

  // ── background services, each switchable from the page ───────────────────

  /// The phone-facing server: off, the phone can't connect at all.
  bool get phoneOn => settings.get('svc_phone') != false;

  /// The UDP beacon (every few seconds) that lets the phone find the Mac.
  bool get beaconOn => settings.get('svc_beacon') != false;

  /// Analysing tracks for the phone (ffmpeg, several cores).
  bool get offloadOn => settings.get('svc_offload') != false;

  /// Keeping the Mac awake while a job runs.
  bool get awakeOn => settings.get('svc_awake') != false;

  /// One analysis at a time instead of one per core: slower, cooler.
  bool get gentle => settings.get('dsp_gentle') == true;

  int get _workers =>
      gentle ? 1 : (dspJobs ?? max(1, Platform.numberOfProcessors - 1));

  static const services = [
    'phone',
    'beacon',
    'offload',
    'gentle',
    'awake',
    'watch',
    'stop_gradle',
  ];

  /// Turn a service on or off; throws [FormatException] for an unknown one.
  Future<void> setService(String name, bool on) async {
    String what;
    switch (name) {
      case 'phone':
        settings.set('svc_phone', on);
        on ? await _listen() : await _unlisten();
        what = on
            ? 'Phone connection on (port $port)'
            : 'Phone connection off: the phone can\'t reach this Mac';
      case 'beacon':
        settings.set('svc_beacon', on);
        if (!on) {
          _beacon?.stop();
          _beacon = null;
        } else if (_http != null) {
          await _startBeacon();
        }
        what = on
            ? 'Discovery on'
            : 'Discovery off: a phone that knows this Mac still connects';
      case 'offload':
        settings.set('svc_offload', on);
        what = on ? 'Analysis for the phone on' : 'Analysis for the phone off';
      case 'gentle':
        settings.set('dsp_gentle', on);
        pool.size = _workers;
        what = on
            ? 'Low-power analysis: one track at a time'
            : 'Analysis uses ${pool.size} cores';
      case 'awake':
        settings.set('svc_awake', on);
        if (!on) {
          _caffeinate?.kill();
          _caffeinate = null;
        }
        what = on
            ? 'The Mac stays awake during jobs'
            : 'The Mac may sleep during jobs';
      case 'watch':
        updates.setAuto(on);
        what = on ? 'Watching for new builds' : 'Not watching for new builds';
      case 'stop_gradle':
        builder.setOption('stop_gradle', on);
        what = on
            ? 'Gradle stops after each build'
            : 'Gradle stays running between builds (faster rebuilds)';
      default:
        throw FormatException('Unknown service $name');
    }
    _note(what);
  }

  /// Revoke every paired phone: new token, new PIN.
  void resetPairing() {
    token = _newToken();
    settings.set('token', token);
    settings.set('last_client', null);
    clients.clear();
    rotatePin();
  }

  void rotatePin() {
    pin = _newPin();
    _pinFailures = 0;
    _changed();
  }

  // ── sleep lock ───────────────────────────────────────────────────────────

  /// Keeps the Mac awake while a job runs. Reference-counted: the first job
  /// to finish must not let the Mac sleep under the others.
  Future<T> awake<T>(Future<T> Function() job) async {
    _sleepHolders++;
    if (awakeOn && Platform.isMacOS && _caffeinate == null) {
      try {
        _caffeinate = await Process.start('caffeinate', ['-i', '-w', '$pid']);
      } catch (_) {}
    }
    try {
      return await job();
    } finally {
      if (--_sleepHolders <= 0) {
        _sleepHolders = 0;
        _caffeinate?.kill();
        _caffeinate = null;
      }
    }
  }

  // ── request plumbing ─────────────────────────────────────────────────────

  Future<void> _dispatch(HttpRequest req) async {
    try {
      final key = '${req.method} ${req.uri.path}';
      switch (key) {
        case 'GET /api/v1/ping':
          return await _ping(req);
        case 'POST /api/v1/pair':
          return await _pair(req);
        case 'POST /api/v1/dsp/match':
          return await _dspMatch(req);
        case 'POST /api/v1/dsp/extract':
          return await _dspExtract(req);
        case 'GET /api/v1/tracks/stream':
          return await _stream(req);
        case 'POST /api/v1/bundle/upload':
          return await _bundleUpload(req);
        case 'POST /api/v1/tracks/receive':
          return await _receive(req);
        case 'GET /api/v1/transfers/pending':
          return await _pending(req);
        case 'POST /api/v1/transfers/queue':
          return await _queueRequest(req);
        case 'POST /api/v1/transfers/ack':
          return await _ack(req);
        case 'GET /api/v1/app/apk':
          return await _appApk(req);
        case 'POST /api/v1/app/status':
          return await _appStatus(req);
        case 'POST /api/v1/report':
          return await _report(req);
      }
      await _json(req, {'error': 'Not found'}, 404);
    } on FormatException catch (e) {
      await _fail(req, 400, e.message);
    } catch (e) {
      _note('Request ${req.method} ${req.uri.path} failed: $e');
      await _fail(req, 500, '$e');
    }
  }

  Future<void> _fail(HttpRequest req, int status, String message) async {
    try {
      await _json(req, {'error': message}, status);
    } catch (_) {
      // Headers already sent (a failure mid-download): drop the connection.
      try {
        (await req.response.detachSocket(writeHeaders: false)).destroy();
      } catch (_) {}
    }
  }

  Future<void> _json(HttpRequest req, Object body, [int status = 200]) async {
    final r = req.response
      ..statusCode = status
      ..headers.contentType = ContentType.json;
    r.write(jsonEncode(body));
    await r.close();
  }

  Future<Map<String, Object?>?> _readJson(HttpRequest req) async {
    try {
      final v = jsonDecode(await utf8.decodeStream(req));
      return v is Map<String, Object?> ? v : null;
    } catch (_) {
      return null;
    }
  }

  bool _authorized(HttpRequest req) {
    final auth = req.headers.value('authorization') ?? '';
    var presented = auth.startsWith('Bearer ') ? auth.substring(7).trim() : '';
    // Download URLs carry the token as a query parameter: the phone's
    // download pipeline issues plain GETs with no custom headers.
    if (presented.isEmpty) presented = req.uri.queryParameters['token'] ?? '';
    if (presented.isNotEmpty) return _constEq(presented, token);
    // USB: `adb reverse` lands phone traffic on loopback. Physical access to
    // the cable is the trust anchor there, so no PIN is needed.
    return trustLoopback &&
        (req.connectionInfo?.remoteAddress.isLoopback ?? false);
  }

  Future<bool> _requireAuth(HttpRequest req) async {
    if (_authorized(req)) return true;
    // Read what the phone is still sending before answering: closing a
    // connection with unread upload data resets it, and the phone would see
    // a network error instead of the 401.
    try {
      await req.drain<void>().timeout(const Duration(seconds: 5));
    } catch (_) {}
    await _json(req, {'error': 'Unauthorized'}, 401);
    return false;
  }

  String _deviceOf(HttpRequest req) =>
      _unquote(req.headers.value('x-device-name') ?? 'Phone');

  void _seen(String name, String? ip, [HttpRequest? req]) {
    final key = '${ip ?? ''}|$name';
    var c = clients[key];
    if (c == null) {
      c = clients[key] = ClientInfo(name, ip ?? '');
      _note('Phone connected: $name (${ip ?? '?'})');
    } else {
      c.lastSeen = DateTime.now();
    }
    final v = req?.headers.value('x-app-version');
    if (v != null && v.isNotEmpty && v != c.appVersion) {
      final was = c.appVersion;
      c.appVersion = v;
      c.appPackage = req?.headers.value('x-app-package') ?? '';
      if (was.isNotEmpty) _note('$name now runs Mai An $v (was $was)');
      // Installed: the phone came back on the offered version after it had
      // started on it (a fresh Mac run only knows the phone's word).
      final offered = updates.current;
      final st = updates.phoneStatus?['state'];
      if (offered != null &&
          v == '${offered['version_name']}+${offered['version_code']}' &&
          (was.isNotEmpty || st == 'installing' || st == 'downloading')) {
        updates.setPhoneStatus({'state': 'installed', 'device': name});
        _notify('Phone app updated', '$name now runs Mai An $v');
      }
    }
    if (settings.lastClient != name) settings.set('last_client', name);
    _changed();
  }

  // ── endpoints ────────────────────────────────────────────────────────────

  Future<void> _ping(HttpRequest req) async {
    final ok = _authorized(req);
    if (ok) {
      _seen(_deviceOf(req), req.connectionInfo?.remoteAddress.address, req);
    }
    final offer = ok ? updates.offer : null;
    await _json(req, {
      'status': 'ok',
      'name': deviceName,
      'device': 'macOS',
      'features_version': featuresVersion,
      'total_tracks': 0,
      'requires_pairing': true,
      // Ping itself is open (discovery needs it), so the client learns here
      // whether its token still works instead of on the next 401.
      'authorized': ok,
      'companion_version': appVersion,
      // The phone app build on offer (only to a paired phone).
      'app_update': ?offer,
    });
  }

  Future<void> _pair(HttpRequest req) async {
    final body = await _readJson(req);
    if (body == null) return _json(req, {'error': 'Invalid JSON'}, 400);
    final attempt = '${body['pin'] ?? ''}'.trim();
    final name = '${body['device_name'] ?? 'Android Client'}';
    if (_constEq(attempt, pin)) {
      _pinFailures = 0;
      _note('Paired with $name');
      _seen(name, req.connectionInfo?.remoteAddress.address, req);
      _notify('Phone paired', name);
      return _json(req, {
        'status': 'paired',
        'token': token,
        'server_name': deviceName,
      });
    }
    _note('Wrong PIN from ${req.connectionInfo?.remoteAddress.address}');
    if (++_pinFailures >= _maxPinFailures) {
      _note('Too many wrong PINs: new PIN');
      rotatePin();
    }
    await _json(req, {'error': 'Invalid PIN'}, 403);
  }

  Future<void> _offloadOff(HttpRequest req) async {
    // Unread upload bodies would keep the phone sending: drop them.
    await req.drain<void>().catchError((_) {});
    return _json(req, {
      'error': 'Analysis is turned off in Mai An Companion on the Mac',
      'reason': 'offload_off',
    }, 503);
  }

  Future<void> _dspMatch(HttpRequest req) async {
    if (!await _requireAuth(req)) return;
    if (!offloadOn) return _offloadOff(req);
    final body = await _readJson(req);
    if (body == null) return _json(req, {'error': 'Invalid JSON'}, 400);
    final cached = <Map<String, Object?>>[];
    final missing = <String>[];
    for (final t in (body['tracks'] as List? ?? const [])) {
      final path = t is Map ? t['path'] as String? : null;
      if (path == null || path.isEmpty) continue;
      final row = cache.get(path);
      if (row != null) {
        cached.add({'path': path, ...row});
      } else {
        missing.add(path);
      }
    }
    offload
      ..total = cached.length + missing.length
      ..cached = cached.length
      ..extracted = 0
      ..failed = 0
      ..current = ''
      ..started = DateTime.now()
      ..last = DateTime.now();
    if (offload.total > 0) {
      _note(
        'Offload: ${offload.total} tracks, ${cached.length} already cached, '
        '${missing.length} to analyse',
      );
    }
    _changed();
    await _json(req, {'cached': cached, 'missing': missing});
  }

  Future<void> _dspExtract(HttpRequest req) async {
    if (!await _requireAuth(req)) return;
    if (!offloadOn) return _offloadOff(req);
    final ff = ffmpeg;
    if (ff == null) {
      return _json(req, {
        'error': 'ffmpeg not found on the Mac (brew install ffmpeg)',
      }, 500);
    }
    await awake(() async {
      File? tmp;
      var trackPath = _unquote(req.headers.value('x-track-path') ?? '');
      try {
        String? uploadName;
        // The phone path comes as a form field ahead of the file: as a
        // header, a Greek/accented path is not valid latin-1 HTTP. The whole
        // body is read, even past the file: answering with upload data
        // still in flight would reset the connection.
        await for (final part in multipartParts(req)) {
          if (part.name == 'path') {
            final t = await part.text();
            if (t.isNotEmpty) trackPath = t;
          } else if (part.name == 'file' && tmp == null) {
            uploadName = part.filename ?? '';
            final ext = _extOf(uploadName);
            tmp = File(
              '${home.tmp.path}/up_${pid}_${_seq++}${ext.isEmpty ? '.tmp' : ext}',
            );
            await saveStream(part.body, tmp);
          } else {
            await part.body.drain<void>();
          }
        }
        if (tmp == null) {
          return _json(req, {'error': 'No file uploaded'}, 400);
        }
        if (trackPath.isEmpty) {
          trackPath = (uploadName?.isEmpty ?? true)
              ? 'unknown.audio'
              : uploadName!;
        }
        offload.current = _baseName(trackPath);
        offload.started ??= DateTime.now();
        _changed();
        final feats = await pool.run(
          () => analyseFile(ff, tmp!.path, home.tmp),
        );
        cache.put(trackPath, feats);
        _offloadStep(extracted: true);
        await _json(req, {'status': 'ok', 'path': trackPath, ...feats});
      } catch (e) {
        _offloadStep(extracted: false);
        _note('DSP failed for ${_baseName(trackPath)}: $e');
        await _json(req, {'error': '$e'}, 500);
      } finally {
        try {
          tmp?.deleteSync();
        } catch (_) {}
      }
    });
  }

  void _offloadStep({required bool extracted}) {
    if (offload.done >= offload.total) offload.total = offload.done + 1;
    if (extracted) {
      offload.extracted++;
    } else {
      offload.failed++;
    }
    offload.last = DateTime.now();
    if (offload.done >= offload.total) {
      _note(
        'Offload done: ${offload.cached + offload.extracted} ok, '
        '${offload.failed} failed',
      );
    }
    _changed();
  }

  Future<void> _stream(HttpRequest req) async {
    if (!_authorized(req)) {
      return _json(req, {'error': 'Unauthorized'}, 401);
    }
    final path = req.uri.queryParameters['path'];
    // Only files queued for transfer are served: a valid token must not
    // turn into read access to the whole disk.
    if (path == null || !queue.hasPath(path)) {
      return _json(req, {'error': 'Track is not queued for transfer'}, 404);
    }
    if (!File(path).existsSync()) {
      return _json(req, {'error': 'Audio file not found'}, 404);
    }
    final q = Quality.parse(req.uri.queryParameters['quality']);
    var send = path;
    if (q != Quality.original && ffmpeg != null) {
      try {
        send = await awake(() => _transcode(path, q));
      } catch (e) {
        _note('Transcode failed for ${_baseName(path)} ($e): sending original');
      }
    }
    final f = File(send);
    final r = req.response
      ..statusCode = 200
      ..headers.contentType = _audioType(send)
      ..headers.set(
        'content-disposition',
        "attachment; filename*=UTF-8''${_rfc5987(_baseName(send))}",
      )
      ..contentLength = f.lengthSync();
    await r.addStream(f.openRead());
    await r.close();
    _note(
      'Sent ${_baseName(path)} to ${req.connectionInfo?.remoteAddress.address}',
    );
  }

  Future<String> _transcode(String path, Quality q) =>
      ffmpeg!.transcode(path, q, home.cache, pathDigest(path));

  Future<void> _bundleUpload(HttpRequest req) async {
    if (!await _requireAuth(req)) return;
    final t = DateTime.now();
    String two(int n) => n.toString().padLeft(2, '0');
    final ts =
        '${t.year}${two(t.month)}${two(t.day)}_${two(t.hour)}${two(t.minute)}${two(t.second)}';
    final dest = File('${home.backups.path}/mai_an_lab_state_$ts.zip');
    int? n;
    await for (final p in multipartParts(req)) {
      if (n == null) {
        n = await saveStream(p.body, dest);
      } else {
        await p.body.drain<void>();
      }
    }
    if (n == null) {
      return _json(req, {'error': 'No bundle file provided'}, 400);
    }
    _note('Saved state bundle (${_size(n)}) to ${dest.path}');
    _changed();
    await _json(req, {'status': 'ok', 'saved_path': dest.path});
  }

  Future<void> _receive(HttpRequest req) async {
    if (!await _requireAuth(req)) return;
    // Taken now: connectionInfo is gone once the request body is read.
    final remote = req.connectionInfo?.remoteAddress.address;
    var meta = <String, Object?>{};
    var name = '';
    var size = 0;
    String? dir;
    File? part;
    int? rejectStatus;
    String? rejectMessage;
    void reject(int status, String message) {
      rejectStatus ??= status;
      rejectMessage ??= message;
    }

    try {
      // The whole body is read, accepted or not: answering with upload data
      // still in flight would reset the connection under the phone.
      await for (final p in multipartParts(req)) {
        if (p.name == 'meta') {
          try {
            final v = jsonDecode(await p.text());
            if (v is Map<String, Object?>) meta = v;
          } catch (_) {}
        } else if (p.name == 'file' && part == null && rejectStatus == null) {
          // The name only, never a path: the phone doesn't choose where we write.
          name = _safeName(_baseName(p.filename ?? ''));
          if (!_receivable.contains(_extOf(name).toLowerCase())) {
            reject(415, 'Not an audio file');
            await p.body.drain<void>();
            continue;
          }
          dir = receiveDir;
          final artist = _safeName('${meta['artist'] ?? ''}');
          final album = _safeName('${meta['album'] ?? ''}');
          if (artist.isNotEmpty && album.isNotEmpty && artist != 'Unknown') {
            dir = '$dir/$artist/$album';
          }
          try {
            Directory(dir).createSync(recursive: true);
          } on FileSystemException catch (e) {
            reject(507, e.message);
            await p.body.drain<void>();
            continue;
          }
          part = File('$dir/.maian_${pid}_${_seq++}.part');
          size = await saveStream(p.body, part);
        } else {
          await p.body.drain<void>();
        }
      }
      if (rejectStatus != null) {
        return _json(req, {'error': rejectMessage}, rejectStatus!);
      }
      if (part == null) return _json(req, {'error': 'No file uploaded'}, 400);
      if (size == 0) return _json(req, {'error': 'Empty file'}, 400);

      var dest = normalizeNfc('$dir/$name');
      var duplicate = false;
      if (File(dest).existsSync() && File(dest).lengthSync() == size) {
        duplicate = true; // sent before
      } else {
        if (File(dest).existsSync()) {
          // A different track with the same file name: never overwrite.
          final ext = _extOf(dest);
          final stem = dest.substring(0, dest.length - ext.length);
          var n = 2;
          while (File('$stem ($n)$ext').existsSync()) {
            n++;
          }
          dest = '$stem ($n)$ext';
        }
        part.renameSync(dest);
        part = null;
      }
      final dsp = meta['dsp_features'];
      if (dsp is Map<String, Object?> && !duplicate) cache.put(dest, dsp);
      final sender = _deviceOf(req);
      _seen(sender, remote, req);
      if (!duplicate) {
        received.insert(0, {
          'name': _baseName(dest),
          'path': dest,
          'from': sender,
          'at': DateTime.now().millisecondsSinceEpoch,
          'size': size,
        });
        if (received.length > 200) received.removeRange(200, received.length);
        atomicWrite(home.receivedFile, jsonEncode(received));
      }
      final remaining = int.tryParse('${meta['remaining'] ?? 0}') ?? 0;
      _note(
        'Received ${_baseName(dest)} from $sender${duplicate ? ' (duplicate)' : ''}'
        '${remaining > 0 ? ' ($remaining to go)' : ''}',
      );
      if (!duplicate) _batchReceived++;
      if (remaining <= 0 && _batchReceived > 0) {
        _notify(
          'From $sender',
          _batchReceived == 1
              ? 'Received ${_baseName(dest)}'
              : 'Received $_batchReceived tracks',
        );
        _batchReceived = 0;
      }
      _changed();
      await _json(req, {'status': 'ok', 'path': dest, 'duplicate': duplicate});
    } finally {
      try {
        part?.deleteSync();
      } catch (_) {}
    }
  }

  void _loadReceived() {
    try {
      final v = jsonDecode(home.receivedFile.readAsStringSync());
      if (v is List) {
        received = [
          for (final r in v)
            if (r is Map<String, Object?> &&
                File((r['path'] as String?) ?? '').existsSync())
              r,
        ];
      }
    } catch (_) {}
  }

  /// The offered phone app build. Served from the copy in updates/, so a
  /// rebuild mid-download can't change the bytes under the phone.
  Future<void> _appApk(HttpRequest req) async {
    if (!_authorized(req)) {
      return _json(req, {'error': 'Unauthorized'}, 401);
    }
    final c = updates.current;
    final f = updates.apk;
    if (c == null || !f.existsSync()) {
      return _json(req, {'error': 'No app update on offer'}, 404);
    }
    final who = _deviceOf(req);
    _seen(who, req.connectionInfo?.remoteAddress.address, req);
    _note(
      '$who is downloading Mai An ${c['version_name']} (${c['version_code']})',
    );
    updates.downloads++;
    updates.setPhoneStatus({'state': 'downloading', 'device': who});
    final r = req.response
      ..statusCode = 200
      ..headers.contentType = ContentType(
        'application',
        'vnd.android.package-archive',
      )
      ..headers.set('etag', '"${c['sha256']}"')
      ..headers.set('x-sha256', '${c['sha256']}')
      ..contentLength = f.lengthSync();
    await r.addStream(f.openRead());
    await r.close();
  }

  /// The phone's progress with the offered build, for the control page.
  Future<void> _appStatus(HttpRequest req) async {
    if (!await _requireAuth(req)) return;
    final body = await _readJson(req) ?? {};
    final state = '${body['state'] ?? ''}';
    const states = {
      'downloading',
      'downloaded',
      'installing',
      'installed',
      'failed',
      'declined',
    };
    if (!states.contains(state)) {
      return _json(req, {'error': 'Unknown state'}, 400);
    }
    final who = _deviceOf(req);
    _seen(who, req.connectionInfo?.remoteAddress.address, req);
    final before = updates.phoneStatus?['state'];
    updates.setPhoneStatus({
      'state': state,
      'device': who,
      if (body['error'] != null) 'error': '${body['error']}',
    });
    if (state == 'failed') {
      _note('App update on $who failed: ${body['error'] ?? 'unknown error'}');
    } else if (state == 'installing') {
      _note('$who is installing the update');
    } else if (state == 'installed' && before != 'installed') {
      final c = updates.current;
      _note('$who runs the offered build');
      _notify(
        'Phone app updated',
        c == null
            ? who
            : '$who now runs Mai An ${c['version_name']} (${c['version_code']})',
      );
    }
    await _json(req, {'status': 'ok'});
  }

  /// A problem report from the phone: kept for the page, and a notification.
  Future<void> _report(HttpRequest req) async {
    if (!await _requireAuth(req)) return;
    final body = await _readJson(req);
    if (body == null) return _json(req, {'error': 'Invalid JSON'}, 400);
    final who = _deviceOf(req);
    _seen(who, req.connectionInfo?.remoteAddress.address, req);
    final r = reports.add(body, device: who);
    final bug = r['kind'] == 'bug';
    _note(
      '${bug ? 'Problem report' : 'Error report'} from $who: ${r['title']}',
    );
    _notify(
      bug ? 'Problem report from $who' : 'Error on $who',
      '${r['title']}',
    );
    _changed();
    await _json(req, {'status': 'ok', 'id': r['id']});
  }

  Future<void> _pending(HttpRequest req) async {
    if (!await _requireAuth(req)) return;
    await _json(req, {
      'status': 'ok',
      'count': queue.items.length,
      'transfers': queue.items,
    });
  }

  Future<void> _queueRequest(HttpRequest req) async {
    if (!await _requireAuth(req)) return;
    final body = await _readJson(req);
    if (body == null) return _json(req, {'error': 'Invalid JSON'}, 400);
    final tracks = [
      for (final t in (body['tracks'] as List? ?? const []))
        if (t is Map) Map<String, Object?>.from(t),
    ];
    final queued = await queueTracks(
      tracks,
      quality: '${body['quality'] ?? 'aac_vbr_max'}',
      precomputeDsp: body['precompute_dsp'] != false,
    );
    await _json(req, {'status': 'ok', 'queued': queued.length});
  }

  Future<void> _ack(HttpRequest req) async {
    if (!await _requireAuth(req)) return;
    final body = await _readJson(req) ?? {};
    if (body['clear_all'] == true) {
      clearQueue();
    } else {
      removeTransfers([
        for (final i in (body['ids'] as List? ?? const [])) '$i',
      ]);
    }
    await _json(req, {'status': 'ok', 'remaining': queue.items.length});
  }

  /// Drop transfers by id (the phone fetched them, or the user removed
  /// them), and delete outbox copies nothing needs any more.
  void removeTransfers(List<String> ids) {
    final gone = [
      for (final t in queue.items)
        if (ids.contains('${t['id']}')) '${t['path']}',
    ];
    queue.ack(ids);
    _cleanOutbox(gone);
    _changed();
  }

  void clearQueue() {
    final gone = [for (final t in queue.items) '${t['path']}'];
    queue.clear();
    _cleanOutbox(gone);
    _changed();
  }

  void _cleanOutbox(Iterable<String> paths) {
    final box = '${home.outbox.path}/';
    for (final p in paths) {
      // Only our own copies: a file sent by path belongs to the user.
      if (!p.startsWith(box) || queue.hasPath(p)) continue;
      try {
        File(p).deleteSync();
      } catch (_) {}
    }
  }

  // ── sending to the phone ─────────────────────────────────────────────────

  /// features for [path]: the cache, else computed here (needs ffmpeg).
  Future<Map<String, Object?>?> featuresFor(String path) async {
    if (!File(path).existsSync()) return null;
    final hit = cache.get(path);
    if (hit != null) return hit;
    final ff = ffmpeg;
    if (ff == null) return null;
    try {
      final f = await awake(
        () => pool.run(() => analyseFile(ff, path, home.tmp)),
      );
      cache.put(path, f);
      return f;
    } catch (e) {
      _note('Feature extraction failed for ${_baseName(path)}: $e');
      return null;
    }
  }

  /// `queue_tracks_for_transfer`: prepare (features, AAC encode) and enqueue
  /// [tracks] for the phone. The whole batch is published at once: the phone
  /// polls the pending list, and publishing track by track made it offer
  /// the first one alone and the rest in a second pop-up.
  Future<List<Map<String, Object?>>> queueTracks(
    List<Map<String, Object?>> tracks, {
    String quality = 'aac_vbr_max',
    bool precomputeDsp = true,
  }) async {
    final q = Quality.parse(quality);
    final ip = await lanIp();
    final todo = [
      for (final t in tracks)
        if (t['path'] is String && File(t['path']! as String).existsSync()) t,
    ];
    if (todo.isEmpty) return const [];
    final cancel = _Cancel();
    _prepCancels.add(cancel);
    _prepTotal += todo.length;
    _changed();
    try {
      final results = await Future.wait([
        for (final t in todo)
          _prepOne(t, q, quality, ip, precomputeDsp, cancel),
      ]);
      if (cancel.set) {
        _note('Send cancelled');
        return const [];
      }
      final queued = [
        for (final r in results)
          if (r != null) r,
      ];
      queue.addAll(queued);
      _note(
        'Queued ${queued.length} track${queued.length == 1 ? '' : 's'} for the phone '
        '(${queue.items.length} waiting)',
      );
      return queued;
    } finally {
      _prepCancels.remove(cancel);
      _prepTotal -= todo.length;
      if (_prepCancels.isEmpty) _prepDone = 0;
      _changed();
    }
  }

  Future<Map<String, Object?>?> _prepOne(
    Map<String, Object?> t,
    Quality q,
    String qualityWire,
    String ip,
    bool precompute,
    _Cancel cancel,
  ) => _prepPool.run(() async {
    if (cancel.set) return null;
    final path = t['path']! as String;
    final tags = await ffmpeg?.tags(path) ?? const {};
    final title =
        _nonEmpty(t['title']) ?? tags['title'] ?? _stemOf(_baseName(path));
    _prepTitle = title;
    _changed();
    Map<String, Object?>? dsp;
    if (precompute) dsp = await featuresFor(path);
    if (q != Quality.original && ffmpeg != null) {
      try {
        await awake(() => _transcode(path, q));
      } catch (e) {
        // The stream handler retries (and falls back to the original).
        _note('Pre-encode failed for ${_baseName(path)}: $e');
      }
    }
    _prepDone++;
    _changed();
    final ext = (q != Quality.original || path.endsWith('.m4a'))
        ? 'm4a'
        : (_extOf(path).isEmpty ? 'flac' : _extOf(path).substring(1));
    // The phone rebuilds the host part from the address it actually reached
    // us on (USB tunnel vs LAN); stream_path is what it needs.
    final streamPath =
        '/api/v1/tracks/stream?path=${Uri.encodeQueryComponent(path).replaceAll('+', '%20')}'
        '&quality=${Uri.encodeQueryComponent(qualityWire)}';
    return {
      'id': transferId(path, qualityWire),
      'title': title,
      'artist': _nonEmpty(t['artist']) ?? tags['artist'] ?? 'Unknown',
      'album': _nonEmpty(t['album']) ?? tags['album'] ?? 'Unknown Album',
      'path': path,
      'quality': qualityWire,
      'extension': ext,
      'stream_url': 'http://$ip:$port$streamPath',
      'stream_path': streamPath,
      'dsp_features': dsp,
      'timestamp': DateTime.now().millisecondsSinceEpoch / 1000,
    };
  });

  void cancelPreparing() {
    for (final c in _prepCancels) {
      c.set = true;
    }
  }

  // ── what the UI shows ────────────────────────────────────────────────────

  Future<Map<String, Object?>> snapshot() async {
    final ifaces = await localInterfaces();
    final now = DateTime.now();
    final backups = home.backups.existsSync()
        ? (home.backups.listSync().whereType<File>().toList()..sort(
            (a, b) => b.lastModifiedSync().compareTo(a.lastModifiedSync()),
          ))
        : <File>[];
    final cl = clients.values.toList()
      ..sort((a, b) => b.lastSeen.compareTo(a.lastSeen));
    return {
      'version': appVersion,
      'name': deviceName,
      'ips': [
        for (final i in ifaces)
          if (!i.vpn) i.ip,
      ],
      'port': port,
      'pin': pin,
      'ffmpeg': ffmpeg?.path,
      'cached_tracks': cache.length,
      'receive_dir': receiveDir,
      'quality': settings.quality,
      'last_client': settings.lastClient,
      'clients': [
        for (final c in cl)
          {
            'name': c.name,
            'ip': c.ip,
            'ago': now.difference(c.lastSeen).inSeconds,
            'app_version': c.appVersion,
            'app_package': c.appPackage,
          },
      ],
      'update': {
        'current': updates.current,
        'phone': updates.phoneStatus,
        'downloads': updates.downloads,
        'watch_dir': updates.watchDir,
        'watch_error': updates.watchError,
        'auto': updates.auto,
        'draft_notes': updates.draftNotes,
      },
      'notify': settings.notify,
      'services': {
        'phone': phoneOn,
        'listening': _http != null,
        'beacon': beaconOn,
        'beacon_live': _beacon != null,
        'offload': offloadOn,
        'gentle': gentle,
        'workers': pool.size,
        'awake': awakeOn,
        'awake_now': _caffeinate != null,
        'watch': updates.auto,
        'stop_gradle': builder.stopGradle,
      },
      'build': builder.toJson(),
      'reports': {
        'items': reports.items.take(40).toList(),
        'total': reports.items.length,
        'unseen': reports.unseen,
        'seen_at': reports.seenAt,
      },
      'login_item': loginItemEnabled(),
      'in_app': appBundlePath() != null,
      'log': logLines.length > 120
          ? logLines.sublist(logLines.length - 120)
          : logLines,
      'offload': offload.toJson(),
      'prep': _prepTotal == 0
          ? null
          : {'done': _prepDone, 'total': _prepTotal, 'title': _prepTitle},
      'pending': [
        for (final t in queue.items)
          {
            'id': t['id'],
            'title': t['title'],
            'artist': t['artist'],
            'quality': t['quality'],
            'size': _fileSize('${t['path']}'),
          },
      ],
      'received': received.take(30).toList(),
      'backups': [
        for (final f in backups.take(8))
          {
            'name': f.uri.pathSegments.last,
            'path': f.path,
            'size': f.lengthSync(),
            'at': f.lastModifiedSync().millisecondsSinceEpoch,
          },
      ],
    };
  }
}

/// A Notification Center banner, through AppleScript (no entitlement, no
/// helper app). Fire and forget.
void macNotify(String title, String body) {
  if (!Platform.isMacOS) return;
  String q(String s) =>
      '"${s.replaceAll('\\', '\\\\').replaceAll('"', '\\"')}"';
  Process.start('/usr/bin/osascript', [
    '-e',
    'display notification ${q(body)} with title ${q('Mai An Companion')} subtitle ${q(title)}',
  ]).then((p) => p.exitCode, onError: (_) => 0);
}

class _Cancel {
  bool set = false;
}

// ── small helpers ──────────────────────────────────────────────────────────

/// Python's `unquote`: percent-decoding that never throws.
String _unquote(String s) {
  try {
    return Uri.decodeComponent(s);
  } catch (_) {
    return s;
  }
}

bool _constEq(String a, String b) {
  if (a.length != b.length) return false;
  var r = 0;
  for (var i = 0; i < a.length; i++) {
    r |= a.codeUnitAt(i) ^ b.codeUnitAt(i);
  }
  return r == 0;
}

String _baseName(String p) {
  final i = p.lastIndexOf('/');
  return i < 0 ? p : p.substring(i + 1);
}

String _extOf(String name) {
  final b = _baseName(name);
  final i = b.lastIndexOf('.');
  return i <= 0 ? '' : b.substring(i);
}

String _stemOf(String name) {
  final e = _extOf(name);
  return e.isEmpty ? name : name.substring(0, name.length - e.length);
}

String? _nonEmpty(Object? v) {
  final s = v == null ? '' : '$v'.trim();
  return s.isEmpty ? null : s;
}

/// A single path component: cleaned like the Flet server, and never `.` or
/// `..` (which would climb out of the receive folder).
String _safeName(String s) {
  final c = cleanFilename(s);
  return (c == '.' || c == '..') ? '' : c;
}

int _fileSize(String path) {
  try {
    return File(path).lengthSync();
  } catch (_) {
    return 0;
  }
}

String _size(int n) => n >= 1 << 20
    ? '${(n / (1 << 20)).toStringAsFixed(1)} MB'
    : '${(n / 1024).toStringAsFixed(0)} kB';

ContentType _audioType(String path) => switch (_extOf(path).toLowerCase()) {
  '.m4a' || '.aac' => ContentType('audio', 'mp4'),
  '.flac' => ContentType('audio', 'flac'),
  '.mp3' => ContentType('audio', 'mpeg'),
  '.wav' => ContentType('audio', 'wav'),
  _ => ContentType.binary,
};

/// RFC 5987 value: percent-encode everything but the unreserved set.
String _rfc5987(String s) => Uri.encodeComponent(s).replaceAllMapped(
  RegExp(r"[!'()*]"),
  (m) => '%${m[0]!.codeUnitAt(0).toRadixString(16).toUpperCase()}',
);
