// Behavioral tests for QobuzClient, DeezerClient, DeezerDownloadable streaming
// decryption, StreamripSearcher session management, and downloadFromSource
// against deterministic fake HTTP fixtures.
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/sources/deezer_client.dart';
import 'package:mai_an_lab/sources/deezer_crypto.dart';
import 'package:mai_an_lab/sources/downloadable.dart';
import 'package:mai_an_lab/sources/metadata_objects.dart';
import 'package:mai_an_lab/sources/qobuz_client.dart';
import 'package:mai_an_lab/sources/source_client.dart';
import 'package:mai_an_lab/sources/source_config.dart';
import 'package:mai_an_lab/sources/source_exceptions.dart';
import 'package:mai_an_lab/sources/streamrip_api.dart';
import 'package:mai_an_lab/sources/streamrip_search.dart';

class _RecordedRequest {
  _RecordedRequest(this.method, this.uri, this.headers, this.body);

  final String method;
  final Uri uri;
  final Map<String, String> headers;
  final List<int>? body;
}

class _FakeHttpClient implements SourceHttpClient {
  _FakeHttpClient(this._handler, [this._streamHandler]);

  final Future<SourceHttpResponse> Function(_RecordedRequest req) _handler;
  final Future<SourceStreamResponse> Function(_RecordedRequest req)?
  _streamHandler;

  final List<_RecordedRequest> requests = [];
  bool closed = false;

  @override
  Future<SourceHttpResponse> request(
    String method,
    Uri uri, {
    Map<String, String>? headers,
    List<int>? body,
    Duration? timeout,
    bool followRedirects = true,
  }) {
    final rec = _RecordedRequest(method, uri, Map.of(headers ?? const {}), body);
    requests.add(rec);
    return _handler(rec);
  }

  @override
  Future<SourceStreamResponse> openStream(
    Uri uri, {
    Map<String, String>? headers,
    Duration? timeout,
    bool followRedirects = true,
  }) async {
    final rec = _RecordedRequest('GET', uri, Map.of(headers ?? const {}), null);
    requests.add(rec);
    if (_streamHandler != null) return _streamHandler(rec);
    final resp = await _handler(rec);
    return SourceStreamResponse(
      statusCode: resp.statusCode,
      headers: resp.headers,
      stream: Stream<List<int>>.fromIterable([resp.bodyBytes]),
    );
  }

  @override
  void close() => closed = true;
}

SourceHttpResponse _jsonResp(Object? body, {int status = 200}) =>
    SourceHttpResponse(
      statusCode: status,
      headers: const {'content-type': 'application/json'},
      bodyBytes: utf8.encode(jsonEncode(body)),
    );

void main() {
  late Directory tempDir;

  setUp(() {
    tempDir = Directory.systemTemp.createTempSync('mai_an_sources');
  });

  tearDown(() {
    if (tempDir.existsSync()) tempDir.deleteSync(recursive: true);
  });

  group('QobuzClient', () {
    test('login without credentials enables guest signed catalog mode', () async {
      final http = _FakeHttpClient((req) async => _jsonResp({}));
      final client = QobuzClient(const SourceConfig(), http: http);
      await client.login();
      expect(client.loggedIn, isTrue);
      expect(client.guestMode, isTrue);
      expect(http.requests, isEmpty);
    });

    test('login with token authenticates via user/get and sets X-User-Auth-Token', () async {
      final http = _FakeHttpClient((req) async {
        expect(req.uri.queryParameters['request_sig'], isNotEmpty);
        if (req.uri.path.endsWith('/user/get')) {
          expect(req.headers['X-User-Auth-Token'], 'my_token_123');
          return _jsonResp({
            'user': {'id': 42},
            'user_auth_token': 'refreshed_tok',
          });
        }
        return _jsonResp({'id': 99, 'title': 'Track'});
      });
      final cfg = const SourceConfig(
        qobuz: QobuzConfig(
          useAuthToken: true,
          emailOrUserid: '42',
          passwordOrToken: 'my_token_123',
        ),
        downloads: DownloadsClientConfig(requestsPerMinute: 0),
      );
      final client = QobuzClient(cfg, http: http);
      await client.login();
      expect(client.loggedIn, isTrue);
      expect(client.guestMode, isFalse);
      expect(http.requests.first.uri.path, endsWith('/user/get'));

      // Next request carries refreshed token
      await client.getMetadata('99', 'track');
      expect(http.requests.last.uri.path, endsWith('/track/get'));
      expect(http.requests.last.headers['X-User-Auth-Token'], 'refreshed_tok');
    });

    test('login with plaintext password hashes to MD5 and falls back to guest on 401', () async {
      final http = _FakeHttpClient((req) async {
        expect(req.uri.path, endsWith('/user/login'));
        expect(QobuzClient.isMd5Hex(req.uri.queryParameters['password']!), isTrue);
        return _jsonResp({'message': 'Invalid credentials'}, status: 401);
      });
      final cfg = const SourceConfig(
        qobuz: QobuzConfig(
          useAuthToken: false,
          emailOrUserid: 'user@example.com',
          passwordOrToken: 'plaintext-secret',
        ),
        downloads: DownloadsClientConfig(requestsPerMinute: 0),
      );
      final client = QobuzClient(cfg, http: http);
      await client.login();
      expect(client.loggedIn, isTrue);
      expect(client.guestMode, isTrue);
    });

    test('search paginates multiple pages and passes signed params', () async {
      final http = _FakeHttpClient((req) async {
        final offset = int.parse(req.uri.queryParameters['offset'] ?? '0');
        return _jsonResp({
          'tracks': {
            'total': 5,
            'limit': 2,
            'offset': offset,
            'items': [
              {'id': offset + 1, 'title': 'T${offset + 1}'},
              if (offset + 2 <= 5) {'id': offset + 2, 'title': 'T${offset + 2}'},
            ],
          },
        });
      });
      final client = QobuzClient(
        const SourceConfig(downloads: DownloadsClientConfig(requestsPerMinute: 0)),
        http: http,
      );
      await client.login();
      final pages = await client.search('track', 'daft punk', limit: 5);
      expect(pages.length, 3);
      expect(http.requests.length, 3);
    });

    test('getDownloadable uses mime_type extension and formats restrictions', () async {
      var mode = 'downgraded';
      final http = _FakeHttpClient((req) async {
        if (mode == 'downgraded') {
          // Asked for FLAC (quality 3) but region downgraded to MP3
          return _jsonResp({
            'url': 'https://cdn.qobuz.com/audio/1.mp3',
            'mime_type': 'audio/mpeg',
          });
        }
        return _jsonResp({
          'restrictions': [
            {'code': 'TrackNotStreamable'},
          ],
        });
      });
      final client = QobuzClient(
        const SourceConfig(downloads: DownloadsClientConfig(requestsPerMinute: 0)),
        http: http,
      );
      await client.login();

      final d = await client.getDownloadable('19512574', 3);
      expect(d.extension, 'mp3');
      expect(d.url, 'https://cdn.qobuz.com/audio/1.mp3');

      mode = 'restricted';
      await expectLater(
        () => client.getDownloadable('19512574', 3),
        throwsA(
          isA<NonStreamableError>().having(
            (e) => e.message,
            'message',
            'Track not streamable.',
          ),
        ),
      );
    });
  });

  group('DeezerClient (mirrors test_deezer_quality.py)', () {
    _FakeHttpClient makeDeezerHttp({
      required bool canHq,
      required bool canLossless,
      Map<String, int>? sizes,
      List<String>? askedFormats,
      bool geoBlockFirst = false,
      bool returnEmptyMediaUrl = false,
    }) {
      var geoBlockedOnce = false;
      final trackSizes = sizes ??
          const {
            'FILESIZE_MP3_128': 1000,
            'FILESIZE_MP3_320': 2000,
            'FILESIZE_FLAC': 3000,
          };
      return _FakeHttpClient((req) async {
        if (req.uri.path.endsWith('/gw-light.php')) {
          final method = req.uri.queryParameters['method'];
          if (method == 'deezer.getUserData') {
            return _jsonResp({
              'error': <Object?>[],
              'results': {
                'checkForm': 'csrf_tok_1',
                'USER': {
                  'USER_ID': 12345,
                  'BLOG_NAME': 'Tester',
                  'MULTI_ACCOUNT': {'ENABLED': false, 'IS_SUB_ACCOUNT': false},
                  'OPTIONS': {
                    'license_token': 'lic_tok',
                    'web_hq': canHq,
                    'mobile_hq': false,
                    'web_lossless': canLossless,
                    'mobile_lossless': false,
                    'license_country': 'FR',
                  },
                  'SETTING': {
                    'global': {'language': 'en'},
                  },
                },
              },
            });
          }
          if (method == 'song.getData') {
            final body = jsonDecode(utf8.decode(req.body!)) as Map;
            final sngId = '${body['SNG_ID']}';
            return _jsonResp({
              'error': <Object?>[],
              'results': {
                'SNG_ID': sngId,
                'TRACK_TOKEN': 'tok_$sngId',
                'MD5_ORIGIN': '0123456789abcdef0123456789abcdef',
                'MEDIA_VERSION': '1',
                if (sngId == '1') 'FALLBACK': {'SNG_ID': '2'},
                ...trackSizes,
              },
            });
          }
        }
        if (req.uri.host == 'media.deezer.com') {
          final body = jsonDecode(utf8.decode(req.body!)) as Map;
          final fmt =
              (((body['media'] as List).first as Map)['formats'] as List).first
                  as Map;
          final fmtStr = fmt['format'] as String;
          askedFormats?.add(fmtStr);
          if (geoBlockFirst && !geoBlockedOnce) {
            geoBlockedOnce = true;
            return _jsonResp({
              'data': [
                {
                  'errors': [
                    {'code': 2002},
                  ],
                },
              ],
            });
          }
          if (returnEmptyMediaUrl) {
            return _jsonResp({
              'data': [<String, Object?>{}],
            });
          }
          return _jsonResp({
            'data': [
              {
                'media': [
                  {
                    'sources': [
                      {'url': 'https://cdn.dz.net/media/$fmtStr'},
                    ],
                  },
                ],
              },
            ],
          });
        }
        if (req.uri.host == 'api.deezer.com') {
          if (req.uri.path == '/search/track') {
            return _jsonResp({
              'total': 1,
              'data': [
                {
                  'id': 3135556,
                  'title': 'One More Time',
                  'artist': {'name': 'Daft Punk'},
                  'album': {'title': 'Discovery', 'cover_big': 'https://dz/c.jpg'},
                },
              ],
            });
          }
          if (req.uri.path == '/album/302127') {
            return _jsonResp({'id': 302127, 'title': 'Discovery'});
          }
          if (req.uri.path == '/album/302127/tracks') {
            return _jsonResp({
              'data': [
                {'id': 3135556, 'title': 'One More Time'},
              ],
            });
          }
        }
        return _jsonResp({}, status: 404);
      });
    }

    test('missing ARL throws MissingCredentialsError', () async {
      final client = DeezerClient(const SourceConfig(), http: _FakeHttpClient((_) async => _jsonResp({})));
      await expectLater(client.login(), throwsA(isA<MissingCredentialsError>()));
    });

    test('FLAC on lossy paid plan falls back to MP3_320 with .mp3 extension', () async {
      final asked = <String>[];
      final http = makeDeezerHttp(canHq: true, canLossless: false, askedFormats: asked);
      final client = DeezerClient(
        const SourceConfig(deezer: DeezerConfig(arl: 'valid_arl', lowerQualityIfNotAvailable: true)),
        http: http,
      );
      await client.login();
      final d = await client.getDownloadable('1', 2);
      expect(asked, ['MP3_320']);
      expect(d.quality, 1);
      expect(d.extension, 'mp3');
      expect(d.url, 'https://cdn.dz.net/media/MP3_320');
    });

    test('free plan falls back all the way to MP3_128', () async {
      final asked = <String>[];
      final http = makeDeezerHttp(canHq: false, canLossless: false, askedFormats: asked);
      final client = DeezerClient(
        const SourceConfig(deezer: DeezerConfig(arl: 'valid_arl', lowerQualityIfNotAvailable: true)),
        http: http,
      );
      await client.login();
      final d = await client.getDownloadable('1', 2);
      expect(asked, ['MP3_128']);
      expect(d.quality, 0);
      expect(d.extension, 'mp3');
    });

    test('no fallback when lowerQualityIfNotAvailable is false names the setting', () async {
      final http = makeDeezerHttp(canHq: true, canLossless: false);
      final client = DeezerClient(
        const SourceConfig(deezer: DeezerConfig(arl: 'valid_arl', lowerQualityIfNotAvailable: false)),
        http: http,
      );
      await client.login();
      await expectLater(
        () => client.getDownloadable('1', 2),
        throwsA(
          isA<NonStreamableError>().having(
            (e) => e.message,
            'message',
            contains('lower_quality_if_not_available'),
          ),
        ),
      );
    });

    test('geolocation error (2002) retries with FALLBACK.SNG_ID', () async {
      final http = makeDeezerHttp(canHq: true, canLossless: true, geoBlockFirst: true);
      final client = DeezerClient(
        const SourceConfig(deezer: DeezerConfig(arl: 'valid_arl')),
        http: http,
      );
      await client.login();
      final d = await client.getDownloadable('1', 2);
      expect(d.id, '2');
      expect(d.extension, 'flac');
    });

    test('empty media URL falls back to AES-128-ECB encrypted proxy URL', () async {
      final http = makeDeezerHttp(canHq: true, canLossless: true, returnEmptyMediaUrl: true);
      final client = DeezerClient(
        const SourceConfig(deezer: DeezerConfig(arl: 'valid_arl')),
        http: http,
      );
      await client.login();
      final d = await client.getDownloadable('3135556', 2);
      expect(d.url, startsWith('https://e-cdns-proxy-0.dzcdn.net/mobile/1/'));
    });

    test('DeezerDownloadable streaming stripe decryption handles multi-block + partial chunks', () async {
      // Build 22000 bytes (> 20000 threshold): 3 full 6144-byte blocks + 3568-byte tail (>= 2048)
      const total = 3 * 6144 + 3568;
      final wire = Uint8List.fromList([
        for (var i = 0; i < total; i++) (i * 29 + 7) & 0xFF,
      ]);
      final key = deezerBlowfishKey('3135556');

      // Compute expected output directly block-by-block
      final expected = BytesBuilder();
      var off = 0;
      while (off < wire.length) {
        final rem = wire.length - off;
        final blkLen = rem >= 6144 ? 6144 : rem;
        final blk = Uint8List.sublistView(wire, off, off + blkLen);
        if (blkLen >= 2048) {
          expected.add(deezerBlowfishCbcDecrypt(key, Uint8List.sublistView(blk, 0, 2048)));
          expected.add(Uint8List.sublistView(blk, 2048));
        } else {
          expected.add(blk);
        }
        off += blkLen;
      }

      // Feed wire in awkward 1000-byte network chunks to exercise carry-buffer boundary logic
      final chunks = <List<int>>[];
      for (var i = 0; i < wire.length; i += 1000) {
        final end = (i + 1000) > wire.length ? wire.length : (i + 1000);
        chunks.add(wire.sublist(i, end));
      }

      final http = _FakeHttpClient(
        (_) async => _jsonResp({}),
        (_) async => SourceStreamResponse(
          statusCode: 200,
          headers: {'content-length': '$total'},
          stream: Stream<List<int>>.fromIterable(chunks),
        ),
      );
      final dl = DeezerDownloadable(http, {
        'id': '3135556',
        'url': 'https://cdn.dz.net/media/1/track',
        'quality': 2,
        'quality_to_size': [1000, 2000, total],
      });
      final outPath = '${tempDir.path}/out.flac';
      var reportedBytes = 0;
      await dl.download(outPath, (n) => reportedBytes += n);

      expect(reportedBytes, total);
      expect(File(outPath).readAsBytesSync(), expected.takeBytes());
    });
  });

  group('StreamripSearcher & downloadFromSource', () {
    test('caches clients and resets when credentials change', () async {
      var cfg = const SourceConfig(
        qobuz: QobuzConfig(emailOrUserid: 'u1', passwordOrToken: 't1'),
        downloads: DownloadsClientConfig(requestsPerMinute: 0),
      );
      var created = 0;
      final searcher = StreamripSearcher(
        readConfig: () async => cfg,
        clientFactory: (source, c) {
          created++;
          final http = _FakeHttpClient((req) async {
            if (req.uri.path.endsWith('/user/login')) {
              return _jsonResp({'user': {'id': 1}, 'user_auth_token': 'tok'});
            }
            return _jsonResp({
              'tracks': {
                'total': 1,
                'limit': 50,
                'offset': 0,
                'items': [
                  {'id': 1, 'title': 'Song', 'performer': {'name': 'Artist'}},
                ],
              },
            });
          });
          return QobuzClient(c, http: http);
        },
      );

      final r1 = await searcher.search('song', source: 'qobuz', mediaTypes: const ['track']);
      expect(r1.error, isNull);
      expect(r1.items.length, 1);
      expect(created, 1);

      // Same creds -> reuses cached client
      await searcher.search('song2', source: 'qobuz', mediaTypes: const ['track']);
      expect(created, 1);

      // Changed creds -> closes old client and logs in fresh
      cfg = const SourceConfig(
        qobuz: QobuzConfig(emailOrUserid: 'u2', passwordOrToken: 't2'),
        downloads: DownloadsClientConfig(requestsPerMinute: 0),
      );
      await searcher.search('song3', source: 'qobuz', mediaTypes: const ['track']);
      expect(created, 2);
      await searcher.closeAll();
    });

    test('downloadFromSource downloads, passes normalized TrackMetadata to tagWriter, and NFC-normalizes filename', () async {
      final audioBytes = List<int>.filled(1024, 0x42);
      final coverBytes = [0xFF, 0xD8, 0xFF, 0xE0];
      TrackMetadata? taggedMeta;
      String? taggedCoverPath;

      final http = _FakeHttpClient((req) async {
        if (req.uri.path.endsWith('/track/get')) {
          return _jsonResp({
            'id': 19512574,
            'title': 'Cafe\u0301: Night?', // NFD + illegal chars
            'performer': {'name': 'Artist'},
            'track_number': 3,
            'media_number': 1,
            'isrc': 'ISRC123',
            'album': {
              'id': 'alb1',
              'title': 'Album',
              'artist': {'name': 'Artist'},
              'release_date': '2024-02-01',
              'tracks_count': 10,
              'media_count': 1,
              'image': {'large': 'https://img.qobuz.com/cover.jpg'},
            },
          });
        }
        if (req.uri.path.endsWith('/track/getFileUrl')) {
          return _jsonResp({
            'url': 'https://cdn.qobuz.com/19512574.flac',
            'mime_type': 'audio/flac',
          });
        }
        if (req.uri.toString() == 'https://cdn.qobuz.com/19512574.flac') {
          if (req.method == 'HEAD') {
            return SourceHttpResponse(
              statusCode: 200,
              headers: {'content-length': '${audioBytes.length}'},
              bodyBytes: const [],
            );
          }
          return SourceHttpResponse(
            statusCode: 200,
            headers: {'content-length': '${audioBytes.length}'},
            bodyBytes: audioBytes,
          );
        }
        if (req.uri.toString() == 'https://img.qobuz.com/cover.jpg') {
          return SourceHttpResponse(
            statusCode: 200,
            headers: const {'content-type': 'image/jpeg'},
            bodyBytes: coverBytes,
          );
        }
        return _jsonResp({}, status: 404);
      });

      final saved = await downloadFromSource(
        url: 'https://www.qobuz.com/track/19512574',
        downloadDir: tempDir.path,
        config: const SourceConfig(
          downloads: DownloadsClientConfig(requestsPerMinute: 0),
        ),
        clientFactory: (_, cfg) => QobuzClient(cfg, http: http),
        tempDir: tempDir,
        tagWriter: (path, meta, coverPath) async {
          taggedMeta = meta;
          taggedCoverPath = coverPath;
          expect(coverPath, isNotNull);
          expect(File(coverPath!).readAsBytesSync(), coverBytes);
        },
      );

      expect(saved.length, 1);
      // NFC composed 'é', ':' and '?' stripped by cleanFilename
      expect(saved.first, endsWith('03. Caf\u00e9 Night.flac'));
      expect(File(saved.first).readAsBytesSync(), audioBytes);
      expect(taggedMeta?.title, 'Cafe\u0301: Night?');
      expect(taggedMeta?.album.year, '2024');
      expect(taggedMeta?.isrc, 'ISRC123');
      // Temp cover cleaned up after tagWriter completes
      expect(File(taggedCoverPath!).existsSync(), isFalse);
    });
  });
}
