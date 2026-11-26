import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'shared.dart';

/// Quality profiles, by the names the phone sends (transcoder.py).
enum Quality {
  original('original'),
  aacVbrMax('aac_vbr_max'),
  aac320('aac_320k'),
  aac256('aac_256k'),
  aac192('aac_192k');

  const Quality(this.wire);
  final String wire;

  static Quality parse(String? s) =>
      values.firstWhere((q) => q.wire == s, orElse: () => original);
}

/// The one external program the Mac side needs. Everything else is Dart.
class Ffmpeg {
  Ffmpeg._(this.path, this.probe, this.hasAacAt);
  final String path;
  final String? probe;
  final bool hasAacAt;

  /// `find_tool`: PATH, then Homebrew's prefixes.
  static String? which(String name) {
    final dirs = [
      ...(Platform.environment['PATH'] ?? '').split(':'),
      '/opt/homebrew/bin',
      '/usr/local/bin',
      '/opt/local/bin',
    ];
    for (final d in dirs) {
      if (d.isEmpty) continue;
      final f = File('$d/$name');
      if (f.existsSync()) return f.path;
    }
    return null;
  }

  static Future<Ffmpeg?> find() async {
    final p = which('ffmpeg');
    if (p == null) return null;
    var aacAt = false;
    try {
      final r = await Process.run(p, ['-hide_banner', '-encoders']);
      aacAt = '${r.stdout}'.contains('aac_at');
    } catch (_) {}
    return Ffmpeg._(p, which('ffprobe'), aacAt);
  }

  /// dsp.py's decode: mono s16le at [kDspSampleRate], skipping 15 s and
  /// reading [kDspMaxSeconds], the same window as the phone and the Flet
  /// server. A very short track is read from the start instead.
  Future<void> decodePcm(String src, String outPcm) async {
    Future<ProcessResult> run(bool seek) => Process.run(path, [
      '-hide_banner',
      '-loglevel',
      'error',
      if (seek) ...['-ss', '15'],
      '-t',
      '$kDspMaxSeconds',
      '-i',
      src,
      '-f',
      's16le',
      '-acodec',
      'pcm_s16le',
      '-ac',
      '1',
      '-ar',
      '$kDspSampleRate',
      '-y',
      outPcm,
    ]);
    bool ok() => File(outPcm).existsSync() && File(outPcm).lengthSync() > 0;
    var r = await run(true);
    if (r.exitCode != 0 || !ok()) {
      r = await run(false);
      if (r.exitCode != 0 || !ok()) {
        throw StateError('ffmpeg decode failed: ${'${r.stderr}'.trim()}');
      }
    }
  }

  /// Artist / album / title tags, when ffprobe is around.
  Future<Map<String, String>> tags(String src) async {
    final fp = probe;
    if (fp == null) return const {};
    try {
      final r = await Process.run(fp, [
        '-v',
        'error',
        '-show_entries',
        'format_tags',
        '-of',
        'json',
        src,
      ]);
      final j = jsonDecode('${r.stdout}') as Map;
      final t = ((j['format'] as Map?)?['tags'] as Map?) ?? const {};
      return {
        for (final e in t.entries) '${e.key}'.toLowerCase(): '${e.value}',
      };
    } catch (_) {
      return const {};
    }
  }

  Future<int> _sampleRate(String src) async {
    final fp = probe;
    if (fp == null) return 44100;
    try {
      final r = await Process.run(fp, [
        '-v',
        'error',
        '-select_streams',
        'a:0',
        '-show_entries',
        'stream=sample_rate',
        '-of',
        'default=noprint_wrappers=1:nokey=1',
        src,
      ]);
      return int.parse('${r.stdout}'.trim());
    } catch (_) {
      return 44100;
    }
  }

  /// transcoder.py `transcode_audio`: an .m4a of [src] in [cacheDir], reused
  /// while newer than the source. Cover art rides along when it can.
  Future<String> transcode(
    String src,
    Quality q,
    Directory cacheDir,
    String digest,
  ) async {
    if (q == Quality.original) return src;
    final stem = src.split('/').last.replaceFirst(RegExp(r'\.[^.]*$'), '');
    final short = stem.length > 60 ? stem.substring(0, 60) : stem;
    final out = '${cacheDir.path}/$short.$digest.transcode_${q.wire}.m4a';
    final outFile = File(out);
    final srcFile = File(src);
    if (outFile.existsSync() &&
        outFile.lengthSync() > 0 &&
        !outFile.lastModifiedSync().isBefore(srcFile.lastModifiedSync())) {
      return out;
    }
    final encoder = hasAacAt ? 'aac_at' : 'aac';
    final sr = await _sampleRate(src);
    final enc = <String>[
      '-c:a',
      encoder,
      if (sr > 48000) ...['-ar', '48000'],
      ...switch ((hasAacAt, q)) {
        (true, Quality.aacVbrMax) => [
          '-aac_at_mode',
          '3',
          '-q:a',
          '0',
          '-aac_at_quality',
          '0',
        ],
        (false, Quality.aacVbrMax) || (_, Quality.aac320) => ['-b:a', '320k'],
        (_, Quality.aac256) => ['-b:a', '256k'],
        (_, Quality.aac192) => ['-b:a', '192k'],
        _ => const <String>[],
      },
    ];
    // Written under a temp name and renamed: a second request must never be
    // handed a half-written file.
    final tmp = '$out.$pid.part.m4a';
    Future<ProcessResult> run(List<String> extra) => Process.run(path, [
      '-y',
      '-hide_banner',
      '-loglevel',
      'error',
      '-i',
      src,
      ...enc,
      ...extra,
      '-map_metadata',
      '0',
      '-movflags',
      '+faststart',
      tmp,
    ]);
    bool ok(ProcessResult r) =>
        r.exitCode == 0 &&
        File(tmp).existsSync() &&
        File(tmp).lengthSync() > 0;
    try {
      var r = await run([
        '-map',
        '0:a:0',
        '-map',
        '0:v:0?',
        '-c:v',
        'copy',
        '-disposition:v:0',
        'attached_pic',
      ]);
      if (!ok(r)) {
        // Some art streams won't mux into MP4; the audio matters more.
        r = await run(['-map', '0:a:0']);
        if (!ok(r)) {
          throw StateError('ffmpeg transcode failed: ${'${r.stderr}'.trim()}');
        }
      }
      File(tmp).renameSync(out);
      return out;
    } finally {
      try {
        File(tmp).deleteSync();
      } catch (_) {}
    }
  }
}
