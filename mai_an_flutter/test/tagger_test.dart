// Phase 6c: Pure-Dart audio tagger (FLAC, MP3 ID3v2.3, M4A/MP4 ilst + covr)
// verified against TinyTag round-trip and Python tagger.py goldens.
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/scan/tinytag.dart';
import 'package:mai_an_lab/sources/metadata_objects.dart';
import 'package:mai_an_lab/sources/tagger.dart';

/// Minimal valid FLAC file: `fLaC` + 34-byte STREAMINFO (44.1 kHz, 2 ch, 16-bit,
/// 44100 samples) + dummy frame bytes.
Uint8List _minimalFlac() {
  final out = BytesBuilder();
  out.add(ascii.encode('fLaC'));
  // Last metadata block (0x80 | 0 = STREAMINFO), length 34.
  out.add([0x80, 0x00, 0x00, 34]);
  final si = Uint8List(34);
  // min/max block size = 4096
  si[0] = 0x10;
  si[1] = 0x00;
  si[2] = 0x10;
  si[3] = 0x00;
  // sample rate 44100 (20 bits) | channels-1=1 (3 bits) | bps-1=15 (5 bits) | total samples=44100 (36 bits)
  // 44100 = 0x0AC44
  si[10] = 0x0A;
  si[11] = 0xC4;
  si[12] = 0x42; // low nibble 4, channels-1 = 1 (001), top bit of bps-1=0
  si[13] = 0xF0; // low 4 bits of bps-1=15 (1111), high 4 bits of samples=0
  si[14] = 0x00;
  si[15] = 0x00;
  si[16] = 0xAC;
  si[17] = 0x44;
  out.add(si);
  // Dummy audio payload to confirm audio frames survive verbatim.
  out.add(const [0xFF, 0xF8, 0x69, 0x18, 0x00, 0x00, 0xAA, 0xBB]);
  return out.takeBytes();
}

/// Minimal valid MPEG-1 Layer III 128 kbps 44.1 kHz frame (417 bytes).
Uint8List _minimalMp3() {
  final frame = Uint8List(417);
  // Sync 0xFFE | MPEG1 (11) | Layer III (01) | no CRC (1) => FF FB
  frame[0] = 0xFF;
  frame[1] = 0xFB;
  // 128 kbps (1001) | 44.1 kHz (00) | padding 0 | private 0 => 0x90
  frame[2] = 0x90;
  frame[3] = 0x00;
  for (var i = 4; i < frame.length; i++) {
    frame[i] = i & 0xFF;
  }
  return frame;
}

/// Minimal valid MP4/M4A file with `ftyp`, `moov` (containing `mvhd` + `trak`
/// with `stco` pointing at `mdat`), and `mdat` AFTER `moov`.
Uint8List _minimalMp4() {
  Uint8List atom(String type, List<int> body) {
    final len = 8 + body.length;
    return Uint8List.fromList([
      (len >> 24) & 0xFF,
      (len >> 16) & 0xFF,
      (len >> 8) & 0xFF,
      len & 0xFF,
      ...latin1.encode(type),
      ...body,
    ]);
  }

  final ftyp = atom('ftyp', [
    ...latin1.encode('M4A '),
    0,
    0,
    0,
    0,
    ...latin1.encode('M4A '),
    ...latin1.encode('isom'),
  ]);

  // mvhd version 0: 100 bytes body (timescale=1000, duration=5000 => 5.0s)
  final mvhdBody = Uint8List(100);
  // timescale at offset 12 = 1000 (0x03E8)
  mvhdBody[14] = 0x03;
  mvhdBody[15] = 0xE8;
  // duration at offset 16 = 5000 (0x1388)
  mvhdBody[18] = 0x13;
  mvhdBody[19] = 0x88;
  final mvhd = atom('mvhd', mvhdBody);

  // Placeholder stco pointing to mdat payload; we compute exact offset below.
  Uint8List buildMoov(int mdatOffset) {
    final stco = atom('stco', [
      0, 0, 0, 0, // version + flags
      0, 0, 0, 1, // entry count = 1
      (mdatOffset >> 24) & 0xFF,
      (mdatOffset >> 16) & 0xFF,
      (mdatOffset >> 8) & 0xFF,
      mdatOffset & 0xFF,
    ]);
    final stbl = atom('stbl', stco);
    final minf = atom('minf', stbl);
    final mdia = atom('mdia', minf);
    final trak = atom('trak', mdia);
    return atom('moov', [...mvhd, ...trak]);
  }

  final probeMoov = buildMoov(0);
  final mdatPayloadOffset = ftyp.length + probeMoov.length + 8;
  final moov = buildMoov(mdatPayloadOffset);
  final mdat = atom('mdat', const [
    0xDE,
    0xAD,
    0xBE,
    0xEF,
    0x11,
    0x22,
    0x33,
    0x44,
  ]);

  return Uint8List.fromList([...ftyp, ...moov, ...mdat]);
}

/// Minimal JPEG SOI..EOI bytes for cover-art embedding tests.
final _fakeJpeg = Uint8List.fromList([
  0xFF,
  0xD8,
  0xFF,
  0xE0,
  0x00,
  0x10,
  0x4A,
  0x46,
  0x49,
  0x46,
  0x00,
  0x01,
  0x01,
  0x00,
  0x00,
  0x01,
  0x00,
  0x01,
  0x00,
  0x00,
  0xFF,
  0xD9,
]);

TrackMetadata _sampleMeta() => const TrackMetadata(
  id: 'trk_2',
  title: 'Archangel',
  artist: 'Burial',
  album: AlbumMetadata(
    id: 'alb_1',
    album: 'Untrue',
    artist: 'Burial',
    year: '2007',
    tracktotal: 13,
    disctotal: 1,
    genres: ['Electronic', 'UK Garage'],
    copyright: '(P) 2007 Hyperdub',
  ),
  tracknumber: 2,
  discnumber: 1,
  isrc: 'GBHYP0700002',
  composer: 'William Bevan',
  lyrics: 'Holding you',
);

/// Helper to verify [needle] byte sequence appears verbatim in [haystack].
bool _containsBytes(Uint8List haystack, Uint8List needle) {
  for (var i = 0; i <= haystack.length - needle.length; i++) {
    var ok = true;
    for (var j = 0; j < needle.length; j++) {
      if (haystack[i + j] != needle[j]) {
        ok = false;
        break;
      }
    }
    if (ok) return true;
  }
  return false;
}

void main() {
  late Directory dir;

  setUp(() {
    dir = Directory.systemTemp.createTempSync('mai_an_tagger');
  });

  tearDown(() => dir.deleteSync(recursive: true));

  test('flacTagPairs matches Python tagger.py golden output', () {
    final golden =
        jsonDecode(File('test/goldens/phase6c.json').readAsStringSync())
            as Map<String, dynamic>;
    final expected = [
      for (final pair in golden['flac_tag_pairs'] as List<dynamic>)
        ('${(pair as List)[0]}', '${pair[1]}'),
    ];
    expect(flacTagPairs(_sampleMeta()), expected);
  });

  test(
    'FLAC tagging round-trips through TinyTag with cover art and preserves audio',
    () async {
      final flacFile = File('${dir.path}/02. Archangel.flac')
        ..writeAsBytesSync(_minimalFlac());
      final coverFile = File('${dir.path}/cover.jpg')
        ..writeAsBytesSync(_fakeJpeg);

      await tagFile(flacFile.path, _sampleMeta(), coverFile.path);

      final parsed = TinyTag.get(flacFile.path);
      expect(parsed.title, 'Archangel');
      expect(parsed.artist, 'Burial');
      expect(parsed.album, 'Untrue');
      expect(parsed.albumartist, 'Burial');
      expect(parsed.year, '2007');
      expect(parsed.genre, 'Electronic, UK Garage');
      expect(parsed.composer, 'William Bevan');
      expect(parsed.track, 2);
      expect(parsed.trackTotal, 13);
      expect(parsed.disc, 1);
      expect(parsed.discTotal, 1);
      expect(parsed.other['isrc'], ['GBHYP0700002']);
      expect(parsed.other['copyright'], ['(P) 2007 Hyperdub']);

      // Embedded PICTURE block contains JPEG bytes and trailing audio survives.
      final bytes = flacFile.readAsBytesSync();
      expect(_containsBytes(bytes, _fakeJpeg), isTrue);
      expect(bytes.sublist(bytes.length - 8), const [
        0xFF,
        0xF8,
        0x69,
        0x18,
        0x00,
        0x00,
        0xAA,
        0xBB,
      ]);
    },
  );

  test(
    'MP3 ID3v2.3 tagging round-trips through TinyTag with Unicode & cover art',
    () async {
      final mp3File = File('${dir.path}/02. Archangel.mp3')
        ..writeAsBytesSync(_minimalMp3());
      final coverFile = File('${dir.path}/cover.jpg')
        ..writeAsBytesSync(_fakeJpeg);
      const unicodeMeta = TrackMetadata(
        id: 'trk_u',
        title: 'Jóga',
        artist: 'Björk',
        album: AlbumMetadata(
          id: 'alb_u',
          album: 'Homogenic',
          artist: 'Björk',
          year: '1997',
          tracktotal: 10,
          disctotal: 1,
          genres: ['Electronic'],
          copyright: '(P) 1997 One Little Indian',
        ),
        tracknumber: 2,
        discnumber: 1,
        isrc: 'GBBKS9700002',
        composer: 'Björk Guðmundsdóttir',
      );

      await tagFile(mp3File.path, unicodeMeta, coverFile.path);

      final parsed = TinyTag.get(mp3File.path);
      expect(parsed.title, 'Jóga');
      expect(parsed.artist, 'Björk');
      expect(parsed.album, 'Homogenic');
      expect(parsed.albumartist, 'Björk');
      expect(parsed.year, '1997');
      expect(parsed.genre, 'Electronic');
      expect(parsed.composer, 'Björk Guðmundsdóttir');
      expect(parsed.track, 2);
      expect(parsed.trackTotal, 10);
      expect(parsed.disc, 1);
      expect(parsed.discTotal, 1);
      expect(parsed.other['isrc'], ['GBBKS9700002']);
      expect(_containsBytes(mp3File.readAsBytesSync(), _fakeJpeg), isTrue);

      // Re-tagging replaces the prior ID3 header without duplicating audio.
      await tagFile(mp3File.path, _sampleMeta(), coverFile.path);
      final retagged = TinyTag.get(mp3File.path);
      expect(retagged.title, 'Archangel');
      expect(retagged.artist, 'Burial');
    },
  );

  test(
    'MP4/M4A tagging round-trips through TinyTag and shifts stco offsets cleanly',
    () async {
      final original = _minimalMp4();
      final m4aFile = File('${dir.path}/02. Archangel.m4a')
        ..writeAsBytesSync(original);
      final coverFile = File('${dir.path}/cover.jpg')
        ..writeAsBytesSync(_fakeJpeg);

      await tagFile(m4aFile.path, _sampleMeta(), coverFile.path);

      final parsed = TinyTag.get(m4aFile.path);
      expect(parsed.title, 'Archangel');
      expect(parsed.artist, 'Burial');
      expect(parsed.album, 'Untrue');
      expect(parsed.albumartist, 'Burial');
      expect(parsed.year, '2007');
      expect(parsed.genre, 'Electronic, UK Garage');
      expect(parsed.composer, 'William Bevan');
      expect(parsed.track, 2);
      expect(parsed.trackTotal, 13);
      expect(parsed.disc, 1);
      expect(parsed.discTotal, 1);

      // Verify covr JPEG bytes present and stco chunk offset points at mdat 0xDEADBEEF.
      final outBytes = m4aFile.readAsBytesSync();
      expect(_containsBytes(outBytes, _fakeJpeg), isTrue);
      var stcoPos = -1;
      for (var i = 0; i < outBytes.length - 16; i++) {
        if (outBytes[i] == 0x73 &&
            outBytes[i + 1] == 0x74 &&
            outBytes[i + 2] == 0x63 &&
            outBytes[i + 3] == 0x6F) {
          stcoPos = i;
          break;
        }
      }
      expect(stcoPos, greaterThan(0));
      final offsetIdx = stcoPos + 12;
      final chunkOffset =
          (outBytes[offsetIdx] << 24) |
          (outBytes[offsetIdx + 1] << 16) |
          (outBytes[offsetIdx + 2] << 8) |
          outBytes[offsetIdx + 3];
      expect(outBytes.sublist(chunkOffset, chunkOffset + 4), const [
        0xDE,
        0xAD,
        0xBE,
        0xEF,
      ]);
    },
  );
}
