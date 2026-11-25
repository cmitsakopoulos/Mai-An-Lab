import 'package:flutter/foundation.dart';

import '../core/py_text.dart';
import '../core/py_values.dart';
import '../sources/source_client.dart';
import 'musicbrainz.dart' show nameClose;

/// `metadata_qobuz.py`: an artist's genres from the albums Qobuz holds for
/// them, folded onto the library's own genre vocabulary.

/// Qobuz's top-level genres by stable id (`_FAMILY_BY_ID`).
const _familyById = <int, String>{
  112: 'rock',
  80: 'jazz',
  10: 'classical',
  6: 'chanson',
  64: 'electronic',
  127: 'soul',
  116: 'metal',
  133: 'hip hop',
  2: 'folk',
  91: 'soundtrack',
  94: 'world',
  123: 'reggae',
};

/// "Divers" and "Enfants": no genre information.
const _junkFamilyIds = {1, 167};

/// "Musiques du monde": its subgenre slugs are mostly country names.
const _geographicFamilyId = 94;

/// `_SLUG_TO_CORPUS`; a null value drops the slug.
const _slugToCorpus = <String, String?>{
  'rap-hip-hop': 'hip hop',
  'alternatif-et-inde': 'alternative rock',
  'punk-new-wave': 'new wave',
  'musiques-du-monde': 'world',
  'electronique': 'electronic',
  'electro': 'electronic',
  'electronique-ou-concrete': 'electronic',
  'classique': 'classical',
  'chanson-francaise': 'chanson',
  'bandes-originales': 'soundtrack',
  'bandes-originales-de-films': 'soundtrack',
  'series-tv': 'soundtrack',
  'soul-funk-r-b': 'soul',
  'rb': 'rhythm and blues',
  'blues-country-folk': 'folk',
  'pop-rock': 'pop rock',
  'afrique': 'african',
  'amerique-latine': 'latin',
  'divers': null,
  'enfants': null,
};

const _albumSample = 12;
const _minAttestingAlbums = 2;

/// `_tokens_for_genre`: one album genre object → corpus tokens.
List<String> tokensForGenre(Object? genre) {
  if (genre is! Map) return const [];
  final path = genre['path'];
  final rawFamily = path is List && path.isNotEmpty ? path[0] : genre['id'];
  final int? familyId = switch (rawFamily) {
    final int i => i,
    final double d when d.isFinite => d.truncate(),
    final String s => int.tryParse(pyStrip(s)),
    final bool b => b ? 1 : 0,
    _ => null,
  };
  if (_junkFamilyIds.contains(familyId)) return const [];

  final slug = pyLower(
    pyStrip(genre['slug'] is String ? genre['slug']! as String : ''),
  );
  if (_slugToCorpus.containsKey(slug)) {
    final mapped = _slugToCorpus[slug];
    return mapped == null ? const [] : [mapped];
  }
  if (familyId == _geographicFamilyId) return const ['world'];
  if (slug.isNotEmpty) return [slug.replaceAll('-', ' ')];
  final fam = _familyById[familyId];
  return fam == null ? const [] : [fam];
}

/// `lookup_artist_genres`: one artist → `{'genres', 'qobuz_id',
/// 'matched_name', ...}` on a confident hit, else `{'genres': [], 'reason'}`.
/// Never throws.
Future<Map<String, Object?>> lookupQobuzArtistGenres(
  SourceClient? client,
  String? name,
) async {
  final raw = pyStrip(name ?? '');
  if (raw.isEmpty || client == null) {
    return {'genres': <Object?>[], 'reason': 'unavailable'};
  }
  final List<Map<String, Object?>> pages;
  try {
    pages = await client.search('artist', raw, limit: 5);
  } catch (e) {
    debugPrint('Qobuz artist search failed for "$raw": $e');
    return {'genres': <Object?>[], 'reason': 'error: ${_excName(e)}: $e'};
  }
  final items = <Object?>[];
  for (final page in pages) {
    final artists = page['artists'];
    final its = artists is Map ? artists['items'] : null;
    if (its is List) items.addAll(its);
  }
  Map<String, Object?>? match;
  for (final a in items) {
    if (a is Map<String, Object?> &&
        nameClose(raw, a['name'] is String ? a['name']! as String : '')) {
      match = a;
      break;
    }
  }
  if (match == null) {
    return {'genres': <Object?>[], 'reason': 'no_confident_match'};
  }

  final aid = match['id'];
  final Map<String, Object?> detail;
  try {
    detail = await client.getMetadata(pyStr(aid), 'artist');
  } catch (e) {
    debugPrint('Qobuz artist/get failed for "$raw" ($aid): $e');
    return {'genres': <Object?>[], 'reason': 'error: ${_excName(e)}: $e'};
  }
  final albumsObj = detail['albums'];
  final allAlbums = albumsObj is Map && albumsObj['items'] is List
      ? albumsObj['items'] as List
      : const <Object?>[];
  final albums = allAlbums.take(_albumSample).toList();
  final counts = <String, int>{};
  var attesting = 0;
  for (final al in albums) {
    final g = al is Map ? al['genre'] : null;
    final toks = tokensForGenre(pyTruthy(g) ? g : const <String, Object?>{});
    if (toks.isNotEmpty) attesting++;
    for (final t in toks) {
      counts[t] = (counts[t] ?? 0) + 1;
    }
  }
  if (counts.isEmpty) {
    return {
      'genres': <Object?>[],
      'reason': 'no_album_genres',
      'qobuz_id': aid,
      'matched_name': match['name'],
    };
  }
  if (attesting < _minAttestingAlbums && albums.length >= _minAttestingAlbums) {
    return {
      'genres': <Object?>[],
      'reason': 'thin_evidence',
      'qobuz_id': aid,
      'matched_name': match['name'],
    };
  }
  final ordered = counts.entries.toList()
    ..sort((a, b) {
      final c = b.value.compareTo(a.value);
      return c != 0 ? c : pyStrCompare(a.key, b.key);
    });
  return {
    'genres': [
      for (final e in ordered.take(6)) {'name': e.key, 'count': e.value},
    ],
    'qobuz_id': aid,
    'matched_name': match['name'],
    'albums_seen': albums.length,
    'reason': null,
  };
}

/// Python's `type(exc).__name__` for the reason strings.
String _excName(Object e) => switch (e) {
  ArgumentError _ => 'ValueError',
  StateError _ => 'RuntimeError',
  _ => e.runtimeType.toString(),
};

/// `open_client(raise_on_error=True)`: a logged-in Qobuz client from
/// [factory]; throws a [StateError] naming the failure.
Future<SourceClient> openQobuzClient(
  Future<SourceClient> Function() factory,
) async {
  SourceClient? client;
  try {
    client = await factory();
    await client.login();
    return client;
  } catch (e) {
    debugPrint('Qobuz client initialization failed ($e)');
    try {
      await client?.close();
    } catch (_) {}
    throw StateError('Qobuz client initialization failed (${_excName(e)}: $e)');
  }
}

/// `close_client`.
Future<void> closeQobuzClient(SourceClient? client) async {
  if (client == null) return;
  try {
    await client.close();
  } catch (e) {
    debugPrint('Qobuz client close failed: $e');
  }
}

/// `fetch_qobuz_artist_metadata`: one artist, failing loudly when Qobuz
/// can't be used.
Future<Map<String, Object?>> fetchQobuzArtistMetadata(
  Future<SourceClient> Function() factory,
  String name,
) async {
  final client = await openQobuzClient(factory);
  try {
    return await lookupQobuzArtistGenres(client, name);
  } finally {
    await closeQobuzClient(client);
  }
}
