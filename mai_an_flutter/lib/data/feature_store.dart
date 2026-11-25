import 'dart:convert';
import 'dart:typed_data';

import 'package:sqlite3/sqlite3.dart';

import '../analysis/audio_features.dart' show AudioFeatures;
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

/// server.py `persist_features` (db_manager `update_track_features`): store
/// wire-form features against a local file, creating its play_counts row
/// if the track has never been played.
void Function(Database) persistFeaturesJob(
  String path,
  Map<String, Object?> dsp,
) => (db) {
  final b64 = dsp['timbre_b64'];
  final timbre = (b64 is String && b64.isNotEmpty) ? base64.decode(b64) : null;
  db.execute(
    '''
    INSERT INTO play_counts
        (track_path, count, last_played, bpm, energy, brightness,
         rolloff, beat_strength, spectral_flatness,
         spectral_contrast, key_index, timbre, features_version)
    VALUES (?, 0, strftime('%s','now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(track_path) DO UPDATE SET
        bpm = EXCLUDED.bpm,
        energy = EXCLUDED.energy,
        brightness = EXCLUDED.brightness,
        rolloff = EXCLUDED.rolloff,
        beat_strength = EXCLUDED.beat_strength,
        spectral_flatness = EXCLUDED.spectral_flatness,
        spectral_contrast = EXCLUDED.spectral_contrast,
        key_index = EXCLUDED.key_index,
        timbre = EXCLUDED.timbre,
        features_version = EXCLUDED.features_version
    ''',
    [
      path,
      dsp['bpm'],
      dsp['energy'],
      dsp['brightness'],
      dsp['rolloff'],
      dsp['beat_strength'],
      dsp['spectral_flatness'],
      dsp['spectral_contrast'],
      dsp['key_index'],
      timbre,
      // dsp.get("features_version", FEATURES_VERSION): a present null stays.
      dsp.containsKey('features_version')
          ? dsp['features_version']
          : kFeaturesVersion,
    ],
  );
};

/// db_manager `update_track_features`: an on-phone analysis result stored
/// against [path] (the row is created if the track was never played).
void Function(Database) updateTrackFeaturesJob(
  String path,
  AudioFeatures f, [
  int featuresVersion = kFeaturesVersion,
]) =>
    (db) => db.execute(
      '''
  INSERT INTO play_counts
      (track_path, count, last_played, bpm, energy, brightness,
       rolloff, beat_strength, spectral_flatness,
       spectral_contrast, key_index, timbre, features_version)
  VALUES (?, 0, strftime('%s','now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  ON CONFLICT(track_path) DO UPDATE SET
      bpm = EXCLUDED.bpm,
      energy = EXCLUDED.energy,
      brightness = EXCLUDED.brightness,
      rolloff = EXCLUDED.rolloff,
      beat_strength = EXCLUDED.beat_strength,
      spectral_flatness = EXCLUDED.spectral_flatness,
      spectral_contrast = EXCLUDED.spectral_contrast,
      key_index = EXCLUDED.key_index,
      timbre = EXCLUDED.timbre,
      features_version = EXCLUDED.features_version
  ''',
      [
        path,
        f.bpm,
        f.energy,
        f.brightness,
        f.rolloff,
        f.beatStrength,
        f.spectralFlatness,
        f.spectralContrast,
        f.keyIndex,
        f.timbreBlob(),
        featuresVersion,
      ],
    );
