import 'dart:convert';
import 'dart:io';

import 'deezer_crypto.dart';
import 'source_client.dart';
import 'source_exceptions.dart';

/// Port of `StreamripApp/utils/downloadable.py`.
typedef BytesProgressCallback = void Function(int byteCount);

/// Streams [url] directly to [path], reporting chunk byte counts.
Future<void> fastAsyncDownload(
  SourceHttpClient http,
  String path,
  String url,
  BytesProgressCallback callback, {
  bool Function()? isCancelled,
}) async {
  final resp = await http.openStream(Uri.parse(url));
  if (resp.statusCode < 200 || resp.statusCode >= 300) {
    throw HttpException(
      'HTTP ${resp.statusCode} downloading $url',
      uri: Uri.parse(url),
    );
  }
  final sink = File(path).openWrite();
  try {
    await for (final chunk in resp.stream) {
      if (isCancelled != null && isCancelled()) break;
      if (chunk.isEmpty) continue;
      sink.add(chunk);
      callback(chunk.length);
    }
    await sink.flush();
  } finally {
    await sink.close();
  }
}

/// Abstract downloadable stream (`utils/downloadable.py` `Downloadable`).
abstract class Downloadable {
  Downloadable({
    required this.http,
    required this.url,
    required this.extension,
    this.source = 'Unknown',
    int? initialSize,
  }) : cachedSize = initialSize;

  final SourceHttpClient http;
  final String url;
  final String extension;
  final String source;
  int? cachedSize;

  Future<int> size() async {
    if (cachedSize != null) return cachedSize!;
    final resp = await http.request('HEAD', Uri.parse(url));
    if (resp.statusCode < 200 || resp.statusCode >= 300) {
      throw HttpException(
        'HTTP ${resp.statusCode} probing $url',
        uri: Uri.parse(url),
      );
    }
    cachedSize = int.tryParse(resp.header('content-length') ?? '') ?? 0;
    return cachedSize!;
  }

  Future<void> download(
    String path,
    BytesProgressCallback callback, {
    bool Function()? isCancelled,
  });
}

/// Unencrypted HTTP stream (`utils/downloadable.py` `BasicDownloadable`).
class BasicDownloadable extends Downloadable {
  BasicDownloadable({
    required super.http,
    required super.url,
    required super.extension,
    super.source = 'Unknown',
  });

  @override
  Future<void> download(
    String path,
    BytesProgressCallback callback, {
    bool Function()? isCancelled,
  }) => fastAsyncDownload(http, path, url, callback, isCancelled: isCancelled);
}

/// Deezer CDN stream, transparently Blowfish-CBC decrypted on the way to disk.
class DeezerDownloadable extends Downloadable {
  DeezerDownloadable._({
    required super.http,
    required super.url,
    required super.extension,
    required this.quality,
    required this.id,
    required super.initialSize,
  }) : super(source: 'deezer');

  static final RegExp isEncryptedPattern = RegExp(r'/m(?:obile|edia)/');

  final int quality;
  final String id;

  factory DeezerDownloadable(
    SourceHttpClient http,
    Map<String, Object?> info,
  ) {
    final rawSizes = info['quality_to_size'];
    final sizes = rawSizes is List
        ? [for (final s in rawSizes) s is num ? s.toInt() : 0]
        : const <int>[];
    final available = <int>[
      for (var i = 0; i < sizes.length; i++)
        if (sizes[i] > 0) i,
    ];
    if (available.isEmpty) {
      throw const NonStreamableError('Missing download info. Skipping.');
    }
    final maxAvailable = available.reduce((a, b) => a > b ? a : b);
    final reqQuality = (info['quality'] as num?)?.toInt() ?? 0;
    final actualQuality = reqQuality < maxAvailable ? reqQuality : maxAvailable;
    final ext = actualQuality <= 1 ? 'mp3' : 'flac';
    return DeezerDownloadable._(
      http: http,
      url: '${info['url'] ?? ''}',
      extension: ext,
      quality: actualQuality,
      id: '${info['id'] ?? ''}',
      initialSize: sizes[actualQuality],
    );
  }

  @override
  Future<void> download(
    String path,
    BytesProgressCallback callback, {
    bool Function()? isCancelled,
  }) async {
    final resp = await http.openStream(Uri.parse(url), followRedirects: true);
    if (resp.statusCode < 200 || resp.statusCode >= 300) {
      throw HttpException(
        'HTTP ${resp.statusCode} downloading $url',
        uri: Uri.parse(url),
      );
    }

    cachedSize = resp.contentLength;
    if (cachedSize! < 20000 && !url.endsWith('.jpg')) {
      final bytes = <int>[];
      await for (final c in resp.stream) {
        bytes.addAll(c);
      }
      Object? info;
      try {
        info = jsonDecode(utf8.decode(bytes));
      } catch (_) {
        throw const NonStreamableError('File not found.');
      }
      if (info is Map && info.containsKey('error')) {
        final msg = '${info['error']} - ${info['message'] ?? ''}'
            .replaceAll(RegExp(r'^[\s-]+|[\s-]+$'), '');
        throw NonStreamableError(msg);
      }
      throw NonStreamableError('$info');
    }

    if (!isEncryptedPattern.hasMatch(url)) {
      final sink = File(path).openWrite();
      try {
        await for (final chunk in resp.stream) {
          if (isCancelled != null && isCancelled()) break;
          if (chunk.isEmpty) continue;
          sink.add(chunk);
          callback(chunk.length);
        }
        await sink.flush();
      } finally {
        await sink.close();
      }
      return;
    }

    final key = deezerBlowfishKey(id);
    final sink = File(path).openWrite();
    try {
      await deezerDecryptStripeStream(
        key: key,
        chunks: resp.stream,
        sink: sink,
        onBytes: callback,
        isCancelled: isCancelled,
      );
      await sink.flush();
    } finally {
      await sink.close();
    }
  }
}
