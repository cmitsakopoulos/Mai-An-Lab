import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/text_fold.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/track_query.dart';

Track _t(
  int id,
  String title, {
  String artist = 'A',
  String album = 'X',
  double? added,
}) => Track(
  id: id,
  path: '/m/$id',
  title: title,
  artist: artist,
  album: album,
  duration: 0,
  format: 'FLAC',
  addedDate: added,
);

void main() {
  group('fold (parity with FTS5 unicode61 remove_diacritics)', () {
    test('Latin and Greek accents, case', () {
      expect(fold('Björk Guðmundsdóttir'), 'bjork guðmundsdottir');
      expect(fold('Αγάπη ΜΟΥ'), 'αγαπη μου');
      expect(fold('Œuvre Straße'), 'oeuvre strasse');
    });
    test('drops combining marks (NFD text, e.g. macOS file names)', () {
      expect(fold('Björk'), 'bjork');
    });
  });

  test('queryWords strips the punctuation DBManager._safe_fts strips', () {
    // Python: _safe_fts('Jay-Z (Remix), "live"') == 'Jay Z Remix live'
    expect(queryWords('Jay-Z (Remix), "live"'), ['jay', 'z', 'remix', 'live']);
    expect(queryWords('  ()  '), isEmpty);
  });

  test('bigramSimilarity matches DatabaseManager._kmer_similarity', () {
    // Reference values printed by the Python function itself.
    const cases = {
      ('crywolf', 'Cry Wolf'): 0.7692307692307693,
      ('Chiltern', 'Chiltern Firehouse'): 0.888888888888889,
      ('radiohed', 'Radiohead'): 0.8571428571428571,
      ('xyz', 'Cry Wolf'): 0.0,
      ('Björk', 'björk'): 1.0,
    };
    cases.forEach((pair, expected) {
      expect(bigramSimilarity(pair.$1, pair.$2), closeTo(expected, 1e-12));
    });
  });

  test('closestMatches keeps scores >= 0.25, best first, stable on ties', () {
    final tracks = [
      _t(1, 'Radiohead Live'),
      _t(2, 'Nothing alike'),
      _t(3, 'Radiohead'),
    ];
    expect(closestMatches(tracks, 'radiohed').map((t) => t.id), [3, 1]);
  });

  group('sortTracks (parity with get_all_tracks sort_map)', () {
    test('date: newest first, undated last', () {
      final s = sortTracks([
        _t(1, 'a', added: 10),
        _t(2, 'b'),
        _t(3, 'c', added: 30),
      ], LibrarySort.date);
      expect(s.map((t) => t.id), [3, 1, 2]);
    });
    test('NOCASE folds ASCII only, like SQLite', () {
      final s = sortTracks([
        _t(1, 'b'),
        _t(2, 'B'),
        _t(3, 'a'),
        _t(4, 'É'),
      ], LibrarySort.track);
      // 'É' (U+00C9) sorts after ASCII letters: SQLite compares bytes.
      expect(s.map((t) => t.id), [3, 1, 2, 4]);
    });
    test('fromConfig falls back to date', () {
      expect(LibrarySort.fromConfig('artist'), LibrarySort.artist);
      expect(LibrarySort.fromConfig('name'), LibrarySort.date);
      expect(LibrarySort.fromConfig(null), LibrarySort.date);
    });
  });
}
