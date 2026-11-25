// Python-parity golden tests for Phase 6b source clients:
//   - Deezer Blowfish key derivation, Blowfish-CBC decryption, AES-128-ECB URL
//   - Qobuz MD5 API & file-URL signatures + QobuzSpoofer bundle parsing
//   - streamrip_api.detectTypeAndId URL parsing
//   - source_metadata.extractTracks & normalizeTrack
//   - search_results.SearchResults & StreamripSearcher.parseResults / humanizeSearchError
//
// Regenerate goldens with:
//   ~/miniconda3/bin/python tool/export_sources_goldens.py
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/sources/deezer_crypto.dart';
import 'package:mai_an_lab/sources/qobuz_client.dart';
import 'package:mai_an_lab/sources/search_results.dart';
import 'package:mai_an_lab/sources/source_exceptions.dart';
import 'package:mai_an_lab/sources/source_metadata.dart';
import 'package:mai_an_lab/sources/streamrip_api.dart';
import 'package:mai_an_lab/sources/streamrip_search.dart';

Uint8List _hexToBytes(String hex) {
  final out = Uint8List(hex.length ~/ 2);
  for (var i = 0; i < out.length; i++) {
    out[i] = int.parse(hex.substring(i * 2, i * 2 + 2), radix: 16);
  }
  return out;
}

String _bytesToHex(List<int> bytes) {
  final buf = StringBuffer();
  for (final b in bytes) {
    buf.write(b.toRadixString(16).padLeft(2, '0'));
  }
  return buf.toString();
}

Map<String, Object?> _deepCopyMap(Map<Object?, Object?> src) => {
  for (final e in src.entries)
    '${e.key}': switch (e.value) {
      final Map<Object?, Object?> m => _deepCopyMap(m),
      final List<Object?> l => _deepCopyList(l),
      final v => v,
    },
};

List<Object?> _deepCopyList(List<Object?> src) => [
  for (final v in src)
    switch (v) {
      final Map<Object?, Object?> m => _deepCopyMap(m),
      final List<Object?> l => _deepCopyList(l),
      final other => other,
    },
];

void main() {
  final golden =
      jsonDecode(File('test/goldens/sources_phase6b.json').readAsStringSync())
          as Map<String, dynamic>;

  group('Deezer crypto goldens (pycryptodomex parity)', () {
    test('Blowfish per-track key derivation matches Python', () {
      final keys = (golden['blowfish_keys'] as Map).cast<String, String>();
      for (final e in keys.entries) {
        expect(_bytesToHex(deezerBlowfishKey(e.key)), e.value, reason: e.key);
      }
    });

    test(
      'Blowfish-CBC 2048-byte stripe decryption matches pycryptodomex bit-for-bit',
      () {
        final cases = (golden['blowfish_decrypts'] as List)
            .cast<Map<String, dynamic>>();
        for (final c in cases) {
          final key = deezerBlowfishKey(c['track_id'] as String);
          final cipher = _hexToBytes(c['cipher_hex'] as String);
          final plain = deezerBlowfishCbcDecrypt(key, cipher);
          expect(
            _bytesToHex(plain),
            c['plain_hex'],
            reason: c['track_id'] as String,
          );
        }
      },
    );

    test('AES-128-ECB fallback encrypted CDN URL matches pycryptodomex', () {
      final cases = (golden['aes_encrypted_urls'] as List)
          .cast<Map<String, dynamic>>();
      for (final c in cases) {
        final url = deezerEncryptedFileUrl(
          trackId: c['track_id'] as String,
          md5Origin: c['md5_origin'] as String,
          mediaVersion: c['media_version'] as String,
        );
        expect(url, c['url'], reason: c['track_id'] as String);
      }
    });
  });

  group('Qobuz signing & spoofer goldens', () {
    test('OrpheusDL MD5 API signing matches Python', () {
      final cases = (golden['qobuz_api_sigs'] as List)
          .cast<Map<String, dynamic>>();
      for (final c in cases) {
        final sig = QobuzClient.signApiParams(
          c['endpoint'] as String,
          (c['params'] as Map).cast<String, String>(),
          c['unix_ts'] as String,
          c['secret'] as String,
        );
        expect(sig, c['sig'], reason: c['endpoint'] as String);
      }
    });

    test('track/getFileUrl format_id and MD5 signature match Python', () {
      final cases = (golden['qobuz_file_sigs'] as List)
          .cast<Map<String, dynamic>>();
      for (final c in cases) {
        final q = c['quality'] as int;
        final fmt = QobuzClient.formatIdForQuality(q);
        expect(fmt, c['format_id']);
        final sig = QobuzClient.signFileUrl(
          trackId: c['track_id'] as String,
          formatId: fmt,
          unixTs: c['unix_ts'] as String,
          secret: c['secret'] as String,
        );
        expect(sig, c['sig'], reason: 'quality=$q');
      }
    });

    test(
      'QobuzSpoofer.parseBundle matches Python regex & timezone ordering',
      () {
        final sp = golden['qobuz_spoofer'] as Map<String, dynamic>;
        final (appId, secrets) = QobuzSpoofer.parseBundle(
          sp['bundle_js'] as String,
        );
        expect(appId, sp['app_id']);
        expect(secrets, (sp['secrets'] as List).cast<String>());
      },
    );
  });

  group('URL detection goldens', () {
    test(
      'detectTypeAndId matches Python streamrip_api._detect_type_and_id',
      () {
        final cases = (golden['url_cases'] as List)
            .cast<Map<String, dynamic>>();
        for (final c in cases) {
          final parsed = detectTypeAndId(c['url'] as String);
          if (c['media_type'] == null) {
            expect(parsed, isNull, reason: c['url'] as String);
          } else {
            expect(parsed, isNotNull, reason: c['url'] as String);
            expect(
              parsed!.mediaType,
              c['media_type'],
              reason: c['url'] as String,
            );
            expect(parsed.id, c['id'], reason: c['url'] as String);
            expect(parsed.source, c['source'], reason: c['url'] as String);
          }
        }
      },
    );
  });

  group('source_metadata goldens', () {
    test('Qobuz extractTracks + normalizeTrack matches Python', () {
      final sm = golden['source_metadata'] as Map<String, dynamic>;
      final payload = _deepCopyMap(sm['qobuz_album_payload'] as Map);
      final tracks = extractTracks('qobuz', 'album', payload);
      final norm = [
        for (var i = 0; i < tracks.length; i++)
          normalizeTrack('qobuz', tracks[i], i + 1),
      ];
      expect(norm, sm['qobuz_normalized']);
    });

    test('Deezer extractTracks + normalizeTrack matches Python', () {
      final sm = golden['source_metadata'] as Map<String, dynamic>;
      final payload = _deepCopyMap(sm['deezer_album_payload'] as Map);
      final tracks = extractTracks('deezer', 'album', payload);
      final norm = [
        for (var i = 0; i < tracks.length; i++)
          normalizeTrack('deezer', tracks[i], i + 1),
      ];
      expect(norm, sm['deezer_normalized']);
    });
  });

  group('SearchResults & StreamripSearcher parsing goldens', () {
    test(
      'parseResults matches Python for Qobuz and Deezer (including dedup)',
      () {
        final sp = golden['search_parsing'] as Map<String, dynamic>;
        final qRaw = [
          for (final item in sp['qobuz_raw'] as List) _deepCopyMap(item as Map),
        ];
        final dzRaw = [
          for (final item in sp['deezer_raw'] as List)
            _deepCopyMap(item as Map),
        ];

        expect(
          StreamripSearcher.parseResults(qRaw, 'qobuz'),
          sp['qobuz_parsed'],
        );
        expect(
          StreamripSearcher.parseResults(dzRaw, 'deezer'),
          sp['deezer_parsed'],
        );
      },
    );

    test('SearchResults summaries and previews match Python', () {
      final sp = golden['search_parsing'] as Map<String, dynamic>;
      final qRaw = [
        for (final item in sp['qobuz_raw'] as List) _deepCopyMap(item as Map),
      ];
      final expected = sp['summaries'] as Map<String, dynamic>;

      final tr = SearchResults.fromPages('qobuz', 'track', [
        {
          'tracks': {
            'items': [qRaw[0]],
          },
        },
      ]);
      expect(tr.summaries(), expected['track']);
      expect(tr.results.first.preview(), expected['track_preview']);

      final al = SearchResults.fromPages('qobuz', 'album', [
        {
          'albums': {
            'items': [qRaw[3]],
          },
        },
      ]);
      expect(al.summaries(), expected['album']);
      expect(al.results.first.preview(), expected['album_preview']);

      final ar = SearchResults.fromPages('qobuz', 'artist', [
        {
          'artists': {
            'items': [qRaw[4]],
          },
        },
      ]);
      expect(ar.summaries(), expected['artist']);
      expect(ar.results.first.preview(), expected['artist_preview']);

      final pl = SearchResults.fromPages('qobuz', 'playlist', [
        {
          'playlists': {
            'items': [qRaw[5]],
          },
        },
      ]);
      expect(pl.summaries(), expected['playlist']);
      expect(pl.results.first.preview(), expected['playlist_preview']);
    });

    test('humanizeSearchError matches Python messages', () {
      final cases = (golden['humanize_errors'] as List)
          .cast<Map<String, dynamic>>();
      for (final c in cases) {
        final Object exc = (c['is_invalid_secret'] as bool)
            ? InvalidAppSecretError(c['input'] as String)
            : Exception(c['input'] as String);
        final out = StreamripSearcher.humanizeSearchError(
          exc,
        ).toString().replaceFirst(RegExp(r'^Exception:\s*'), '');
        expect(out, c['output'], reason: c['label'] as String);
      }
    });
  });
}
