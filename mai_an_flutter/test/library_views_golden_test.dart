// The Albums, Artists and Genres tabs against the Python app's output
// (PORT_MAP §0 rule 3): db_manager's list and expansion queries on a real
// library. Personal data, so the goldens sit beside the gitignored bundles:
//
//   ~/miniconda3/bin/python tool/export_station_goldens.py   # copies library.db
//   ~/miniconda3/bin/python tool/export_library_goldens.py   # writes the goldens
//   flutter test test/library_views_golden_test.dart
//
// LIBRARY_GOLDEN=/path/to/library_views.json picks a specific export. Skipped
// when there is none.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/data/browse_queries.dart';
import 'package:mai_an_lab/data/library_repository.dart';

import 'landing_queries_test.dart' show landingRows;

File? _golden() {
  final env = Platform.environment['LIBRARY_GOLDEN'];
  if (env != null) return File(env);
  final dir = Directory('../tools/state_backups/goldens');
  if (!dir.existsSync()) return null;
  final found = [
    for (final d in dir.listSync().whereType<Directory>())
      if (File('${d.path}/library_views.json').existsSync())
        File('${d.path}/library_views.json'),
  ]..sort((a, b) => b.path.compareTo(a.path));
  return found.firstOrNull;
}

List<Object?> _album(AlbumRow a) => [a.id, a.title, a.artist, a.trackCount];
List<Object?> _artist(ArtistRow a) => [
  a.id,
  a.name,
  a.albumCount,
  a.trackCount,
];

void main() {
  final file = _golden();
  if (file == null) {
    test('library view goldens', () {}, skip: 'no exported goldens');
    return;
  }
  final golden = jsonDecode(file.readAsStringSync()) as Map<String, dynamic>;
  late LibraryRepository repo;

  setUpAll(() async {
    repo = await LibraryRepository.open('${file.parent.path}/${golden['db']}');
  });
  tearDownAll(() => repo.close());

  test('albums, every sort', () async {
    for (final MapEntry(:key, :value) in (golden['albums'] as Map).entries) {
      final rows = await repo.allAlbums(key as String);
      expect([for (final a in rows) _album(a)], value, reason: key);
    }
  });

  test('artists, every sort', () async {
    for (final MapEntry(:key, :value) in (golden['artists'] as Map).entries) {
      final rows = await repo.allArtists(key as String);
      expect([for (final a in rows) _artist(a)], value, reason: key);
    }
  });

  test('genres, every sort', () async {
    for (final MapEntry(:key, :value) in (golden['genres'] as Map).entries) {
      final rows = await repo.allGenres(key as String);
      expect(
        [
          for (final g in rows)
            [g.name, g.artistCount, g.albumCount, g.trackCount],
        ],
        value,
        reason: key,
      );
    }
  });

  test('genre expansions: artists, their albums, the play queue', () async {
    for (final MapEntry(key: genre as String, :value)
        in (golden['by_genre'] as Map).entries) {
      final g = value as Map<String, dynamic>;
      final artists = await repo.artistsByGenre(genre);
      expect(
        [for (final a in artists) _artist(a)],
        g['artists'],
        reason: genre,
      );
      for (final MapEntry(key: artist as String, value: albums)
          in (g['albums'] as Map).entries) {
        final rows = await repo.albumsByArtist(artist, genre: genre);
        expect(
          [for (final a in rows) _album(a)],
          albums,
          reason: '$genre / $artist',
        );
      }
      final tracks = await repo.tracksByGenre(genre);
      expect([for (final t in tracks) t.path], g['tracks'], reason: genre);
    }
  });

  test('artist expansions', () async {
    for (final MapEntry(key: artist as String, value: albums)
        in (golden['by_artist'] as Map).entries) {
      final rows = await repo.albumsByArtist(artist);
      expect([for (final a in rows) _album(a)], albums, reason: artist);
    }
  });

  test('album expansions', () async {
    for (final a in golden['by_album'] as List) {
      final tracks = await repo.tracksByAlbum(
        a['album'] as String?,
        a['artist'] as String?,
      );
      expect([for (final t in tracks) t.path], a['tracks'], reason: '$a');
    }
  });

  test('Search landing queries', () async {
    final landing = golden['landing'] as Map<String, dynamic>?;
    if (landing == null) {
      markTestSkipped('export predates the landing queries');
      return;
    }
    for (final MapEntry(:key, :value) in landing.entries) {
      expect(await landingRows(repo, int.parse(key)), value, reason: key);
    }
  });
}
