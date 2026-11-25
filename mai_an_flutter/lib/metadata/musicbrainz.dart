import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../core/py_text.dart';
import '../core/py_values.dart';
import '../search/new_releases.dart'
    show
        ReleaseHttpGet,
        ReleaseHttpResponse,
        kMusicBrainzBase,
        kMusicBrainzDefaultContact;
import '../stations/credit_keys.dart';

/// The MusicBrainz half of `metadata_enrich.py`: the name matching, the
/// genre/country extraction and the rate-limited `MusicBrainzClient` with
/// its resolution cascade (combined Lucene query, credit decomposition,
/// direct-lookup genre follow-up).
///
/// Results are Python's dicts as `Map<String, Object?>` with the same keys
/// (`status`, `mbid`, `country`, `area`, `genres`, `score`, `reason`), so the
/// enrichment pass, the workbench and the goldens read them as Flet does.

/// MusicBrainz search score below which even a genuine name/alias match is
/// held back for review (`_GENUINE_SCORE_FLOOR`).
const kGenuineScoreFloor = 70;

const _luceneSpecial = r'+-&|!(){}[]^"~*?:\/';

/// `_escape_lucene`.
String escapeLucene(String? s) {
  final out = StringBuffer();
  for (final r in (s ?? '').runes) {
    final c = String.fromCharCode(r);
    if (_luceneSpecial.contains(c)) out.write('\\');
    out.write(c);
  }
  return out.toString();
}

/// `_norm_tokens`: folded alphanumeric word tokens.
Set<String> normTokens(String? s) {
  final b = StringBuffer();
  for (final r in foldName(s ?? '').runes) {
    if (isAlnum(r)) {
      b.writeCharCode(r);
    } else {
      b.write(' ');
    }
  }
  return {
    for (final w in b.toString().split(' '))
      if (w.isNotEmpty) w,
  };
}

/// `_name_close`: the folded comparison every provider's name gate uses.
bool nameClose(String? a, String? b) {
  final na = normName(a ?? ''), nb = normName(b ?? '');
  if (na.isEmpty || nb.isEmpty) return false;
  if (na == nb) return true;
  final ta = normTokens(a), tb = normTokens(b);
  if (ta.isEmpty || tb.isEmpty) return false;
  if (ta.length == tb.length && ta.containsAll(tb)) return true;
  final inter = ta.intersection(tb).length;
  if (inter > 0) {
    if (inter == ta.length && ta.length == tb.length) return true;
    final lo = ta.length < tb.length ? ta.length : tb.length;
    if (inter >= lo && (ta.length - tb.length).abs() <= 1) return true;
  }
  return false;
}

/// Python's `int(x or 0)` for a JSON value, with `TypeError`/`ValueError`
/// falling back to [fallback].
int pyIntOr(Object? v, int fallback) {
  if (v == null || v == false) return fallback;
  if (v == true) return 1;
  if (v is int) return v;
  if (v is double) {
    if (v == 0) return fallback;
    return v.isFinite ? v.truncate() : fallback;
  }
  if (v is String) {
    if (v.isEmpty) return fallback;
    final t = pyStrip(v).replaceAll('_', '');
    return int.tryParse(t) ?? fallback;
  }
  return fallback;
}

/// `_extract_genres`: `[{'name', 'count'}]`, count-descending, curated
/// `genres` preferred over free `tags`.
List<Map<String, Object?>> extractGenres(
  Map<String, Object?> obj, {
  int top = 8,
}) {
  Object? raw = obj['genres'];
  if (!pyTruthy(raw)) raw = obj['tags'];
  if (raw is! List) return [];
  final out = <Map<String, Object?>>[];
  for (final g in raw) {
    final m = g is Map ? g : const <Object?, Object?>{};
    final name = m['name'];
    if (!pyTruthy(name)) continue;
    out.add({'name': name, 'count': pyIntOr(m['count'], 0)});
  }
  // list.sort is stable in Python; Dart's isn't, so tie-break on position.
  final idx = [for (var i = 0; i < out.length; i++) i];
  idx.sort((a, b) {
    final c = (out[b]['count']! as int).compareTo(out[a]['count']! as int);
    return c != 0 ? c : a.compareTo(b);
  });
  return [for (final i in idx.take(top)) out[i]];
}

/// `_closest_match`: the first artist whose name, sort-name or an alias
/// genuinely matches [name]; never the top hit by default.
Map<String, Object?>? closestMatch(String name, List<Object?> artists) {
  for (final a in artists) {
    if (a is! Map<String, Object?>) continue;
    if (nameClose(name, _str(a['name']))) return a;
    if (nameClose(name, _str(a['sort-name']))) return a;
    final aliases = a['aliases'];
    if (aliases is! List) continue;
    for (final al in aliases) {
      if (al is Map) {
        final n = al['name'], sn = al['sort-name'];
        if (pyTruthy(n) && nameClose(name, '$n')) return a;
        if (pyTruthy(sn) && nameClose(name, '$sn')) return a;
      } else if (al is String && nameClose(name, al)) {
        return a;
      }
    }
  }
  return null;
}

String? _str(Object? v) => v is String ? v : null;

const _junkEntityMarkers = ['tribute', 'karaoke', 'cover band', 'covers band'];

/// `_looks_like_junk`: a tribute/karaoke/cover act.
bool looksLikeJunk(Map<String, Object?> a) {
  final txt = pyLower(
    '${_str(a['name']) ?? ''} ${_str(a['disambiguation']) ?? ''}',
  );
  return _junkEntityMarkers.any(txt.contains);
}

/// `_COUNTRY_NAME_TO_ISO`, in Python's order (the first substring hit wins).
const countryNameToIso = <String, String>{
  'united states': 'US', 'usa': 'US', 'canada': 'CA', 'mexico': 'MX', //
  'puerto rico': 'PR', 'jamaica': 'JM', 'cuba': 'CU', 'trinidad': 'TT',
  'dominican republic': 'DO',
  'united kingdom': 'GB', 'uk': 'GB', 'england': 'GB', 'scotland': 'GB',
  'wales': 'GB', 'northern ireland': 'GB',
  'greece': 'GR', 'ελλάδα': 'GR', 'france': 'FR', 'germany': 'DE',
  'deutschland': 'DE',
  'sweden': 'SE', 'norway': 'NO', 'finland': 'FI', 'denmark': 'DK',
  'iceland': 'IS',
  'netherlands': 'NL', 'holland': 'NL', 'belgium': 'BE', 'switzerland': 'CH',
  'austria': 'AT',
  'italy': 'IT', 'spain': 'ES', 'portugal': 'PT', 'ireland': 'IE',
  'poland': 'PL',
  'czech': 'CZ', 'czechia': 'CZ', 'slovakia': 'SK', 'hungary': 'HU',
  'romania': 'RO',
  'bulgaria': 'BG', 'serbia': 'RS', 'croatia': 'HR', 'slovenia': 'SI',
  'bosnia': 'BA',
  'ukraine': 'UA', 'russia': 'RU', 'belarus': 'BY', 'georgia': 'GE',
  'armenia': 'AM',
  'turkey': 'TR', 'türkiye': 'TR', 'cyprus': 'CY', 'albania': 'AL',
  'north macedonia': 'MK',
  'brazil': 'BR', 'brasil': 'BR', 'argentina': 'AR', 'chile': 'CL',
  'colombia': 'CO',
  'peru': 'PE', 'venezuela': 'VE', 'uruguay': 'UY',
  'japan': 'JP', 'nippon': 'JP', 'south korea': 'KR', 'korea': 'KR',
  'china': 'CN',
  'taiwan': 'TW', 'hong kong': 'HK', 'india': 'IN', 'indonesia': 'ID',
  'philippines': 'PH',
  'thailand': 'TH', 'vietnam': 'VN', 'singapore': 'SG', 'malaysia': 'MY',
  'israel': 'IL', 'palestine': 'PS', 'lebanon': 'LB', 'egypt': 'EG',
  'iran': 'IR',
  'australia': 'AU', 'new zealand': 'NZ', 'south africa': 'ZA',
  'nigeria': 'NG',
  'ghana': 'GH', 'kenya': 'KE', 'ethiopia': 'ET', 'morocco': 'MA',
  'senegal': 'SN',
};

/// `_extract_country`: the artist's ISO code from `country`, the area's
/// or begin-area's ISO codes, then the area name.
String? extractCountry(Object? obj) {
  if (obj is! Map || obj.isEmpty) return null;
  final c = obj['country'];
  if (c is String && pyStrip(c).runes.length == 2) {
    return pyStrip(c).toUpperCase();
  }
  final area = obj['area'] is Map
      ? obj['area'] as Map
      : const <Object?, Object?>{};
  final codes = area['iso-3166-1-codes'];
  if (codes is List && codes.isNotEmpty && '${codes[0]}'.runes.length == 2) {
    return '${codes[0]}'.toUpperCase();
  }
  final begin = obj['begin-area'] is Map
      ? obj['begin-area'] as Map
      : const <Object?, Object?>{};
  final bcodes = begin['iso-3166-1-codes'];
  if (bcodes is List && bcodes.isNotEmpty && '${bcodes[0]}'.runes.length == 2) {
    return '${bcodes[0]}'.toUpperCase();
  }
  var aname = _str(area['name']) ?? '';
  if (aname.isEmpty) aname = _str(begin['name']) ?? '';
  aname = pyStrip(pyLower(aname));
  for (final MapEntry(:key, :value) in countryNameToIso.entries) {
    if (aname.contains(key)) return value;
  }
  return null;
}

String? _areaName(Object? obj) {
  if (obj is! Map) return null;
  final area = obj['area'];
  return area is Map ? _str(area['name']) : null;
}

Map<String, Object?> _copyResult(Map<String, Object?> res) => {
  ...res,
  'genres': [
    for (final g in (res['genres'] as List?) ?? const [])
      Map<String, Object?>.from(g as Map),
  ],
};

/// Rate-limited MusicBrainz lookups (`MusicBrainzClient`). One request at a
/// time, at least [minInterval] apart; a 503 backs off and retries.
class MusicBrainzClient {
  MusicBrainzClient({
    String contact = 'anonymous@example.com',
    this.minInterval = const Duration(milliseconds: 1100),
    this.backoffUnit = const Duration(milliseconds: 1500),
    String appName = 'MaiAnLab',
    ReleaseHttpGet? httpGet,
    this.baseUrl = kMusicBrainzBase,
  }) : userAgent = '$appName/1.0 ( $contact )',
       _httpGet = httpGet,
       _client = httpGet == null ? HttpClient() : null;

  final String userAgent;
  final Duration minInterval;
  final Duration backoffUnit;
  final String baseUrl;
  final ReleaseHttpGet? _httpGet;
  final HttpClient? _client;

  final _clock = Stopwatch()..start();
  Duration? _last;
  Future<void> _tail = Future<void>.value();

  /// Per-pass memo of resolved artists (`_artist_memo`); errors aren't kept.
  final _memo = <(String, bool), Map<String, Object?>>{};

  void close() => _client?.close(force: true);

  Future<T> _locked<T>(Future<T> Function() fn) {
    final prev = _tail;
    final done = Completer<void>();
    _tail = done.future;
    return prev.then((_) async {
      try {
        return await fn();
      } finally {
        done.complete();
      }
    });
  }

  Future<ReleaseHttpResponse> _send(Uri uri) async {
    final headers = {'User-Agent': userAgent, 'Accept': 'application/json'};
    const timeout = Duration(seconds: 20);
    final hook = _httpGet;
    if (hook != null) {
      final r = await hook(uri, headers: headers, timeout: timeout);
      if (r == null) throw const SocketException('no response');
      return r;
    }
    final req = await _client!.getUrl(uri).timeout(timeout);
    headers.forEach(req.headers.set);
    final resp = await req.close().timeout(timeout);
    final body = await resp.transform(utf8.decoder).join().timeout(timeout);
    return ReleaseHttpResponse(resp.statusCode, body);
  }

  /// `_get`: throttled GET → (json or null, HTTP status; -1 = offline).
  Future<(Map<String, Object?>?, int)> get(
    String path,
    Map<String, String> params, {
    int retries = 3,
  }) async {
    var status = -1;
    final uri = Uri.parse('$baseUrl/$path').replace(queryParameters: params);
    for (var attempt = 0; attempt < retries; attempt++) {
      final out = await _locked<(Map<String, Object?>?, int)?>(() async {
        final last = _last;
        if (last != null) {
          final wait = minInterval - (_clock.elapsed - last);
          if (wait > Duration.zero) await Future<void>.delayed(wait);
        }
        try {
          final resp = await _send(uri);
          _last = _clock.elapsed;
          if (resp.statusCode == 200) {
            final decoded = jsonDecode(resp.body);
            return (
              decoded is Map<String, Object?> ? decoded : <String, Object?>{},
              200,
            );
          }
          if (resp.statusCode == 503) {
            status = 503;
            debugPrint(
              'MusicBrainz rate-limited (503) on $path, '
              'attempt ${attempt + 1}/$retries',
            );
            return null;
          }
          debugPrint('MusicBrainz HTTP ${resp.statusCode} on $path');
          return (null, resp.statusCode);
        } catch (e) {
          _last = _clock.elapsed;
          status = -1;
          debugPrint(
            'MusicBrainz request failed on $path '
            '(attempt ${attempt + 1}/$retries): $e',
          );
          return null;
        }
      });
      if (out != null) return out;
      await Future<void>.delayed(backoffUnit * (attempt + 1));
    }
    debugPrint(
      'MusicBrainz unreachable after $retries attempts on $path '
      '(last status $status) — enrichment cannot proceed',
    );
    return (null, status);
  }

  /// `lookup_artist`: one artist name → provenance + genres. Never throws.
  Future<Map<String, Object?>> lookupArtist(
    String? name, {
    bool withGenres = false,
  }) async {
    final raw = pyStrip(name ?? '');
    final key = (pyLower(raw), withGenres);
    final cached = _memo[key];
    if (cached != null) return _copyResult(cached);
    final result = await _lookupUncached(raw, withGenres: withGenres);
    if (result['status'] != 'error') _memo[key] = _copyResult(result);
    return result;
  }

  Future<Map<String, Object?>> _lookupUncached(
    String rawName, {
    required bool withGenres,
  }) async {
    final empty = <String, Object?>{
      'status': 'notfound',
      'mbid': null,
      'country': null,
      'area': null,
      'genres': <Object?>[],
      'score': 0,
      'reason': null,
    };
    if (rawName.isEmpty) return empty;

    final q = escapeLucene(rawName);
    final parts = _pySplit(rawName);
    var combined = 'artist:"$q" OR alias:"$q" OR artist:$q OR $q';
    if (parts.length >= 2) {
      final sortQ = escapeLucene(
        '${parts.last}, ${parts.sublist(0, parts.length - 1).join(' ')}',
      );
      combined =
          'artist:"$q" OR sortname:"$sortQ" OR alias:"$q" OR artist:$q OR $q';
    }

    Map<String, Object?>? best, weak;
    var matched = false, sawError = false;
    String? reason;
    final (data, status) = await get('artist', {
      'query': combined,
      'fmt': 'json',
      'limit': '10',
    });
    if (data == null) {
      sawError = true;
      reason = status == 503
          ? 'rate_limited'
          : status == -1
          ? 'offline'
          : 'http_$status';
    } else {
      final arts = data['artists'] is List
          ? data['artists'] as List
          : const <Object?>[];
      final m = closestMatch(rawName, arts);
      if (m != null) {
        best = m;
        matched = true;
      } else {
        for (final a in arts) {
          if (a is Map<String, Object?> && !looksLikeJunk(a)) {
            weak = a;
            break;
          }
        }
      }
    }

    var isCredit = false;
    if (!matched) {
      final members = splitArtistCredits(rawName);
      if (members.isNotEmpty) {
        isCredit = true;
        final merged = await _lookupCreditMembers(
          members,
          withGenres: withGenres,
        );
        if (merged != null) return merged;
      }
    }

    if (best == null && !isCredit) best = weak;
    if (best == null) {
      return {
        ...empty,
        'status': sawError ? 'error' : 'notfound',
        'reason': sawError
            ? reason
            : (isCredit ? 'credit_unresolved' : 'no_match'),
      };
    }

    final score = pyIntOr(best['score'], 0);
    final mbid = best['id'];
    if (!matched) {
      return {
        'status': 'lowconfidence',
        'mbid': mbid,
        'country': extractCountry(best),
        'area': _areaName(best),
        'genres': extractGenres(best),
        'score': score,
        'reason': 'weak_match',
      };
    }

    final country = extractCountry(best);
    final genres = extractGenres(best);
    var st = score >= kGenuineScoreFloor ? 'ok' : 'lowconfidence';
    if (st == 'ok' &&
        normTokens(rawName).length < 2 &&
        genres.isEmpty &&
        country == null) {
      st = 'lowconfidence';
    }
    final result = <String, Object?>{
      'status': st,
      'mbid': mbid,
      'country': country,
      'area': _areaName(best),
      'genres': genres,
      'score': score,
      'reason': genres.isEmpty ? 'no_tags' : null,
    };

    if (withGenres && pyTruthy(mbid) && genres.isEmpty) {
      final (gdata, _) = await get('artist/$mbid', {
        'inc': 'genres+tags',
        'fmt': 'json',
      });
      if (gdata != null && gdata.isNotEmpty) {
        final g2 = extractGenres(gdata);
        if (g2.isNotEmpty) {
          result['genres'] = g2;
          result['reason'] = null;
        }
        result['country'] = extractCountry(gdata) ?? result['country'];
        result['area'] = pyTruthy(result['area'])
            ? result['area']
            : _areaName(gdata);
      }
    }
    return result;
  }

  /// `_lookup_credit_members`: each member of a multi-artist credit resolved
  /// and fused; only genuine ('ok') members with data count.
  Future<Map<String, Object?>?> _lookupCreditMembers(
    List<String> members, {
    required bool withGenres,
  }) async {
    final genreCounts = <String, int>{};
    final countries = <String>[];
    final mbids = <Object>[];
    var resolved = 0;
    final capped = members.take(4).toList();
    for (final m in capped) {
      final sub = await lookupArtist(m, withGenres: withGenres);
      if (sub['status'] != 'ok') continue;
      final genres = (sub['genres'] as List?) ?? const [];
      if (genres.isEmpty && !pyTruthy(sub['country'])) continue;
      resolved++;
      for (final g in genres) {
        final gm = g is Map ? g : const <Object?, Object?>{};
        final nm = gm['name'];
        if (!pyTruthy(nm)) continue;
        final c = pyIntOr(gm['count'], 1);
        genreCounts['$nm'] = (genreCounts['$nm'] ?? 0) + (c > 1 ? c : 1);
      }
      if (pyTruthy(sub['country'])) countries.add(sub['country']! as String);
      if (pyTruthy(sub['mbid'])) mbids.add(sub['mbid']!);
    }
    if (resolved == 0) return null;
    final country = countries.isNotEmpty && countries.toSet().length == 1
        ? countries.first
        : null;
    final genres = [
      for (final e in mostCommon(genreCounts, 8))
        {'name': e.key, 'count': e.value},
    ];
    final full = resolved == capped.length;
    return {
      'status': full ? 'ok' : 'lowconfidence',
      'mbid': mbids.isNotEmpty ? mbids.first : null,
      'country': country,
      'area': null,
      'genres': genres,
      'score': full ? 100 : 50,
      'reason': genres.isNotEmpty
          ? null
          : (full ? 'no_tags' : 'partial_credit'),
    };
  }

  /// `search_candidates`: a direct search, every hit as a candidate.
  Future<List<Map<String, Object?>>> searchCandidates(
    String? name, {
    int limit = 10,
  }) async {
    final raw = pyStrip(name ?? '');
    if (raw.isEmpty) return [];
    final q = escapeLucene(raw);
    final (data, _) = await get('artist', {
      'query': 'artist:"$q" OR alias:"$q" OR $q',
      'fmt': 'json',
      'limit': '$limit',
    });
    if (data == null || data.isEmpty) return [];
    final arts = data['artists'] is List
        ? data['artists'] as List
        : const <Object?>[];
    return [
      for (final a in arts.whereType<Map<String, Object?>>())
        {
          'name': pyTruthy(a['name']) ? a['name'] : raw,
          'mbid': a['id'],
          'disambiguation': a['disambiguation'] ?? '',
          'country': extractCountry(a),
          'area': _areaName(a),
          'genres': extractGenres(a),
          'score': pyIntOr(a['score'], 0),
          'is_junk': looksLikeJunk(a),
        },
    ];
  }

  /// `musicbrainz_artist_details`: genres/country/area for a known MBID;
  /// {} on any failure.
  Future<Map<String, Object?>> artistDetails(String? mbid) async {
    if (mbid == null || mbid.isEmpty) return {};
    final (gdata, _) = await get('artist/$mbid', {
      'inc': 'genres+tags',
      'fmt': 'json',
    });
    if (gdata == null || gdata.isEmpty) return {};
    return {
      'genres': extractGenres(gdata),
      'country': extractCountry(gdata),
      'area': _areaName(gdata),
    };
  }
}

/// `search_musicbrainz_artists_candidates`.
Future<List<Map<String, Object?>>> searchMusicBrainzCandidates(
  String name, {
  String contact = kMusicBrainzDefaultContact,
  int limit = 10,
}) async {
  final client = MusicBrainzClient(contact: contact);
  try {
    return await client.searchCandidates(name, limit: limit);
  } finally {
    client.close();
  }
}

/// `musicbrainz_artist_details`.
Future<Map<String, Object?>> musicBrainzArtistDetails(
  String mbid, {
  String contact = kMusicBrainzDefaultContact,
}) async {
  final client = MusicBrainzClient(contact: contact);
  try {
    return await client.artistDetails(mbid);
  } catch (e) {
    debugPrint('MB detail lookup failed for $mbid: $e');
    return {};
  } finally {
    client.close();
  }
}

/// `Counter.most_common(n)`: count-descending, ties in insertion order.
List<MapEntry<String, int>> mostCommon(Map<String, int> counts, [int? n]) {
  final entries = counts.entries.toList();
  final idx = [for (var i = 0; i < entries.length; i++) i];
  idx.sort((a, b) {
    final c = entries[b].value.compareTo(entries[a].value);
    return c != 0 ? c : a.compareTo(b);
  });
  final ordered = [for (final i in idx) entries[i]];
  return n == null ? ordered : ordered.take(n).toList();
}

/// `str.split()`.
List<String> _pySplit(String s) {
  final out = <String>[];
  final b = StringBuffer();
  for (final r in s.runes) {
    if (pyIsSpace(r)) {
      if (b.isNotEmpty) {
        out.add(b.toString());
        b.clear();
      }
    } else {
      b.writeCharCode(r);
    }
  }
  if (b.isNotEmpty) out.add(b.toString());
  return out;
}
