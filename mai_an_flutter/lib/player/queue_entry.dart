import 'dart:convert';

import '../data/library_repository.dart';

/// Tag keys from utils/queue_layout.py.
const kQueued = '_queued';
const kAutoplay = '_autoplay';

/// One row of the play queue, as the Flet app stores it in queue_state.json:
///
///     {"path", "track_title", "artist_name", "album_title", "duration"?,
///      "_queued"?, "_autoplay"?, ...}
///
/// The map is kept whole, unknown keys included, so a queue the Flet app wrote
/// survives a round trip through the port (and back, until cutover).
///
/// Rows are compared by IDENTITY, like the Flet queue sheet: the same song
/// can be queued twice, and each row is a separate entry.
class QueueEntry {
  QueueEntry(Map<String, Object?> data) : data = Map.unmodifiable(data);

  factory QueueEntry.fromTrack(Track t) => QueueEntry({
    'path': t.path,
    'track_title': t.title,
    'artist_name': t.artist,
    'album_title': t.album,
    // Flet's _play_track_core leaves this out; its engine then seeds the
    // slider from 0 until the decoder reports. Carrying it is harmless to the
    // Flet reader (it reads "duration" when present) and spares that gap.
    if (t.duration > 0) 'duration': t.duration,
  });

  final Map<String, Object?> data;

  String get path => _str(data['path']) ?? '';

  /// `track_title or basename(path) or "Unknown"` (engine._track_to_playlist_item).
  String get title {
    final t = _str(data['track_title']);
    if (t != null && t.isNotEmpty) return t;
    final base = path.split('/').last;
    return base.isNotEmpty ? base : 'Unknown';
  }

  /// `track.get("artist_name", "Unknown Artist")`: a present-but-empty value
  /// stays empty, as in Python.
  String get artist => _str(data['artist_name']) ?? 'Unknown Artist';
  String get album => _str(data['album_title']) ?? 'Unknown Album';

  /// Seconds; 0 when unknown.
  double get duration {
    final d = data['duration'];
    if (d is num) return d.toDouble();
    if (d is String) return double.tryParse(d) ?? 0;
    return 0;
  }

  bool get queued => data[kQueued] == true;
  bool get autoplay => data[kAutoplay] == true;

  /// The id the native player knows this row by (MediaItem.id).
  String get src => pathToUri(path);

  /// queue_layout.as_queued: a copy marked as the listener's pick.
  QueueEntry asQueued() => QueueEntry(
    {...data}
      ..remove(kAutoplay)
      ..[kQueued] = true,
  );

  QueueEntry withTags(Map<String, Object?> tags) =>
      QueueEntry({...data, ...tags});

  Map<String, Object?> toJson() => data;

  static String? _str(Object? v) => v == null ? null : '$v';

  @override
  String toString() => 'QueueEntry($path)';
}

const _unreserved = '-._~/';

/// `AudioEngine._to_uri`: Python's `pathlib.Path(p).as_uri()`, byte for byte.
/// The result is the MediaItem id, so it is also what the play ledger and the
/// native resume record store. Matching Python exactly keeps those files
/// identical whichever app wrote them.
///
/// pathlib percent-encodes every UTF-8 byte except ASCII letters, digits and
/// `-._~/`. (Dart's Uri.file leaves `(`, `'`, `&`, … alone, so it isn't used.)
String pathToUri(String p) {
  if (p.isEmpty) return '';
  if (p.startsWith('http://') ||
      p.startsWith('https://') ||
      p.startsWith('file://')) {
    return p;
  }
  // as_uri() raises on a relative path and _to_uri falls back to it as is.
  if (!p.startsWith('/')) return p;
  final out = StringBuffer('file://');
  for (final b in utf8.encode(p)) {
    final isAlnum =
        (b >= 0x30 && b <= 0x39) ||
        (b >= 0x41 && b <= 0x5A) ||
        (b >= 0x61 && b <= 0x7A);
    if (isAlnum || _unreserved.codeUnits.contains(b)) {
      out.writeCharCode(b);
    } else {
      out.write('%${b.toRadixString(16).toUpperCase().padLeft(2, '0')}');
    }
  }
  return out.toString();
}

/// `play_ledger.src_to_path`: file URIs and bare paths map back to the library
/// path; network streams have no library identity and map to ''.
String srcToPath(String src) {
  if (src.isEmpty) return '';
  if (src.startsWith('http://') || src.startsWith('https://')) return '';
  if (!src.startsWith('file://')) return src;
  // urlparse: drop the netloc, then the query and fragment.
  var rest = src.substring('file://'.length);
  final slash = rest.indexOf('/');
  rest = slash < 0 ? '' : rest.substring(slash);
  final cut = rest.indexOf(RegExp('[?#]'));
  if (cut >= 0) rest = rest.substring(0, cut);
  return _unquote(rest);
}

/// urllib.parse.unquote: decodes %XX runs as UTF-8, replacing invalid
/// sequences, and leaves a malformed escape as it is.
/// Like Python, each run of escapes is decoded on its own.
String _unquote(String s) {
  if (!s.contains('%')) return s;
  return s.replaceAllMapped(RegExp('(?:%[0-9A-Fa-f]{2})+'), (m) {
    final run = m[0]!;
    final bytes = [
      for (var i = 0; i < run.length; i += 3)
        int.parse(run.substring(i + 1, i + 3), radix: 16),
    ];
    return utf8.decode(bytes, allowMalformed: true);
  });
}
