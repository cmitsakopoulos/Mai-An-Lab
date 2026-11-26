import 'dart:async';
import 'dart:collection';
import 'dart:convert';
import 'dart:io';
import 'dart:isolate';

import 'ffmpeg.dart';
import 'shared.dart';

/// `FEATURES_VERSION`: a cached row of any other version is stale.
const featuresVersion = kDspFeaturesVersion;

/// A track's features in wire form (what /dsp/extract answers and
/// /dsp/match returns): the scalars, `timbre_b64`, `features_version`.
Map<String, Object?> wireOf(AudioFeatures f) => {
  'bpm': f.bpm,
  'energy': f.energy,
  'brightness': f.brightness,
  'rolloff': f.rolloff,
  'beat_strength': f.beatStrength,
  'spectral_flatness': f.spectralFlatness,
  'spectral_contrast': f.spectralContrast,
  'key_index': f.keyIndex,
  'timbre_b64': base64.encode(f.timbreBlob()),
  'features_version': featuresVersion,
};

Map<String, Object?> _extractInIsolate(String pcmPath, int sr) =>
    wireOf(extractFeaturesFromPcmBytes(File(pcmPath).readAsBytesSync(), sr));

/// Decode [audio] with ffmpeg and extract its features on a worker isolate:
/// the same pipeline as the phone's `analyzeTrack`.
Future<Map<String, Object?>> analyseFile(
  Ffmpeg ffmpeg,
  String audio,
  Directory scratch,
) async {
  final pcm = '${scratch.path}/dsp_${pid}_${_seq++}.pcm';
  try {
    await ffmpeg.decodePcm(audio, pcm);
    return await Isolate.run(() => _extractInIsolate(pcm, kDspSampleRate));
  } finally {
    try {
      File(pcm).deleteSync();
    } catch (_) {}
  }
}

int _seq = 0;

/// The Mac's feature cache (Flet: tools/offload_cache/feature_cache.db): a
/// phone asking about a track the Mac already analysed gets the answer
/// without uploading the audio again. One JSON line per track, last line
/// wins; the whole file is read once at start.
class FeatureCache {
  FeatureCache(this.file);
  final File file;
  final _rows = <String, Map<String, Object?>>{};
  IOSink? _sink;

  int get length => _rows.length;

  void load() {
    if (!file.existsSync()) return;
    for (final line in file.readAsLinesSync()) {
      if (line.isEmpty) continue;
      try {
        final m = jsonDecode(line) as Map<String, Object?>;
        _rows[m['path']! as String] = m;
      } catch (_) {
        // A torn last line from a crash: skip it.
      }
    }
  }

  /// The cached wire row for [path], only if it is the current version.
  Map<String, Object?>? get(String path) {
    final r = _rows[path];
    if (r == null || r['features_version'] != featuresVersion) return null;
    return {...r}..remove('path');
  }

  void put(String path, Map<String, Object?> wire) {
    final row = {...wire, 'path': path};
    _rows[path] = row;
    _sink ??= file.openWrite(mode: FileMode.append);
    _sink!.writeln(jsonEncode(row));
  }

  Future<void> close() async {
    await _sink?.flush();
    await _sink?.close();
    _sink = null;
  }
}

/// At most [size] jobs at once.
class Pool {
  Pool(this.size);
  final int size;
  int _active = 0;
  final _waiting = Queue<Completer<void>>();

  Future<T> run<T>(Future<T> Function() job) async {
    if (_active >= size) {
      final c = Completer<void>();
      _waiting.add(c);
      await c.future;
    } else {
      _active++;
    }
    try {
      return await job();
    } finally {
      if (_waiting.isNotEmpty) {
        _waiting.removeFirst().complete();
      } else {
        _active--;
      }
    }
  }
}
