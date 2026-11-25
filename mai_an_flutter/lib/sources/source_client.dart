import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'downloadable.dart';

/// Port of `StreamripApp/utils/client.py`.
const kDefaultUserAgent =
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:83.0) Gecko/20100101 Firefox/83.0';

typedef ProgressStageCallback = void Function(String stage, String detail);

/// Minimal sleep-based rate limiter matching Python's `SimpleLimiter`.
class SimpleRateLimiter {
  SimpleRateLimiter(
    int requestsPerMin, {
    DateTime Function()? clock,
    Future<void> Function(Duration)? sleep,
  }) : delay = requestsPerMin > 0
           ? Duration(microseconds: (60000000 / requestsPerMin).round())
           : Duration.zero,
       _clock = clock ?? DateTime.now,
       _sleep = sleep ?? Future<void>.delayed;

  final Duration delay;
  final DateTime Function() _clock;
  final Future<void> Function(Duration) _sleep;

  DateTime? _lastRequest;
  Future<void> _chain = Future<void>.value();

  Future<T> run<T>(Future<T> Function() action) {
    if (delay == Duration.zero) return action();
    final completer = Completer<void>();
    final prev = _chain;
    _chain = completer.future;
    return () async {
      try {
        await prev;
        final now = _clock();
        if (_lastRequest != null) {
          final elapsed = now.difference(_lastRequest!);
          if (elapsed < delay) {
            await _sleep(delay - elapsed);
          }
        }
        _lastRequest = _clock();
      } finally {
        completer.complete();
      }
      return await action();
    }();
  }
}

/// Response returned by [SourceHttpClient] for buffered requests.
class SourceHttpResponse {
  const SourceHttpResponse({
    required this.statusCode,
    required this.headers,
    required this.bodyBytes,
  });

  final int statusCode;
  final Map<String, String> headers;
  final List<int> bodyBytes;

  String get bodyText => utf8.decode(bodyBytes, allowMalformed: true);

  Object? decodeJson() => jsonDecode(bodyText);

  String? header(String name) {
    final lower = name.toLowerCase();
    for (final e in headers.entries) {
      if (e.key.toLowerCase() == lower) return e.value;
    }
    return null;
  }
}

/// Streaming response returned by [SourceHttpClient.openStream].
class SourceStreamResponse {
  const SourceStreamResponse({
    required this.statusCode,
    required this.headers,
    required this.stream,
  });

  final int statusCode;
  final Map<String, String> headers;
  final Stream<List<int>> stream;

  int get contentLength => int.tryParse(header('content-length') ?? '') ?? 0;

  String? header(String name) {
    final lower = name.toLowerCase();
    for (final e in headers.entries) {
      if (e.key.toLowerCase() == lower) return e.value;
    }
    return null;
  }
}

/// Injectable HTTP transport so Qobuz, Deezer, and [Downloadable] can run
/// against `dart:io` in production and deterministic fakes in tests.
abstract class SourceHttpClient {
  Future<SourceHttpResponse> request(
    String method,
    Uri uri, {
    Map<String, String>? headers,
    List<int>? body,
    Duration? timeout,
    bool followRedirects = true,
  });

  Future<SourceStreamResponse> openStream(
    Uri uri, {
    Map<String, String>? headers,
    Duration? timeout,
    bool followRedirects = true,
  });

  void close();
}

/// `dart:io` [HttpClient]-backed [SourceHttpClient] with optional SSL toggle
/// and session-scoped cookie handling.
class IoSourceHttpClient implements SourceHttpClient {
  IoSourceHttpClient({
    bool verifySsl = true,
    Map<String, String>? defaultHeaders,
    HttpClient Function()? clientFactory,
  }) : _defaultHeaders = {'User-Agent': kDefaultUserAgent, ...?defaultHeaders} {
    _client = clientFactory?.call() ?? HttpClient();
    if (!verifySsl) {
      _client.badCertificateCallback = (_, _, _) => true;
    }
    _client.connectionTimeout = const Duration(seconds: 30);
  }

  late final HttpClient _client;
  final Map<String, String> _defaultHeaders;
  final Map<String, Cookie> _cookies = {};
  bool _closed = false;

  bool get isClosed => _closed;

  Map<String, String> get defaultHeaders => _defaultHeaders;

  void setCookie(Cookie cookie) {
    _cookies[cookie.name] = cookie;
  }

  String? getCookieValue(String name) => _cookies[name]?.value;

  void _applyHeaders(HttpClientRequest req, Map<String, String>? headers) {
    final merged = <String, String>{..._defaultHeaders, ...?headers};
    merged.forEach((k, v) => req.headers.set(k, v));
    if (_cookies.isNotEmpty) {
      req.cookies.addAll(_cookies.values);
    }
  }

  void _captureCookies(HttpClientResponse resp) {
    for (final c in resp.cookies) {
      _cookies[c.name] = c;
    }
  }

  static Map<String, String> _flattenHeaders(HttpHeaders headers) {
    final out = <String, String>{};
    headers.forEach((name, values) {
      if (values.isNotEmpty) {
        out[name.toLowerCase()] = values.join(', ');
      }
    });
    return out;
  }

  @override
  Future<SourceHttpResponse> request(
    String method,
    Uri uri, {
    Map<String, String>? headers,
    List<int>? body,
    Duration? timeout,
    bool followRedirects = true,
  }) async {
    Future<SourceHttpResponse> run() async {
      final req = await _client.openUrl(method, uri);
      req.followRedirects = followRedirects;
      _applyHeaders(req, headers);
      if (body != null) {
        req.add(body);
      }
      final resp = await req.close();
      _captureCookies(resp);
      final chunks = <int>[];
      await for (final chunk in resp) {
        chunks.addAll(chunk);
      }
      return SourceHttpResponse(
        statusCode: resp.statusCode,
        headers: _flattenHeaders(resp.headers),
        bodyBytes: chunks,
      );
    }

    return timeout == null ? run() : run().timeout(timeout);
  }

  @override
  Future<SourceStreamResponse> openStream(
    Uri uri, {
    Map<String, String>? headers,
    Duration? timeout,
    bool followRedirects = true,
  }) async {
    Future<SourceStreamResponse> run() async {
      final req = await _client.getUrl(uri);
      req.followRedirects = followRedirects;
      _applyHeaders(req, headers);
      final resp = await req.close();
      _captureCookies(resp);
      return SourceStreamResponse(
        statusCode: resp.statusCode,
        headers: _flattenHeaders(resp.headers),
        stream: resp,
      );
    }

    return timeout == null ? run() : run().timeout(timeout);
  }

  @override
  void close() {
    if (_closed) return;
    _closed = true;
    _client.close(force: true);
  }
}

/// Abstract streaming source client (`utils/client.py` `Client`).
abstract class SourceClient {
  String get source;
  int get maxQuality;
  bool get loggedIn;
  bool get isClosed;

  Future<void> login();

  Future<Map<String, Object?>> getMetadata(
    String itemId,
    String mediaType, {
    int limit = 500,
    int offset = 0,
  });

  Future<List<Map<String, Object?>>> search(
    String mediaType,
    String query, {
    int limit = 500,
    int offset = 0,
    ProgressStageCallback? progressCallback,
  });

  Future<Downloadable> getDownloadable(String itemId, int quality);

  Future<List<int>?> fetchBytes(String url);

  Future<void> close();
}
