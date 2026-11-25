import 'package:flutter/foundation.dart';

import '../data/library_repository.dart';
import '../sources/source_client.dart';
import 'metadata_qobuz.dart';
import '../search/new_releases.dart' show kMusicBrainzDefaultContact;
import 'musicbrainz.dart';

/// The enrichment passes of `metadata_enrich.py`: [enrichLibrary] (the
/// workbench's manual Sync, one explicit provider) and [enrichPending] (the
/// automatic pass: new artists plus failed lookups, each rolling between
/// MusicBrainz and Qobuz until the third failure parks it).

/// Failed lookups before an artist parks at status 'error'.
const kEnrichMaxAttempts = 3;
const _altProvider = {'musicbrainz': 'qobuz', 'qobuz': 'musicbrainz'};

/// `progress(i, total, name, res)`.
typedef EnrichProgress =
    void Function(int i, int total, String name, Map<String, Object?> res);

/// What a pass needs from the outside world. [refresh] is
/// `_do_refresh_walk_models` (the repository's, plus whatever rebuilds the
/// stations); [qobuzClient] makes an unopened Qobuz client from the config.
class EnrichDeps {
  EnrichDeps({
    required this.repo,
    required this.refresh,
    this.qobuzClient,
    MusicBrainzClient Function()? musicBrainz,
  }) : musicBrainz =
           musicBrainz ??
           (() => MusicBrainzClient(contact: kMusicBrainzDefaultContact));

  final LibraryRepository repo;
  final Future<Map<String, Object?>> Function() refresh;
  final Future<SourceClient> Function()? qobuzClient;
  final MusicBrainzClient Function() musicBrainz;
}

class _PassResult {
  int done = 0;
  String status = 'completed';
  final counts = <String, int>{};
  final reasons = <String, int>{};
  int parked = 0;
  String? unavailable;
}

void _inc(Map<String, int> m, String k, [int by = 1]) =>
    m[k] = (m[k] ?? 0) + by;

/// `_provider_pass`: [artists] looked up against ONE provider and written.
/// [blankIsFailure] (the provider is the fallback) counts an answer with no
/// genres and no country as a failed attempt instead of storing it.
/// Failures are written in a batch: a run long enough to abort is the
/// connection failing, not those artists, and costs nobody an attempt.
Future<_PassResult> _providerPass(
  EnrichDeps deps,
  String provider,
  List<String> artists, {
  required bool withGenres,
  required int maxConsecutiveErrors,
  EnrichProgress? progress,
  bool Function()? cancelled,
  int indexBase = 0,
  int? grandTotal,
  bool blankIsFailure = false,
}) async {
  final repo = deps.repo;
  final total = grandTotal ?? artists.length;
  final r = _PassResult();
  final streak = <String>[];

  Future<void> recordFailure(String name) async {
    try {
      final st = await repo.recordEnrichmentFailure(
        name,
        source: provider,
        maxAttempts: kEnrichMaxAttempts,
      );
      if (st == 'error') r.parked++;
    } catch (e) {
      debugPrint('could not record enrichment failure for "$name": $e');
    }
  }

  Future<void> flushFailures() async {
    for (final n in streak) {
      await recordFailure(n);
    }
    streak.clear();
  }

  void report(int i, String name, Map<String, Object?> res) =>
      progress?.call(indexBase + i, total, name, res);

  bool onError(int i, String name, String? reason, Map<String, Object?> res) {
    _inc(r.counts, 'error');
    _inc(r.reasons, reason ?? 'unknown');
    streak.add(name);
    report(i, name, res);
    if (streak.length >= maxConsecutiveErrors) {
      r.counts['aborted'] = 1;
      r.status = 'aborted';
      debugPrint(
        'enrich_library: aborting after ${streak.length} consecutive '
        '$provider failures (last: $reason)',
      );
      streak.clear();
      return true;
    }
    return false;
  }

  if (provider == 'qobuz') {
    final factory = deps.qobuzClient;
    final SourceClient client;
    try {
      if (factory == null) throw StateError('Qobuz is not configured');
      client = await openQobuzClient(factory);
    } catch (e) {
      debugPrint('enrich_library: failed to open Qobuz client: $e');
      r.unavailable = 'error: Qobuz connection failed: $e';
      return r;
    }
    try {
      for (var i = 1; i <= artists.length; i++) {
        final name = artists[i - 1];
        if (cancelled?.call() ?? false) {
          r.counts['cancelled'] = 1;
          r.status = 'cancelled';
          break;
        }
        Map<String, Object?> q;
        try {
          q = await lookupQobuzArtistGenres(client, name);
        } catch (e) {
          q = {'genres': <Object?>[], 'reason': 'error: $e'};
        }
        final genres = (q['genres'] as List?) ?? const [];
        final reason = q['reason'] as String?;
        if (reason != null && reason.startsWith('error:')) {
          if (onError(i, name, reason, {
            'status': 'error',
            'reason': reason,
            'genres': <Object?>[],
          })) {
            break;
          }
          continue;
        }
        await flushFailures();
        if (genres.isEmpty && blankIsFailure) {
          _inc(r.counts, 'blank');
          _inc(r.reasons, reason ?? 'no_tags');
          await recordFailure(name);
          report(i, name, {
            'status': 'notfound',
            'reason': reason,
            'genres': <Object?>[],
            'source': 'qobuz',
          });
          continue;
        }
        final String st;
        if (genres.isNotEmpty) {
          st = 'ok';
          await repo.upsertArtistEnrichment(
            name,
            genres: genres,
            source: 'qobuz',
            score: 100,
            status: 'ok',
            provenance: {'country': null, 'genres': 'qobuz'},
          );
          _inc(r.counts, 'ok');
          _inc(r.counts, 'from_qobuz');
          r.done++;
        } else {
          st = 'lowconfidence';
          _inc(r.reasons, reason ?? 'no_tags');
          _inc(r.counts, 'no_tags');
          await repo.upsertArtistEnrichment(
            name,
            genres: const [],
            source: 'qobuz',
            score: 0,
            status: 'lowconfidence',
            provenance: {'country': null, 'genres': null},
          );
        }
        report(i, name, {
          'status': st,
          'reason': reason,
          'genres': genres,
          'source': 'qobuz',
        });
      }
    } finally {
      await closeQobuzClient(client);
    }
  } else {
    final client = deps.musicBrainz();
    try {
      for (var i = 1; i <= artists.length; i++) {
        final name = artists[i - 1];
        if (cancelled?.call() ?? false) {
          r.counts['cancelled'] = 1;
          r.status = 'cancelled';
          break;
        }
        Map<String, Object?> res;
        try {
          res = await client.lookupArtist(name, withGenres: withGenres);
        } catch (e) {
          debugPrint('MusicBrainz lookup_artist raised for "$name": $e');
          res = {
            'status': 'error',
            'mbid': null,
            'country': null,
            'area': null,
            'genres': <Object?>[],
            'score': 0,
            'reason': 'error: $e',
          };
        }
        final st = res['status']! as String;
        if (st == 'error') {
          if (onError(i, name, res['reason'] as String?, res)) break;
          continue;
        }
        await flushFailures();
        final genres = (res['genres'] as List?) ?? const [];
        final country = res['country'] as String?;
        final hasCountry = country != null && country.isNotEmpty;
        if (blankIsFailure && genres.isEmpty && !hasCountry) {
          _inc(r.counts, 'blank');
          _inc(r.reasons, (res['reason'] as String?) ?? st);
          await recordFailure(name);
          report(i, name, res);
          continue;
        }
        if (genres.isEmpty) {
          _inc(r.reasons, (res['reason'] as String?) ?? 'no_tags');
          _inc(r.counts, 'no_tags');
        } else {
          _inc(r.counts, 'ok');
        }
        await repo.upsertArtistEnrichment(
          name,
          mbid: res['mbid'],
          country: country,
          area: res['area'] as String?,
          genres: genres,
          source: 'musicbrainz',
          score: res['score'],
          status: st,
          provenance: {
            'country': hasCountry ? 'musicbrainz' : null,
            'genres': genres.isNotEmpty ? 'musicbrainz' : null,
          },
        );
        r.done++;
        report(i, name, res);
      }
    } finally {
      client.close();
    }
  }
  if (r.status != 'aborted') await flushFailures();
  return r;
}

/// `enrich_library`: the manual Sync against ONE provider, no fallbacks.
/// The summary's `status`, `reasons`, `aborted` and `cancelled` are meant
/// to be shown, not discarded.
Future<Map<String, Object?>> enrichLibrary(
  EnrichDeps deps, {
  String provider = 'musicbrainz',
  bool withGenres = true,
  int? limit,
  bool includeFailed = false,
  bool retryIncomplete = false,
  int maxConsecutiveErrors = 3,
  EnrichProgress? progress,
  bool Function()? cancelled,
}) async {
  final List<String> artists;
  try {
    artists = [
      for (final (n, _) in await deps.repo.artistsNeedingEnrichment(
        limit: limit,
        includeFailed: includeFailed,
        retryIncomplete: retryIncomplete,
      ))
        n,
    ];
  } catch (e) {
    debugPrint('enrich_library: cannot list artists needing enrichment: $e');
    return {'enriched': 0, 'status': 'error: $e'};
  }
  if (artists.isEmpty) return {'enriched': 0, 'status': 'uptodate'};

  final res = await _providerPass(
    deps,
    provider,
    artists,
    withGenres: withGenres,
    maxConsecutiveErrors: maxConsecutiveErrors,
    progress: progress,
    cancelled: cancelled,
  );
  if (res.unavailable != null) {
    return {'enriched': 0, 'status': res.unavailable, 'total': artists.length};
  }
  final refresh = res.done > 0
      ? await deps.refresh()
      : const <String, Object?>{};
  final summary = <String, Object?>{
    'enriched': res.done,
    'status': res.status,
    'provider': provider,
    'total': artists.length,
    'reasons': Map<String, int>.of(res.reasons),
    'parked': res.parked,
    ...refresh,
    ...res.counts,
  };
  debugPrint('enrich_library: $summary');
  return summary;
}

/// `enrich_pending`: the automatic pass. Each artist's provider rolls with
/// its failed-attempt count; a provider that can't be used hands its
/// artists to the other; an aborted pass stops the run.
Future<Map<String, Object?>> enrichPending(
  EnrichDeps deps, {
  String preferred = 'musicbrainz',
  bool withGenres = true,
  int maxConsecutiveErrors = 3,
  EnrichProgress? progress,
  bool Function()? cancelled,
}) async {
  final List<(String, int)> rows;
  try {
    rows = await deps.repo.artistsNeedingEnrichment();
  } catch (e) {
    debugPrint('enrich_pending: cannot list artists needing enrichment: $e');
    return {'enriched': 0, 'status': 'error: $e'};
  }
  if (rows.isEmpty) return {'enriched': 0, 'status': 'uptodate'};

  if (!_altProvider.containsKey(preferred)) preferred = 'musicbrainz';
  final order = [preferred, _altProvider[preferred]!];
  final groups = <String, List<String>>{for (final p in order) p: []};
  for (final (name, attempts) in rows) {
    groups[order[attempts % order.length]]!.add(name);
  }

  final total = rows.length;
  final queue = [
    for (final p in order)
      if (groups[p]!.isNotEmpty) p,
  ];
  final unavailable = <String, String>{};
  final counts = <String, int>{};
  final reasons = <String, int>{};
  final perProvider = <String, int>{};
  var done = 0, parked = 0, base = 0;
  var status = 'completed';

  while (queue.isNotEmpty) {
    final provider = queue.removeAt(0);
    final names = groups.remove(provider)!;
    final res = await _providerPass(
      deps,
      provider,
      names,
      withGenres: withGenres,
      maxConsecutiveErrors: maxConsecutiveErrors,
      progress: progress,
      cancelled: cancelled,
      indexBase: base,
      grandTotal: total,
      blankIsFailure: provider != preferred,
    );
    if (res.unavailable != null) {
      unavailable[provider] = res.unavailable!;
      final alt = _altProvider[provider]!;
      if (!unavailable.containsKey(alt)) {
        debugPrint(
          'enrich_pending: $provider unavailable (${res.unavailable}); '
          'rolling ${names.length} artist(s) to $alt',
        );
        groups.putIfAbsent(alt, () => []).addAll(names);
        if (!queue.contains(alt)) queue.add(alt);
      }
      continue;
    }
    base += names.length;
    done += res.done;
    parked += res.parked;
    perProvider[provider] = (perProvider[provider] ?? 0) + res.done;
    res.counts.forEach((k, v) => _inc(counts, k, v));
    res.reasons.forEach((k, v) => _inc(reasons, k, v));
    if (res.status == 'aborted' || res.status == 'cancelled') {
      status = res.status;
      break;
    }
  }

  if (status == 'completed' && unavailable.isNotEmpty && perProvider.isEmpty) {
    status =
        'error: no metadata provider available '
        '(${[for (final e in unavailable.entries) '${e.key}: ${e.value}'].join('; ')})';
  }
  final refresh = done > 0 ? await deps.refresh() : const <String, Object?>{};
  final summary = <String, Object?>{
    'enriched': done,
    'status': status,
    'provider': preferred,
    'providers': perProvider,
    'total': total,
    'reasons': reasons,
    'parked': parked,
    ...refresh,
    ...counts,
  };
  debugPrint('enrich_pending: $summary');
  return summary;
}

/// `metadata_progress_cb`'s notification text.
String metadataProgressText(int i, int total, String name) =>
    '$i / $total artists · $name';

/// `metadata_retry_action`: Retry for a sync that failed outright or lost
/// its connection.
(String, String)? metadataRetryAction(Map<String, Object?> summary) {
  final status = (summary['status'] as String?) ?? '';
  if (status.startsWith('error') || status == 'aborted') {
    return ('Retry', 'workbench:sync');
  }
  return null;
}

/// `metadata_summary_text`.
String metadataSummaryText(Map<String, Object?> summary) {
  final status = (summary['status'] as String?) ?? '';
  final enriched = summary['enriched'] ?? 0;
  final total = summary['total'] ?? 0;
  if (status.startsWith('error')) {
    final i = status.indexOf(':');
    final rest = i < 0 ? '' : status.substring(i + 1).trim();
    return 'Metadata sync failed: $rest';
  }
  if (status == 'aborted') {
    return 'Metadata sync stopped — connection lost ($enriched of $total done).';
  }
  if (status == 'cancelled') {
    return 'Metadata sync cancelled ($enriched of $total done).';
  }
  return 'Metadata updated for $enriched of $total artists.';
}
