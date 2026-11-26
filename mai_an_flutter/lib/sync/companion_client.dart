import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';

import '../core/py_text.dart';
import 'multipart.dart';

/// utils/sync/client.py: the phone's half of the Companion protocol, spoken
/// to the Mac's CompanionServer (Flet, on the Mac until phase 8).
///
/// Every call is one short-lived HttpClient, as each Python call is one
/// aiohttp session. Errors keep Python's split: [CompanionAuthError] is its
/// PermissionError (HTTP 401 / wrong PIN), [CompanionHttpError] its
/// RuntimeError, and network failures stay dart:io's own exceptions
/// ([isNetworkError]).

const defaultCompanionPort = 8945;

/// "Not paired" (401) or a wrong PIN (403): Python's PermissionError.
class CompanionAuthError implements Exception {
  const CompanionAuthError(this.message);
  final String message;
  @override
  String toString() => message;
}

/// The server answered, but not with what was asked for: Python's
/// RuntimeError.
class CompanionHttpError implements Exception {
  const CompanionHttpError(this.message, {this.status});
  final String message;
  final int? status;
  @override
  String toString() => message;
}

/// aiohttp.ClientConnectionError or asyncio.TimeoutError: the network, not
/// the server, failed.
bool isNetworkError(Object e) =>
    e is SocketException ||
    e is TimeoutException ||
    e is HttpException ||
    e is HandshakeException;

/// Uploads of whole FLACs over Wi-Fi plus a server-side decode can take well
/// over a minute; a total cap would kill exactly the big files. Bound the
/// connect and each read stall instead (client.py `_TRANSFER_TIMEOUT`).
const _transferConnect = Duration(seconds: 10);
const _transferRead = Duration(seconds: 180);

/// Consecutive per-track network failures before an offload gives up: the
/// Mac went away, and pushing on would just fail the rest one by one.
const maxConsecutiveNetFailures = 3;

/// One `play_counts` feature row, in `update_tracks_features_batch` order.
class FeatureRow {
  const FeatureRow({
    required this.path,
    required this.bpm,
    required this.energy,
    required this.brightness,
    required this.rolloff,
    required this.beatStrength,
    required this.spectralFlatness,
    required this.spectralContrast,
    required this.keyIndex,
    required this.timbre,
    required this.featuresVersion,
  });

  final String path;
  final num? bpm, energy, brightness, rolloff, beatStrength;
  final num? spectralFlatness, spectralContrast;
  final num? keyIndex;
  final Uint8List timbre;
  final int featuresVersion;

  /// client.py `_row`: the wire form (timbre base64) of one track. A missing
  /// key raises, as `r["bpm"]` does. The Mac's features_version, not ours:
  /// stamping a newer version on older features would hide them from every
  /// future re-analysis.
  factory FeatureRow.fromWire(
    String path,
    Map<String, Object?> r,
    int fallbackVersion,
  ) {
    num? need(String k) {
      if (!r.containsKey(k)) throw FormatException('missing $k');
      return r[k] as num?;
    }

    final b64 = r['timbre_b64'];
    final v = r['features_version'];
    return FeatureRow(
      path: path,
      bpm: need('bpm'),
      energy: need('energy'),
      brightness: need('brightness'),
      rolloff: need('rolloff'),
      beatStrength: need('beat_strength'),
      spectralFlatness: need('spectral_flatness'),
      spectralContrast: need('spectral_contrast'),
      keyIndex: need('key_index'),
      timbre: b64 is String && b64.isNotEmpty
          ? base64.decode(b64)
          : Uint8List(0),
      featuresVersion: v is num && v != 0 ? v.toInt() : fallbackVersion,
    );
  }
}

typedef OffloadResult = ({int analysed, int failed, int total});

class CompanionClient {
  CompanionClient(
    this.host, {
    this.port = defaultCompanionPort,
    this.authToken,
    this.deviceName = 'Android Phone',
  });

  final String host;
  final int port;
  String? authToken;
  final String deviceName;

  String get baseUrl => 'http://$host:$port';

  /// This app's own build, sent with every request so the Mac shows which
  /// version the phone runs ("0.3.0+4", the package name). Set once at
  /// startup by the app updater.
  static Map<String, String> appHeaders = const {};

  Map<String, String> headers() => {
    'X-Device-Name': pyQuote(deviceName),
    ...appHeaders,
    if (authToken case final t? when t.isNotEmpty) 'Authorization': 'Bearer $t',
  };

  /// The Mac's owner switched analysis off on the control page: stop the
  /// whole run rather than failing every track.
  static void _stopIfOffloadOff(_Reply r) {
    if (r.status != 503) return;
    var msg = 'Analysis is turned off on the Mac';
    try {
      final j = r.json();
      if (j['error'] case final String e) msg = e;
    } catch (_) {}
    throw CompanionHttpError(msg, status: 503);
  }

  static void _check(_Reply r, String what) {
    if (r.status == 401) {
      throw CompanionAuthError('$what: not paired (HTTP 401)');
    }
    if (r.status != 200) {
      throw CompanionHttpError('$what: HTTP ${r.status}', status: r.status);
    }
  }

  /// Download URL for a pending transfer, addressed the way WE reach the
  /// Mac. The server's own stream_url names its LAN IP, which a phone on the
  /// USB tunnel (127.0.0.1) or another subnet cannot route to.
  String streamUrl(Map<String, Object?> transfer) {
    var path = transfer['stream_path'] as String? ?? '';
    if (path.isEmpty) {
      final url = transfer['stream_url'] as String? ?? '';
      final i = url.indexOf('/api/v1/');
      path = i >= 0 ? url.substring(i) : '';
    }
    var url = '$baseUrl$path';
    if (authToken case final t? when t.isNotEmpty) {
      url += '${url.contains('?') ? '&' : '?'}token=${pyQuote(t)}';
    }
    return url;
  }

  /// Fast HTTP handshake probe. The reply's `authorized` field says whether
  /// our token is still accepted.
  Future<Map<String, Object?>> ping({
    Duration timeout = const Duration(milliseconds: 1500),
  }) async {
    final r = await _request('GET', '/api/v1/ping', total: timeout);
    if (r.status == 200) return r.json();
    throw CompanionHttpError(
      'Server returned status ${r.status}',
      status: r.status,
    );
  }

  /// Pair with the Mac's 4-digit PIN for a persistent token.
  Future<String?> pair(String pin, {String? deviceName}) async {
    final r = await _request(
      'POST',
      '/api/v1/pair',
      total: const Duration(seconds: 5),
      json: {'pin': pin, 'device_name': deviceName ?? this.deviceName},
      auth: false,
    );
    if (r.status == 200) {
      authToken = r.json()['token'] as String?;
      return authToken;
    }
    if (r.status == 403) throw const CompanionAuthError('Incorrect PIN');
    throw CompanionHttpError(
      'Pairing failed: HTTP ${r.status}',
      status: r.status,
    );
  }

  /// The DSP offload: tracks the Mac's cache already knows come back at
  /// once (no audio sent); the rest are uploaded one by one and their
  /// features stored in batches of 10 through [persist]. Whatever was
  /// computed is stored even when the run aborts.
  Future<OffloadResult> offloadDsp(
    List<String> missingPaths, {
    required Future<void> Function(List<FeatureRow> rows) persist,
    void Function(int done, int total, String current, double rate)? progress,
    bool Function()? cancelled,
    int featuresVersion = 5,
  }) async {
    final total = missingPaths.length;
    if (total == 0) return (analysed: 0, failed: 0, total: 0);
    var analysed = 0, failed = 0;
    final clock = Stopwatch()..start();

    void notify(String current) {
      if (progress == null) return;
      final elapsed = clock.elapsedMicroseconds / 1e6;
      final rate = analysed / (elapsed < 0.001 ? 0.001 : elapsed);
      try {
        progress(analysed + failed, total, current, rate);
      } catch (_) {}
    }

    // 1. Cache probe.
    var uncached = List<String>.of(missingPaths);
    try {
      final r = await _request(
        'POST',
        '/api/v1/dsp/match',
        json: {
          'tracks': [
            for (final p in missingPaths) {'path': p},
          ],
        },
      );
      _stopIfOffloadOff(r);
      _check(r, 'Cache probe');
      final data = r.json();
      final cached = (data['cached'] as List?) ?? const [];
      if (data.containsKey('missing')) {
        uncached = [for (final p in data['missing']! as List) p as String];
      }
      if (cached.isNotEmpty) {
        final batch = [
          for (final c in cached.cast<Map<String, Object?>>())
            FeatureRow.fromWire(c['path']! as String, c, featuresVersion),
        ];
        await persist(batch);
        analysed += batch.length;
        notify('Loaded from Mac cache');
      }
    } on CompanionAuthError {
      rethrow;
    } on CompanionHttpError catch (e) {
      if (e.status == 503) rethrow;
      debugPrint('Cache probe failed ($e), falling back to full extraction');
    } catch (e) {
      debugPrint('Cache probe failed ($e), falling back to full extraction');
    }

    // 2. Upload and extract the rest.
    final chunk = <FeatureRow>[];
    var netFailures = 0;
    try {
      for (final path in uncached) {
        if (cancelled?.call() ?? false) break;
        final name = _basename(path);
        if (!FileSystemEntity.isFileSync(path)) {
          failed++;
          notify(name);
          continue;
        }
        try {
          final r = await _upload(
            '/api/v1/dsp/extract',
            // The path as a form field, not a header: header values must be
            // latin-1, and Greek paths are not.
            MultipartBody([
              TextPart('path', path),
              FilePart('file', File(path), filename: name),
            ]),
          );
          if (r.status == 401) _check(r, 'Offload');
          _stopIfOffloadOff(r);
          if (r.status == 200) {
            chunk.add(FeatureRow.fromWire(path, r.json(), featuresVersion));
            analysed++;
          } else {
            failed++;
            debugPrint('Extract failed for $path: HTTP ${r.status}');
          }
          netFailures = 0;
        } on CompanionAuthError {
          rethrow;
        } on CompanionHttpError catch (e) {
          if (e.status == 503) rethrow;
          failed++;
        } catch (e) {
          failed++;
          debugPrint('Error offloading $path: $e');
          if (isNetworkError(e) && ++netFailures >= maxConsecutiveNetFailures) {
            throw const SocketException('Lost connection to Mac');
          }
        }
        if (chunk.length >= 10) {
          await persist(List.of(chunk));
          chunk.clear();
        }
        notify(name);
      }
    } finally {
      if (chunk.isNotEmpty) {
        await persist(List.of(chunk));
        chunk.clear();
      }
    }
    return (analysed: analysed, failed: failed, total: total);
  }

  /// Upload an exported state bundle (.zip) to the Mac.
  Future<Map<String, Object?>> uploadStateBundle(String bundlePath) async {
    final f = File(bundlePath);
    if (!f.existsSync()) {
      throw PathNotFoundException(
        bundlePath,
        const OSError('Bundle not found'),
      );
    }
    final r = await _upload(
      '/api/v1/bundle/upload',
      MultipartBody([
        // aiohttp guesses the type from the name when none is given.
        FilePart(
          'file',
          f,
          filename: _basename(bundlePath),
          contentType: 'application/zip',
        ),
      ]),
    );
    _check(r, 'Upload');
    return r.json();
  }

  /// Send one library track to the Mac ("Send to Mac"). [meta] carries
  /// artist/album (the Mac files it under them), optional `dsp_features`,
  /// and `remaining`: tracks still to come in this batch.
  Future<Map<String, Object?>> pushTrack(
    String path,
    Map<String, Object?> meta,
  ) async {
    if (!FileSystemEntity.isFileSync(path)) {
      throw PathNotFoundException(path, const OSError('No such file'));
    }
    final r = await _upload(
      '/api/v1/tracks/receive',
      MultipartBody([
        TextPart('meta', jsonEncode(meta), contentType: 'application/json'),
        FilePart('file', File(path), filename: _basename(path)),
      ]),
    );
    _check(r, 'Send to Mac');
    return r.json();
  }

  /// Transfers queued on the Mac. Throws on failure: an empty list must mean
  /// "nothing queued", not "the Mac stopped answering" or "not paired".
  Future<List<Map<String, Object?>>> getPendingTransfers() async {
    final r = await _request(
      'GET',
      '/api/v1/transfers/pending',
      total: const Duration(seconds: 5),
    );
    _check(r, 'Pending transfers');
    final list = r.json()['transfers'] as List? ?? const [];
    return [for (final t in list) Map<String, Object?>.from(t as Map)];
  }

  /// Acknowledge received transfers so the Mac drops them from its queue.
  Future<bool> ackTransfers(List<String> ids) async {
    final r = await _request(
      'POST',
      '/api/v1/transfers/ack',
      total: const Duration(seconds: 5),
      json: {'ids': ids},
    );
    return r.status == 200;
  }

  /// Download the app build the Mac offers ([path] from the ping's
  /// `app_update`) to [dest]. Returns the bytes written. Stalls are bounded,
  /// not the total: 30-100 MB over a slow hotspot takes a while.
  Future<int> downloadAppUpdate(
    String path,
    File dest, {
    void Function(int received, int total)? progress,
    bool Function()? cancelled,
  }) async {
    final http = HttpClient()..connectionTimeout = _transferConnect;
    IOSink? out;
    try {
      final req = await http.getUrl(Uri.parse('$baseUrl$path'));
      headers().forEach(req.headers.set);
      final resp = await req.close().timeout(_transferRead);
      if (resp.statusCode != 200) {
        await resp.drain<void>().catchError((_) {});
        _check(_Reply(resp.statusCode, const []), 'App download');
      }
      final total = resp.contentLength;
      var got = 0;
      out = dest.openWrite();
      await for (final chunk in resp.timeout(_transferRead)) {
        if (cancelled?.call() ?? false) {
          throw const CompanionHttpError('Cancelled');
        }
        out.add(chunk);
        got += chunk.length;
        progress?.call(got, total);
      }
      await out.flush();
      return got;
    } finally {
      await out?.close();
      http.close(force: true);
    }
  }

  /// Tell the Mac how the offered build is doing on this phone
  /// (downloading, installing, installed, failed, declined). Best effort.
  Future<void> postAppStatus(String state, {String? error}) async {
    try {
      await _request(
        'POST',
        '/api/v1/app/status',
        total: const Duration(seconds: 4),
        json: {'state': state, 'error': ?error},
      );
    } catch (e) {
      debugPrint('app status report failed: $e');
    }
  }

  /// A problem report (the user's, or a caught error) for the Mac's page.
  Future<void> postReport(Map<String, Object?> report) async {
    final r = await _request(
      'POST',
      '/api/v1/report',
      total: const Duration(seconds: 15),
      json: report,
    );
    _check(r, 'Problem report');
  }

  // ── transport ─────────────────────────────────────────────────────────────

  /// A JSON (or empty) request. [total] bounds the whole exchange, as an
  /// aiohttp `ClientTimeout(total=…)`; without it, the transfer timeouts.
  Future<_Reply> _request(
    String method,
    String path, {
    Duration? total,
    Object? json,
    bool auth = true,
  }) {
    final http = HttpClient()..connectionTimeout = total ?? _transferConnect;
    Future<_Reply> run() async {
      final req = await http.openUrl(method, Uri.parse('$baseUrl$path'));
      if (auth) headers().forEach(req.headers.set);
      if (json != null) {
        final body = utf8.encode(jsonEncode(json));
        req.headers.contentType = ContentType.json;
        req.contentLength = body.length;
        req.add(body);
      }
      return _Reply.read(
        await req.close(),
        total == null ? _transferRead : null,
      );
    }

    final f = total == null ? run() : run().timeout(total);
    return f.whenComplete(() => http.close(force: true));
  }

  Future<_Reply> _upload(String path, MultipartBody body) async {
    final http = HttpClient()..connectionTimeout = _transferConnect;
    try {
      final req = await http.postUrl(Uri.parse('$baseUrl$path'));
      headers().forEach(req.headers.set);
      req.headers.set(HttpHeaders.contentTypeHeader, body.contentType);
      req.contentLength = await body.length();
      await req.addStream(body.stream());
      final resp = await req.close().timeout(_transferRead);
      return await _Reply.read(resp, _transferRead);
    } finally {
      http.close(force: true);
    }
  }
}

class _Reply {
  _Reply(this.status, this.body);
  final int status;
  final List<int> body;

  /// [stall] bounds each wait for the next chunk (aiohttp `sock_read`).
  static Future<_Reply> read(HttpClientResponse resp, Duration? stall) async {
    final chunks = stall == null ? resp : resp.timeout(stall);
    final bytes = BytesBuilder(copy: false);
    await for (final c in chunks) {
      bytes.add(c);
    }
    return _Reply(resp.statusCode, bytes.takeBytes());
  }

  Map<String, Object?> json() {
    final v = jsonDecode(utf8.decode(body));
    if (v is Map<String, Object?>) return v;
    throw const FormatException('expected a JSON object');
  }
}

String _basename(String path) {
  final i = path.lastIndexOf('/');
  return i < 0 ? path : path.substring(i + 1);
}
