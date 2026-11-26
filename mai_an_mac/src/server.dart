import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'beacon.dart';
import 'features.dart';
import 'ffmpeg.dart';
import 'home.dart';
import 'multipart.dart';
import 'net.dart';
import 'shared.dart';
import 'transfers.dart';

const appVersion = '0.1.0';
const defaultPort = 8945;

/// Wrong PINs tolerated before the PIN rotates: a 4-digit PIN falls to a
/// scripted loop in seconds otherwise.
const _maxPinFailures = 5;

/// Tracks the phone may push to us; anything else is refused, so a token
/// never becomes write access for arbitrary files.
const _receivable = {
  '.flac', '.mp3', '.m4a', '.aac', '.alac', '.ogg', '.opus',
  '.wav', '.aif', '.aiff', '.wv', '.ape',
};

/// A phone that has talked to us.
class ClientInfo {
  ClientInfo(this.name, this.ip) : lastSeen = DateTime.now();
  String name;
  String ip;
  DateTime lastSeen;
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
    pool = Pool(dspJobs ?? max(1, Platform.numberOfProcessors - 1));
  }

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
  final _prepPool = Pool(3);

  final clients = <String, ClientInfo>{};
  final offload = OffloadStats();
  List<Map<String, Object?>> received = [];
  int _prepDone = 0, _prepTotal = 0;
  String _prepTitle = '';
  final _prepCancels = <_Cancel>{};

  HttpServer? _http;
  Beacon? _beacon;
  int _pinFailures = 0;
  int _seq = 0;
  int _sleepHolders = 0;
  Process? _caffeinate;
  Timer? _tick;

  /// Fires when anything the UI shows has changed.
  final changes = StreamController<void>.broadcast();
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
    HttpServer? s;
    var p = port;
    for (var i = 0; i < 5 && s == null; i++, p++) {
      try {
        s = await HttpServer.bind(InternetAddress.anyIPv4, p);
      } on SocketException {
        log?.call('Port $p is busy, trying ${p + 1}');
      }
    }
    if (s == null) throw StateError('No free port from $port to ${p - 1}');
    s.autoCompress = false;
    _http = s;
    port = s.port;
    s.listen(_dispatch);
    _beacon = Beacon(
      name: deviceName,
      port: port,
      udp: beaconUdpPort,
      announcePort: beaconAnnouncePort,
      interval: beaconInterval,
      targets: beaconTargets,
      log: log,
    );
    await _beacon!.start();
    // Keeps the UI's "seen 12 s ago" honest and ends a finished offload.
    _tick = Timer.periodic(const Duration(seconds: 5), (_) => _changed());
    return port;
  }

  Future<void> stop() async {
    _tick?.cancel();
    _beacon?.stop();
    await _http?.close(force: true);
    _http = null;
    _sleepHolders = 0;
    _caffeinate?.kill();
    _caffeinate = null;
    await cache.close();
    await changes.close();
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
    if (Platform.isMacOS && _caffeinate == null) {
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
      }
      await _json(req, {'error': 'Not found'}, 404);
    } on FormatException catch (e) {
      await _fail(req, 400, e.message);
    } catch (e) {
      log?.call('Request ${req.method} ${req.uri.path} failed: $e');
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

  void _seen(String name, String? ip) {
    final key = '${ip ?? ''}|$name';
    final c = clients[key];
    if (c == null) {
      clients[key] = ClientInfo(name, ip ?? '');
      log?.call('Phone connected: $name (${ip ?? '?'})');
    } else {
      c.lastSeen = DateTime.now();
    }
    if (settings.lastClient != name) settings.set('last_client', name);
    _changed();
  }

  // ── endpoints ────────────────────────────────────────────────────────────

  Future<void> _ping(HttpRequest req) async {
    final ok = _authorized(req);
    if (ok) _seen(_deviceOf(req), req.connectionInfo?.remoteAddress.address);
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
    });
  }

  Future<void> _pair(HttpRequest req) async {
    final body = await _readJson(req);
    if (body == null) return _json(req, {'error': 'Invalid JSON'}, 400);
    final attempt = '${body['pin'] ?? ''}'.trim();
    final name = '${body['device_name'] ?? 'Android Client'}';
    if (_constEq(attempt, pin)) {
      _pinFailures = 0;
      log?.call('Paired with $name');
      _seen(name, req.connectionInfo?.remoteAddress.address);
      return _json(req, {
        'status': 'paired',
        'token': token,
        'server_name': deviceName,
      });
    }
    log?.call('Wrong PIN from ${req.connectionInfo?.remoteAddress.address}');
    if (++_pinFailures >= _maxPinFailures) {
      log?.call('Too many wrong PINs: new PIN');
      rotatePin();
    }
    await _json(req, {'error': 'Invalid PIN'}, 403);
  }

  Future<void> _dspMatch(HttpRequest req) async {
    if (!await _requireAuth(req)) return;
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
      log?.call(
        'Offload: ${offload.total} tracks, ${cached.length} already cached, '
        '${missing.length} to analyse',
      );
    }
    _changed();
    await _json(req, {'cached': cached, 'missing': missing});
  }

  Future<void> _dspExtract(HttpRequest req) async {
    if (!await _requireAuth(req)) return;
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
          trackPath = (uploadName?.isEmpty ?? true) ? 'unknown.audio' : uploadName!;
        }
        offload.current = _baseName(trackPath);
        offload.started ??= DateTime.now();
        _changed();
        final feats = await pool.run(() => analyseFile(ff, tmp!.path, home.tmp));
        cache.put(trackPath, feats);
        _offloadStep(extracted: true);
        await _json(req, {'status': 'ok', 'path': trackPath, ...feats});
      } catch (e) {
        _offloadStep(extracted: false);
        log?.call('DSP failed for ${_baseName(trackPath)}: $e');
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
      log?.call(
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
        log?.call('Transcode failed for ${_baseName(path)} ($e): sending original');
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
    log?.call('Sent ${_baseName(path)} to ${req.connectionInfo?.remoteAddress.address}');
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
    log?.call('Saved state bundle (${_size(n)}) to ${dest.path}');
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
      _seen(sender, remote);
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
      log?.call(
        'Received ${_baseName(dest)} from $sender${duplicate ? ' (duplicate)' : ''}'
        '${remaining > 0 ? ' ($remaining to go)' : ''}',
      );
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
      removeTransfers([for (final i in (body['ids'] as List? ?? const [])) '$i']);
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
      log?.call('Feature extraction failed for ${_baseName(path)}: $e');
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
        for (final t in todo) _prepOne(t, q, quality, ip, precomputeDsp, cancel),
      ]);
      if (cancel.set) {
        log?.call('Send cancelled');
        return const [];
      }
      final queued = [
        for (final r in results)
          if (r != null) r,
      ];
      queue.addAll(queued);
      log?.call(
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
        log?.call('Pre-encode failed for ${_baseName(path)}: $e');
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
        ? (home.backups.listSync().whereType<File>().toList()
            ..sort(
              (a, b) =>
                  b.lastModifiedSync().compareTo(a.lastModifiedSync()),
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
          },
      ],
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
