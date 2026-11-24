import 'dart:typed_data';

import '../core/py_re.dart';
import '../core/py_text.dart';

/// `llm_tools.py`'s helpers: Markdown flattened for speech, and database
/// rows projected to what the model needs.

final _mdLink = PyRe(r"\[([^\]]+)\]\([^)]+\)");
final _mdEmph = PyRe(r"(\*\*|__|\*|_|`)");
final _mdHead = PyRe(r"^\s{0,3}#{1,6}\s*", multiline: true);
final _mdBullet = PyRe(r"^\s*[•\-\*]\s+", multiline: true);
final _ws = PyRe(r"[ \t]{2,}");

/// `strip_markdown`: plain prose for TTS, so it doesn't read '**' or link
/// URLs aloud. Keeps the words, drops the syntax.
String stripMarkdown(String? text) {
  if (text == null || text.isEmpty) return '';
  var t = _mdLink.subMapped(text, (m) => m.group(1)!);
  t = _mdHead.sub(t, '');
  t = _mdBullet.sub(t, '');
  t = _mdEmph.sub(t, '');
  t = _ws.sub(t, ' ');
  return pyStrip(t);
}

/// `_TRACK_FIELDS`.
const trackFields = [
  'path', 'title', 'artist', 'album', 'genre', 'year', //
  'duration', 'format', 'bpm', 'energy', 'track_num', 'count',
  'last_played',
];

/// `_slim_track`: the JSON-safe, model-relevant fields of a row, in
/// `_TRACK_FIELDS` order; None and BLOBs (the timbre vector) left out.
Map<String, Object?> slimTrack(Map<String, Object?> d) => {
  for (final k in trackFields)
    if (d[k] != null && d[k] is! Uint8List) k: d[k],
};
