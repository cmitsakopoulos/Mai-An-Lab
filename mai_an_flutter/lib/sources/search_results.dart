// Port of `StreamripApp/utils/search_results.py`.

/// First usable artwork URL among the shapes Qobuz and Deezer use.
String pickImage(List<Object?> candidates) {
  for (final raw in candidates) {
    if (raw is String && raw.isNotEmpty) return raw;
    if (raw is Map && raw.isNotEmpty) {
      final picked =
          _nonEmptyStr(raw['large']) ??
          _nonEmptyStr(raw['extralarge']) ??
          _nonEmptyStr(raw['medium']) ??
          _nonEmptyStr(raw['small']);
      if (picked != null) return picked;
      for (final v in raw.values) {
        if (v is String && v.isNotEmpty) return v;
      }
    }
  }
  return '';
}

/// Deezer-style flat artwork keys, largest first.
String flatCover(Object? item) {
  if (item is! Map) return '';
  const keys = [
    'cover_xl',
    'cover_big',
    'cover_medium',
    'cover',
    'picture_xl',
    'picture_big',
    'picture_medium',
    'picture',
  ];
  for (final key in keys) {
    final val = item[key];
    if (val is String && val.isNotEmpty) return val;
  }
  return '';
}

String cleanSummaryText(Object? raw, {bool trunc = true}) {
  var s = raw is String ? raw : '$raw';
  s = s.replaceAll('|', '').replaceAll('\n', '');
  if (trunc && s.length > 50) {
    return s.substring(0, 50);
  }
  return s;
}

sealed class SearchSummary {
  const SearchSummary();

  String get id;
  String get name;
  String get mediaType;
  String summarize();
  String preview();

  @override
  String toString() => summarize();
}

class ArtistSummary extends SearchSummary {
  const ArtistSummary({
    required this.id,
    required this.name,
    required this.numAlbums,
  });

  @override
  final String id;
  @override
  final String name;
  final Object numAlbums;

  @override
  String get mediaType => 'artist';

  @override
  String summarize() => cleanSummaryText(name);

  @override
  String preview() => '$numAlbums Albums\n\nID: $id';

  factory ArtistSummary.fromItem(Map<String, Object?> item) {
    final id = '${item['id']}';
    final performer = _asMap(item['performer']);
    final artistObj = item['artist'];
    final publisher = _asMap(item['publisher_metadata']);
    final name =
        _nonEmptyStr(item['name']) ??
        _nonEmptyStr(performer['name']) ??
        _nonEmptyStr(artistObj) ??
        _nonEmptyStr(_asMap(artistObj)['name']) ??
        _nonEmptyStr(publisher['artist']) ??
        'Unknown';
    final rawAlbums = item['albums_count'];
    final numAlbums = _isTruthy(rawAlbums) ? rawAlbums! : 'Unknown';
    return ArtistSummary(id: id, name: name, numAlbums: numAlbums);
  }
}

class TrackSummary extends SearchSummary {
  const TrackSummary({
    required this.id,
    required this.name,
    required this.artist,
    required this.dateReleased,
    required this.imageUrl,
    required this.albumName,
  });

  @override
  final String id;
  @override
  final String name;
  final String artist;
  final String? dateReleased;
  final String imageUrl;
  final String albumName;

  @override
  String get mediaType => 'track';

  @override
  String summarize() =>
      '${cleanSummaryText(name)} by ${cleanSummaryText(artist)}';

  @override
  String preview() => 'Released on:\n$dateReleased\n\nID: $id';

  factory TrackSummary.fromItem(Map<String, Object?> item) {
    final id = '${item['id']}';
    final rawName =
        _nonEmptyStr(item['title']) ?? _nonEmptyStr(item['name']) ?? 'Unknown';
    final performer = _asMap(item['performer']);
    final artistRaw = item['artist'];
    final publisher = _asMap(item['publisher_metadata']);

    Object? artistVal = _nonEmptyStr(performer['name']);
    artistVal ??= _isTruthy(artistRaw) ? artistRaw : null;
    artistVal ??= _nonEmptyStr(_asMap(artistRaw)['name']);
    artistVal ??= _nonEmptyStr(publisher['artist']);
    artistVal ??= 'Unknown';
    if (artistVal is Map && artistVal.containsKey('name')) {
      artistVal = artistVal['name'];
    }
    final artist = '$artistVal';

    final albumMap = _asMap(item['album']);
    final dateReleased =
        _truthyOrStr(item['release_date']) ??
        _truthyOrStr(item['streamStartDate']) ??
        _truthyOrStr(albumMap['release_date_original']) ??
        _truthyOrStr(item['display_date']) ??
        _truthyOrStr(item['date']) ??
        _truthyOrStr(item['year']) ??
        'Unknown';

    final albumName = _nonEmptyStr(albumMap['title']) ?? '';

    var imageUrl = pickImage([item['image'], albumMap['image']]);
    if (imageUrl.isEmpty) imageUrl = flatCover(item);
    if (imageUrl.isEmpty) imageUrl = flatCover(albumMap);

    return TrackSummary(
      id: id,
      name: rawName.trim(),
      artist: artist,
      dateReleased: dateReleased,
      imageUrl: imageUrl,
      albumName: albumName,
    );
  }
}

class AlbumSummary extends SearchSummary {
  const AlbumSummary({
    required this.id,
    required this.name,
    required this.artist,
    required this.numTracks,
    required this.dateReleased,
    required this.imageUrl,
  });

  @override
  final String id;
  @override
  final String name;
  final String artist;
  final String numTracks;
  final String? dateReleased;
  final String imageUrl;

  @override
  String get mediaType => 'album';

  @override
  String summarize() =>
      '${cleanSummaryText(name)} by ${cleanSummaryText(artist)}';

  @override
  String preview() =>
      'Date released:\n$dateReleased\n\n$numTracks Tracks\n\nID: $id';

  factory AlbumSummary.fromItem(Map<String, Object?> item) {
    final id = '${item['id']}';
    final title = (_nonEmptyStr(item['title']) ?? '').trim();
    final version = (_nonEmptyStr(item['version']) ?? '').trim();
    final name = title + (version.isNotEmpty ? ' ($version)' : '');

    final performer = _asMap(item['performer']);
    final artistRaw = item['artist'];
    final publisher = _asMap(item['publisher_metadata']);
    final artist =
        _nonEmptyStr(performer['name']) ??
        _nonEmptyStr(_asMap(artistRaw)['name']) ??
        _nonEmptyStr(artistRaw) ??
        _nonEmptyStr(publisher['artist']) ??
        'Unknown';

    var numTracksVal = _truthyInt(item['tracks_count']);
    numTracksVal ??= _truthyInt(item['nb_tracks']);
    numTracksVal ??= _truthyInt(item['numberOfTracks']);
    if (numTracksVal == null) {
      final t = item['tracks'];
      final it = item['items'];
      if (t is List && t.isNotEmpty) {
        numTracksVal = t.length;
      } else if (t is Map && t.isNotEmpty) {
        numTracksVal = t.length;
      } else if (it is List) {
        numTracksVal = it.length;
      } else {
        numTracksVal = 0;
      }
    }

    final dateReleased =
        _truthyOrStr(item['release_date_original']) ??
        _truthyOrStr(item['release_date']) ??
        _truthyOrStr(item['releaseDate']) ??
        _truthyOrStr(item['display_date']) ??
        _truthyOrStr(item['date']) ??
        _truthyOrStr(item['year']) ??
        'Unknown';

    var imageUrl = pickImage([item['image']]);
    if (imageUrl.isEmpty) imageUrl = flatCover(item);

    return AlbumSummary(
      id: id,
      name: name,
      artist: artist,
      numTracks: '$numTracksVal',
      dateReleased: dateReleased,
      imageUrl: imageUrl,
    );
  }
}

class LabelSummary extends SearchSummary {
  const LabelSummary({required this.id, required this.name});

  @override
  final String id;
  @override
  final String name;

  @override
  String get mediaType => 'label';

  @override
  String summarize() => name;

  @override
  String preview() => name;

  factory LabelSummary.fromItem(Map<String, Object?> item) =>
      LabelSummary(id: '${item['id']}', name: '${item['name']}');
}

class PlaylistSummary extends SearchSummary {
  const PlaylistSummary({
    required this.id,
    required this.name,
    required this.creator,
    required this.numTracks,
    required this.description,
  });

  @override
  final String id;
  @override
  final String name;
  final String creator;
  final int numTracks;
  final String description;

  @override
  String get mediaType => 'playlist';

  @override
  String summarize() =>
      '${cleanSummaryText(name)} by ${cleanSummaryText(creator)}';

  @override
  String preview() {
    final desc = cleanSummaryText(description, trunc: false);
    return '$numTracks tracks\n\nDescription:\n$desc\n\nID: $id';
  }

  factory PlaylistSummary.fromItem(Map<String, Object?> item) {
    final rawId = _isTruthy(item['id'])
        ? item['id']
        : (_isTruthy(item['uuid']) ? item['uuid'] : 'Unknown');
    final name =
        _nonEmptyStr(item['name']) ?? _nonEmptyStr(item['title']) ?? 'Unknown';
    final publisher = _asMap(item['publisher_metadata']);
    final owner = _asMap(item['owner']);
    final user = _asMap(item['user']);
    final creator =
        _nonEmptyStr(publisher['artist']) ??
        _nonEmptyStr(owner['name']) ??
        _nonEmptyStr(user['username']) ??
        _nonEmptyStr(user['name']) ??
        'Unknown';

    int? numTracks = _truthyInt(item['tracks_count']);
    numTracks ??= _truthyInt(item['nb_tracks']);
    numTracks ??= _truthyInt(item['numberOfTracks']);
    if (numTracks == null) {
      final t = item['tracks'];
      if (t is List && t.isNotEmpty) numTracks = t.length;
    }
    final description = _nonEmptyStr(item['description']) ?? 'No description';

    return PlaylistSummary(
      id: '$rawId',
      name: name,
      creator: creator,
      numTracks: numTracks ?? -1,
      description: description,
    );
  }
}

class SearchResults {
  const SearchResults(this.results);

  final List<SearchSummary> results;

  factory SearchResults.fromPages(
    String source,
    String mediaType,
    List<Map<String, Object?>> pages,
  ) {
    final SearchSummary Function(Map<String, Object?>) builder =
        switch (mediaType) {
          'track' => TrackSummary.fromItem,
          'album' => AlbumSummary.fromItem,
          'label' => LabelSummary.fromItem,
          'artist' => ArtistSummary.fromItem,
          'playlist' => PlaylistSummary.fromItem,
          _ => throw ArgumentError('invalid media type $mediaType'),
        };

    final key = '${mediaType}s';
    final out = <SearchSummary>[];
    for (final page in pages) {
      Object? rawItems;
      final nested = page[key];
      if (nested is Map && nested.containsKey('items')) {
        rawItems = nested['items'];
      } else if (page.containsKey('items')) {
        rawItems = page['items'];
      } else if (page.containsKey('data')) {
        rawItems = page['data'];
      } else {
        continue;
      }
      if (rawItems is! List) continue;
      for (final item in rawItems) {
        if (item is Map) {
          out.add(builder(_asMap(item)));
        }
      }
    }
    return SearchResults(out);
  }

  List<String> summaries() => [
    for (var i = 0; i < results.length; i++)
      '${i + 1}. ${results[i].summarize()}',
  ];
}

Map<String, Object?> _asMap(Object? v) {
  if (v is Map<String, Object?>) return v;
  if (v is Map) return {for (final e in v.entries) '${e.key}': e.value};
  return const {};
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

String? _nonEmptyStr(Object? v) => v is String && v.isNotEmpty ? v : null;

String? _truthyOrStr(Object? v) {
  if (!_isTruthy(v)) return null;
  return '$v';
}

int? _truthyInt(Object? v) {
  if (v is int && v != 0) return v;
  if (v is num && v != 0) return v.toInt();
  return null;
}
