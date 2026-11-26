import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'mac.dart';
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
  UiServer(this.server, {this.port = defaultUiPort, this.log, this.onQuit});

  final CompanionServer server;
  int port;
  final void Function(String)? log;

  /// "Quit" on the page: stop the whole program.
  final void Function()? onQuit;
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
      ..headers.set(
        'content-type',
        type.contains('json') ? '$type; charset=utf-8' : type,
      )
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

  Future<void> _bad(HttpRequest req, String message, [int status = 400]) =>
      _send(req, status, 'application/json', jsonEncode({'error': message}));

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
          return _send(
            req,
            404,
            'application/json',
            '{"error":"Unknown file"}',
          );
        }
        await _open(['-R', p]);
        return _ok(req);
      case '/api/upload':
        return await _upload(req);
      case '/api/quit':
        await _ok(req);
        onQuit?.call();
        return;
      case '/api/notify':
        final b = await _body(req);
        server.settings.set('notify', b['on'] == true);
        server.note('Notifications ${b['on'] == true ? 'on' : 'off'}');
        return _ok(req);
      case '/api/login-item':
        final b = await _body(req);
        await setLoginItem(b['on'] == true);
        server.note(
          b['on'] == true ? 'Will start at login' : 'Won\'t start at login',
        );
        return _ok(req);
      case '/api/choose-receive-dir':
        final d = await chooseFolder(prompt: 'Save tracks from the phone in');
        if (d != null) {
          server.settings.set('receive_dir', d);
          server.note('Tracks from the phone now go to $d');
        }
        return _ok(req, {'path': d});
      case '/api/choose-send':
        final picked = await chooseFiles(
          prompt: 'Send to the phone',
          extensions: const [
            'flac',
            'mp3',
            'm4a',
            'aac',
            'wav',
            'aif',
            'aiff',
            'ogg',
            'opus',
          ],
        );
        if (picked.isNotEmpty) {
          final b = await _body(req);
          unawaited(
            server.queueTracks(
              [
                for (final p in picked) {'path': p},
              ],
              quality: '${b['quality'] ?? server.settings.quality}',
              precomputeDsp: b['dsp'] != false,
            ),
          );
        }
        return _ok(req, {'queued': picked.length, 'native': Platform.isMacOS});
      // ── phone app updates ──
      case '/api/update/choose':
        final picked = await chooseFiles(
          prompt: 'Choose the Mai An APK to offer to the phone',
          extensions: const ['apk'],
          multiple: false,
        );
        if (picked.isEmpty) return _ok(req, {'picked': false});
        return await _publish(req, picked.first);
      case '/api/update/upload':
        final tmp = File('${server.home.tmp.path}/upload_${pid}.apk');
        try {
          await saveStream(req, tmp);
          return await _publish(
            req,
            tmp.path,
            name: req.uri.queryParameters['name'],
            source: 'dropped on the page',
          );
        } finally {
          try {
            tmp.deleteSync();
          } catch (_) {}
        }
      case '/api/update/publish':
        // `maian publish`: a path on this Mac.
        final b = await _body(req);
        final path = '${b['path'] ?? ''}';
        if (!path.startsWith('/') || !File(path).existsSync()) {
          return _send(
            req,
            400,
            'application/json',
            '{"error":"No such file"}',
          );
        }
        return await _publish(req, path, source: 'maian publish');
      case '/api/update/withdraw':
        server.updates.withdraw();
        return _ok(req);
      case '/api/update/auto':
        final b = await _body(req);
        server.updates.setAuto(b['on'] == true);
        return _ok(req);
      case '/api/update/watch-dir':
        final b = await _body(req);
        if (b['clear'] == true) {
          server.updates.setWatchDir(null);
          return _ok(req);
        }
        final d = await chooseFolder(
          prompt: 'Folder with app-release.apk (flutter build apk)',
        );
        if (d != null) server.updates.setWatchDir(d);
        return _ok(req, {'path': d});
      // ── release notes ──
      case '/api/update/draft-notes':
        final b = await _body(req);
        server.updates.setDraftNotes('${b['text'] ?? ''}');
        return _ok(req);
      case '/api/update/notes':
        final b = await _body(req);
        try {
          server.updates.setNotes('${b['text'] ?? ''}');
        } on FormatException catch (e) {
          return _bad(req, e.message);
        }
        return _ok(req);
      case '/api/update/git-notes':
        try {
          return _ok(req, {'text': await server.builder.gitNotes()});
        } on FormatException catch (e) {
          return _bad(req, e.message);
        }
      // ── problem reports ──
      case '/api/report/text':
        final b = await _body(req);
        final t = server.reports.textOf('${b['id']}');
        if (t == null) return _bad(req, 'Unknown report', 404);
        return _ok(req, {'text': t});
      case '/api/report/reveal':
        final b = await _body(req);
        final p = server.reports.pathOf('${b['id']}');
        if (p == null) return _bad(req, 'Unknown report', 404);
        await _open(['-R', p]);
        return _ok(req);
      case '/api/report/delete':
        final b = await _body(req);
        server.reports.delete('${b['id']}');
        server.changed();
        return _ok(req);
      case '/api/report/clear':
        server.reports.clear();
        server.changed();
        return _ok(req);
      case '/api/report/seen':
        server.reports.markSeen();
        server.changed();
        return _ok(req);
      // ── background services ──
      case '/api/service':
        final b = await _body(req);
        try {
          await server.setService('${b['name']}', b['on'] == true);
        } on FormatException catch (e) {
          return _send(
            req,
            400,
            'application/json',
            jsonEncode({'error': e.message}),
          );
        }
        return _ok(req);
      // ── building the phone app ──
      case '/api/build/start':
        if (server.builder.running) return _ok(req, {'started': false});
        unawaited(
          server.builder.build().then((_) {
            final b = server.builder;
            if (b.phase == 'done') {
              server.notify(
                'Phone app built',
                '${b.built} is offered to the phone',
              );
            } else if (b.phase == 'failed') {
              server.notify('Build failed', b.error);
            }
          }),
        );
        return _ok(req, {'started': true});
      case '/api/build/cancel':
        server.builder.cancel();
        return _ok(req);
      case '/api/build/option':
        final b = await _body(req);
        try {
          server.builder.setOption('${b['name']}', b['on'] == true);
        } on FormatException catch (e) {
          return _send(
            req,
            400,
            'application/json',
            jsonEncode({'error': e.message}),
          );
        }
        return _ok(req);
      case '/api/build/project':
        final d = await chooseFolder(
          prompt: 'The Mai An Flutter project (the folder with pubspec.yaml)',
        );
        if (d == null) return _ok(req, {'path': null});
        if (!File('$d/pubspec.yaml').existsSync()) {
          return _send(
            req,
            400,
            'application/json',
            jsonEncode({'error': 'No pubspec.yaml in $d'}),
          );
        }
        server.builder.setProject(d);
        server.note('Builds come from $d');
        return _ok(req, {'path': d});
      case '/api/update/reveal':
        if (server.updates.current == null) return _ok(req);
        await _open(['-R', server.updates.apk.path]);
        return _ok(req);
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

  Future<void> _publish(
    HttpRequest req,
    String path, {
    String? name,
    String source = 'chosen',
  }) async {
    try {
      final meta = await server.updates.publish(path, source: source);
      if (name != null && name.isNotEmpty) {
        server.updates.current?['file_name'] = _cleanName(name);
      }
      return _ok(req, {'update': meta});
    } on FormatException catch (e) {
      return _send(
        req,
        400,
        'application/json',
        jsonEncode({'error': 'Not an Android app: ${e.message}'}),
      );
    }
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
    req.response
        .detachSocket(writeHeaders: false)
        .then((s) => s.destroy(), onError: (_) {});
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
