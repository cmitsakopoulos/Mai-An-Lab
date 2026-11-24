import 'dart:async';
import 'dart:io';

import '../downloads/companion_download.dart' show JobCancelled;
import '../downloads/file_names.dart';
import 'deezer_client.dart';
import 'downloadable.dart';
import 'metadata_objects.dart';
import 'qobuz_client.dart';
import 'source_client.dart';
import 'source_config.dart';
import 'source_metadata.dart';

/// Port of `StreamripApp/utils/streamrip_api.py` (URL detection, client
/// construction, quality defaults, and Qobuz/Deezer download orchestration).

class ParsedSourceUrl {
  const ParsedSourceUrl({
    required this.mediaType,
    required this.id,
    required this.source,
  });

  final String mediaType;
  final String id;
  final String source;

  @override
  bool operator ==(Object other) =>
      other is ParsedSourceUrl &&
      other.mediaType == mediaType &&
      other.id == id &&
      other.source == source;

  @override
  int get hashCode => Object.hash(mediaType, id, source);

  @override
  String toString() => 'ParsedSourceUrl($mediaType, $id, $source)';
}

final List<(RegExp, String, String)> _kUrlPatterns = [
  (RegExp(r'qobuz\.com/.*?track/(?:.*/)?(?<id>\w+)(?:[/?#]|$)'), 'track', 'qobuz'),
  (RegExp(r'qobuz\.com/.*?album/(?:.*/)?(?<id>\w+)(?:[/?#]|$)'), 'album', 'qobuz'),
  (RegExp(r'qobuz\.com/.*?artist/(?:.*/)?(?<id>\w+)(?:[/?#]|$)'), 'artist', 'qobuz'),
  (RegExp(r'qobuz\.com/.*?playlist/(?:.*/)?(?<id>\w+)(?:[/?#]|$)'), 'playlist', 'qobuz'),
  (RegExp(r'deezer\.[a-z.]+/(?:[a-z]{2}/)?track/(?<id>\d+)'), 'track', 'deezer'),
  (RegExp(r'deezer\.[a-z.]+/(?:[a-z]{2}/)?album/(?<id>\d+)'), 'album', 'deezer'),
  (RegExp(r'deezer\.[a-z.]+/(?:[a-z]{2}/)?artist/(?<id>\d+)'), 'artist', 'deezer'),
  (RegExp(r'deezer\.[a-z.]+/(?:[a-z]{2}/)?playlist/(?<id>\d+)'), 'playlist', 'deezer'),
];

/// Parse a Qobuz, Deezer, or Companion URL into `(mediaType, id, source)`.
/// Matches `streamrip_api._detect_type_and_id`.
ParsedSourceUrl? detectTypeAndId(String url) {
  if (url.contains('/api/v1/tracks/stream') || url.startsWith('companion://')) {
    return const ParsedSourceUrl(
      mediaType: 'track',
      id: 'companion_track',
      source: 'companion',
    );
  }
  for (final (pattern, mtype, source) in _kUrlPatterns) {
    final m = pattern.firstMatch(url);
    if (m != null) {
      return ParsedSourceUrl(
        mediaType: mtype,
        id: m.namedGroup('id')!,
        source: source,
      );
    }
  }
  return null;
}

/// Construct a [SourceClient] for [source] (`qobuz` or `deezer`).
SourceClient buildSourceClient(
  String source,
  SourceConfig config, {
  SourceHttpClient? http,
}) {
  if (source == 'deezer') return DeezerClient(config, http: http);
  return QobuzClient(config, http: http);
}

/// Default numeric quality for [source] from [config].
int defaultQualityForSource(String source, SourceConfig config) =>
    source == 'deezer' ? config.deezer.quality : config.qobuz.quality;

typedef SourceDownloadProgress =
    void Function(String status, int percent, String message);

typedef TrackTagWriter =
    Future<void> Function(
      String filePath,
      TrackMetadata metadata,
      String? coverPath,
    );

typedef SourceClientFactory =
    SourceClient Function(String source, SourceConfig config);

/// Orchestrate a Qobuz or Deezer download (`streamrip_api.download`).
///
/// Tag writing and platform media-scanning hooks are injectable so Phase 6b
/// tests the full client & decryption pipeline cleanly and Phase 6c drops in
/// `tag_file`, history DBs, and Android `MediaScannerConnection`.
Future<List<String>> downloadFromSource({
  required String url,
  required String downloadDir,
  required SourceConfig config,
  int? quality,
  bool Function(String path)? isAppPrivate,
  SourceDownloadProgress? progressCallback,
  bool Function()? isCancelled,
  SourceClientFactory? clientFactory,
  TrackTagWriter? tagWriter,
  Future<void> Function(String path)? onFileSaved,
  Directory? tempDir,
}) async {
  if (isAppPrivate != null && isAppPrivate(downloadDir)) {
    throw FileSystemException(
      'Refusing to download into app-private storage: $downloadDir',
      downloadDir,
    );
  }
  await Directory(downloadDir).create(recursive: true);

  final parsed = detectTypeAndId(url);
  if (parsed == null || parsed.source == 'companion') {
    throw FormatException('Could not parse Qobuz or Deezer URL: $url');
  }

  final client =
      (clientFactory ?? (s, c) => buildSourceClient(s, c))(
        parsed.source,
        config,
      );

  try {
    await client.login();
    progressCallback?.call('Initializing', 0, 'Fetching metadata...');

    final meta = await client.getMetadata(parsed.id, parsed.mediaType);
    var tracks = <Map<String, Object?>>[];
    if (const {'track', 'album', 'playlist'}.contains(parsed.mediaType)) {
      tracks = extractTracks(parsed.source, parsed.mediaType, meta);
    } else if (parsed.mediaType == 'artist') {
      final rawAlbums = meta['albums'];
      final albumList = rawAlbums is Map
          ? (rawAlbums['items'] is List
              ? rawAlbums['items']! as List<Object?>
              : const <Object?>[])
          : (rawAlbums is List ? rawAlbums : const <Object?>[]);
      for (final a in albumList) {
        if (a is! Map || a['id'] == null) continue;
        final aMeta = await client.getMetadata('${a['id']}', 'album');
        tracks.addAll(extractTracks(parsed.source, 'album', aMeta));
      }
    }

    if (tracks.isEmpty) return const [];

    progressCallback?.call('Preparing', 0, 'Resolving stream URLs...');
    final targetQuality =
        quality ?? defaultQualityForSource(parsed.source, config);

    final resolveSem = _Semaphore(10);
    Object? firstError;
    final resolved = await Future.wait([
      for (final t in tracks)
        resolveSem.run(() async {
          try {
            final d = await client.getDownloadable('${t['id']}', targetQuality);
            await d.size();
            return d;
          } catch (e) {
            firstError ??= e;
            return null;
          }
        }),
    ]);

    final activeJobs = <(Map<String, Object?>, Downloadable)>[];
    var totalBytes = 0;
    for (var i = 0; i < resolved.length; i++) {
      final d = resolved[i];
      if (d != null) {
        activeJobs.add((tracks[i], d));
        totalBytes += d.cachedSize ?? 0;
      }
    }
    if (activeJobs.isEmpty) {
      final reason = firstError != null ? ': $firstError' : '.';
      throw Exception('No tracks could be resolved for download$reason');
    }

    var downloadedBytes = 0;
    var completedTracks = 0;
    var lastUpdateMs = 0;
    final maxConn =
        config.downloads.maxConnections > 0 ? config.downloads.maxConnections : 6;
    final dlSem = _Semaphore(maxConn);
    final savedPaths = <String>[];

    void triggerUpdate([String status = 'Downloading', bool force = false]) {
      final now = DateTime.now().millisecondsSinceEpoch;
      if (!force && now - lastUpdateMs < 100) return;
      lastUpdateMs = now;
      if (progressCallback == null) return;
      final pct = totalBytes > 0 ? (downloadedBytes / totalBytes * 100).toInt() : 0;
      final msg =
          '${(downloadedBytes / 1e6).toStringAsFixed(1)}/'
          '${(totalBytes / 1e6).toStringAsFixed(1)} MB • '
          '$completedTracks/${activeJobs.length} tracks';
      progressCallback(status, pct.clamp(0, 100), msg);
    }

    final scratchDir = tempDir ?? Directory.systemTemp;

    await Future.wait([
      for (var i = 0; i < activeJobs.length; i++)
        () async {
          if (isCancelled != null && isCancelled()) {
            throw const JobCancelled();
          }
          final (trackMeta, downloadable) = activeJobs[i];
          await dlSem.run(() async {
            if (isCancelled != null && isCancelled()) {
              throw const JobCancelled();
            }
            final trackId = '${trackMeta['id']}';
            final norm = normalizeTrack(parsed.source, trackMeta, i + 1);
            final albumNorm = norm['album']! as Map<String, Object?>;
            final title = '${norm['title'] ?? 'Unknown'}';
            final trackNum = int.tryParse('${norm['track_number']}') ?? (i + 1);
            final padded = trackNum.toString().padLeft(2, '0');
            final filename = cleanFilename('$padded. $title.${downloadable.extension}');
            final destPath = normalizeNfc(
              '$downloadDir${Platform.pathSeparator}$filename',
            );

            try {
              await downloadable.download(
                destPath,
                (len) {
                  downloadedBytes += len;
                  triggerUpdate();
                },
                isCancelled: isCancelled,
              );
              if (isCancelled != null && isCancelled()) {
                final partial = File(destPath);
                if (partial.existsSync()) partial.deleteSync();
                throw const JobCancelled();
              }

              triggerUpdate('Tagging', true);

              String? coverPath;
              final coverUrl = '${albumNorm['cover_url'] ?? ''}';
              if (tagWriter != null && coverUrl.startsWith('http')) {
                try {
                  final bytes = await client.fetchBytes(coverUrl);
                  if (bytes != null && bytes.isNotEmpty) {
                    coverPath =
                        '${scratchDir.path}${Platform.pathSeparator}__cover_$trackId.jpg';
                    await File(coverPath).writeAsBytes(bytes);
                  }
                } catch (_) {
                  coverPath = null;
                }
              }

              if (tagWriter != null) {
                final rawGenres = albumNorm['genres'];
                final albumObj = AlbumMetadata(
                  id: '${albumNorm['id'] ?? ''}',
                  album: '${albumNorm['album'] ?? 'Unknown Album'}',
                  artist: '${albumNorm['artist'] ?? 'Unknown Artist'}',
                  year: '${albumNorm['year'] ?? ''}',
                  tracktotal: int.tryParse('${albumNorm['tracktotal']}') ?? 1,
                  disctotal: int.tryParse('${albumNorm['disctotal']}') ?? 1,
                  genres: rawGenres is List
                      ? [for (final g in rawGenres) '$g']
                      : null,
                  copyright: albumNorm['copyright'] as String?,
                );
                final trackObj = TrackMetadata(
                  id: trackId,
                  title: title,
                  artist: '${norm['artist'] ?? 'Unknown Artist'}',
                  album: albumObj,
                  tracknumber: trackNum,
                  discnumber: int.tryParse('${norm['disc_number']}') ?? 1,
                  isrc: norm['isrc'] as String?,
                  composer: norm['composer'] as String?,
                );
                await tagWriter(destPath, trackObj, coverPath);
                if (coverPath != null) {
                  final cf = File(coverPath);
                  if (cf.existsSync()) cf.deleteSync();
                }
              }

              if (onFileSaved != null) {
                await onFileSaved(destPath);
              }
              savedPaths.add(destPath);
            } finally {
              completedTracks++;
              triggerUpdate();
            }
          });
        }(),
    ]);

    progressCallback?.call('Finished', 100, 'Download complete.');
    return savedPaths;
  } finally {
    await client.close();
  }
}

class _Semaphore {
  _Semaphore(this._permits);

  int _permits;
  final List<Completer<void>> _waiters = [];

  Future<T> run<T>(Future<T> Function() action) async {
    if (_permits <= 0) {
      final c = Completer<void>();
      _waiters.add(c);
      await c.future;
    } else {
      _permits--;
    }
    try {
      return await action();
    } finally {
      if (_waiters.isNotEmpty) {
        _waiters.removeAt(0).complete();
      } else {
        _permits++;
      }
    }
  }
}
