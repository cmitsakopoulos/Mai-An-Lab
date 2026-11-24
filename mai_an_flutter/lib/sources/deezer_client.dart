import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'deezer_crypto.dart';
import 'downloadable.dart';
import 'source_client.dart';
import 'source_config.dart';
import 'source_exceptions.dart';

/// Port of `StreamripApp/utils/deezer.py` + the subset of `deezer-py` HTTP
/// endpoints it uses (`gw-light.php`, `api.deezer.com`, `media.deezer.com`).

const _kDeezerUserAgent =
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/79.0.3945.130 Safari/537.36';

class _DeezerWrongLicense implements Exception {
  const _DeezerWrongLicense(this.format);
  final String format;
}

class _DeezerWrongGeolocation implements Exception {
  const _DeezerWrongGeolocation(this.country);
  final String country;
}

class _DeezerApiError implements Exception {
  const _DeezerApiError(this.message);
  final String message;

  @override
  String toString() => 'DeezerApiError: $message';
}

class DeezerClient extends SourceClient {
  DeezerClient(
    this.config, {
    SourceHttpClient? http,
    Future<void> Function(Duration)? sleep,
  }) : _injectedHttp = http,
       _sleep = sleep ?? Future<void>.delayed;

  static const List<(int gwCode, String format)> qualityMap = [
    (9, 'MP3_128'), // quality 0
    (3, 'MP3_320'), // quality 1
    (1, 'FLAC'), // quality 2
  ];

  final SourceConfig config;
  final SourceHttpClient? _injectedHttp;
  final Future<void> Function(Duration) _sleep;

  SourceHttpClient? _http;
  bool _loggedIn = false;
  bool _closed = false;

  String? _gwApiToken;
  String _arlCookie = '';
  final Map<String, String> _headers = {'User-Agent': _kDeezerUserAgent};
  Map<String, Object?> _currentUser = const {};

  @override
  String get source => 'deezer';

  @override
  int get maxQuality => 2;

  @override
  bool get loggedIn => _loggedIn;

  @override
  bool get isClosed => _closed;

  Map<String, Object?> get currentUser => _currentUser;

  SourceHttpClient get _client {
    final c = _http;
    if (c == null) {
      throw StateError('DeezerClient used before login()');
    }
    return c;
  }

  Map<String, String> _requestHeaders({bool withCookie = false}) {
    final h = Map<String, String>.of(_headers);
    if (withCookie && _arlCookie.isNotEmpty) {
      h['Cookie'] = 'arl=$_arlCookie';
    }
    return h;
  }

  @override
  Future<void> login() async {
    _http ??=
        _injectedHttp ??
        IoSourceHttpClient(verifySsl: config.downloads.verifySsl);
    _closed = false;

    final arl = config.deezer.arl.trim();
    if (arl.isEmpty) {
      throw const MissingCredentialsError();
    }
    _arlCookie = arl;
    if (_http case final IoSourceHttpClient io) {
      io.setCookie(
        Cookie('arl', arl)
          ..domain = '.deezer.com'
          ..path = '/'
          ..httpOnly = true,
      );
    }

    final userData = await _gwCall('deezer.getUserData');
    final user = _asMap(userData['USER']);
    final userId = user['USER_ID'];
    if (userData.isEmpty || userId == null || userId == 0 || userId == '0') {
      _loggedIn = false;
      throw const AuthenticationError();
    }

    await _postLogin(userData);
    _loggedIn = true;
  }

  Future<void> _postLogin(Map<String, Object?> userData) async {
    final user = _asMap(userData['USER']);
    final options = _asMap(user['OPTIONS']);
    final setting = _asMap(user['SETTING']);
    final globalSetting = _asMap(setting['global']);
    final multi = _asMap(user['MULTI_ACCOUNT']);
    final isFamily =
        _isTruthy(multi['ENABLED']) && !_isTruthy(multi['IS_SUB_ACCOUNT']);

    Map<String, Object?> buildAccount(Map<String, Object?> acct) => {
      'id': acct['USER_ID'],
      'name': acct['BLOG_NAME'],
      'picture': acct['USER_PICTURE'] ?? '',
      'license_token': options['license_token'],
      'can_stream_hq':
          _isTruthy(options['web_hq']) || _isTruthy(options['mobile_hq']),
      'can_stream_lossless':
          _isTruthy(options['web_lossless']) ||
          _isTruthy(options['mobile_lossless']),
      'country': options['license_country'] ?? '',
      'language': globalSetting['language'] ?? '',
      'loved_tracks': acct['LOVEDTRACKS_ID'],
    };

    final accounts = <Map<String, Object?>>[];
    if (isFamily) {
      final rawChildren = await _gwCallRaw('deezer.getChildAccounts');
      if (rawChildren is List) {
        for (final c in rawChildren) {
          final cm = _asMap(c);
          final extra = _asMap(cm['EXTRA_FAMILY']);
          if (_isTruthy(extra['IS_LOGGABLE_AS'])) {
            accounts.add(buildAccount(cm));
          }
        }
      }
    }
    if (accounts.isEmpty) {
      accounts.add(buildAccount(user));
    }
    _currentUser = accounts.first;
    final lang = '${_currentUser['language'] ?? ''}'.replaceAll(
      RegExp(r'[^0-9A-Za-z *,\-.;=]'),
      '',
    );
    _headers['Accept-Language'] = lang;
  }

  @override
  Future<Map<String, Object?>> getMetadata(
    String itemId,
    String mediaType, {
    int limit = 500,
    int offset = 0,
  }) async {
    return switch (mediaType) {
      'track' => getTrack(itemId),
      'album' => getAlbum(itemId),
      'playlist' => getPlaylist(itemId),
      'artist' => getArtist(itemId),
      _ => throw NonStreamableError(
        'Media type $mediaType not available on deezer',
      ),
    };
  }

  Future<Map<String, Object?>> getTrack(String itemId) async {
    final Map<String, Object?> item;
    try {
      item = await _publicApiCall('track/$itemId');
    } catch (e) {
      throw NonStreamableError('$e');
    }
    final albumObj = _asMap(item['album']);
    final albumId = albumObj['id'];
    if (albumId == null) return item;
    try {
      final (albumMeta, albumTracks) = await (
        _publicApiCall('album/$albumId'),
        _publicApiCall('album/$albumId/tracks', {'index': '0', 'limit': '-1'}),
      ).wait;
      final data = _asList(albumTracks['data']);
      albumMeta['tracks'] = data;
      albumMeta['track_total'] = data.length;
      item['album'] = albumMeta;
    } catch (_) {
      return item;
    }
    return item;
  }

  Future<Map<String, Object?>> getAlbum(String itemId) async {
    final (albumMeta, albumTracks) = await (
      _publicApiCall('album/$itemId'),
      _publicApiCall('album/$itemId/tracks', {'index': '0', 'limit': '-1'}),
    ).wait;
    final data = _asList(albumTracks['data']);
    albumMeta['tracks'] = data;
    albumMeta['track_total'] = data.length;
    return albumMeta;
  }

  Future<Map<String, Object?>> getPlaylist(String itemId) async {
    final (plMeta, plTracks) = await (
      _publicApiCall('playlist/$itemId'),
      _publicApiCall('playlist/$itemId/tracks', {'index': '0', 'limit': '-1'}),
    ).wait;
    final data = _asList(plTracks['data']);
    plMeta['tracks'] = data;
    plMeta['track_total'] = data.length;
    return plMeta;
  }

  Future<Map<String, Object?>> getArtist(String itemId) async {
    final (artist, albums) = await (
      _publicApiCall('artist/$itemId'),
      _publicApiCall('artist/$itemId/albums', {'index': '0', 'limit': '-1'}),
    ).wait;
    artist['albums'] = _asList(albums['data']);
    return artist;
  }

  @override
  Future<List<Map<String, Object?>>> search(
    String mediaType,
    String query, {
    int limit = 200,
    int offset = 0,
    ProgressStageCallback? progressCallback,
  }) async {
    final Map<String, Object?> response;
    if (mediaType == 'featured') {
      final sub = query.isEmpty ? 'releases' : query;
      if (!const {'releases', 'selection', 'charts'}.contains(sub)) {
        throw NonStreamableError('Invalid editorial selection "$query"');
      }
      response = await _publicApiCall('editorial/0/$sub', {
        'index': '0',
        'limit': '$limit',
      });
    } else {
      if (!const {
        'track',
        'album',
        'artist',
        'playlist',
        'radio',
        'user',
      }.contains(mediaType)) {
        throw NonStreamableError('Invalid media type $mediaType');
      }
      response = await _publicApiCall('search/$mediaType', {
        'q': query,
        'index': '$offset',
        'limit': '$limit',
      });
    }
    final total = (response['total'] as num?)?.toInt() ?? 0;
    return total > 0 ? [response] : const [];
  }

  @override
  Future<DeezerDownloadable> getDownloadable(
    String itemId,
    int quality, {
    bool isRetry = false,
  }) async {
    if (itemId.isEmpty) {
      throw const NonStreamableError(
        'No item id provided. This can happen when searching for fallback songs.',
      );
    }
    final clampedQuality = quality.clamp(0, maxQuality);
    final dlInfo = <String, Object?>{'id': itemId};

    final trackInfo = await _gwCall('song.getData', {'SNG_ID': itemId});
    final fallbackMap = _asMap(trackInfo['FALLBACK']);
    final fallbackId = fallbackMap['SNG_ID']?.toString();

    final sizes = <int>[
      for (final (_, fmt) in qualityMap)
        int.tryParse('${trackInfo['FILESIZE_$fmt'] ?? 0}') ?? 0,
    ];
    dlInfo['quality_to_size'] = sizes;

    final lower = config.deezer.lowerQualityIfNotAvailable;
    var candidates = <int>[clampedQuality];
    if (lower) {
      final available = <int>[
        for (var q = clampedQuality; q >= 0; q--)
          if (sizes[q] > 0) q,
      ];
      candidates = available.isNotEmpty ? available : [clampedQuality];
    }

    final token = '${trackInfo['TRACK_TOKEN'] ?? ''}';
    String? url;
    for (var i = 0; i < candidates.length; i++) {
      final q = candidates[i];
      final (_, fmtStr) = qualityMap[q];
      try {
        url = await _getTrackUrl(token, fmtStr);
      } on _DeezerWrongLicense {
        if (i + 1 < candidates.length) continue;
        throw NonStreamableError(
          'The requested quality is not available with your Deezer plan. '
          'FLAC needs a plan with lossless; 320 kbps MP3 needs a paid plan.'
          '${lower ? '' : ' Turn on lower_quality_if_not_available to fall back.'}',
        );
      } on _DeezerWrongGeolocation {
        if (!isRetry && fallbackId != null && fallbackId.isNotEmpty) {
          return getDownloadable(fallbackId, clampedQuality, isRetry: true);
        }
        throw const NonStreamableError(
          'The requested track is not available. This may be due to your country/location.',
        );
      }
      dlInfo['quality'] = q;
      break;
    }

    dlInfo.putIfAbsent('quality', () => clampedQuality);

    url ??= deezerEncryptedFileUrl(
      trackId: itemId,
      md5Origin: '${trackInfo['MD5_ORIGIN'] ?? ''}',
      mediaVersion: '${trackInfo['MEDIA_VERSION'] ?? ''}',
    );
    dlInfo['url'] = url;
    return DeezerDownloadable(_client, dlInfo);
  }

  Future<String?> _getTrackUrl(String trackToken, String trackFormat) async {
    final licenseToken = _currentUser['license_token'];
    if (licenseToken is! String || licenseToken.isEmpty) return null;

    final canLossless = _isTruthy(_currentUser['can_stream_lossless']);
    final canHq = _isTruthy(_currentUser['can_stream_hq']);
    if (((trackFormat == 'FLAC' || trackFormat.startsWith('MP4_RA')) &&
            !canLossless) ||
        (trackFormat == 'MP3_320' && !canHq)) {
      throw _DeezerWrongLicense(trackFormat);
    }

    final payload = jsonEncode({
      'license_token': licenseToken,
      'media': [
        {
          'type': 'FULL',
          'formats': [
            {'cipher': 'BF_CBC_STRIPE', 'format': trackFormat},
          ],
        },
      ],
      'track_tokens': [trackToken],
    });
    final resp = await _client.request(
      'POST',
      Uri.parse('https://media.deezer.com/v1/get_url'),
      headers: {
        ..._requestHeaders(withCookie: true),
        'Content-Type': 'application/json',
      },
      body: utf8.encode(payload),
      timeout: const Duration(seconds: 30),
    );
    if (resp.statusCode < 200 || resp.statusCode >= 300) return null;

    final json = _asMap(resp.decodeJson());
    final dataList = _asList(json['data']);
    if (dataList.isEmpty) return null;
    final first = _asMap(dataList.first);
    if (first.containsKey('errors')) {
      final errors = _asList(first['errors']);
      if (errors.isNotEmpty) {
        final err0 = _asMap(errors.first);
        if ((err0['code'] as num?)?.toInt() == 2002) {
          throw _DeezerWrongGeolocation('${_currentUser['country'] ?? ''}');
        }
        throw _DeezerApiError(jsonEncode(json));
      }
    }
    final media = _asList(first['media']);
    if (media.isNotEmpty) {
      final sources = _asList(_asMap(media.first)['sources']);
      if (sources.isNotEmpty) {
        final srcUrl = _asMap(sources.first)['url'];
        if (srcUrl is String && srcUrl.isNotEmpty) return srcUrl;
      }
    }
    return null;
  }

  Future<Map<String, Object?>> _publicApiCall(
    String method, [
    Map<String, String>? args,
    int retryDepth = 0,
  ]) async {
    final uri = Uri.parse('https://api.deezer.com/$method').replace(
      queryParameters: args == null || args.isEmpty ? null : args,
    );
    SourceHttpResponse resp;
    try {
      resp = await _client.request(
        'GET',
        uri,
        headers: _requestHeaders(),
        timeout: const Duration(seconds: 30),
      );
    } catch (_) {
      if (retryDepth >= 3) rethrow;
      await _sleep(const Duration(seconds: 2));
      return _publicApiCall(method, args, retryDepth + 1);
    }

    final json = _asMap(resp.decodeJson());
    final err = json['error'];
    if (err is Map) {
      final code = (err['code'] as num?)?.toInt();
      if ((code == 4 || code == 700) && retryDepth < 3) {
        await _sleep(const Duration(seconds: 5));
        return _publicApiCall(method, args, retryDepth + 1);
      }
      final msg = '${err['message'] ?? ''}';
      throw _DeezerApiError(
        code != null ? 'Deezer API error $code ($method): $msg' : jsonEncode(err),
      );
    }
    return json;
  }

  Future<Map<String, Object?>> _gwCall(
    String method, [
    Map<String, Object?>? args,
    int retryDepth = 0,
  ]) async {
    final raw = await _gwCallRaw(method, args, retryDepth);
    return _asMap(raw);
  }

  Future<Object?> _gwCallRaw(
    String method, [
    Map<String, Object?>? args,
    int retryDepth = 0,
  ]) async {
    final bodyArgs = Map<String, Object?>.of(args ?? const {});
    if ((_gwApiToken == null || _gwApiToken!.isEmpty) &&
        method != 'deezer.getUserData') {
      final u = await _gwCall('deezer.getUserData');
      _gwApiToken = '${u['checkForm'] ?? ''}';
    }

    final uri = Uri.parse('https://www.deezer.com/ajax/gw-light.php').replace(
      queryParameters: {
        'api_version': '1.0',
        'api_token': method == 'deezer.getUserData'
            ? 'null'
            : (_gwApiToken ?? 'null'),
        'input': '3',
        'method': method,
      },
    );

    SourceHttpResponse resp;
    try {
      resp = await _client.request(
        'POST',
        uri,
        headers: {
          ..._requestHeaders(withCookie: true),
          'Content-Type': 'application/json',
        },
        body: utf8.encode(jsonEncode(bodyArgs)),
        timeout: const Duration(seconds: 30),
      );
    } catch (_) {
      if (retryDepth >= 3) rethrow;
      await _sleep(const Duration(seconds: 2));
      return _gwCallRaw(method, bodyArgs, retryDepth + 1);
    }

    final json = _asMap(resp.decodeJson());
    final err = json['error'];
    final hasError =
        (err is Map && err.isNotEmpty) || (err is List && err.isNotEmpty);
    if (hasError) {
      if (err is Map &&
          retryDepth < 2 &&
          (err['GATEWAY_ERROR'] == 'invalid api token' ||
              err['VALID_TOKEN_REQUIRED'] == 'Invalid CSRF token')) {
        _gwApiToken = null;
        final u = await _gwCall('deezer.getUserData');
        _gwApiToken = '${u['checkForm'] ?? ''}';
        return _gwCallRaw(method, bodyArgs, retryDepth + 1);
      }
      final payload = _asMap(json['payload']);
      final fallback = _asMap(payload['FALLBACK']);
      if (fallback.isNotEmpty && retryDepth < 2) {
        bodyArgs.addAll(fallback);
        return _gwCallRaw(method, bodyArgs, retryDepth + 1);
      }
      throw _DeezerApiError(jsonEncode(err));
    }

    final results = json['results'];
    if ((_gwApiToken == null || _gwApiToken!.isEmpty) &&
        method == 'deezer.getUserData' &&
        results is Map) {
      _gwApiToken = '${results['checkForm'] ?? ''}';
    }
    return results;
  }

  @override
  Future<List<int>?> fetchBytes(String url) async {
    final resp = await _client.request(
      'GET',
      Uri.parse(url),
      headers: _requestHeaders(),
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

Map<String, Object?> _asMap(Object? v) {
  if (v is Map<String, Object?>) return v;
  if (v is Map) return {for (final e in v.entries) '${e.key}': e.value};
  return const {};
}

List<Object?> _asList(Object? v) => v is List ? v : const [];

bool _isTruthy(Object? v) => switch (v) {
  null => false,
  bool b => b,
  num n => n != 0,
  String s => s.isNotEmpty,
  Iterable<Object?> l => l.isNotEmpty,
  Map<Object?, Object?> m => m.isNotEmpty,
  _ => true,
};
