// Port of `StreamripApp/utils/source_metadata.py`.
//
// Per-source normalization of raw Qobuz and Deezer API payloads into one
// common shape for the download & tagging pipeline.

/// Pull the track maps out of a metadata payload, attaching album context.
List<Map<String, Object?>> extractTracks(
  String source,
  String mediaType,
  Map<String, Object?> meta,
) {
  if (source == 'deezer') {
    if (mediaType == 'track') return [meta];
    final raw = meta['tracks'];
    final tracks = _mapList(raw);
    if (mediaType == 'album') {
      for (final t in tracks) {
        t.putIfAbsent('album', () => meta);
      }
    }
    return tracks;
  }

  // qobuz
  if (mediaType == 'track') return [meta];
  final tracksObj = meta['tracks'];
  final items = tracksObj is Map ? tracksObj['items'] : null;
  final tracks = _mapList(items);
  if (mediaType == 'album') {
    for (final t in tracks) {
      if (!t.containsKey('album')) {
        t['album'] = meta;
      }
    }
  }
  return tracks;
}

/// Map one raw track payload onto the common normalized shape.
Map<String, Object?> normalizeTrack(
  String source,
  Map<String, Object?> trackMeta,
  int fallbackNumber,
) {
  if (source == 'deezer') {
    return _normalizeDeezer(trackMeta, fallbackNumber);
  }
  return _normalizeQobuz(trackMeta, fallbackNumber);
}

Map<String, Object?> _normalizeQobuz(
  Map<String, Object?> trackMeta,
  int fallbackNumber,
) {
  final album = _asMap(trackMeta['album']);

  final imageRaw = album['image'];
  var coverUrl = '';
  if (imageRaw is String) {
    coverUrl = imageRaw;
  } else if (imageRaw is Map) {
    coverUrl =
        _truthyStr(imageRaw['large']) ??
        _truthyStr(imageRaw['extralarge']) ??
        _truthyStr(imageRaw['medium']) ??
        _truthyStr(imageRaw['small']) ??
        '';
  }

  final artistRaw = album.containsKey('artist')
      ? album['artist']
      : const <String, Object?>{};
  final albumArtist = artistRaw is Map
      ? (artistRaw.containsKey('name')
            ? '${artistRaw['name']}'
            : 'Unknown Artist')
      : '$artistRaw';

  final genreData = album['genre'];
  final genres = <String>[];
  if (genreData is Map) {
    final path = genreData['path'];
    if (path is List) {
      for (final g in path) {
        if (g is Map) {
          final n = g['name'];
          if (n != null) genres.add('$n');
        }
      }
    }
  }

  final performer = _asMap(trackMeta['performer']);
  final trackArtistMap = _asMap(trackMeta['artist']);
  final trackArtist =
      _truthyStr(performer['name']) ??
      _truthyStr(trackArtistMap['name']) ??
      'Unknown Artist';

  final composerRaw = trackMeta['composer'];
  final composer = composerRaw is Map ? composerRaw['name'] : null;

  final releaseDate = '${album['release_date'] ?? ''}';
  final yearPrefix = releaseDate.split('-').first;
  final year = yearPrefix.length > 4 ? yearPrefix.substring(0, 4) : yearPrefix;

  return {
    'id': '${trackMeta['id'] ?? ''}',
    'title': trackMeta.containsKey('title') ? trackMeta['title'] : 'Unknown',
    'artist': trackArtist,
    'track_number': trackMeta.containsKey('track_number')
        ? trackMeta['track_number']
        : fallbackNumber,
    'disc_number': trackMeta.containsKey('media_number')
        ? trackMeta['media_number']
        : 1,
    'isrc': trackMeta['isrc'],
    'composer': composer,
    'album': <String, Object?>{
      'id': '${album['id'] ?? ''}',
      'album': album.containsKey('title') ? album['title'] : 'Unknown Album',
      'artist': albumArtist,
      'year': year,
      'tracktotal': album.containsKey('tracks_count')
          ? album['tracks_count']
          : 1,
      'disctotal': album.containsKey('media_count') ? album['media_count'] : 1,
      'genres': genres.isNotEmpty ? genres : null,
      'copyright': album['copyright'],
      'cover_url': coverUrl,
    },
  };
}

Map<String, Object?> _normalizeDeezer(
  Map<String, Object?> trackMeta,
  int fallbackNumber,
) {
  final album = _asMap(trackMeta['album']);

  final coverUrl =
      _truthyStr(album['cover_xl']) ??
      _truthyStr(album['cover_big']) ??
      _truthyStr(album['cover_medium']) ??
      _truthyStr(album['cover']) ??
      '';

  final albumArtist =
      _deezerArtistName(album['artist']) ??
      _deezerArtistName(trackMeta['artist']) ??
      'Unknown Artist';

  final genreData = album['genres'];
  final genres = <String>[];
  if (genreData is Map) {
    final data = genreData['data'];
    if (data is List) {
      for (final g in data) {
        if (g is Map) {
          final n = g['name'];
          if (n != null) genres.add('$n');
        }
      }
    }
  }

  var disctotal = 1;
  final albumTracks = album['tracks'];
  if (albumTracks is List && albumTracks.isNotEmpty) {
    for (final t in albumTracks) {
      if (t is Map) {
        final d = t['disk_number'];
        final dn = d is int ? d : (d is num ? d.toInt() : 1);
        if (dn > disctotal) disctotal = dn;
      }
    }
  }

  final trackArtist =
      _deezerArtistName(trackMeta['artist']) ??
      _deezerContributorName(trackMeta) ??
      'Unknown Artist';

  final releaseDate = '${album['release_date'] ?? ''}';
  final yearPrefix = releaseDate.split('-').first;
  final year = yearPrefix.length > 4 ? yearPrefix.substring(0, 4) : yearPrefix;

  final trackTotal = _isTruthy(album['track_total'])
      ? album['track_total']
      : (album.containsKey('nb_tracks') ? album['nb_tracks'] : 1);

  return {
    'id': '${trackMeta['id'] ?? ''}',
    'title': trackMeta.containsKey('title') ? trackMeta['title'] : 'Unknown',
    'artist': trackArtist,
    'track_number': trackMeta.containsKey('track_position')
        ? trackMeta['track_position']
        : fallbackNumber,
    'disc_number': trackMeta.containsKey('disk_number')
        ? trackMeta['disk_number']
        : 1,
    'isrc': trackMeta['isrc'],
    'composer': null,
    'album': <String, Object?>{
      'id': '${album['id'] ?? ''}',
      'album': album.containsKey('title') ? album['title'] : 'Unknown Album',
      'artist': albumArtist,
      'year': year,
      'tracktotal': trackTotal,
      'disctotal': disctotal,
      'genres': genres.isNotEmpty ? genres : null,
      'copyright': null,
      'cover_url': coverUrl,
    },
  };
}

String? _deezerArtistName(Object? value) {
  if (value is Map) return _truthyStr(value['name']);
  if (value is String && value.isNotEmpty) return value;
  return null;
}

String? _deezerContributorName(Map<String, Object?> trackMeta) {
  final contributors = trackMeta['contributors'];
  if (contributors is List) {
    for (final c in contributors) {
      if (c is Map) {
        final name = _truthyStr(c['name']);
        if (name != null) return name;
      }
    }
  }
  return null;
}

Map<String, Object?> _asMap(Object? v) {
  if (v is Map<String, Object?>) return v;
  if (v is Map) {
    return {for (final e in v.entries) '${e.key}': e.value};
  }
  return const {};
}

List<Map<String, Object?>> _mapList(Object? v) {
  if (v is! List) return [];
  return [
    for (final item in v)
      if (item is Map<String, Object?>)
        item
      else if (item is Map)
        {for (final e in item.entries) '${e.key}': e.value},
  ];
}

bool _isTruthy(Object? v) => switch (v) {
  null => false,
  bool b => b,
  num n => n != 0,
  String s => s.isNotEmpty,
  Iterable<Object?> l => l.isNotEmpty,
  Map<Object?, Object?> m => m.isNotEmpty,
  _ => true,
};

String? _truthyStr(Object? v) {
  if (v is String && v.isNotEmpty) return v;
  return null;
}
