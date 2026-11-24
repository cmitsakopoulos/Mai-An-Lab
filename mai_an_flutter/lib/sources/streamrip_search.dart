import 'dart:async';

import 'search_results.dart';
import 'source_client.dart';
import 'source_config.dart';
import 'source_exceptions.dart';
import 'streamrip_api.dart';

/// Port of `StreamripApp/utils/streamrip_search.py`.

const kSupportedSources = ['qobuz', 'deezer'];

const kSearchTimeout = Duration(seconds: 45);

const kSourceColorHex = <String, String>{
  'qobuz': '#FF9F0A',
  'deezer': '#BF5AF2',
};

const _kDefaultColorHex = '#FFFFFF';

String _sourceUrl(String source, String mediaType, String id) =>
    source == 'deezer'
        ? 'https://www.deezer.com/$mediaType/$id'
        : 'https://www.qobuz.com/$mediaType/$id';

int _asInt(Object? value) {
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}

/// Items out of one raw search page (`streamrip_search._page_items`).
List<Map<String, Object?>> pageItems(
  String source,
  Map<String, Object?> page,
  String mediaType,
) {
  final Object? raw;
  if (source == 'deezer') {
    raw = page['data'];
  } else {
    final container = page['${mediaType}s'];
    raw = container is Map ? container['items'] : null;
  }
  if (raw is! List) return const [];
  return [
    for (final item in raw)
      if (item is Map<String, Object?>)
        item
      else if (item is Map)
        {for (final e in item.entries) '${e.key}': e.value},
  ];
}

class _CachedClient {
  _CachedClient(this.client, this.config);

  final SourceClient client;
  final SourceConfig config;
}

/// Manages cached Qobuz/Deezer sessions, search pagination, album/artist
/// expansion, stream URL resolution, and inactivity cleanup.
class StreamripSearcher {
  StreamripSearcher({
    required this.readConfig,
    SourceClientFactory? clientFactory,
    this.searchTimeout = kSearchTimeout,
    this.inactivityTimeout = const Duration(minutes: 5),
    DateTime Function()? clock,
  }) : _clientFactory = clientFactory ?? ((s, c) => buildSourceClient(s, c)),
       _clock = clock ?? DateTime.now;

  final Future<SourceConfig> Function() readConfig;
  final SourceClientFactory _clientFactory;
  final Duration searchTimeout;
  final Duration inactivityTimeout;
  final DateTime Function() _clock;

  final Map<String, _CachedClient> _clients = {};
  Future<void> _lock = Future<void>.value();
  Timer? _inactivityTimer;
  DateTime _lastActivity = DateTime.fromMillisecondsSinceEpoch(0);

  void _touch() {
    _lastActivity = _clock();
    _inactivityTimer ??= Timer.periodic(const Duration(seconds: 15), (_) {
      if (_clock().difference(_lastActivity) >= inactivityTimeout) {
        unawaited(closeAll());
      }
    });
  }

  /// Whether stored credentials changed for [source].
  static bool credentialsChanged(
    String source,
    SourceConfig oldCfg,
    SourceConfig newCfg,
  ) {
    if (source == 'deezer') {
      return oldCfg.deezer.arl != newCfg.deezer.arl;
    }
    return oldCfg.qobuz.emailOrUserid != newCfg.qobuz.emailOrUserid ||
        oldCfg.qobuz.passwordOrToken != newCfg.qobuz.passwordOrToken ||
        oldCfg.qobuz.appId != newCfg.qobuz.appId ||
        oldCfg.qobuz.useAuthToken != newCfg.qobuz.useAuthToken;
  }

  Future<SourceClient> _getClient(
    String source, {
    ProgressStageCallback? progressCallback,
  }) {
    final normalized = (source.isEmpty ? 'qobuz' : source).toLowerCase();
    if (!kSupportedSources.contains(normalized)) {
      return Future.error(Exception("Source '$normalized' is not supported."));
    }
    _touch();
    final completer = Completer<void>();
    final prev = _lock;
    _lock = completer.future;
    return () async {
      try {
        await prev;
        final cfg = await readConfig();
        var cached = _clients[normalized];
        if (cached != null &&
            credentialsChanged(normalized, cached.config, cfg)) {
          try {
            await cached.client.close();
          } catch (_) {}
          _clients.remove(normalized);
          cached = null;
        }
        if (cached == null || cached.client.isClosed) {
          final client = _clientFactory(normalized, cfg);
          progressCallback?.call('Authenticating', 'Signing in\u2026');
          await client.login();
          cached = _CachedClient(client, cfg);
          _clients[normalized] = cached;
        }
        return cached.client;
      } finally {
        completer.complete();
      }
    }();
  }

  Future<String> getTrackStreamUrl(
    String trackId, {
    int quality = 1,
    String source = 'qobuz',
  }) async {
    final client = await _getClient(source);
    final downloadable = await client.getDownloadable(trackId, quality);
    return downloadable.url;
  }

  Future<List<Map<String, Object?>>> getArtistAlbums(
    String artistId, {
    int limit = 30,
    int offset = 0,
    String source = 'qobuz',
  }) async {
    final src = (source.isEmpty ? 'qobuz' : source).toLowerCase();
    final client = await _getClient(src);

    final List<Map<String, Object?>> rawAlbums;
    if (src == 'deezer') {
      final resp = await client.getMetadata(artistId, 'artist');
      final all = _mapList(resp['albums']);
      final start = offset.clamp(0, all.length);
      final end = (offset + limit).clamp(start, all.length);
      rawAlbums = all.sublist(start, end);
    } else {
      final resp = await client.getMetadata(
        artistId,
        'artist',
        limit: limit,
        offset: offset,
      );
      final albumsObj = resp['albums'];
      final items = _mapList(albumsObj is Map ? albumsObj['items'] : null);
      rawAlbums = items.length > limit ? items.sublist(0, limit) : items;
    }

    for (final a in rawAlbums) {
      a['_media_type'] = 'album';
    }
    final parsed = parseResults(rawAlbums, src);

    if (rawAlbums.isNotEmpty) {
      parsed.add({
        'media_type': 'load_more_artist',
        'id': artistId,
        'source': src,
        'offset': offset + limit,
        'limit': limit,
        'ui_title': 'Load More',
        'name': 'Load More',
      });
    } else if (offset > 0) {
      parsed.add({
        'media_type': 'search_exhausted',
        'ui_title': 'All albums loaded',
        'name': 'Exhausted',
      });
    }
    return parsed;
  }

  Future<List<Map<String, Object?>>> getAlbumTracks(
    String albumId, {
    String source = 'qobuz',
  }) async {
    final src = (source.isEmpty ? 'qobuz' : source).toLowerCase();
    final client = await _getClient(src);
    final resp = await client.getMetadata(albumId, 'album');
    final raw = resp['tracks'];
    final rawTracks = _mapList(raw is Map ? raw['items'] : raw);
    for (final t in rawTracks) {
      t['_media_type'] = 'track';
      t.putIfAbsent('album', () => resp);
    }
    return parseResults(rawTracks, src);
  }

  /// Search [source] for [query] across [mediaTypes].
  /// Returns `({List<Map<String, Object?>> items, String? error})`.
  Future<({List<Map<String, Object?>> items, String? error})> search(
    String query, {
    String source = 'qobuz',
    List<String> mediaTypes = const ['track', 'album'],
    int limit = 50,
    int offset = 0,
    ProgressStageCallback? progressCallback,
  }) async {
    final src = (source.isEmpty ? 'qobuz' : source).toLowerCase();
    if (!kSupportedSources.contains(src)) {
      return (
        items: const <Map<String, Object?>>[],
        error:
            "Source '$src' is not supported. Available: ${kSupportedSources.join(', ')}.",
      );
    }

    final trimmed = query.trim();
    try {
      final items = await _searchAsync(
        trimmed,
        mediaTypes: mediaTypes,
        limit: limit,
        offset: offset,
        progressCallback: progressCallback,
        source: src,
      ).timeout(searchTimeout);
      return (items: items, error: null);
    } on TimeoutException {
      final label = '${src[0].toUpperCase()}${src.substring(1)}';
      return (
        items: const <Map<String, Object?>>[],
        error:
            "$label didn't answer within ${searchTimeout.inSeconds} seconds. "
            'Check your connection and try again.',
      );
    } catch (e) {
      final msg = e.toString().replaceFirst(RegExp(r'^Exception:\s*'), '');
      return (items: const <Map<String, Object?>>[], error: msg);
    }
  }

  Future<List<Map<String, Object?>>> _searchAsync(
    String query, {
    required List<String> mediaTypes,
    required int limit,
    required int offset,
    required String source,
    ProgressStageCallback? progressCallback,
  }) async {
    final label = '${source[0].toUpperCase()}${source.substring(1)}';
    final missingCredsHint = source == 'deezer'
        ? 'Deezer credentials are missing. Please enter your ARL cookie in the Settings tab.'
        : 'Qobuz credentials are missing. Please enter your User ID and Token in the Settings tab.';

    final SourceClient client;
    try {
      progressCallback?.call('Connecting', 'Contacting API\u2026');
      client = await _getClient(source, progressCallback: progressCallback);
    } on MissingCredentialsError {
      throw Exception(missingCredsHint);
    } on AuthenticationError {
      throw Exception(
        '$label authentication failed. Please check your credentials in the Settings tab.',
      );
    } catch (e) {
      throw Exception('Connection failed: $e');
    }

    final resultsPerType = <List<Map<String, Object?>>>[];
    final fetchErrors = <Object>[];
    var anyOk = false;

    for (final m in mediaTypes) {
      progressCallback?.call('Searching', 'Looking up ${m}s\u2026');
      try {
        final pages = await client.search(
          m,
          query,
          limit: limit,
          offset: offset,
          progressCallback: progressCallback,
        );
        final itemsOut = <Map<String, Object?>>[];
        for (final page in pages) {
          for (final item in pageItems(source, page, m)) {
            item['_media_type'] = m;
            itemsOut.add(item);
          }
        }
        anyOk = true;
        resultsPerType.add(itemsOut);
      } catch (e) {
        fetchErrors.add(e);
      }
    }

    if (!anyOk && fetchErrors.isNotEmpty) {
      throw humanizeSearchError(fetchErrors.first);
    }

    progressCallback?.call('Processing', 'Formatting results\u2026');
    final raw = <Map<String, Object?>>[
      for (final list in resultsPerType) ...list,
    ];
    return parseResults(raw, source);
  }

  /// Translate raw transport / API errors into concise messages.
  /// Matches `StreamripSearcher._humanize_search_error`.
  static Exception humanizeSearchError(Object exc) {
    final raw = exc.toString().replaceFirst(RegExp(r'^Exception:\s*'), '').trim();
    final msg = raw.isEmpty ? '${exc.runtimeType}' : raw;
    final low = msg.toLowerCase();
    final statusMatch = RegExp(
      r'status[:=]\s*(\d{3})',
      caseSensitive: false,
    ).firstMatch(msg);
    final status = statusMatch?.group(1);

    if (exc is InvalidAppSecretError ||
        low.contains('app secret') ||
        low.contains('appsecret')) {
      return Exception(
        'Qobuz rejected the App Secret. Check the App ID / Secret in the Settings tab.',
      );
    }
    if (status == '400' || status == '401' || status == '403') {
      return Exception(
        'Qobuz rejected the request (HTTP $status). Your App ID / Secret or '
        'credentials look invalid — verify them in the Settings tab.',
      );
    }
    if (status == '429') {
      return Exception(
        'Qobuz is rate-limiting requests (HTTP 429). Wait a moment and try again.',
      );
    }
    const netTokens = [
      'timeout',
      'timed out',
      'connect',
      'getaddrinfo',
      'resolve',
      'network',
      'ssl',
      'connection reset',
      'unreachable',
    ];
    if (netTokens.any(low.contains)) {
      return Exception(
        "Couldn't reach Qobuz — check your internet connection and try again.",
      );
    }
    return Exception('Qobuz search failed: $msg');
  }

  /// Parse and deduplicate raw tagged items into Search UI result dicts.
  /// Matches `StreamripSearcher._parse_results`.
  static List<Map<String, Object?>> parseResults(
    List<Map<String, Object?>> rawItems,
    String source,
  ) {
    final tracks = [
      for (final i in rawItems)
        if (i['_media_type'] == 'track') i,
    ];
    final albums = [
      for (final i in rawItems)
        if (i['_media_type'] == 'album') i,
    ];
    final artists = [
      for (final i in rawItems)
        if (i['_media_type'] == 'artist') i,
    ];
    final playlists = [
      for (final i in rawItems)
        if (i['_media_type'] == 'playlist') i,
    ];

    final summaries = <SearchSummary>[];
    if (tracks.isNotEmpty) {
      summaries.addAll(
        SearchResults.fromPages(source, 'track', [
          {'tracks': {'items': tracks}},
        ]).results,
      );
    }
    if (albums.isNotEmpty) {
      summaries.addAll(
        SearchResults.fromPages(source, 'album', [
          {'albums': {'items': albums}},
        ]).results,
      );
    }
    if (artists.isNotEmpty) {
      summaries.addAll(
        SearchResults.fromPages(source, 'artist', [
          {'artists': {'items': artists}},
        ]).results,
      );
    }
    if (playlists.isNotEmpty) {
      summaries.addAll(
        SearchResults.fromPages(source, 'playlist', [
          {'playlists': {'items': playlists}},
        ]).results,
      );
    }

    final parsed = <Map<String, Object?>>[];
    for (final r in summaries) {
      final mType = r.mediaType;
      final dateReleased = switch (r) {
        TrackSummary(:final dateReleased) => dateReleased,
        AlbumSummary(:final dateReleased) => dateReleased,
        _ => null,
      };
      final year =
          dateReleased != null &&
              dateReleased.isNotEmpty &&
              dateReleased != 'Unknown'
          ? _first4(dateReleased.split('-').first)
          : 'N/A';
      final imageUrl = switch (r) {
        TrackSummary(:final imageUrl) => imageUrl,
        AlbumSummary(:final imageUrl) => imageUrl,
        _ => '',
      };
      final albumName = switch (r) {
        TrackSummary(:final albumName) => albumName,
        _ => '',
      };
      final artist = switch (r) {
        TrackSummary(:final artist) => artist,
        AlbumSummary(:final artist) => artist,
        _ => '',
      };
      final numTracks = switch (r) {
        AlbumSummary(:final numTracks) => _asInt(numTracks),
        PlaylistSummary(:final numTracks) => numTracks,
        _ => 0,
      };
      final uiSubtitle = switch (r) {
        TrackSummary(:final artist) => artist,
        AlbumSummary(:final artist) => artist,
        ArtistSummary(:final numAlbums) => '$numAlbums Albums',
        _ => '',
      };

      final detailParts = <String>[];
      if (albumName.isNotEmpty &&
          (mType != 'track' ||
              albumName.trim().toLowerCase() != r.name.trim().toLowerCase())) {
        detailParts.add(albumName);
      }
      if (year != 'N/A') detailParts.add(year);
      detailParts.add(mType.toUpperCase());

      parsed.add({
        'id': r.id,
        'name': r.name,
        'artist': artist,
        'source': source,
        'media_type': mType,
        'url': _sourceUrl(source, mType, r.id),
        'year': year,
        'album': albumName,
        'image': imageUrl,
        'num_tracks': numTracks,
        'ui_title': r.name,
        'ui_subtitle': uiSubtitle,
        'ui_detail': detailParts.join('  •  '),
        'ui_source_color': kSourceColorHex[source] ?? _kDefaultColorHex,
      });
    }

    final seen = <(String, String, String)>{};
    final deduped = <Map<String, Object?>>[];
    for (final entry in parsed) {
      final mType = '${entry['media_type']}';
      final title = '${entry['ui_title'] ?? entry['name'] ?? ''}'
          .trim()
          .toLowerCase();
      final artist = '${entry['ui_subtitle'] ?? entry['artist'] ?? ''}'
          .trim()
          .toLowerCase();
      final key = (mType == 'track' || mType == 'album')
          ? (mType, title, artist)
          : (mType, title, '');
      if (seen.add(key)) {
        deduped.add(entry);
      }
    }
    return deduped;
  }

  Future<void> closeAll() async {
    _inactivityTimer?.cancel();
    _inactivityTimer = null;
    final current = Map<String, _CachedClient>.of(_clients);
    _clients.clear();
    for (final entry in current.values) {
      try {
        await entry.client.close();
      } catch (_) {}
    }
  }
}

String _first4(String s) => s.length > 4 ? s.substring(0, 4) : s;

List<Map<String, Object?>> _mapList(Object? raw) {
  if (raw is! List) return const [];
  return [
    for (final item in raw)
      if (item is Map<String, Object?>)
        item
      else if (item is Map)
        {for (final e in item.entries) '${e.key}': e.value},
  ];
}
