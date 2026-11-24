import 'dart:async';
import 'dart:io';

import 'file_names.dart';

/// streamrip_api.py `download`, the `companion` branch: one track the Mac
/// queued for this phone, streamed into the download folder.

/// One progress report: `{"status", "percent", "message"}` in Python.
typedef DownloadProgress =
    void Function(String status, int? percent, String message);

/// An error the download dock shows as `str(exc)`; [pyType] is the Python
/// exception class Flet raises for the same case.
class DownloadError implements Exception {
  const DownloadError(this.pyType, this.message);
  final String pyType;
  final String message;
  @override
  String toString() => message;
}

/// `JobCancelledException`: the user cancelled.
class JobCancelled implements Exception {
  const JobCancelled();
  @override
  String toString() => 'Job cancelled';
}

/// `_detect_type_and_id`'s companion rule.
bool isCompanionUrl(String url) =>
    url.contains('/api/v1/tracks/stream') || url.startsWith('companion://');

const _connectTimeout = Duration(seconds: 10);

/// No total cap: a lossless album track over slow Wi-Fi, after a server-side
/// transcode, legitimately takes minutes. A stalled read is what fails.
const _readStall = Duration(seconds: 120);

/// `urllib.parse.parse_qs(query).get(key, [fallback])[0]`: blank values are
/// dropped, `+` is a space.
String _queryFirst(String url, String key, String fallback) {
  final q = url.indexOf('?');
  if (q < 0) return fallback;
  var query = url.substring(q + 1);
  final hash = query.indexOf('#');
  if (hash >= 0) query = query.substring(0, hash);
  for (final pair in query.split('&')) {
    final eq = pair.indexOf('=');
    if (eq < 0) continue;
    final value = pair.substring(eq + 1);
    if (value.isEmpty) continue;
    if (_unquotePlus(pair.substring(0, eq)) == key) return _unquotePlus(value);
  }
  return fallback;
}

String _unquotePlus(String s) {
  try {
    return Uri.decodeQueryComponent(s);
  } on ArgumentError {
    return s.replaceAll('+', ' ');
  }
}

/// The file name a transfer is saved under (before the no-overwrite suffix):
/// the Mac's file name, with `.m4a` when the Mac transcodes to AAC.
String companionFileName(String url) {
  final remote = _queryFirst(url, 'path', 'track.audio');
  final parts = pyPathParts(remote);
  final ext =
      (url.toLowerCase().contains('quality=aac') || remote.endsWith('.m4a'))
      ? 'm4a'
      : (parts.suffix.isEmpty ? 'flac' : parts.suffix.substring(1));
  return cleanFilename('${parts.stem}.$ext');
}

/// Download [url] into [downloadDir]. Returns the saved file's path (one,
/// as Python's list). [isCancelled] is polled between chunks. [onSaved]
/// runs once the file is in place (Android: the media scan).
Future<List<String>> companionDownload(
  String url,
  String downloadDir, {
  DownloadProgress? progress,
  bool Function()? isCancelled,
  bool Function(String dir)? isAppPrivate,
  Future<void> Function(String path)? onSaved,
  HttpClient Function()? httpClient,
}) async {
  if (isAppPrivate?.call(downloadDir) ?? false) {
    // Last line of defence: a file saved in the sandbox can't be reached
    // by any files app on modern Android.
    throw DownloadError(
      'PermissionError',
      'Refusing to download into app-private storage: $downloadDir',
    );
  }
  await Directory(downloadDir).create(recursive: true);
  if (!isCompanionUrl(url)) {
    throw DownloadError(
      'Exception',
      'Could not parse Qobuz, Deezer, or Companion URL: $url',
    );
  }

  final filename = companionFileName(url);
  var destPath = normalizeNfc('${_withSlash(downloadDir)}$filename');
  progress?.call('Transferring', 0, 'Receiving $filename from Mac...');

  final http = (httpClient ?? HttpClient.new)()
    ..connectionTimeout = _connectTimeout;
  // Streamed to a .part file and renamed only once complete and
  // size-checked: a dropped Wi-Fi link used to leave a truncated file under
  // the real name, which the scanner then imported.
  var partPath = '$destPath.part';
  var downloaded = 0;
  try {
    final req = await http.getUrl(Uri.parse(url));
    final resp = await req.close().timeout(_readStall);
    if (resp.statusCode == 401) {
      await resp.drain<void>().catchError((_) {});
      throw const DownloadError(
        'PermissionError',
        'Mac rejected the transfer (not paired)',
      );
    }
    if (resp.statusCode == 404) {
      await resp.drain<void>().catchError((_) {});
      throw const DownloadError(
        'FileNotFoundError',
        'Track is no longer queued on the Mac',
      );
    }
    if (resp.statusCode >= 400) {
      await resp.drain<void>().catchError((_) {});
      // aiohttp's ClientResponseError.
      throw DownloadError(
        'ClientResponseError',
        "${resp.statusCode}, message='${resp.reasonPhrase}', url='$url'",
      );
    }
    final totalBytes = resp.contentLength > 0 ? resp.contentLength : 0;
    final dest = File(destPath);
    if (totalBytes > 0 &&
        await dest.exists() &&
        await dest.length() == totalBytes) {
      // Same file already received (re-sent from the Mac).
      await resp.drain<void>().catchError((_) {});
      progress?.call('Finished', 100, 'Already on this device.');
      return [destPath];
    }
    if (await dest.exists()) {
      // A different track with the same file name (two albums' "01.
      // Intro"): never overwrite it.
      final dot = _extIndex(destPath);
      final stem = destPath.substring(0, dot), ext = destPath.substring(dot);
      var n = 2;
      while (await File('$stem ($n)$ext').exists()) {
        n++;
      }
      destPath = '$stem ($n)$ext';
      partPath = '$destPath.part';
    }
    final sink = File(partPath).openWrite();
    try {
      await for (final chunk in resp.timeout(_readStall)) {
        if (isCancelled?.call() ?? false) throw const JobCancelled();
        sink.add(chunk);
        downloaded += chunk.length;
        if (progress != null) {
          final pct = totalBytes > 0 ? downloaded * 100 ~/ totalBytes : 50;
          progress(
            'Transferring',
            pct < 99 ? pct : 99,
            '${_mb(downloaded)}/${_mb(totalBytes)} MB',
          );
        }
      }
    } finally {
      await sink.close();
    }
    if (totalBytes > 0 && downloaded != totalBytes) {
      throw DownloadError(
        'OSError',
        'Transfer incomplete ($downloaded of $totalBytes bytes)',
      );
    }
    if (downloaded == 0) {
      throw const DownloadError('OSError', 'Mac sent an empty file');
    }
    await File(partPath).rename(destPath);
  } finally {
    http.close(force: true);
    final part = File(partPath);
    try {
      if (await part.exists()) await part.delete();
    } on FileSystemException {
      // Python ignores it too.
    }
  }

  if (onSaved != null) {
    try {
      await onSaved(destPath);
    } catch (_) {}
  }
  progress?.call('Finished', 100, 'Transfer complete.');
  return [destPath];
}

String _withSlash(String dir) => dir.endsWith('/') ? dir : '$dir/';

/// `os.path.splitext`'s split point: the last dot of the name, unless the
/// name is all leading dots.
int _extIndex(String path) {
  final slash = path.lastIndexOf('/');
  final dot = path.lastIndexOf('.');
  if (dot <= slash) return path.length;
  var i = slash + 1;
  while (i < dot && path[i] == '.') {
    i++;
  }
  return i == dot ? path.length : dot;
}

/// `f"{n/1e6:.1f}"`. Python rounds the double's exact value half to even,
/// Dart's toStringAsFixed rounds the tie up. A tie needs the quotient to be
/// exactly m.x5 in binary, i.e. m.25 or m.75, and only m.25 rounds
/// differently (m.2 against m.3).
String _mb(int bytes) => bytes % 1000000 == 250000
    ? '${bytes ~/ 1000000}.2'
    : (bytes / 1e6).toStringAsFixed(1);
