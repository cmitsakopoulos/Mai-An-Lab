// NFC (filepath_utils.normalize_nfc), clean_filename and pathlib's name
// parts, against Python's own output (PORT_MAP §0 rule 3). Python runs once
// on every input; skipped without ~/miniconda3/bin/python.
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/nfc_table.dart';
import 'package:mai_an_lab/downloads/companion_download.dart';
import 'package:mai_an_lab/downloads/file_names.dart';

final _python = '${Platform.environment['HOME']}/miniconda3/bin/python';

const _script = r'''
import json, sys, unicodedata
from pathlib import Path
sys.path.insert(0, "../StreamripApp")
from utils.filepath_utils import clean_filename
d = json.load(sys.stdin)
out = {
  "nfc": [unicodedata.normalize("NFC", s) for s in d["nfc"]],
  "clean": [clean_filename(s) for s in d["clean"]],
  "parts": [[Path(p).name, Path(p).stem, Path(p).suffix] for p in d["parts"]],
}
json.dump(out, sys.stdout)
''';

/// Every character with a canonical decomposition, alone and decomposed,
/// plus Hangul, reordering and blocking cases and random mixes.
List<String> _nfcInputs() {
  final out = <String>[];
  for (var i = 0; i < kCanonicalDecomp.length;) {
    final cp = kCanonicalDecomp[i], n = kCanonicalDecomp[i + 1];
    out
      ..add(String.fromCharCode(cp))
      ..add(String.fromCharCodes(kCanonicalDecomp.sublist(i + 2, i + 2 + n)));
    i += 2 + n;
  }
  out.addAll([
    '',
    'plain ascii',
    'Ελληνικό',
    'Ελληνικό'.runes.map(String.fromCharCode).join(),
    'ȩ́', // acute then cedilla: reordered, then composed
    'ȩ́',
    'ǟ', // ǟ
    'ā̈',
    'Ǻ', // Ǻ
    '가', // 가
    '각', // 각
    '각', // LV + T
    '가̀ᆨ', // blocked by a mark
    'Ά́',
    'q̣̇',
    '̈́', // excluded, non-starter decomposition
    'क़', // composition exclusion
    'Ω', // singleton
    '́abc', // leading mark
    '😀é',
  ]);
  final rnd = Random(7);
  const pool = [
    0x61,
    0x65,
    0x6f,
    0x41,
    0x391,
    0x3b1,
    0x3c9,
    0x1100,
    0x1161,
    0x11a8,
    0xac00,
    0x300,
    0x301,
    0x308,
    0x323,
    0x327,
    0x342,
    0x345,
    0x5d0,
    0x5b4,
    0x93f,
    0x94d,
    0x20,
    0xe9,
  ];
  for (var i = 0; i < 2000; i++) {
    final len = 1 + rnd.nextInt(6);
    out.add(
      String.fromCharCodes([
        for (var j = 0; j < len; j++) pool[rnd.nextInt(pool.length)],
      ]),
    );
  }
  return out;
}

final _cleanInputs = [
  '01 Ένα.flac',
  'AC/DC: Back in Black?.mp3',
  r'a\b*c?d:e"f<g>h|i.m4a',
  '  padded name .flac  ',
  ' nbsp .flac',
  'Ελληνικό'.runes.map(String.fromCharCode).join(),
  'é.flac',
  'x' * 300,
  '${'α' * 127}β.flac', // 254 + 2 bytes: the cut falls inside β
  '${'α' * 126}xβ.flac',
  '${'😀' * 64}.flac',
  '',
  '.flac',
];

final _partsInputs = [
  '/Users/me/Music/Άλμπουμ/01 Ένα.flac',
  'track.audio',
  '/a/b/.hidden',
  '/a/b/name.',
  '/a/b/archive.tar.gz',
  '/a/b/',
  '/a/./b/c.mp3',
  'noext',
  '/a/b/..flac',
];

void main() {
  final hasPython = File(_python).existsSync();

  group('against Python', () {
    late Map<String, Object?> py;
    final nfcIn = _nfcInputs();

    setUpAll(() async {
      final proc = await Process.start(_python, ['-c', _script]);
      proc.stdin.write(
        jsonEncode({
          'nfc': nfcIn,
          'clean': _cleanInputs,
          'parts': _partsInputs,
        }),
      );
      await proc.stdin.close();
      final out = await proc.stdout.transform(utf8.decoder).join();
      final err = await proc.stderr.transform(utf8.decoder).join();
      expect(await proc.exitCode, 0, reason: err);
      py = jsonDecode(out) as Map<String, Object?>;
    });

    test('NFC on ${nfcIn.length} strings', () {
      final want = (py['nfc']! as List).cast<String>();
      final bad = <String>[];
      for (var i = 0; i < nfcIn.length; i++) {
        if (nfc(nfcIn[i]) != want[i]) {
          bad.add(nfcIn[i].runes.map((r) => r.toRadixString(16)).join(' '));
        }
      }
      expect(bad, isEmpty);
    });

    test('clean_filename', () {
      final want = (py['clean']! as List).cast<String>();
      for (var i = 0; i < _cleanInputs.length; i++) {
        expect(
          cleanFilename(_cleanInputs[i]),
          want[i],
          reason: _cleanInputs[i],
        );
      }
    });

    test('Path name, stem, suffix', () {
      final want = py['parts']! as List;
      for (var i = 0; i < _partsInputs.length; i++) {
        final p = pyPathParts(_partsInputs[i]);
        expect([p.name, p.stem, p.suffix], want[i], reason: _partsInputs[i]);
      }
    });
  }, skip: hasPython ? false : 'needs $_python');

  test('companion file names follow the Mac name and its encode', () {
    expect(
      companionFileName(
        'http://h:1/api/v1/tracks/stream?path=%2Fm%2F01%20A.flac&quality=original&token=t',
      ),
      '01 A.flac',
    );
    expect(
      companionFileName(
        'http://h:1/api/v1/tracks/stream?path=%2Fm%2F01+A.flac&quality=aac_256k',
      ),
      '01 A.m4a',
    );
    expect(
      companionFileName('http://h:1/api/v1/tracks/stream?path=&quality=x'),
      'track.audio',
    );
    expect(
      companionFileName('http://h:1/api/v1/tracks/stream?path=%2Fm%2Fnoext'),
      'noext.flac',
    );
  });
}

String nfc(String s) => normalizeNfc(s);
