import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'metadata_objects.dart';

/// Clean-room pure-Dart audio tagger matching `StreamripApp/utils/tagger.py`:
/// writes metadata tags and optional cover art to FLAC (Vorbis Comment +
/// PICTURE metadata blocks), MP3 (ID3v2.3 frames + APIC), and M4A/MP4
/// (`moov/udta/meta/ilst` + `covr` with automatic `stco`/`co64` offset
/// adjustment when `moov` precedes `mdat`).
///
/// Round-trips cleanly through both `lib/scan/tinytag.dart` and Python's
/// `mutagen` / `tinytag`.

const kFlacMaxBlockSize = 16777215;

const _kMetadataTypes = [
  'title',
  'artist',
  'album',
  'albumartist',
  'composer',
  'year',
  'comment',
  'description',
  'purchase_date',
  'grouping',
  'genre',
  'lyrics',
  'encoder',
  'copyright',
  'compilation',
  'tracknumber',
  'discnumber',
  'tracktotal',
  'disctotal',
  'date',
  'isrc',
];

const _kZeroPadKeys = {'tracknumber', 'discnumber', 'tracktotal', 'disctotal'};

/// Container format supported by [tagFile].
enum TagContainer { flac, aac, mp3 }

/// Resolve [TagContainer] from [path]'s extension, or null if unsupported.
TagContainer? containerForPath(String path) {
  final dot = path.lastIndexOf('.');
  if (dot < 0) return null;
  return switch (path.substring(dot + 1).toLowerCase()) {
    'flac' => TagContainer.flac,
    'm4a' || 'mp4' => TagContainer.aac,
    'mp3' => TagContainer.mp3,
    _ => null,
  };
}

/// Look up a metadata attribute from [meta] matching `Container._attr_from_meta`.
String? attrFromMeta(TrackMetadata meta, String attr) {
  return switch (attr) {
    'title' => meta.title,
    'artist' => meta.artist,
    'album' => meta.album.album,
    'albumartist' => meta.album.artist,
    'tracknumber' => '${meta.tracknumber}',
    'discnumber' => '${meta.discnumber}',
    'tracktotal' => '${meta.album.tracktotal}',
    'disctotal' => '${meta.album.disctotal}',
    'composer' => meta.composer,
    'isrc' => meta.isrc,
    'lyrics' => meta.lyrics,
    'year' => meta.album.year,
    'genre' => meta.album.getGenres(),
    'copyright' => meta.album.getCopyright(),
    _ => null,
  };
}

/// Compute the `(KEY, VALUE)` Vorbis Comment pairs for FLAC (`_tag_flac`).
List<(String, String)> flacTagPairs(TrackMetadata meta) {
  final out = <(String, String)>[];
  for (final k in _kMetadataTypes) {
    var tag = attrFromMeta(meta, k);
    if (tag != null && tag.isNotEmpty) {
      if (_kZeroPadKeys.contains(k)) {
        final n = int.tryParse(tag);
        if (n != null) tag = n.toString().padLeft(2, '0');
      }
      out.add((k.toUpperCase(), tag));
    }
  }
  return out;
}

/// Tag [path] in place with [meta] and optional cover image at [coverPath].
/// Matches `tagger.tag_file`.
Future<void> tagFile(
  String path,
  TrackMetadata meta, [
  String? coverPath,
]) async {
  final container = containerForPath(path);
  if (container == null) return;
  final file = File(path);
  if (!await file.exists()) return;

  Uint8List? coverBytes;
  if (coverPath != null && coverPath.isNotEmpty) {
    final cf = File(coverPath);
    if (await cf.exists()) {
      final raw = await cf.readAsBytes();
      if (raw.isNotEmpty) coverBytes = raw;
    }
  }

  final input = await file.readAsBytes();
  final Uint8List output;
  switch (container) {
    case TagContainer.flac:
      output = tagFlacBytes(input, meta, coverBytes: coverBytes);
    case TagContainer.mp3:
      output = tagMp3Bytes(input, meta, coverBytes: coverBytes);
    case TagContainer.aac:
      output = tagMp4Bytes(input, meta, coverBytes: coverBytes);
  }
  if (identical(output, input)) return;

  final tmp = File('$path.tagtmp');
  await tmp.writeAsBytes(output, flush: true);
  await tmp.rename(path);
}

bool _isPng(Uint8List data) =>
    data.length >= 8 &&
    data[0] == 0x89 &&
    data[1] == 0x50 &&
    data[2] == 0x4E &&
    data[3] == 0x47;

String _coverMime(Uint8List data) => _isPng(data) ? 'image/png' : 'image/jpeg';

// ── FLAC ──────────────────────────────────────────────────────────────────────

/// Tag raw FLAC bytes with Vorbis comments and optional front-cover PICTURE
/// block. Returns [input] unchanged if it does not begin with `fLaC`.
Uint8List tagFlacBytes(
  Uint8List input,
  TrackMetadata meta, {
  Uint8List? coverBytes,
}) {
  if (input.length < 8 ||
      input[0] != 0x66 ||
      input[1] != 0x4C ||
      input[2] != 0x61 ||
      input[3] != 0x43) {
    return input;
  }

  // Parse existing metadata blocks.
  final keptBlocks = <(int, Uint8List)>[];
  String vendor = 'Mai An Lab';
  final existingComments = <(String, String)>[];

  var pos = 4;
  var isLast = false;
  while (!isLast && pos + 4 <= input.length) {
    final header = input[pos];
    isLast = (header & 0x80) != 0;
    final blockType = header & 0x7F;
    final length =
        (input[pos + 1] << 16) | (input[pos + 2] << 8) | input[pos + 3];
    pos += 4;
    if (pos + length > input.length) return input;
    final payload = Uint8List.sublistView(input, pos, pos + length);
    pos += length;

    if (blockType == 4) {
      // VORBIS_COMMENT: preserve vendor and unrelated existing keys.
      final parsed = _parseVorbisComment(payload);
      if (parsed != null) {
        if (parsed.vendor.isNotEmpty) vendor = parsed.vendor;
        existingComments.addAll(parsed.comments);
      }
    } else if (blockType == 1) {
      // Drop old padding; we append fresh compact padding if needed.
    } else if (blockType == 6 && coverBytes != null) {
      // Replace existing front covers when embedding a new cover.
      if (payload.length >= 4 && _readU32BE(payload, 0) == 3) continue;
      keptBlocks.add((blockType, payload));
    } else {
      keptBlocks.add((blockType, payload));
    }
  }
  final audioFrames = Uint8List.sublistView(input, pos);

  // Merge new tags over existing comments (case-insensitive key replacement).
  // Vorbis/FLAC readers (including tinytag) read DATE as `year`, while
  // tagger.py writes YEAR; emit DATE alongside YEAR so both parsers see it.
  final newPairs = [
    ...flacTagPairs(meta),
    if (meta.album.year.isNotEmpty) ('DATE', meta.album.year),
  ];
  final replacedKeys = {for (final (k, _) in newPairs) k.toUpperCase()};
  final merged = <(String, String)>[
    for (final (k, v) in existingComments)
      if (!replacedKeys.contains(k.toUpperCase())) (k, v),
    ...newPairs,
  ];

  final vorbisPayload = _buildVorbisComment(vendor, merged);
  final blocks = <(int, Uint8List)>[
    ...keptBlocks,
    (4, vorbisPayload),
    if (coverBytes != null && coverBytes.length < kFlacMaxBlockSize - 128)
      (6, _buildFlacPicture(coverBytes)),
  ];

  final out = BytesBuilder(copy: false);
  out.add(const [0x66, 0x4C, 0x61, 0x43]); // "fLaC"
  for (var i = 0; i < blocks.length; i++) {
    final (type, payload) = blocks[i];
    final lastFlag = i == blocks.length - 1 ? 0x80 : 0x00;
    final len = payload.length;
    out.add([
      lastFlag | (type & 0x7F),
      (len >> 16) & 0xFF,
      (len >> 8) & 0xFF,
      len & 0xFF,
    ]);
    out.add(payload);
  }
  out.add(audioFrames);
  return out.takeBytes();
}

({String vendor, List<(String, String)> comments})? _parseVorbisComment(
  Uint8List data,
) {
  if (data.length < 8) return null;
  var pos = 0;
  final vendorLen = _readU32LE(data, pos);
  pos += 4;
  if (pos + vendorLen + 4 > data.length) return null;
  final vendor = utf8.decode(
    Uint8List.sublistView(data, pos, pos + vendorLen),
    allowMalformed: true,
  );
  pos += vendorLen;
  final count = _readU32LE(data, pos);
  pos += 4;
  final comments = <(String, String)>[];
  for (var i = 0; i < count; i++) {
    if (pos + 4 > data.length) break;
    final len = _readU32LE(data, pos);
    pos += 4;
    if (pos + len > data.length) break;
    final s = utf8.decode(
      Uint8List.sublistView(data, pos, pos + len),
      allowMalformed: true,
    );
    pos += len;
    final eq = s.indexOf('=');
    if (eq > 0) {
      comments.add((s.substring(0, eq), s.substring(eq + 1)));
    }
  }
  return (vendor: vendor, comments: comments);
}

Uint8List _buildVorbisComment(String vendor, List<(String, String)> pairs) {
  final out = BytesBuilder(copy: false);
  final vendorBytes = utf8.encode(vendor);
  out.add(_u32LE(vendorBytes.length));
  out.add(vendorBytes);
  out.add(_u32LE(pairs.length));
  for (final (k, v) in pairs) {
    final entry = utf8.encode('$k=$v');
    out.add(_u32LE(entry.length));
    out.add(entry);
  }
  return out.takeBytes();
}

Uint8List _buildFlacPicture(Uint8List imageBytes) {
  final mimeBytes = ascii.encode(_coverMime(imageBytes));
  final out = BytesBuilder(copy: false);
  out.add(_u32BE(3)); // Front Cover
  out.add(_u32BE(mimeBytes.length));
  out.add(mimeBytes);
  out.add(_u32BE(0)); // description length
  out.add(_u32BE(0)); // width
  out.add(_u32BE(0)); // height
  out.add(_u32BE(0)); // color depth
  out.add(_u32BE(0)); // indexed colors
  out.add(_u32BE(imageBytes.length));
  out.add(imageBytes);
  return out.takeBytes();
}

// ── MP3 (ID3v2.3) ─────────────────────────────────────────────────────────────

/// Tag raw MP3 bytes with an ID3v2.3 header (`v2_version=3`), replacing any
/// existing ID3v2 header while keeping the MPEG audio payload intact.
Uint8List tagMp3Bytes(
  Uint8List input,
  TrackMetadata meta, {
  Uint8List? coverBytes,
}) {
  // Strip any existing leading ID3v2 tag.
  var audioStart = 0;
  if (input.length >= 10 &&
      input[0] == 0x49 &&
      input[1] == 0x44 &&
      input[2] == 0x33) {
    final flags = input[5];
    final size = _readSyncsafe32(input, 6);
    final footer = (flags & 0x10) != 0 ? 10 : 0;
    final end = 10 + size + footer;
    if (end <= input.length) {
      audioStart = end;
    }
  }
  final audio = Uint8List.sublistView(input, audioStart);

  // Build ID3v2.3 frames matching tagger.py MP3_KEY.
  final frames = BytesBuilder(copy: false);

  void addText(String id, String? text) {
    if (text == null || text.isEmpty) return;
    final payload = _encodeId3Utf16(text);
    _writeId3v23Frame(frames, id, payload);
  }

  addText('TIT2', meta.title);
  addText('TPE1', meta.artist);
  addText('TALB', meta.album.album);
  addText('TPE2', meta.album.artist);
  addText('TCOM', meta.composer);
  addText('TYER', meta.album.year);
  addText('TCON', meta.album.getGenres());
  if (meta.lyrics != null && meta.lyrics!.isNotEmpty) {
    _writeId3v23LangFrame(frames, 'USLT', meta.lyrics!);
  }
  addText('TCOP', meta.album.getCopyright());
  addText('TRCK', '${meta.tracknumber}/${meta.album.tracktotal}');
  addText('TPOS', '${meta.discnumber}/${meta.album.disctotal}');
  addText('TSRC', meta.isrc);

  if (coverBytes != null && coverBytes.isNotEmpty) {
    final mime = ascii.encode(_coverMime(coverBytes));
    final apic = BytesBuilder(copy: false)
      ..addByte(0x00) // ISO-8859-1 description encoding
      ..add(mime)
      ..addByte(0x00) // MIME terminator
      ..addByte(0x03) // Front Cover
      ..addByte(0x00) // Empty description terminator
      ..add(coverBytes);
    _writeId3v23Frame(frames, 'APIC', apic.takeBytes());
  }

  final frameBytes = frames.takeBytes();
  final out = BytesBuilder(copy: false);
  out.add([
    0x49, 0x44, 0x33, // "ID3"
    0x03, 0x00, // v2.3.0
    0x00, // flags
    ..._encodeSyncsafe32(frameBytes.length),
  ]);
  out.add(frameBytes);
  out.add(audio);
  return out.takeBytes();
}

Uint8List _encodeId3Utf16(String text) {
  final units = text.codeUnits;
  final out = Uint8List(3 + units.length * 2);
  out[0] = 0x01; // UTF-16 with BOM
  out[1] = 0xFF;
  out[2] = 0xFE;
  for (var i = 0; i < units.length; i++) {
    final u = units[i];
    out[3 + i * 2] = u & 0xFF;
    out[4 + i * 2] = (u >> 8) & 0xFF;
  }
  return out;
}

void _writeId3v23LangFrame(BytesBuilder out, String id, String text) {
  final units = text.codeUnits;
  final payload = BytesBuilder(copy: false)
    ..addByte(0x01) // UTF-16 with BOM
    ..add(const [0x65, 0x6E, 0x67]) // "eng"
    ..add(const [0xFF, 0xFE, 0x00, 0x00]) // empty desc + NUL
    ..add(const [0xFF, 0xFE]); // BOM for value
  for (final u in units) {
    payload.addByte(u & 0xFF);
    payload.addByte((u >> 8) & 0xFF);
  }
  _writeId3v23Frame(out, id, payload.takeBytes());
}

void _writeId3v23Frame(BytesBuilder out, String id, Uint8List payload) {
  out.add(ascii.encode(id));
  out.add(_u32BE(payload.length));
  out.add(const [0x00, 0x00]); // flags
  out.add(payload);
}

// ── MP4 / M4A ─────────────────────────────────────────────────────────────────

class _Mp4Atom {
  _Mp4Atom(this.type, this.offset, this.headerSize, this.size);

  final String type;
  final int offset;
  final int headerSize;
  final int size;

  int get dataOffset => offset + headerSize;
  int get dataLength => size - headerSize;
  int get end => offset + size;
}

List<_Mp4Atom> _parseAtoms(Uint8List data, int start, int end) {
  final atoms = <_Mp4Atom>[];
  var pos = start;
  while (pos + 8 <= end) {
    final rawSize = _readU32BE(data, pos);
    final type = latin1.decode(Uint8List.sublistView(data, pos + 4, pos + 8));
    var headerSize = 8;
    int size;
    if (rawSize == 1) {
      if (pos + 16 > end) break;
      size = _readU64BE(data, pos + 8);
      headerSize = 16;
    } else if (rawSize == 0) {
      size = end - pos;
    } else {
      size = rawSize;
    }
    if (size < headerSize || pos + size > end) break;
    atoms.add(_Mp4Atom(type, pos, headerSize, size));
    pos += size;
  }
  return atoms;
}

/// Tag raw MP4/M4A bytes with iTunes `moov/udta/meta/ilst` atoms and optional
/// `covr` artwork, adjusting `stco`/`co64` chunk offsets whenever `moov`
/// precedes `mdat`.
Uint8List tagMp4Bytes(
  Uint8List input,
  TrackMetadata meta, {
  Uint8List? coverBytes,
}) {
  final topAtoms = _parseAtoms(input, 0, input.length);
  final moovIdx = topAtoms.indexWhere((a) => a.type == 'moov');
  if (moovIdx < 0) return input;
  final moov = topAtoms[moovIdx];
  final mdatIdx = topAtoms.indexWhere((a) => a.type == 'mdat');
  final moovBeforeMdat = mdatIdx >= 0 && moovIdx < mdatIdx;

  // Build new udta -> meta -> (hdlr + ilst).
  final udtaBytes = _buildMp4Udta(meta, coverBytes: coverBytes);

  // Rebuild moov children (keeping everything except existing udta).
  final moovChildren = _parseAtoms(input, moov.dataOffset, moov.end);
  final moovBodyBuilder = BytesBuilder(copy: false);
  for (final child in moovChildren) {
    if (child.type == 'udta') continue;
    moovBodyBuilder.add(Uint8List.sublistView(input, child.offset, child.end));
  }
  moovBodyBuilder.add(udtaBytes);
  final rawMoovBody = moovBodyBuilder.takeBytes();
  final newMoovSize = 8 + rawMoovBody.length;
  final delta = newMoovSize - moov.size;

  // Shift stco/co64 offsets if moov sits before mdat.
  final adjustedMoovBody = (moovBeforeMdat && delta != 0)
      ? _adjustChunkOffsets(Uint8List.fromList(rawMoovBody), delta)
      : rawMoovBody;

  final out = BytesBuilder(copy: false);
  for (var i = 0; i < topAtoms.length; i++) {
    final a = topAtoms[i];
    if (i == moovIdx) {
      out.add(_u32BE(8 + adjustedMoovBody.length));
      out.add(latin1.encode('moov'));
      out.add(adjustedMoovBody);
    } else {
      out.add(Uint8List.sublistView(input, a.offset, a.end));
    }
  }
  return out.takeBytes();
}

const _kMp4Containers = {
  'moov',
  'trak',
  'mdia',
  'minf',
  'stbl',
  'edts',
  'dinf',
};

Uint8List _adjustChunkOffsets(Uint8List body, int delta) {
  void walk(int start, int end) {
    final children = _parseAtoms(body, start, end);
    for (final a in children) {
      if (a.type == 'stco') {
        if (a.dataLength >= 8) {
          final count = _readU32BE(body, a.dataOffset + 4);
          for (var i = 0; i < count; i++) {
            final offPos = a.dataOffset + 8 + i * 4;
            if (offPos + 4 > a.end) break;
            final oldVal = _readU32BE(body, offPos);
            _writeU32BE(body, offPos, oldVal + delta);
          }
        }
      } else if (a.type == 'co64') {
        if (a.dataLength >= 8) {
          final count = _readU32BE(body, a.dataOffset + 4);
          for (var i = 0; i < count; i++) {
            final offPos = a.dataOffset + 8 + i * 8;
            if (offPos + 8 > a.end) break;
            final oldVal = _readU64BE(body, offPos);
            _writeU64BE(body, offPos, oldVal + delta);
          }
        }
      } else if (_kMp4Containers.contains(a.type)) {
        walk(a.dataOffset, a.end);
      }
    }
  }

  walk(0, body.length);
  return body;
}

Uint8List _mp4Atom(String fourcc, List<int> body) {
  final out = BytesBuilder(copy: false);
  out.add(_u32BE(8 + body.length));
  out.add(latin1.encode(fourcc));
  out.add(body);
  return out.takeBytes();
}

Uint8List _mp4DataAtom(int dataType, List<int> payload) {
  final body = BytesBuilder(copy: false)
    ..add(_u32BE(dataType)) // version=0 + 24-bit type indicator
    ..add(const [0, 0, 0, 0]) // locale
    ..add(payload);
  return _mp4Atom('data', body.takeBytes());
}

Uint8List _buildMp4Udta(TrackMetadata meta, {Uint8List? coverBytes}) {
  final ilst = BytesBuilder(copy: false);

  void addText(String fourcc, String? val) {
    if (val == null || val.isEmpty) return;
    ilst.add(_mp4Atom(fourcc, _mp4DataAtom(1, utf8.encode(val))));
  }

  addText('\u00A9nam', meta.title);
  addText('\u00A9ART', meta.artist);
  addText('\u00A9alb', meta.album.album);
  addText('aART', meta.album.artist);
  addText('\u00A9wrt', meta.composer);
  addText('\u00A9day', meta.album.year);
  addText('\u00A9gen', meta.album.getGenres());
  addText('\u00A9lyr', meta.lyrics);
  addText('cprt', meta.album.getCopyright());

  // trkn: 8 bytes [0, 0, trackHi, trackLo, totalHi, totalLo, 0, 0]
  final tn = meta.tracknumber.clamp(0, 0xFFFF);
  final tt = meta.album.tracktotal.clamp(0, 0xFFFF);
  ilst.add(
    _mp4Atom(
      'trkn',
      _mp4DataAtom(0, [
        0,
        0,
        (tn >> 8) & 0xFF,
        tn & 0xFF,
        (tt >> 8) & 0xFF,
        tt & 0xFF,
        0,
        0,
      ]),
    ),
  );

  // disk: 6 bytes [0, 0, discHi, discLo, totalHi, totalLo]
  final dn = meta.discnumber.clamp(0, 0xFFFF);
  final dt = meta.album.disctotal.clamp(0, 0xFFFF);
  ilst.add(
    _mp4Atom(
      'disk',
      _mp4DataAtom(0, [
        0,
        0,
        (dn >> 8) & 0xFF,
        dn & 0xFF,
        (dt >> 8) & 0xFF,
        dt & 0xFF,
      ]),
    ),
  );

  // ----:com.apple.iTunes:ISRC
  if (meta.isrc != null && meta.isrc!.isNotEmpty) {
    final mean = _mp4Atom('mean', [
      0,
      0,
      0,
      0,
      ...utf8.encode('com.apple.iTunes'),
    ]);
    final name = _mp4Atom('name', [0, 0, 0, 0, ...utf8.encode('ISRC')]);
    final data = _mp4DataAtom(1, utf8.encode(meta.isrc!));
    ilst.add(_mp4Atom('----', [...mean, ...name, ...data]));
  }

  // covr: JPEG (13) or PNG (14)
  if (coverBytes != null && coverBytes.isNotEmpty) {
    final fmt = _isPng(coverBytes) ? 14 : 13;
    ilst.add(_mp4Atom('covr', _mp4DataAtom(fmt, coverBytes)));
  }

  // Standard metadata hdlr atom (`mdir` / `appl`).
  final hdlrBody = <int>[
    0, 0, 0, 0, // version + flags
    0, 0, 0, 0, // pre_defined
    ...latin1.encode('mdir'),
    ...latin1.encode('appl'),
    0, 0, 0, 0, // reserved
    0, 0, 0, 0, // reserved
    0, 0, 0, 0, // reserved
    0, // empty name NUL
  ];
  final hdlrAtom = _mp4Atom('hdlr', hdlrBody);
  final ilstAtom = _mp4Atom('ilst', ilst.takeBytes());
  final metaAtom = _mp4Atom('meta', [
    0, 0, 0, 0, // FullBox version + flags
    ...hdlrAtom,
    ...ilstAtom,
  ]);
  return _mp4Atom('udta', metaAtom);
}

// ── Binary helpers ────────────────────────────────────────────────────────────

int _readU32BE(Uint8List b, int o) =>
    ((b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]) & 0xFFFFFFFF;

int _readU32LE(Uint8List b, int o) =>
    (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) & 0xFFFFFFFF;

int _readU64BE(Uint8List b, int o) =>
    (_readU32BE(b, o) << 32) | _readU32BE(b, o + 4);

List<int> _u32BE(int v) => [
  (v >> 24) & 0xFF,
  (v >> 16) & 0xFF,
  (v >> 8) & 0xFF,
  v & 0xFF,
];

List<int> _u32LE(int v) => [
  v & 0xFF,
  (v >> 8) & 0xFF,
  (v >> 16) & 0xFF,
  (v >> 24) & 0xFF,
];

void _writeU32BE(Uint8List b, int o, int v) {
  b[o] = (v >> 24) & 0xFF;
  b[o + 1] = (v >> 16) & 0xFF;
  b[o + 2] = (v >> 8) & 0xFF;
  b[o + 3] = v & 0xFF;
}

void _writeU64BE(Uint8List b, int o, int v) {
  _writeU32BE(b, o, (v >> 32) & 0xFFFFFFFF);
  _writeU32BE(b, o + 4, v & 0xFFFFFFFF);
}

int _readSyncsafe32(Uint8List b, int o) =>
    ((b[o] & 0x7F) << 21) |
    ((b[o + 1] & 0x7F) << 14) |
    ((b[o + 2] & 0x7F) << 7) |
    (b[o + 3] & 0x7F);

List<int> _encodeSyncsafe32(int v) => [
  (v >> 21) & 0x7F,
  (v >> 14) & 0x7F,
  (v >> 7) & 0x7F,
  v & 0x7F,
];
