import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../core/py_re.dart';
import '../core/py_text.dart';
import '../data/library_repository.dart';
import '../data/release_queries.dart';

/// Port of `StreamripApp/utils/new_releases.py` and the scheduling hooks in
/// `main.py` (`ensure_new_releases_refresh`, `maybe_recheck_new_releases`,
/// `trigger_manual_new_releases_refresh`).
///
/// Looks up discographies of library artists via public Deezer (`api.deezer.com`)
/// and public MusicBrainz (`musicbrainz.org/ws/2/release-group`), caches them in
/// `artist_releases`, logs passes in `release_sync_log`, and selects unowned
/// recent releases for the Search landing page.

const kDeezerApi = 'https://api.deezer.com';
const kMusicBrainzBase = 'https://musicbrainz.org/ws/2';
const kMusicBrainzDefaultContact = 'mitsacopoulos@gmail.com';
const kMusicBrainzUserAgent = 'MaiAnLab/1.0 ( $kMusicBrainzDefaultContact )';

const kRefreshAfterS = 20 * 3600.0;
const kKeepDays = 730;
const kShowDays = 365;
const kSingleDays = 45;

final _bracketedRe = PyRe(r'[\(\[][^\)\]]*[\)\]]');
final _dashEditionRe = PyRe(r'\s-\s.*(edition|version|remaster).*$');
final _nonWordRe = PyRe(r'[^\w]+');
final _nonSpaceRe = PyRe(r'\S+');
final _creditSeparatorsRe = PyRe(
  r',\s|\s&\s|\sfeat\.?\s|\sft\.?\s|\sx\s',
  ignoreCase: true,
);
final _variantRe = PyRe(
  r'\b(remix|live|sped up|slowed|instrumental|acoustic version)\b',
  ignoreCase: true,
);

const _skipArtists = {
  'various artists',
  'various',
  'unknown',
  'unknown artist',
  '',
};

/// `new_releases.norm`: comparable form of a title or name without accents,
/// case, bracketed edition notes, dash edition suffixes, or punctuation.
String normReleaseText(String? text) {
  var s = pyLower(nfkdStrip(text ?? ''));
  s = _bracketedRe.sub(s, ' ');
  s = _dashEditionRe.sub(s, ' ');
  s = _nonWordRe.sub(s, ' ');
  return _nonSpaceRe.findall(s).join(' ');
}

/// `new_releases.is_lookup_candidate`: solo artist entries only.
bool isLookupCandidate(String? name) {
  final raw = name ?? '';
  return !_skipArtists.contains(normReleaseText(raw)) &&
      _creditSeparatorsRe.search(raw) == null;
}

String _cutoffDate(int days, DateTime today) =>
    formatIsoDate(today.subtract(Duration(days: days)));

/// `search.py` `_release_date_label`: "Today", "Yesterday", "3 days ago",
/// "12 Oct", or "7 Mar 2019".
String releaseDateLabel(String? date, {DateTime? today}) {
  final raw = date ?? '';
  final head = raw.length >= 10 ? raw.substring(0, 10) : raw;
  final parsed = _parseIsoDate(head);
  if (parsed == null) return raw;
  final now = today ?? DateTime.now();
  final todayDate = DateTime(now.year, now.month, now.day);
  final days = todayDate.difference(parsed).inDays;
  if (days <= 0) return 'Today';
  if (days == 1) return 'Yesterday';
  if (days < 7) return '$days days ago';
  const months = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', //
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
  ];
  final month = months[parsed.month - 1];
  if (parsed.year == todayDate.year) return '${parsed.day} $month';
  return '${parsed.day} $month ${parsed.year}';
}

DateTime? _parseIsoDate(String s) {
  final m = RegExp(r'^(\d{4})-(\d{2})-(\d{2})$').firstMatch(s);
  if (m == null) return null;
  final y = int.parse(m[1]!);
  final mo = int.parse(m[2]!);
  final d = int.parse(m[3]!);
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;
  final dt = DateTime(y, mo, d);
  if (dt.year != y || dt.month != mo || dt.day != d) return null;
  return dt;
}

/// Result of one artist discography lookup: [status] is `'ok'`, `'notfound'`,
/// or `'error'` (transient network/HTTP failure; do not cache).
class ReleaseFetchResult {
  const ReleaseFetchResult({
    required this.status,
    this.sourceId,
    this.releases,
  });

  final String status;
  final String? sourceId;
  final List<ReleaseItem>? releases;
}

/// Minimal HTTP GET result for injectable transport.
class ReleaseHttpResponse {
  const ReleaseHttpResponse(this.statusCode, this.body);
  final int statusCode;
  final String body;
}

typedef ReleaseHttpGet =
    Future<ReleaseHttpResponse?> Function(
      Uri uri, {
      Map<String, String> headers,
      Duration timeout,
    });

/// Public Deezer + MusicBrainz discography client with polite rate-limiting.
class ReleaseFetcher {
  ReleaseFetcher({
    ReleaseHttpGet? httpGet,
    HttpClient? httpClient,
    Duration deezerInterval = const Duration(milliseconds: 150),
    Duration deezerQuotaBackoff = const Duration(seconds: 5),
    Duration mbInterval = const Duration(milliseconds: 1100),
    Duration mbBackoffUnit = const Duration(milliseconds: 1500),
    DateTime Function()? clock,
  }) : _httpGet = httpGet,
       _ownClient = httpGet == null && httpClient == null,
       _client = httpGet == null ? (httpClient ?? HttpClient()) : null,
       _deezerInterval = deezerInterval,
       _deezerQuotaBackoff = deezerQuotaBackoff,
       _mbInterval = mbInterval,
       _mbBackoffUnit = mbBackoffUnit,
       _clock = clock ?? DateTime.now;

  final ReleaseHttpGet? _httpGet;
  final bool _ownClient;
  final HttpClient? _client;
  final Duration _deezerInterval;
  final Duration _deezerQuotaBackoff;
  final Duration _mbInterval;
  final Duration _mbBackoffUnit;
  final DateTime Function() _clock;

  DateTime? _lastDeezer;
  Future<void> _deezerLock = Future<void>.value();
  DateTime? _lastMb;
  Future<void> _mbLock = Future<void>.value();

  void close() {
    if (_ownClient) _client?.close(force: true);
  }

  Future<ReleaseHttpResponse?> _performGet(
    Uri uri, {
    Map<String, String> headers = const {},
    required Duration timeout,
  }) async {
    if (_httpGet != null) {
      try {
        return await _httpGet(uri, headers: headers, timeout: timeout);
      } catch (e) {
        debugPrint('ReleaseFetcher GET failed on $uri: $e');
        return null;
      }
    }
    final client = _client!;
    try {
      final req = await client.getUrl(uri).timeout(timeout);
      headers.forEach(req.headers.set);
      final resp = await req.close().timeout(timeout);
      final body = await resp.transform(utf8.decoder).join().timeout(timeout);
      return ReleaseHttpResponse(resp.statusCode, body);
    } catch (e) {
      debugPrint('ReleaseFetcher GET failed on $uri: $e');
      return null;
    }
  }

  Future<T> _serialized<T>(
    Future<void> Function() getTail,
    void Function(Future<void>) setTail,
    Future<T> Function() fn,
  ) {
    final prev = getTail();
    final completer = Completer<void>();
    setTail(completer.future);
    return prev.then((_) async {
      try {
        return await fn();
      } finally {
        completer.complete();
      }
    });
  }

  Future<Map<String, Object?>?> _deezerGet(
    String path, [
    Map<String, String>? params,
  ]) async {
    final base = Uri.parse('$kDeezerApi/$path');
    final uri = params == null ? base : base.replace(queryParameters: params);
    for (var attempt = 0; attempt < 2; attempt++) {
      final data = await _serialized(
        () => _deezerLock,
        (f) => _deezerLock = f,
        () async {
          final last = _lastDeezer;
          if (last != null && _deezerInterval > Duration.zero) {
            final wait = _deezerInterval - _clock().difference(last);
            if (wait > Duration.zero) await Future<void>.delayed(wait);
          }
          final resp = await _performGet(
            uri,
            timeout: const Duration(seconds: 15),
          );
          _lastDeezer = _clock();
          if (resp == null || resp.statusCode != 200) {
            if (resp != null) {
              debugPrint('Deezer HTTP ${resp.statusCode} on $path');
            }
            return null;
          }
          try {
            final decoded = jsonDecode(resp.body);
            return decoded is Map<String, Object?> ? decoded : null;
          } catch (e) {
            debugPrint('Deezer JSON decode failed on $path: $e');
            return null;
          }
        },
      );
      if (data == null) return null;
      final err = data['error'];
      if (err is! Map) return data;
      if (err['code'] == 4 && attempt == 0) {
        if (_deezerQuotaBackoff > Duration.zero) {
          await Future<void>.delayed(_deezerQuotaBackoff);
        }
        continue;
      }
      debugPrint('Deezer error on $path: $err');
      return null;
    }
    return null;
  }

  Future<List<Map<String, Object?>>?> _deezerAlbums(Object artistId) async {
    final data = await _deezerGet('artist/$artistId/albums', {'limit': '200'});
    if (data == null) return null;
    final raw = data['data'];
    if (raw is! List) return const [];
    return [
      for (final a in raw)
        if (a is Map<String, Object?>) a,
    ];
  }

  /// `ReleaseFetcher.deezer_releases`.
  Future<ReleaseFetchResult> deezerReleases(
    String name,
    Set<String> ownedTitles, {
    String? knownId,
  }) async {
    if (knownId != null && knownId.isNotEmpty) {
      final albums = await _deezerAlbums(knownId);
      if (albums == null) return const ReleaseFetchResult(status: 'error');
      return ReleaseFetchResult(
        status: 'ok',
        sourceId: knownId,
        releases: fromDeezerAlbums(albums, today: _clock()),
      );
    }

    final found = await _deezerGet('search/artist', {'q': name, 'limit': '10'});
    if (found == null) return const ReleaseFetchResult(status: 'error');

    final target = normReleaseText(name);
    final rawList = found['data'];
    final candidates = <Map<String, Object?>>[];
    if (rawList is List) {
      for (final a in rawList) {
        if (a is Map<String, Object?> &&
            normReleaseText('${a['name'] ?? ''}') == target) {
          candidates.add(a);
          if (candidates.length >= 3) break;
        }
      }
    }
    if (candidates.isEmpty) {
      return const ReleaseFetchResult(status: 'notfound', releases: []);
    }
    if (ownedTitles.isEmpty && candidates.length > 1) {
      return const ReleaseFetchResult(status: 'notfound', releases: []);
    }
    for (final cand in candidates) {
      final id = cand['id'];
      if (id == null) continue;
      final albums = await _deezerAlbums(id);
      if (albums == null) return const ReleaseFetchResult(status: 'error');
      final albumTitles = {
        for (final a in albums) normReleaseText('${a['title'] ?? ''}'),
      };
      if (ownedTitles.isEmpty || ownedTitles.intersection(albumTitles).isNotEmpty) {
        return ReleaseFetchResult(
          status: 'ok',
          sourceId: '$id',
          releases: fromDeezerAlbums(albums, today: _clock()),
        );
      }
    }
    return const ReleaseFetchResult(status: 'notfound', releases: []);
  }

  Future<(Map<String, Object?>?, int)> _musicBrainzGet(
    String path,
    Map<String, String> params, {
    int retries = 3,
  }) async {
    final uri = Uri.parse(
      '$kMusicBrainzBase/$path',
    ).replace(queryParameters: params);
    const headers = {
      'User-Agent': kMusicBrainzUserAgent,
      'Accept': 'application/json',
    };
    var status = -1;
    for (var attempt = 0; attempt < retries; attempt++) {
      final result = await _serialized(
        () => _mbLock,
        (f) => _mbLock = f,
        () async {
          final last = _lastMb;
          if (last != null && _mbInterval > Duration.zero) {
            final wait = _mbInterval - _clock().difference(last);
            if (wait > Duration.zero) await Future<void>.delayed(wait);
          }
          final resp = await _performGet(
            uri,
            headers: headers,
            timeout: const Duration(seconds: 20),
          );
          _lastMb = _clock();
          if (resp == null) return (null, -1);
          if (resp.statusCode == 200) {
            try {
              final decoded = jsonDecode(resp.body);
              return (
                decoded is Map<String, Object?> ? decoded : null,
                200,
              );
            } catch (_) {
              return (null, -1);
            }
          }
          return (null, resp.statusCode);
        },
      );
      if (result.$2 == 200 && result.$1 != null) return result;
      status = result.$2;
      if (status != -1 && status != 503) {
        return (null, status);
      }
      if (attempt + 1 < retries && _mbBackoffUnit > Duration.zero) {
        await Future<void>.delayed(_mbBackoffUnit * (attempt + 1));
      }
    }
    return (null, status);
  }

  /// `ReleaseFetcher.musicbrainz_releases`.
  Future<ReleaseFetchResult> musicBrainzReleases(String mbid) async {
    if (mbid.isEmpty) {
      return const ReleaseFetchResult(status: 'notfound', releases: []);
    }
    final (data, status) = await _musicBrainzGet('release-group', {
      'artist': mbid,
      'type': 'album|ep|single',
      'limit': '100',
      'fmt': 'json',
    });
    if (data == null) {
      if (status == 400 || status == 404) {
        return const ReleaseFetchResult(status: 'notfound', releases: []);
      }
      return const ReleaseFetchResult(status: 'error');
    }
    final raw = data['release-groups'];
    final groups = <Map<String, Object?>>[
      if (raw is List)
        for (final g in raw)
          if (g is Map<String, Object?>) g,
    ];
    return ReleaseFetchResult(
      status: 'ok',
      sourceId: mbid,
      releases: fromMusicBrainzGroups(groups, today: _clock()),
    );
  }
}

/// `new_releases._from_deezer`.
List<ReleaseItem> fromDeezerAlbums(
  List<Map<String, Object?>> albums, {
  DateTime? today,
}) {
  final cutoff = _cutoffDate(kKeepDays, today ?? DateTime.now());
  final out = <ReleaseItem>[];
  for (final a in albums) {
    final date = '${a['release_date'] ?? ''}';
    if (date.compareTo(cutoff) < 0 || a['record_type'] == 'compile') {
      continue;
    }
    final recordType = '${a['record_type'] ?? ''}';
    out.add(
      ReleaseItem(
        title: '${a['title'] ?? ''}',
        date: date,
        type: recordType.isEmpty ? 'album' : recordType,
        cover: '${a['cover_medium'] ?? ''}',
        source: 'deezer',
      ),
    );
  }
  return out;
}

/// `new_releases._from_musicbrainz`.
List<ReleaseItem> fromMusicBrainzGroups(
  List<Map<String, Object?>> groups, {
  DateTime? today,
}) {
  final cutoff = _cutoffDate(kKeepDays, today ?? DateTime.now());
  final out = <ReleaseItem>[];
  for (final g in groups) {
    final date = '${g['first-release-date'] ?? ''}';
    final secondary = g['secondary-types'];
    final hasSecondary = secondary is List && secondary.isNotEmpty;
    if (date.compareTo(cutoff) < 0 || hasSecondary) continue;
    final primaryType = '${g['primary-type'] ?? ''}';
    final id = '${g['id'] ?? ''}';
    out.add(
      ReleaseItem(
        title: '${g['title'] ?? ''}',
        date: date,
        type: pyLower(primaryType.isEmpty ? 'album' : primaryType),
        cover: 'https://coverartarchive.org/release-group/$id/front-250',
        source: 'musicbrainz',
      ),
    );
  }
  return out;
}

/// `new_releases.refresh_new_releases`.
Future<int> refreshNewReleases(
  LibraryRepository repo, {
  ReleaseFetcher? fetcher,
  double refreshAfterS = kRefreshAfterS,
  String triggerType = 'daily_auto',
  Duration cooperativeDelay = const Duration(milliseconds: 80),
  DateTime Function()? clock,
}) async {
  final ownFetcher = fetcher == null;
  final activeFetcher = fetcher ?? ReleaseFetcher(clock: clock);
  final nowFn = clock ?? DateTime.now;

  int? logId;
  try {
    logId = await repo.logReleaseSyncStart(
      triggerType: triggerType,
      now: nowFn(),
    );
  } catch (e) {
    debugPrint('Failed to log release sync start: $e');
  }

  try {
    final (cache, artists, allOwnedMap) = await (
      repo.artistReleaseCache(),
      repo.releaseLookupArtists(),
      repo.allOwnedArtistTitles(),
    ).wait;
    final nowS = nowFn().millisecondsSinceEpoch / 1000.0;
    var written = 0;
    var errorsInARow = 0;
    var totalChecked = 0;

    for (final name in artists) {
      if (!isLookupCandidate(name)) continue;
      final lower = pyLower(name);
      final row = cache[lower];
      if (row != null && nowS - row.fetchedAt < refreshAfterS) continue;

      totalChecked++;
      final rawOwned = allOwnedMap[lower] ?? const <String>{};
      final owned = <String>{
        for (final t in rawOwned)
          if (normReleaseText(t).isNotEmpty) normReleaseText(t),
      };

      final known = row != null && row.source == 'deezer' ? row.sourceId : null;
      var result = await activeFetcher.deezerReleases(
        name,
        owned,
        knownId: known,
      );
      var source = 'deezer';

      if (result.status == 'notfound') {
        final enr = await repo.artistEnrichment(name);
        final mbid = enr?['mbid'];
        if (mbid is String && mbid.isNotEmpty) {
          result = await activeFetcher.musicBrainzReleases(mbid);
          source = 'musicbrainz';
        }
      }

      if (result.status == 'error') {
        errorsInARow++;
        if (errorsInARow >= 3) {
          debugPrint('New releases: network looks down, stopping this pass');
          break;
        }
        continue;
      }

      errorsInARow = 0;
      String saveSource;
      String? saveSourceId;
      List<ReleaseItem> saveReleases;
      if (result.status == 'notfound') {
        saveSource = 'none';
        saveSourceId = null;
        saveReleases = const [];
      } else {
        saveSource = source;
        saveSourceId = result.sourceId;
        saveReleases = result.releases ?? const [];
      }
      await repo.saveArtistReleases(
        name,
        saveSource,
        saveSourceId,
        saveReleases,
        fetchedAt: nowFn().millisecondsSinceEpoch / 1000.0,
      );
      written++;

      if (cooperativeDelay > Duration.zero) {
        await Future<void>.delayed(cooperativeDelay);
      }
    }

    if (logId != null) {
      final finalStatus = errorsInARow >= 3 ? 'interrupted' : 'completed';
      await repo.logReleaseSyncFinish(
        logId,
        artistsChecked: totalChecked,
        newFound: written,
        status: finalStatus,
        now: nowFn(),
      );
    }
    return written;
  } catch (_) {
    if (logId != null) {
      try {
        await repo.logReleaseSyncFinish(logId, status: 'error', now: nowFn());
      } catch (_) {}
    }
    rethrow;
  } finally {
    if (ownFetcher) activeFetcher.close();
  }
}

/// `new_releases.get_new_releases`: cached releases the library doesn't hold,
/// newest first, albums/EPs within [days], singles within `min(days, 45)` and
/// without variant suffixes, at most [perArtist] per artist, album beating a
/// single of the same normalized title.
Future<List<ReleaseItem>> getNewReleases(
  LibraryRepository repo, {
  int days = kShowDays,
  int limit = 12,
  int perArtist = 2,
  DateTime? today,
}) async {
  final now = today ?? DateTime.now();
  final todayStr = formatIsoDate(now);
  final cutoff = _cutoffDate(days, now);
  final singleCutoff = _cutoffDate(days < kSingleDays ? days : kSingleDays, now);

  final (cache, rawOwnedMap) = await (
    repo.artistReleaseCache(),
    repo.allOwnedArtistTitles(),
  ).wait;
  final ownedCache = <String, Set<String>>{
    for (final e in rawOwnedMap.entries)
      e.key: {for (final t in e.value) normReleaseText(t)},
  };

  final picked = <(String, String), ReleaseItem>{};
  for (final row in cache.values) {
    final artist = row.artistName;
    final artistLower = pyLower(artist);
    final owned = ownedCache[artistLower] ?? const <String>{};

    for (final rel in row.releases) {
      final date = rel.date;
      final isSingle = rel.type == 'single';
      final minDate = isSingle ? singleCutoff : cutoff;
      if (date.compareTo(minDate) < 0 || date.compareTo(todayStr) > 0) {
        continue;
      }
      if (isSingle && _variantRe.search(rel.title) != null) continue;
      final normTitle = normReleaseText(rel.title);
      if (owned.contains(normTitle)) continue;

      final key = (artistLower, normTitle);
      final prev = picked[key];
      if (prev == null || (prev.type != 'album' && rel.type == 'album')) {
        picked[key] = rel.withArtist(artist);
      }
    }
  }

  final sorted = picked.values.toList()
    ..sort((a, b) => b.date.compareTo(a.date));
  final out = <ReleaseItem>[];
  final per = <String, int>{};
  for (final rel in sorted) {
    final a = pyLower(rel.artist);
    final count = per[a] ?? 0;
    if (count >= perArtist) continue;
    per[a] = count + 1;
    out.add(rel);
    if (out.length >= limit) break;
  }
  return out;
}

/// Port of `main.py`'s `_RELEASES_STATE` and scheduling methods:
/// one pass at a time, boot delay, daily DB gate, hourly landing re-check,
/// and synchronous manual-refresh claim so the UI spinner appears at once.
class NewReleasesScheduler extends ChangeNotifier {
  NewReleasesScheduler({
    required this.repository,
    required this.isEnabled,
    Future<int> Function(
      LibraryRepository repo, {
      required double refreshAfterS,
      required String triggerType,
    })?
    runner,
    DateTime Function()? clock,
    this.recheckInterval = const Duration(hours: 1),
  }) : _runner =
           runner ??
           ((repo, {required refreshAfterS, required triggerType}) =>
               refreshNewReleases(
                 repo,
                 refreshAfterS: refreshAfterS,
                 triggerType: triggerType,
               )),
       _clock = clock ?? DateTime.now;

  static const bootDelay = Duration(seconds: 90);

  final LibraryRepository? Function() repository;
  final bool Function() isEnabled;
  final Future<int> Function(
    LibraryRepository repo, {
    required double refreshAfterS,
    required String triggerType,
  })
  _runner;
  final DateTime Function() _clock;
  final Duration recheckInterval;

  /// Fired after a pass actually executes (not when skipped by the daily gate).
  void Function(int written)? onRefreshed;

  bool _pending = false;
  bool _running = false;
  DateTime? _gateCheckedAt;
  bool _disposed = false;

  bool get isRefreshing => _running;
  bool get isPending => _pending;

  /// `main.py` `ensure_new_releases_refresh`.
  void ensureRefresh({
    Duration delay = Duration.zero,
    bool force = false,
  }) {
    if (_disposed || _pending || _running) return;
    if (!isEnabled()) return;
    if (!force) _gateCheckedAt = _clock();
    _pending = true;
    _running = force;
    if (force) notifyListeners();
    unawaited(_run(delay, force: force));
  }

  /// `main.py` `maybe_recheck_new_releases`: re-checks the daily gate at most
  /// once per [recheckInterval], after boot has done its initial check.
  void maybeRecheck() {
    final last = _gateCheckedAt;
    if (last == null || _clock().difference(last) < recheckInterval) return;
    ensureRefresh();
  }

  /// `main.py` `trigger_manual_new_releases_refresh`.
  void triggerManualRefresh() {
    ensureRefresh(delay: Duration.zero, force: true);
  }

  Future<void> _run(Duration delay, {required bool force}) async {
    int? written;
    try {
      if (delay > Duration.zero) {
        await Future<void>.delayed(delay);
      }
      if (_disposed) return;
      final repo = repository();
      if (repo == null || !repo.writable) return;

      if (!force) {
        try {
          if (!await repo.shouldRunDailyReleaseSync(now: _clock())) return;
        } catch (e) {
          debugPrint('Error checking daily release sync gate: $e');
        }
      }

      if (_disposed) return;
      _running = true;
      notifyListeners();
      final trigger = force ? 'manual' : 'daily_auto';
      final refreshAfterS = force ? 0.0 : kRefreshAfterS;
      written = 0;
      try {
        written = await _runner(
          repo,
          refreshAfterS: refreshAfterS,
          triggerType: trigger,
        );
      } catch (e) {
        debugPrint('New releases refresh failed: $e');
      }
    } finally {
      _pending = false;
      final wasRunning = _running;
      _running = false;
      if (!_disposed && wasRunning) {
        notifyListeners();
      }
    }
    if (!_disposed && written != null) {
      onRefreshed?.call(written);
    }
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}
