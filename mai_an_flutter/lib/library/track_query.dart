import '../core/text_fold.dart';
import '../data/library_repository.dart';

/// The Tracks tab's sort modes, as in the Flet app
/// (`db_manager.get_all_tracks` sort_map; the default comes from config
/// `general.library_sort`, falling back to "date").
enum LibrarySort {
  date('Date Added'),
  artist('Artist (A–Z)'),
  album('Album (A–Z)'),
  track('Track (A–Z)');

  const LibrarySort(this.label);
  final String label;

  static LibrarySort fromConfig(String? value) => LibrarySort.values.firstWhere(
    (s) => s.name == value,
    orElse: () => LibrarySort.date,
  );
}

/// Sorted copy, matching the Flet SQL:
///   date   → added_date DESC        artist → artist COLLATE NOCASE
///   album  → album COLLATE NOCASE   track  → title COLLATE NOCASE
/// Sorting in memory instead of re-querying makes switching modes instant.
/// Ties break on id so the order is deterministic (SQLite leaves it unspecified).
List<Track> sortTracks(List<Track> tracks, LibrarySort sort) {
  int byId(Track a, Track b) => a.id.compareTo(b.id);
  final Comparator<Track> cmp = switch (sort) {
    // SQLite sorts NULL lowest, so DESC puts tracks without a date last.
    LibrarySort.date => (a, b) {
      final c = (b.addedDate ?? double.negativeInfinity).compareTo(
        a.addedDate ?? double.negativeInfinity,
      );
      return c != 0 ? c : -byId(a, b);
    },
    LibrarySort.artist => (a, b) {
      final c = _nocase(a.artist).compareTo(_nocase(b.artist));
      return c != 0 ? c : byId(a, b);
    },
    LibrarySort.album => (a, b) {
      final c = _nocase(a.album).compareTo(_nocase(b.album));
      return c != 0 ? c : byId(a, b);
    },
    LibrarySort.track => (a, b) {
      final c = _nocase(a.title).compareTo(_nocase(b.title));
      return c != 0 ? c : byId(a, b);
    },
  };
  return [...tracks]..sort(cmp);
}

/// SQLite's NOCASE folds ASCII A–Z only.
String _nocase(String s) => String.fromCharCodes(
  s.codeUnits.map((c) => c >= 0x41 && c <= 0x5A ? c + 32 : c),
);

/// The punctuation `DBManager._safe_fts` strips before an FTS query.
final _ftsPunctuation = RegExp(r'''["*\-^'\[\]{}~:()|,]''');

/// Query → the words every match must contain (folded, punctuation-free).
/// Empty when the query has no searchable content.
List<String> queryWords(String raw) => fold(
  raw.replaceAll(_ftsPunctuation, ' '),
).split(RegExp(r'\s+')).where((w) => w.isNotEmpty).toList();

/// Text a track is searched by: title, artist and album, as FTS indexes them.
String searchKey(Track t) =>
    fold('${t.title}\u0000${t.artist}\u0000${t.album}');

/// Port of `DBManager._kmer_similarity`: 1.0 for equal, 0.8–1.0 for a
/// substring, else the Dice coefficient over character bigrams.
double bigramSimilarity(String query, String target) {
  if (query.isEmpty || target.isEmpty) return 0;
  final q = query.toLowerCase().trim();
  final t = target.toLowerCase().trim();
  if (q == t) return 1;
  if (t.contains(q)) return 0.8 + 0.2 * (q.length / t.length);
  Set<String> bigrams(String s) => {
    for (var i = 0; i < s.length - 1; i++) s.substring(i, i + 2),
  };
  final qs = bigrams(q), ts = bigrams(t);
  if (qs.isEmpty || ts.isEmpty) return 0;
  return 2 * qs.intersection(ts).length / (qs.length + ts.length);
}

/// The Flet app's last-resort search: when nothing matches, rank every track
/// by its best field score and keep those ≥ 0.25 ("CLOSEST MATCH").
/// [tracks] is already in display order; equal scores keep that order.
List<Track> closestMatches(List<Track> tracks, String rawQuery) =>
    closestBy(tracks, rawQuery, (t) => [t.title, t.album, t.artist]);

/// [closestMatches] for any row: [fields] are the texts a row is scored on
/// (its best one counts).
List<T> closestBy<T>(
  List<T> rows,
  String rawQuery,
  List<String> Function(T) fields,
) {
  final scored = <(double, int)>[];
  for (var i = 0; i < rows.length; i++) {
    final s = fields(
      rows[i],
    ).map((f) => bigramSimilarity(rawQuery, f)).reduce((a, b) => a > b ? a : b);
    if (s >= 0.25) scored.add((s, i));
  }
  scored.sort((a, b) {
    final c = b.$1.compareTo(a.$1);
    return c != 0 ? c : a.$2.compareTo(b.$2);
  });
  return [for (final (_, i) in scored) rows[i]];
}

/// One library view's search: rows whose folded key holds every query word,
/// or whose id [matchedIds] reports (an album or artist matching through its
/// tracks); when none do, [closestBy] over [fields]. [rows] carry their
/// folded keys and are in display order. Returns the rows to show and
/// whether they are the closest-match fallback.
(List<T>, bool) searchRows<T>(
  List<(T, String)> rows,
  String rawQuery, {
  required List<String> Function(T) fields,
  Set<int> Function(List<String> words)? matchedIds,
  int Function(T)? id,
}) {
  final words = queryWords(rawQuery);
  if (words.isEmpty) return ([for (final (r, _) in rows) r], false);
  final ids = matchedIds?.call(words) ?? const <int>{};
  final hits = [
    for (final (r, key) in rows)
      if (words.every(key.contains) || (id != null && ids.contains(id(r)))) r,
  ];
  if (hits.isNotEmpty) return (hits, false);
  return (
    closestBy([for (final (r, _) in rows) r], rawQuery.trim(), fields),
    true,
  );
}
