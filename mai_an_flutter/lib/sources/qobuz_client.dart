import 'dart:async';
import 'dart:convert';

import 'package:crypto/crypto.dart' as crypto;

import 'downloadable.dart';
import 'source_client.dart';
import 'source_config.dart';
import 'source_exceptions.dart';

/// Port of `StreamripApp/utils/qobuz.py`.
const kQobuzBaseUrl = 'https://www.qobuz.com/api.json/0.2';

const kQobuzFeaturedKeys = <String>{
  'most-streamed',
  'recent-releases',
  'best-sellers',
  'press-awards',
  'ideal-discography',
  'editor-picks',
  'most-featured',
  'qobuzissims',
  'new-releases',
  'new-releases-full',
  'harmonia-mundi',
  'universal-classic',
  'universal-jazz',
  'universal-jeunesse',
  'universal-chanson',
};

const _kQobuzAndroidHeaders = <String, String>{
  'X-Device-Platform': 'android',
  'X-Device-Model': 'Pixel 3',
  'X-Device-Os-Version': '10',
  'X-Device-Manufacturer-Id': 'ffffffff-5783-1f51-ffff-ffffef05ac4a',
  'X-App-Version': '5.16.1.5',
  'User-Agent':
      'Dalvik/2.1.0 (Linux; U; Android 10; Pixel 3 Build/QP1A.190711.020) QobuzMobileAndroid/5.16.1.5-b21041415',
};

/// Scrapes `play.qobuz.com/login` + `bundle.js` for app_id and secrets.
/// Mirrors `QobuzSpoofer`.
class QobuzSpoofer {
  QobuzSpoofer({SourceHttpClient? http}) : _http = http;

  final SourceHttpClient? _http;

  static final RegExp bundleUrlRegex = RegExp(
    r'<script src="(/resources/\d+\.\d+\.\d+-[a-z]\d{3}/bundle\.js)"></script>',
  );
  static final RegExp appIdRegex = RegExp(
    r'production:\{api:\{appId:"(?<app_id>\d{9})",appSecret:"(\w{32})',
  );
  static final RegExp seedTimezoneRegex = RegExp(
    r'[a-z]\.initialSeed\("(?<seed>[\w=]+)",window\.utimezone\.(?<timezone>[a-z]+)\)',
  );

  /// Pure parser for bundle JS text (unit-testable offline).
  static (String appId, List<String> secrets) parseBundle(String bundle) {
    final appMatch = appIdRegex.firstMatch(bundle);
    if (appMatch == null) {
      throw StateError(
        'Could not find app id in bundle (length: ${bundle.length}).',
      );
    }
    final appId = appMatch.namedGroup('app_id')!;

    final order = <String>[];
    final parts = <String, List<String>>{};
    for (final m in seedTimezoneRegex.allMatches(bundle)) {
      final seed = m.namedGroup('seed')!;
      final tz = m.namedGroup('timezone')!;
      if (!parts.containsKey(tz)) order.add(tz);
      parts[tz] = [seed];
    }

    if (order.length > 1) {
      final second = order.removeAt(1);
      order.insert(0, second);
    }

    if (order.isNotEmpty) {
      final tzAlt = order
          .map((t) => t.isEmpty ? '' : '${t[0].toUpperCase()}${t.substring(1)}')
          .join('|');
      final infoExtrasRe = RegExp(
        'name:"\\w+/(?<timezone>$tzAlt)",info:"(?<info>[\\w=]+)",extras:"(?<extras>[\\w=]+)"',
      );
      for (final m in infoExtrasRe.allMatches(bundle)) {
        final tz = m.namedGroup('timezone')!.toLowerCase();
        parts[tz]?.addAll([m.namedGroup('info')!, m.namedGroup('extras')!]);
      }
    }

    final vals = <String>[];
    for (final tz in order) {
      final joined = (parts[tz] ?? const []).join();
      if (joined.length <= 44) continue;
      final trimmed = joined.substring(0, joined.length - 44);
      final decoded = utf8.decode(base64.decode(trimmed));
      if (decoded.isNotEmpty) vals.add(decoded);
    }
    return (appId, vals);
  }

  Future<(String appId, List<String> secrets)> getAppIdAndSecrets() async {
    final owns = _http == null;
    final client = _http ?? IoSourceHttpClient(verifySsl: false);
    try {
      final loginResp = await client.request(
        'GET',
        Uri.parse('https://play.qobuz.com/login'),
        timeout: const Duration(seconds: 30),
      );
      final bundleMatch = bundleUrlRegex.firstMatch(loginResp.bodyText);
      if (bundleMatch == null) {
        throw StateError('Could not find bundle.js url in login page.');
      }
      final bundleResp = await client.request(
        'GET',
        Uri.parse('https://play.qobuz.com${bundleMatch.group(1)}'),
        timeout: const Duration(seconds: 60),
      );
      return parseBundle(bundleResp.bodyText);
    } finally {
      if (owns) client.close();
    }
  }
}

/// Port of `StreamripApp/utils/qobuz.py` `QobuzClient`.
class QobuzClient extends SourceClient {
  QobuzClient(
    this.config, {
    SourceHttpClient? http,
    DateTime Function()? clock,
    Future<void> Function(Duration)? sleep,
  }) : _injectedHttp = http,
       _clock = clock ?? DateTime.now,
       _sleep = sleep ?? Future<void>.delayed,
       rateLimiter = SimpleRateLimiter(
         config.downloads.requestsPerMinute,
         clock: clock,
         sleep: sleep,
       );

  final SourceConfig config;
  final SourceHttpClient? _injectedHttp;
  final DateTime Function() _clock;
  final Future<void> Function(Duration) _sleep;
  final SimpleRateLimiter rateLimiter;

  SourceHttpClient? _http;
  final Map<String, String> _sessionHeaders = {};
  bool _loggedIn = false;
  bool _closed = false;
  bool guestMode = false;
  String appId = kQobuzDefaultAppId;
  String? secret;

  @override
  String get source => 'qobuz';

  @override
  int get maxQuality => 4;

  @override
  bool get loggedIn => _loggedIn;

  @override
  bool get isClosed => _closed;

  SourceHttpClient get _client {
    final c = _http;
    if (c == null) {
      throw StateError('QobuzClient used before login()');
    }
    return c;
  }

  /// Map streamrip quality `1..4` to Qobuz `format_id` `(5, 6, 7, 27)`.
  static int formatIdForQuality(int quality) {
    const map = [5, 6, 7, 27];
    if (quality < 1 || quality > 4) {
      throw RangeError.range(quality, 1, 4, 'quality');
    }
    return map[quality - 1];
  }

  /// Compute OrpheusDL MD5 signature for an API endpoint & parameter map.
  static String signApiParams(
    String endpoint,
    Map<String, String> params,
    String unixTs,
    String secret,
  ) {
    final buf = StringBuffer(endpoint.replaceAll('/', ''));
    final keys = params.keys.toList()..sort();
    for (final key in keys) {
      if (key == 'app_id' || key == 'user_auth_token') continue;
      buf
        ..write(key)
        ..write(params[key]);
    }
    buf
      ..write(unixTs)
      ..write(secret);
    return crypto.md5.convert(utf8.encode(buf.toString())).toString();
  }

  /// Compute MD5 signature for `track/getFileUrl`.
  static String signFileUrl({
    required String trackId,
    required int formatId,
    required String unixTs,
    required String secret,
  }) {
    final raw =
        'trackgetFileUrlformat_id${formatId}intentstreamtrack_id$trackId$unixTs$secret';
    return crypto.md5.convert(utf8.encode(raw)).toString();
  }

  static bool isMd5Hex(String value) =>
      value.length == 32 && RegExp(r'^[0-9a-fA-F]{32}$').hasMatch(value);

  static Map<String, Object?> redactAuthPayload(Map<String, Object?> payload) {
    final out = Map<String, Object?>.of(payload);
    for (final key in const ['password', 'user_auth_token']) {
      final v = out[key];
      if (v != null && '$v'.isNotEmpty) {
        out[key] = '***REDACTED***';
      }
    }
    return out;
  }

  /// Map Qobuz `mime_type` to a file extension, falling back on quality.
  static String extensionForMimeType(String? mimeType, int quality) {
    final lower = (mimeType ?? '').toLowerCase();
    if (lower.contains('flac')) return 'flac';
    if (lower.contains('mpeg') || lower.contains('mp3')) return 'mp3';
    if (lower.contains('mp4') || lower.contains('aac')) return 'm4a';
    return quality > 1 ? 'flac' : 'mp3';
  }

  /// Turn Qobuz CamelCase restriction code (e.g. `TrackNotStreamable`) into a
  /// readable sentence (`Track not streamable.`).
  static String formatRestrictionCode(String code) {
    final words = [
      for (final m in RegExp(r'([A-Z][a-z]+)').allMatches(code)) m.group(1)!,
    ];
    if (words.isEmpty) return '$code.';
    if (words.length == 1) return '${words.first}.';
    return '${words.first} ${words.skip(1).map((w) => w.toLowerCase()).join(' ')}.';
  }

  @override
  Future<void> login() async {
    _http ??=
        _injectedHttp ??
        IoSourceHttpClient(verifySsl: config.downloads.verifySsl);
    _closed = false;

    final c = config.qobuz;
    appId = c.effectiveAppId;
    secret = c.effectiveSecret;

    if (!c.hasCredentials) {
      _sessionHeaders.remove('X-User-Auth-Token');
      guestMode = true;
      _loggedIn = true;
      return;
    }

    if (_loggedIn) {
      throw StateError('Already logged in');
    }

    _sessionHeaders
      ..addAll(_kQobuzAndroidHeaders)
      ..['X-App-Id'] = appId;

    final Map<String, String> params;
    final String endpoint;
    if (c.useAuthToken) {
      _sessionHeaders['X-User-Auth-Token'] = c.passwordOrToken;
      params = {
        'user_auth_token': c.passwordOrToken,
        'app_id': appId,
      };
      endpoint = 'user/get';
    } else {
      var password = c.passwordOrToken;
      if (password.isNotEmpty && !isMd5Hex(password)) {
        password = crypto.md5.convert(utf8.encode(password)).toString();
      }
      params = {
        'username': c.emailOrUserid,
        'password': password,
        'extra': 'partner',
        'app_id': appId,
      };
      endpoint = 'user/login';
    }

    final (status, resp) = await _apiRequest(endpoint, params);
    if (status == 200 &&
        (resp['user'] != null || endpoint == 'user/get')) {
      final uat = resp['user_auth_token'] is String &&
              (resp['user_auth_token']! as String).isNotEmpty
          ? resp['user_auth_token']! as String
          : c.passwordOrToken;
      if (uat.isNotEmpty) {
        _sessionHeaders['X-User-Auth-Token'] = uat;
      }
      secret = c.effectiveSecret;
      guestMode = false;
      _loggedIn = true;
      return;
    }

    // Fallback to guest signed catalog mode for searches.
    _sessionHeaders.remove('X-User-Auth-Token');
    secret = kQobuzDefaultSecret;
    guestMode = true;
    _loggedIn = true;
  }

  @override
  Future<Map<String, Object?>> getMetadata(
    String itemId,
    String mediaType, {
    int limit = 500,
    int offset = 0,
  }) async {
    if (mediaType == 'label') return getLabel(itemId);

    final params = <String, String>{
      'app_id': config.qobuz.appId.isNotEmpty ? config.qobuz.appId : appId,
      '${mediaType}_id': itemId,
      'limit': '$limit',
      'offset': '$offset',
    };
    const extras = {
      'artist': 'albums',
      'playlist': 'tracks',
      'label': 'albums',
    };
    if (extras.containsKey(mediaType)) {
      params['extra'] = extras[mediaType]!;
    }

    final (status, resp) = await _apiRequest('$mediaType/get', params);
    if (status != 200) {
      throw NonStreamableError(
        'Error fetching metadata. Message: "${resp['message'] ?? ''}"',
      );
    }
    return resp;
  }

  Future<Map<String, Object?>> getLabel(String labelId) async {
    const pageLimit = 500;
    final aid = config.qobuz.appId.isNotEmpty ? config.qobuz.appId : appId;
    final (status, labelResp) = await _apiRequest('label/get', {
      'app_id': aid,
      'label_id': labelId,
      'limit': '$pageLimit',
      'offset': '0',
      'extra': 'albums',
    });
    if (status != 200) {
      throw NonStreamableError('Status: $status, Response: $labelResp');
    }
    final albumsCount = (labelResp['albums_count'] as num?)?.toInt() ?? 0;
    if (albumsCount <= pageLimit) return labelResp;

    final futures = <Future<(int, Map<String, Object?>)>>[];
    for (var off = pageLimit; off < albumsCount; off += pageLimit) {
      futures.add(
        _apiRequest('label/get', {
          'app_id': aid,
          'label_id': labelId,
          'limit': '$pageLimit',
          'offset': '$off',
          'extra': 'albums',
        }),
      );
    }
    final results = await Future.wait(futures);
    final albumsMap = labelResp['albums'];
    final items = albumsMap is Map && albumsMap['items'] is List
        ? List<Object?>.from(albumsMap['items']! as List)
        : <Object?>[];
    for (final (st, resp) in results) {
      if (st != 200) {
        throw NonStreamableError('Status: $st, Response: $resp');
      }
      final pageAlbums = resp['albums'];
      if (pageAlbums is Map && pageAlbums['items'] is List) {
        items.addAll(pageAlbums['items']! as List);
      }
    }
    if (albumsMap is Map) {
      albumsMap['items'] = items;
    }
    return labelResp;
  }

  @override
  Future<List<Map<String, Object?>>> search(
    String mediaType,
    String query, {
    int limit = 500,
    int offset = 0,
    ProgressStageCallback? progressCallback,
  }) async {
    const allowed = {'artist', 'album', 'track', 'playlist'};
    if (!allowed.contains(mediaType)) {
      throw ArgumentError('$mediaType not available for search on qobuz');
    }
    return _paginate(
      'catalog/search',
      {'query': query, 'type': '${mediaType}s'},
      limit: limit,
      offset: offset,
      progressCallback: progressCallback,
    );
  }

  Future<List<Map<String, Object?>>> getFeatured(
    String query, {
    int limit = 500,
  }) async {
    if (!kQobuzFeaturedKeys.contains(query)) {
      throw ArgumentError('query "$query" is invalid.');
    }
    return _paginate('album/getFeatured', {'type': query}, limit: limit);
  }

  Future<List<Map<String, Object?>>> getUserFavorites(
    String mediaType, {
    int limit = 500,
  }) async {
    if (!const {'track', 'artist', 'album'}.contains(mediaType)) {
      throw ArgumentError('Invalid media type $mediaType');
    }
    return _paginate(
      'favorite/getUserFavorites',
      {'type': '${mediaType}s'},
      limit: limit,
    );
  }

  Future<List<Map<String, Object?>>> getUserPlaylists({
    int limit = 500,
  }) => _paginate('playlist/getUserPlaylists', {}, limit: limit);

  @override
  Future<Downloadable> getDownloadable(String itemId, int quality) async {
    if (secret == null || !_loggedIn || quality < 1 || quality > 4) {
      throw StateError(
        'QobuzClient.getDownloadable requires loggedIn, secret, and quality 1..4',
      );
    }
    final (status, respJson) = await _requestFileUrl(itemId, quality, secret!);
    if (status != 200) {
      throw NonStreamableError('Status: $status, Response: $respJson');
    }
    final streamUrl = respJson['url'];
    if (streamUrl is! String || streamUrl.isEmpty) {
      final restrictions = respJson['restrictions'];
      if (restrictions is List && restrictions.isNotEmpty) {
        final first = restrictions.first;
        if (first is Map && first['code'] is String) {
          throw NonStreamableError(
            formatRestrictionCode(first['code']! as String),
          );
        }
      }
      throw const NonStreamableError();
    }

    final ext = extensionForMimeType(respJson['mime_type'] as String?, quality);
    return BasicDownloadable(
      http: _client,
      url: streamUrl,
      extension: ext,
      source: 'qobuz',
    );
  }

  Future<(int, Map<String, Object?>)> _requestFileUrl(
    String trackId,
    int quality,
    String appSecret,
  ) {
    final formatId = formatIdForQuality(quality);
    final unixTs = (_clock().millisecondsSinceEpoch / 1000.0).toString();
    final sig = signFileUrl(
      trackId: trackId,
      formatId: formatId,
      unixTs: unixTs,
      secret: appSecret,
    );
    return _apiRequest('track/getFileUrl', {
      'app_id': appId,
      'request_ts': unixTs,
      'request_sig': sig,
      'track_id': trackId,
      'format_id': '$formatId',
      'intent': 'stream',
    });
  }

  Future<List<Map<String, Object?>>> _paginate(
    String endpoint,
    Map<String, String> baseParams, {
    int? limit = 500,
    int offset = 0,
    ProgressStageCallback? progressCallback,
  }) async {
    final params = Map<String, String>.of(baseParams);
    if (limit != null) params['limit'] = '$limit';
    params['offset'] = '$offset';
    final aid = appId.isNotEmpty ? appId : config.qobuz.appId;
    if (aid.isNotEmpty) params['app_id'] = aid;

    final (status, page) = await _apiRequest(
      endpoint,
      params,
      progressCallback: progressCallback,
    );
    if (status != 200) {
      throw Exception('Status: $status, Response: $page');
    }

    final key = params['type'] ?? '${endpoint.split('/').first}s';
    final itemsObj = page[key];
    final itemsMap = itemsObj is Map ? itemsObj : const <Object?, Object?>{};
    var total = (itemsMap['total'] as num?)?.toInt() ?? 0;
    if (limit != null && limit < total) total = limit;
    if (total == 0) return const [];

    final pageLimit = (itemsMap['limit'] as num?)?.toInt() ?? 500;
    var curOffset = (itemsMap['offset'] as num?)?.toInt() ?? 0;
    params['limit'] = '$pageLimit';

    final pages = <Map<String, Object?>>[page];
    final futures = <Future<(int, Map<String, Object?>)>>[];
    while (curOffset + pageLimit < total) {
      curOffset += pageLimit;
      final nextParams = Map<String, String>.of(params)
        ..['offset'] = '$curOffset'
        ..remove('request_sig')
        ..remove('request_ts');
      futures.add(
        _apiRequest(endpoint, nextParams, progressCallback: progressCallback),
      );
    }

    if (futures.isNotEmpty) {
      for (final (st, resp) in await Future.wait(futures)) {
        if (st != 200) throw Exception('Status: $st, Response: $resp');
        pages.add(resp);
      }
    }
    return pages;
  }

  Future<(int, Map<String, Object?>)> _apiRequest(
    String endpoint,
    Map<String, String> inputParams, {
    int retries = 3,
    ProgressStageCallback? progressCallback,
  }) async {
    final params = Map<String, String>.of(inputParams);
    if (guestMode) {
      params.remove('user_auth_token');
      _sessionHeaders.remove('X-User-Auth-Token');
    }

    if (!params.containsKey('request_sig')) {
      final sec = secret ?? config.qobuz.effectiveSecret;
      final unixTs = '${_clock().millisecondsSinceEpoch ~/ 1000}';
      final sig = signApiParams(endpoint, params, unixTs, sec);
      params['request_ts'] = unixTs;
      params['request_sig'] = sig;
    }

    final uri = Uri.parse('$kQobuzBaseUrl/$endpoint').replace(
      queryParameters: params,
    );

    for (var attempt = 0; attempt < retries; attempt++) {
      try {
        return await rateLimiter.run(() async {
          progressCallback?.call(
            'HTTP Request',
            'Waiting for response from Qobuz server...',
          );
          final resp = await _client.request(
            'GET',
            uri,
            headers: _sessionHeaders,
          );
          progressCallback?.call(
            'Data Streaming',
            'Streaming search results data...',
          );
          try {
            final decoded = resp.decodeJson();
            if (decoded is Map) {
              return (
                resp.statusCode,
                {for (final e in decoded.entries) '${e.key}': e.value},
              );
            }
            return (resp.statusCode, <String, Object?>{});
          } catch (_) {
            return (resp.statusCode, <String, Object?>{});
          }
        });
      } catch (_) {
        if (attempt == retries - 1) rethrow;
        await _sleep(Duration(seconds: attempt + 1));
      }
    }
    return (0, const <String, Object?>{});
  }

  @override
  Future<List<int>?> fetchBytes(String url) async {
    final resp = await _client.request(
      'GET',
      Uri.parse(url),
      headers: _sessionHeaders,
    );
    return resp.statusCode == 200 ? resp.bodyBytes : null;
  }

  @override
  Future<void> close() async {
    if (_closed) return;
    _closed = true;
    _http?.close();
  }
}
