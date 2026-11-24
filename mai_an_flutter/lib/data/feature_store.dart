import 'dart:convert';
import 'dart:typed_data';

import 'package:sqlite3/sqlite3.dart';

import 'library_repository.dart' show kFeaturesVersion;

/// The acoustic features of one track, as server.py moves them between the
/// two apps (`stored_features` / `persist_features`): the play_counts
/// columns, with the timbre BLOB base64-encoded.

/// server.py `stored_features`: null when the track hasn't been analysed
/// (no row, or no bpm).
Map<String, Object?>? storedFeaturesJob(Database db, String path) {
  final rows = db.select(
    'SELECT bpm, energy, brightness, rolloff, beat_strength, '
    'spectral_flatness, spectral_contrast, key_index, timbre, features_version '
    'FROM play_counts WHERE track_path = ?',
    [path],
  );
  if (rows.isEmpty || rows.first['bpm'] == null) return null;
  final r = rows.first;
  final timbre = r['timbre'];
  final v = r['features_version'];
  return {
    'bpm': r['bpm'],
    'energy': r['energy'],
    'brightness': r['brightness'],
    'rolloff': r['rolloff'],
    'beat_strength': r['beat_strength'],
    'spectral_flatness': r['spectral_flatness'],
    'spectral_contrast': r['spectral_contrast'],
    'key_index': r['key_index'],
    'timbre_b64': timbre is Uint8List && timbre.isNotEmpty
        ? base64.encode(timbre)
        : '',
    'features_version': (v is int && v != 0) ? v : kFeaturesVersion,
  };
}
