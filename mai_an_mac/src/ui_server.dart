import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'multipart.dart';
import 'server.dart';
import 'shared.dart';
import 'ui_page.dart';

const defaultUiPort = 8946;

/// The local control page and its small JSON API, on loopback only: the
/// phone never sees it, and other machines can't reach it.
///
/// A web page in any tab could otherwise POST to localhost, so every request
/// must carry a localhost Host header (defeats DNS rebinding) and every POST
/// the custom `x-maian` header (a cross-site page can't add it without a
/// CORS preflight, which we never answer).
class UiServer {
  UiServer(this.server, {this.port = defaultUiPort, this.log});

  final CompanionServer server;
  int port;
  final void Function(String)? log;
  HttpServer? _http;

  String get url => 'http://localhost:$port';

  Future<int> start() async {
    HttpServer? s;
    var p = port;
    for (var i = 0; i < 5 && s == null; i++, p++) {
      try {
        s = await HttpServer.bind(InternetAddress.loopbackIPv4, p);
      } on SocketException {
        // busy: try the next
      }
    }
    if (s == null) throw StateError('No free UI port from $port');
    _http = s;
    port = s.port;
    s.listen(_dispatch);
    return port;
  }

  Future<void> stop() async {
    await _http?.close(force: true);
    _http = null;
  }

  bool _hostOk(HttpRequest req) {
    final h = req.headers.value('host') ?? '';
    return h == 'localhost:$port' || h == '127.0.0.1:$port';
  }

  Future<void> _dispatch(HttpRequest req) async {
    try {
      if (!_hostOk(req)) return _send(req, 403, 'text/plain', 'Forbidden');
      final path = req.uri.path;
      if (req.method == 'GET') {
        switch (path) {
          case '/':
            return _send(req, 200, 'text/html; charset=utf-8', uiHtml);
          case '/api/state':
            return _send(
              req,
              200,
              'application/json',
              jsonEncode(await server.snapshot()),
            );
          case '/api/events':
            return await _events(req);
        }
        return _send(req, 404, 'text/plain', 'Not found');
      }
      if (req.method != 'POST' || req.headers.value('x-maian') != '1') {
        return _send(req, 403, 'text/plain', 'Forbidden');
      }
      final origin = req.headers.value('origin');
      if (origin != null &&
          origin != 'http://localhost:$port' &&
          origin != 'http://127.0.0.1:$port') {
        return _send(req, 403, 'text/plain', 'Forbidden');
      }
      await _post(req, path);
    } catch (e) {
      log?.call('UI request ${req.uri.path} failed: $e');
      try {
        await _send(req, 500, 'application/json', jsonEncode({'error': '$e'}));
      } catch (_) {
        abortResponse(req);
      }
    }
  }

  Future<void> _send(
    HttpRequest req,
    int status,
    String type,
    String body,
  ) async {
    // Bytes, not write(): a response without a charset is Latin-1, and a
    // Greek file name in the JSON would throw.
    final bytes = utf8.encode(body);
    final r = req.response
      ..statusCode = status
      ..headers.set('content-type', type.contains('json') ? '$type; charset=utf-8' : type)
      ..headers.set('cache-control', 'no-store')
      ..contentLength = bytes.length;
    r.add(bytes);
    await r.close();
  }

  Future<Map<String, Object?>> _body(HttpRequest req) async {
    try {
      final v = jsonDecode(await utf8.decodeStream(req));
      return v is Map<String, Object?> ? v : {};
    } catch (_) {
      return {};
    }
  }

  Future<void> _ok(HttpRequest req, [Map<String, Object?> extra = const {}]) =>
      _send(req, 200, 'application/json', jsonEncode({'ok': true, ...extra}));

  Future<void> _post(HttpRequest req, String path) async {
    switch (path) {
      case '/api/rotate-pin':
        server.rotatePin();
        return _ok(req);
      case '/api/unpair':
        server.resetPairing();
        return _ok(req);
      case '/api/cancel-send':
        server.cancelPreparing();
        return _ok(req);
      case '/api/clear-queue':
        server.clearQueue();
        return _ok(req);
      case '/api/remove':
        final b = await _body(req);
        server.removeTransfers(['${b['id']}']);
        return _ok(req);
      case '/api/quality':
        final b = await _body(req);
        server.settings.set('quality', '${b['quality']}');
        return _ok(req);
      case '/api/open-folder':
        final d = Directory(server.receiveDir)..createSync(recursive: true);
        await _open([d.path]);
        return _ok(req);
      case '/api/reveal':
        final b = await _body(req);
        final p = '${b['path']}';
        // Only files we handed out: this is not a general file opener.
        final known =
            server.received.any((r) => r['path'] == p) ||
            p.startsWith('${server.home.backups.path}/');
        if (!known || !File(p).existsSync()) {
          return _send(req, 404, 'application/json', '{"error":"Unknown file"}');
        }
        await _open(['-R', p]);
        return _ok(req);
      case '/api/upload':
        return await _upload(req);
      case '/api/send':
        final b = await _body(req);
        final paths = [
          for (final p in (b['paths'] as List? ?? const []))
            if (p is String && p.startsWith('/')) p,
        ];
        final missing = [
          for (final p in paths)
            if (!File(p).existsSync()) p,
        ];
        if (paths.isEmpty || missing.isNotEmpty) {
          return _send(
            req,
            400,
            'application/json',
            jsonEncode({
              'error': paths.isEmpty
                  ? 'No files'
                  : 'Not found: ${missing.join(', ')}',
            }),
          );
        }
        final quality = '${b['quality'] ?? server.settings.quality}';
        final dsp = b['dsp'] != false;
        if (b['wait'] == true) {
          final q = await server.queueTracks(
            [
              for (final p in paths) {'path': p},
            ],
            quality: quality,
            precomputeDsp: dsp,
          );
          return _ok(req, {'queued': q.length});
        }
        // The page watches progress over the event stream.
        unawaited(
          server.queueTracks(
            [
              for (final p in paths) {'path': p},
            ],
            quality: quality,
            precomputeDsp: dsp,
          ),
        );
        return _ok(req, {'queued': paths.length});
    }
    return _send(req, 404, 'application/json', '{"error":"Not found"}');
  }

  /// A file dropped on the page: the browser can't give us its path, so it
  /// sends the bytes, and we keep them in the outbox until the phone has them.
  Future<void> _upload(HttpRequest req) async {
    var name = _cleanName(req.uri.queryParameters['name'] ?? '');
    if (name.isEmpty) name = 'track';
    var dest = File('${server.home.outbox.path}/$name');
    var n = 2;
    while (dest.existsSync()) {
      final dot = name.lastIndexOf('.');
      final stem = dot <= 0 ? name : name.substring(0, dot);
      final ext = dot <= 0 ? '' : name.substring(dot);
      dest = File('${server.home.outbox.path}/$stem ($n)$ext');
      n++;
    }
    await saveStream(req, dest);
    return _ok(req, {'path': dest.path});
  }

  String _cleanName(String s) {
    final base = s.split('/').last;
    final c = cleanFilename(base);
    return (c == '.' || c == '..') ? '' : c;
  }

  Future<void> _events(HttpRequest req) async {
    final r = req.response
      ..statusCode = 200
      ..headers.set('content-type', 'text/event-stream; charset=utf-8')
      ..headers.set('cache-control', 'no-store')
      ..bufferOutput = false;
    var closed = false;
    unawaited(r.done.then((_) => closed = true, onError: (_) => closed = true));
    Timer? debounce;
    Future<void> push() async {
      if (closed) return;
      try {
        r.write('data: ${jsonEncode(await server.snapshot())}\n\n');
        await r.flush();
      } catch (e) {
        log?.call('Event stream ended: $e');
        closed = true;
      }
    }

    final sub = server.changes.stream.listen((_) {
      // Coalesce bursts (an offload fires several changes a second).
      debounce ??= Timer(const Duration(milliseconds: 250), () {
        debounce = null;
        push();
      });
    });
    final keepAlive = Timer.periodic(const Duration(seconds: 15), (_) {
      if (closed) return;
      try {
        r.write(': ping\n\n');
      } catch (_) {
        closed = true;
      }
    });
    await push();
    while (!closed) {
      await Future<void>.delayed(const Duration(milliseconds: 500));
    }
    keepAlive.cancel();
    debounce?.cancel();
    await sub.cancel();
    try {
      await r.close();
    } catch (_) {}
  }
}

/// Drop a connection whose response can no longer be completed, so the
/// client sees an error instead of waiting forever.
void abortResponse(HttpRequest req) {
  try {
    req.response.detachSocket(writeHeaders: false).then(
      (s) => s.destroy(),
      onError: (_) {},
    );
  } catch (_) {}
}

/// `open` is macOS's own; elsewhere the UI's "show" buttons simply do nothing.
Future<void> _open(List<String> args) async {
  try {
    if (Platform.isMacOS) await Process.run('open', args);
  } catch (_) {}
}

/// Open [url] in the default browser.
Future<void> openBrowser(String url) => _open([url]);
