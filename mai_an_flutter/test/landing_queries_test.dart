// The Search tab's landing queries against the Python app's output (PORT_MAP
// §0 rule 3): `get_most_played`, `get_most_played_artists`,
// `get_most_played_genres` and `get_library_counts` on the small library
// that tool/export_library_goldens.py builds (`LANDING_SETUP`). The real-
// library half is in library_views_golden_test.dart.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/data/library_repository.dart';

/// The JSON shape the export writes for one limit.
Future<Map<String, Object?>> landingRows(LibraryRepository repo, int n) async {
  final c = await repo.libraryCounts();
  return {
    'most_played': [
      for (final r in await repo.mostPlayed(n))
        [r.path, r.title, r.artist, r.count],
    ],
    'artists': [
      for (final r in await repo.mostPlayedArtists(n))
        [r.name, r.plays, r.secondary],
    ],
    'genres': [
      for (final r in await repo.mostPlayedGenres(n))
        [r.name, r.plays, r.secondary],
    ],
    'counts': [c.tracks, c.albums, c.artists, c.playlists],
  };
}

void main() {
  final golden =
      (jsonDecode(
                File('test/goldens/library_synthetic.json').readAsStringSync(),
              )
              as Map<String, dynamic>)['landing']
          as Map<String, dynamic>;

  test('landing queries match db_manager on the synthetic library', () async {
    final dir = Directory.systemTemp.createTempSync('landing');
    addTearDown(() => dir.deleteSync(recursive: true));
    final repo = await LibraryRepository.open(
      '${dir.path}/library.db',
      writable: true,
    );
    addTearDown(repo.close);
    await repo.debugExecute([
      for (final s in (golden['setup'] as List).cast<String>()) (s, const []),
    ]);
    for (final MapEntry(:key, :value)
        in (golden['limits'] as Map<String, dynamic>).entries) {
      expect(await landingRows(repo, int.parse(key)), value, reason: key);
    }
  });
}
