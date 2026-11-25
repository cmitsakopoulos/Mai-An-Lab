import 'dart:io';
import 'dart:isolate';

import 'package:flutter/foundation.dart';
import 'package:maian_native/maian_native.dart';

import 'audio_features.dart';

/// dsp.py `decode_to_pcm` / `analyze_track` and track_graph.py
/// `bulk_analyze_library`: decode each track to PCM, extract its features
/// off the UI isolate, persist them.

/// A decoded track: a raw int16 LE mono PCM file and its sample rate. The
/// analyser deletes the file when done.
typedef DecodedPcm = ({String path, int sampleRate});

/// `decode_to_pcm`'s platform split.
abstract interface class PcmDecoder {
  Future<DecodedPcm> decode(String path);

  /// The decoder for this platform: MediaCodec on Android, ffmpeg on a
  /// desktop that has it, else null (Flet: "native analyser unavailable").
  static Future<PcmDecoder?> platform() async {
    if (kIsWeb) return null;
    if (Platform.isAndroid) return const NativePcmDecoder();
    final ffmpeg = await FfmpegPcmDecoder.find();
    return ffmpeg == null ? null : FfmpegPcmDecoder(ffmpeg);
  }
}

/// The Kotlin decoder (`flet_audio_service.decode_pcm`).
class NativePcmDecoder implements PcmDecoder {
  const NativePcmDecoder();

  @override
  Future<DecodedPcm> decode(String path) async {
    final r = await MaianNative.decodePcm(path);
    if (r == null) throw StateError('native decoder unavailable');
    return r;
  }
}

/// `_decode_pcm_ffmpeg`, the desktop fallback: skip the first 15 s, read
/// [kDspMaxSeconds], downmix and resample to [kDspSampleRate]; retried from
/// the start when the seek fails (a very short track).
class FfmpegPcmDecoder implements PcmDecoder {
  FfmpegPcmDecoder(this.ffmpeg, {String? cacheDir})
    : cacheDir =
          cacheDir ??
          '${Directory.systemTemp.path}${Platform.pathSeparator}dsp_pcm';

  final String ffmpeg;
  final String cacheDir;

  /// `shutil.which("ffmpeg")`, plus Homebrew's prefixes, which a GUI app's
  /// PATH often lacks.
  static Future<String?> find() async {
    final sep = Platform.isWindows ? ';' : ':';
    final exe = Platform.isWindows ? 'ffmpeg.exe' : 'ffmpeg';
    final dirs = [
      ...(Platform.environment['PATH'] ?? '').split(sep),
      '/opt/homebrew/bin',
      '/usr/local/bin',
    ];
    for (final d in dirs) {
      if (d.isEmpty) continue;
      final f = File('$d${Platform.pathSeparator}$exe');
      if (await f.exists()) return f.path;
    }
    return null;
  }

  static int _counter = 0;

  @override
  Future<DecodedPcm> decode(String path) async {
    await Directory(cacheDir).create(recursive: true);
    final out =
        '$cacheDir${Platform.pathSeparator}'
        '${path.hashCode.abs()}_${pid}_${_counter++}.pcm';
    List<String> args({required bool seek}) => [
      '-hide_banner',
      '-loglevel',
      'error',
      if (seek) ...['-ss', '15'],
      '-t',
      '$kDspMaxSeconds',
      '-i',
      path,
      '-f',
      's16le',
      '-acodec',
      'pcm_s16le',
      '-ac',
      '1',
      '-ar',
      '$kDspSampleRate',
      '-y',
      out,
    ];
    var r = await Process.run(ffmpeg, args(seek: true));
    if (r.exitCode != 0) {
      r = await Process.run(ffmpeg, args(seek: false));
      if (r.exitCode != 0) {
        throw ProcessException(
          ffmpeg,
          const [],
          'ffmpeg failed for $path: '
          '${r.stderr}',
          r.exitCode,
        );
      }
    }
    return (path: out, sampleRate: kDspSampleRate);
  }
}

AudioFeatures _extractFile(String pcmPath, int sr) =>
    extractFeaturesFromPcmBytes(File(pcmPath).readAsBytesSync(), sr);

/// `analyze_track`: decode, extract on a worker isolate, delete the PCM.
Future<AudioFeatures> analyzeTrack(PcmDecoder decoder, String path) async {
  final pcm = await decoder.decode(path);
  try {
    final p = pcm.path, sr = pcm.sampleRate;
    return await Isolate.run(() => _extractFile(p, sr));
  } finally {
    try {
      await File(pcm.path).delete();
    } on FileSystemException {
      // Already gone.
    }
  }
}

/// `bulk_analyze_library`'s counts.
typedef AnalysisResult = ({int analysed, int failed, int total});

/// `bulk_analyze_library`: analyse each of [missing] in order, persisting
/// as it goes. [progress] runs after every track (done, total, path,
/// failures so far); [cancelled] is polled before each one.
Future<AnalysisResult> bulkAnalyzeLibrary({
  required List<String> missing,
  required PcmDecoder decoder,
  required Future<void> Function(String path, AudioFeatures f) persist,
  void Function(int done, int total, String current, int failures)? progress,
  bool Function()? cancelled,
  Future<AudioFeatures> Function(PcmDecoder, String)? analyse,
}) async {
  final total = missing.length;
  if (total == 0) return (analysed: 0, failed: 0, total: 0);
  final run = analyse ?? analyzeTrack;
  var analysed = 0;
  var failures = 0;

  void emit(int done, String current) {
    if (progress == null) return;
    try {
      progress(done, total, current, failures);
    } catch (e) {
      debugPrint('track_graph: progress_cb raised $e');
    }
  }

  for (var i = 1; i <= total; i++) {
    final path = missing[i - 1];
    try {
      if (cancelled?.call() ?? false) break;
    } catch (_) {
      // A broken check never stops the run.
    }
    final AudioFeatures features;
    try {
      features = await run(decoder, path);
    } catch (e) {
      failures++;
      debugPrint('track_graph: analyse failed for $path: $e');
      emit(i, path);
      continue;
    }
    try {
      await persist(path, features);
      analysed++;
    } catch (e) {
      failures++;
      debugPrint('track_graph: persist failed for $path: $e');
    }
    emit(i, path);
  }
  return (analysed: analysed, failed: failures, total: total);
}
