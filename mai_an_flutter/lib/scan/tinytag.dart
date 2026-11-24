// A Dart port of tinytag 2.3.2 (tinytag/tinytag.py), the tag reader the
// Flet app's scanner uses (library_scanner.py; the shipping APK bundles
// tinytag 2.3.2). Ported line by line so a scan writes the same titles,
// durations and bitrates as Python; test/tinytag_test.dart checks it against
// tinytag's own output (tool/export_scan_goldens.py).
//
// Source: https://github.com/tinytag/tinytag (PyPI tinytag==2.3.2)
//
// MIT License
//
// Copyright (c) 2014-2026 Tom Wallroth, Mat (mathiascode), et al.
//
// Permission is hereby granted, free of charge, to any person obtaining a
// copy of this software and associated documentation files (the "Software"),
// to deal in the Software without restriction, including without limitation
// the rights to use, copy, modify, merge, publish, distribute, sublicense,
// and/or sell copies of the Software, and to permit persons to whom the
// Software is furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
// OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
// FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
// DEALINGS IN THE SOFTWARE.
//
// What is left out: images (the scanner never asks for them, so their
// bytes are skipped exactly where tinytag skips them), and the ASF and AIFF
// parsers, which tinytag reaches only for .wma/.asf/.aif* names or by magic
// bytes; none of the scanner's extensions maps there (an .aac file is sniffed
// and lands on ID3 or MPEG). Such a file raises UnsupportedFormatError.

import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';

import '../core/py_num.dart';
import '../core/py_text.dart';

class TinyTagException implements Exception {
  TinyTagException(this.message);
  final String message;
  @override
  String toString() => '$runtimeType: $message';
}

class ParseError extends TinyTagException {
  ParseError(super.message);
}

class UnsupportedFormatError extends TinyTagException {
  UnsupportedFormatError(super.message);
}

// ── File handles: Python's BinaryIO, buffered like io.BufferedReader ───────

const _seekSet = 0, _seekCur = 1, _seekEnd = 2;

abstract class _Fh {
  /// Up to [n] bytes; fewer at the end of the data.
  Uint8List read(int n);

  /// Python's `seek`: returns the new absolute position. A negative result
  /// raises, as it does for files and BytesIO.
  int seek(int offset, [int whence = _seekSet]);
  int tell();
  int get length;
}

int _target(int pos, int len, int offset, int whence) {
  final p = switch (whence) {
    _seekCur => pos + offset,
    _seekEnd => len + offset,
    _ => offset,
  };
  if (p < 0) throw const FileSystemException('negative seek value');
  return p;
}

class _BytesFh implements _Fh {
  _BytesFh(this._b);
  final Uint8List _b;
  int _pos = 0;

  @override
  int get length => _b.length;

  @override
  Uint8List read(int n) {
    if (n < 0) n = _b.length;
    final start = math.min(_pos, _b.length);
    final end = math.min(start + n, _b.length);
    _pos = math.max(_pos, end);
    return Uint8List.sublistView(_b, start, end);
  }

  @override
  int seek(int offset, [int whence = _seekSet]) =>
      _pos = _target(_pos, _b.length, offset, whence);

  @override
  int tell() => _pos;
}

/// A file read through a 64 KiB window, so tinytag's many small reads (the
/// MPEG frame walk reads 4 bytes at a time) don't each cost a system call.
class _FileFh implements _Fh {
  _FileFh(this._f) : length = _f.lengthSync();
  final RandomAccessFile _f;
  @override
  final int length;
  int _pos = 0;
  Uint8List _buf = Uint8List(0);
  int _bufStart = 0;
  static const _window = 64 * 1024;

  @override
  Uint8List read(int n) {
    if (n < 0) n = length;
    final start = math.min(_pos, length);
    final end = math.min(start + n, length);
    _pos = math.max(_pos, end);
    final want = end - start;
    if (want == 0) return Uint8List(0);
    if (start >= _bufStart && end <= _bufStart + _buf.length) {
      return Uint8List.sublistView(_buf, start - _bufStart, end - _bufStart);
    }
    if (want >= _window) {
      _f.setPositionSync(start);
      return _f.readSync(want);
    }
    _f.setPositionSync(start);
    _buf = _f.readSync(_window);
    _bufStart = start;
    return Uint8List.sublistView(_buf, 0, math.min(want, _buf.length));
  }

  @override
  int seek(int offset, [int whence = _seekSet]) =>
      _pos = _target(_pos, length, offset, whence);

  @override
  int tell() => _pos;
}

// ── Python bytes/str helpers ────────────────────────────────────────────────

/// `b[start:end]` with Python's clamping and negative indices.
Uint8List _sl(Uint8List b, [int? start, int? end]) {
  final n = b.length;
  int fix(int i) => i < 0 ? math.max(0, n + i) : math.min(i, n);
  final s = start == null ? 0 : fix(start);
  final e = end == null ? n : fix(end);
  return e <= s ? Uint8List(0) : Uint8List.sublistView(b, s, e);
}

/// `b[i]`, negative indices from the end, IndexError out of range.
int _at(Uint8List b, int i) {
  final j = i < 0 ? b.length + i : i;
  if (j < 0 || j >= b.length) throw RangeError.index(i, b, 'index');
  return b[j];
}

bool _startsWith(Uint8List b, List<int> prefix, [int at = 0]) {
  if (at + prefix.length > b.length) return false;
  for (var i = 0; i < prefix.length; i++) {
    if (b[at + i] != prefix[i]) return false;
  }
  return true;
}

/// `b.find(sub, start)`.
int _find(Uint8List b, List<int> sub, [int start = 0]) {
  if (start < 0) start = math.max(0, b.length + start);
  for (var i = start; i + sub.length <= b.length; i++) {
    var ok = true;
    for (var j = 0; j < sub.length; j++) {
      if (b[i + j] != sub[j]) {
        ok = false;
        break;
      }
    }
    if (ok) return i;
  }
  return -1;
}

bool _eq(Uint8List b, List<int> other) =>
    b.length == other.length && _startsWith(b, other);

List<int> _ascii(String s) => s.codeUnits;

String _latin1(Uint8List b) => latin1.decode(b);
String _utf8(Uint8List b) => utf8.decode(b, allowMalformed: true);

/// `bytes.decode('utf-16-le'/'utf-16-be', 'replace')`.
String _utf16(Uint8List b, {required bool bigEndian}) {
  final out = StringBuffer();
  final units = b.length ~/ 2;
  var i = 0;
  int unit(int k) => bigEndian
      ? (b[2 * k] << 8) | b[2 * k + 1]
      : b[2 * k] | (b[2 * k + 1] << 8);
  while (i < units) {
    final u = unit(i);
    if (u >= 0xD800 && u <= 0xDBFF) {
      if (i + 1 < units) {
        final l = unit(i + 1);
        if (l >= 0xDC00 && l <= 0xDFFF) {
          out.writeCharCode(0x10000 + ((u - 0xD800) << 10) + (l - 0xDC00));
          i += 2;
          continue;
        }
        out.writeCharCode(0xFFFD);
        i += 1;
        continue;
      }
      // A high surrogate at the very end: Python reports it together with
      // a trailing odd byte as one truncated sequence.
      out.writeCharCode(0xFFFD);
      return out.toString();
    }
    if (u >= 0xDC00 && u <= 0xDFFF) {
      out.writeCharCode(0xFFFD);
    } else {
      out.writeCharCode(u);
    }
    i += 1;
  }
  if (b.length.isOdd) out.writeCharCode(0xFFFD);
  return out.toString();
}

String _rstripNul(String s) {
  var e = s.length;
  while (e > 0 && s.codeUnitAt(e - 1) == 0) {
    e--;
  }
  return s.substring(0, e);
}

Uint8List _rstripNulBytes(Uint8List b) {
  var e = b.length;
  while (e > 0 && b[e - 1] == 0) {
    e--;
  }
  return _sl(b, 0, e);
}

/// `bytes.lower()`: ASCII only.
Uint8List _bytesLower(Uint8List b) => Uint8List.fromList([
  for (final c in b) (c >= 0x41 && c <= 0x5A) ? c + 32 : c,
]);

bool _isAsciiBytes(Uint8List b) => b.every((c) => c < 0x80);

ByteData _bd(Uint8List b) => ByteData.sublistView(b);

/// `unpack_from(fmt, b, offset)` for one big-endian unsigned field.
int _u16be(Uint8List b, int o) => _bd(b).getUint16(o, Endian.big);
int _u32be(Uint8List b, int o) => _bd(b).getUint32(o, Endian.big);
int _i32be(Uint8List b, int o) => _bd(b).getInt32(o, Endian.big);
int _u64be(Uint8List b, int o) => _bd(b).getUint64(o, Endian.big);
int _u16le(Uint8List b, int o) => _bd(b).getUint16(o, Endian.little);
int _u32le(Uint8List b, int o) => _bd(b).getUint32(o, Endian.little);
int _i32le(Uint8List b, int o) => _bd(b).getInt32(o, Endian.little);
int _i64le(Uint8List b, int o) => _bd(b).getInt64(o, Endian.little);

/// `unpack(fmt, b)`: the buffer must be exactly [size] bytes.
Uint8List _exact(Uint8List b, int size) {
  if (b.length != size) {
    throw FormatException('unpack requires a buffer of $size bytes');
  }
  return b;
}

/// Python's `str(value)` for the field values tinytag stores.
String _pyStr(Object v) => switch (v) {
  bool b => b ? 'True' : 'False',
  double d => pyFloatRepr(d),
  _ => '$v',
};

/// Python `==` between field values (1 == 1.0 == True).
bool _pyEq(Object? a, Object? b) {
  num? asNum(Object? x) => x is bool ? (x ? 1 : 0) : (x is num ? x : null);
  final na = asNum(a), nb = asNum(b);
  if (na != null && nb != null) return na == nb;
  return a == b;
}

// ── TinyTag ─────────────────────────────────────────────────────────────────

/// The public attributes of a `TinyTag`, in `__init__` order.
const _publicFields = [
  'filename', 'filesize', 'mime_type', 'is_lossless', 'duration', //
  'channels', 'bitrate', 'bitdepth', 'samplerate', 'artist', 'albumartist',
  'composer', 'album', 'disc', 'disc_total', 'title', 'track', 'track_total',
  'genre', 'year', 'comment',
];

// Attribute names a field from `other.` may not take (`fieldname in
// self.__dict__`): the public ones plus `images` and `other`.
const _reserved = {..._publicFields, 'images', 'other'};

const _otherPrefix = 'other.';

typedef _Field = (String, Object);

class TinyTag {
  TinyTag._();

  /// Attribute values (Python's `__dict__`, public part).
  final Map<String, Object?> _f = {for (final k in _publicFields) k: null};

  /// `TinyTag.other`: extra fields, each a list of strings.
  final Map<String, List<String>> other = {};

  _Fh? _fh;
  String? _defaultEncoding;
  bool _parseDuration = true;
  bool _parseTags = true;
  bool _durationParsed = false;
  bool _tagsParsed = false;

  String? get filename => _f['filename'] as String?;
  int get filesize => (_f['filesize'] as int?) ?? 0;
  String? get mimeType => _f['mime_type'] as String?;
  bool? get isLossless => _f['is_lossless'] as bool?;
  double? get duration => (_f['duration'] as num?)?.toDouble();
  int? get channels => _f['channels'] as int?;
  double? get bitrate => (_f['bitrate'] as num?)?.toDouble();
  int? get bitdepth => _f['bitdepth'] as int?;
  int? get samplerate => _f['samplerate'] as int?;
  String? get artist => _f['artist'] as String?;
  String? get albumartist => _f['albumartist'] as String?;
  String? get composer => _f['composer'] as String?;
  String? get album => _f['album'] as String?;
  int? get disc => _f['disc'] as int?;
  int? get discTotal => _f['disc_total'] as int?;
  String? get title => _f['title'] as String?;
  int? get track => _f['track'] as int?;
  int? get trackTotal => _f['track_total'] as int?;
  String? get genre => _f['genre'] as String?;
  String? get year => _f['year'] as String?;
  String? get comment => _f['comment'] as String?;

  /// Every set attribute (raw values) and `other`, for the golden tests.
  Map<String, Object?> get fields => {
    for (final e in _f.entries)
      if (e.value != null) e.key: e.value,
  };

  static const supportedFileExtensions = [
    '.mp1', '.mp2', '.mp3', //
    '.oga', '.ogv', '.ogg', '.opus', '.spx',
    '.wav', '.flac',
    '.wma', '.wmv', '.asf',
    '.m4b', '.m4a', '.m4r', '.m4v', '.mp4', '.aax', '.aaxc',
    '.aiff', '.aifc', '.aif', '.afc',
  ];

  /// `TinyTag.get(filename)`: tags and duration, no images, magic bytes
  /// checked. Throws [UnsupportedFormatError] or [ParseError].
  static TinyTag get(String filename, {String? encoding}) {
    final raf = File(filename).openSync();
    try {
      return _getFrom(_FileFh(raf), filename, encoding: encoding);
    } finally {
      raf.closeSync();
    }
  }

  /// `TinyTag.get(file_obj=BytesIO(data))`, for tests.
  static TinyTag fromBytes(
    Uint8List data, {
    String? filename,
    String? encoding,
  }) => _getFrom(_BytesFh(data), filename, encoding: encoding);

  static TinyTag _getFrom(_Fh fh, String? filename, {String? encoding}) {
    final make = _parserClass(filename, fh);
    fh.seek(0, _seekEnd);
    final filesize = fh.tell();
    fh.seek(0);
    final tag = make();
    tag._fh = fh;
    tag._defaultEncoding = encoding;
    tag._f['filename'] = filename;
    tag._f['filesize'] = filesize;
    try {
      tag._load(tags: true, duration: true);
    } on TinyTagException {
      rethrow;
    } catch (e) {
      throw ParseError('$e');
    }
    return tag;
  }

  static TinyTag Function()? _parserForFilename(String filename) {
    final f = filename.toLowerCase();
    bool any(List<String> exts) => exts.any(f.endsWith);
    if (any(['.mp1', '.mp2', '.mp3', '.flac'])) return _ID3.new;
    if (any(['.oga', '.ogv', '.ogg', '.opus', '.spx'])) return _Ogg.new;
    if (any(['.wav'])) return _Wave.new;
    if (any(['.wma', '.wmv', '.asf'])) return _unsupported;
    if (any(['.m4b', '.m4a', '.m4r', '.m4v', '.mp4', '.aax', '.aaxc'])) {
      return _MP4.new;
    }
    if (any(['.aiff', '.aifc', '.aif', '.afc'])) return _unsupported;
    return null;
  }

  static TinyTag _unsupported() =>
      throw UnsupportedFormatError('ASF and AIFF are not ported');

  static TinyTag Function()? _parserForFileHandle(_Fh fh) {
    final header = fh.read(30);
    if (_startsWith(header, _ascii('ID3'))) return _ID3.new;
    if (header.length >= 2 && header[0] == 0xFF && (header[1] & 0xE0) == 0xE0) {
      Uint8List? footer;
      try {
        fh.seek(-_ID3._id3v1TagSize, _seekEnd);
        footer = fh.read(3);
      } on FileSystemException {
        // File smaller than ID3v1 tag size
      }
      if (footer != null && _eq(footer, _ascii('TAG'))) return _ID3.new;
      return _MPEG.new;
    }
    if (_startsWith(header, _ascii('fLaC'))) return _Flac.new;
    if (_startsWith(header, _ascii('ftyp'), 4)) return _MP4.new;
    if (_startsWith(header, _ascii('OggS'))) return _Ogg.new;
    if (_startsWith(header, _ascii('RIFF')) &&
        _startsWith(header, _ascii('WAVE'), 8)) {
      return _Wave.new;
    }
    if (_startsWith(header, const [
          0x30, 0x26, 0xB2, 0x75, 0x8E, 0x66, 0xCF, 0x11, //
          0xA6, 0xD9, 0x00, 0xAA, 0x00, 0x62, 0xCE, 0x6C,
        ]) &&
        _startsWith(header, const [0x02], 29)) {
      return _unsupported;
    }
    if (_startsWith(header, _ascii('FORM')) &&
        (_startsWith(header, _ascii('AIFF'), 8) ||
            _startsWith(header, _ascii('AIFC'), 8))) {
      return _unsupported;
    }
    return null;
  }

  static TinyTag Function() _parserClass(String? filename, _Fh fh) {
    if (filename != null && filename.isNotEmpty) {
      final p = _parserForFilename(filename);
      if (p != null) return p;
    }
    final p = _parserForFileHandle(fh);
    if (p != null) return p;
    throw UnsupportedFormatError('No tag reader found to support file type');
  }

  void _load({required bool tags, required bool duration}) {
    _parseTags = tags;
    _parseDuration = duration;
    if (_fh == null) throw StateError('File handle is required');
    if (filesize == 0) return;
    if (tags || duration) _parse(_fh!);
  }

  void _setField(String fieldname, Object value) {
    if (value is String && value.isEmpty) return;
    final old = _f[fieldname];
    if (_pyEq(value, old)) return;
    if (old != null) {
      _setOtherField(fieldname, _pyStr(value));
      return;
    }
    if (fieldname.startsWith(_otherPrefix)) {
      fieldname = fieldname.substring(_otherPrefix.length);
      if (_reserved.contains(fieldname)) fieldname = '_$fieldname';
      _setOtherField(fieldname, _pyStr(value));
      return;
    }
    _f[fieldname] = value;
  }

  void _setOtherField(String fieldname, String value) {
    if (value.isEmpty) return;
    final list = other.putIfAbsent(fieldname, () => []);
    if (list.contains(value)) return;
    list.add(value);
  }

  void _parse(_Fh fh) => throw UnimplementedError();

  void _update(TinyTag o) {
    const ignored = {'filename', 'filesize', 'mime_type'};
    for (final e in o._f.entries) {
      if (ignored.contains(e.key)) continue;
      if (e.value != null) _setField(e.key, e.value!);
    }
    // `images` (skipped) and `other` come after the public attributes in
    // `__dict__`.
    for (final e in o.other.entries) {
      for (final v in e.value) {
        _setOtherField(e.key, v);
      }
    }
  }

  TinyTag _child(TinyTag Function() make, _Fh fh) {
    final t = make();
    t._f['filename'] = filename;
    t._f['filesize'] = filesize;
    t._fh = fh;
    return t;
  }
}

// ── MP4 ─────────────────────────────────────────────────────────────────────

typedef _AtomParser = Iterable<_Field> Function(Uint8List data);

/// Atom names as latin-1 strings (b'\xA9ART' is '©ART').
String _atom(Uint8List b) => _latin1(b);

class _MP4 extends TinyTag {
  _MP4() : super._();

  static const _customFieldNameMapping = {
    'artists': 'artist',
    'conductor': 'other.conductor',
    'discsubtitle': 'other.set_subtitle',
    'initialkey': 'other.initial_key',
    'isrc': 'other.isrc',
    'language': 'other.language',
    'lyricist': 'other.lyricist',
    'media': 'other.media',
    'website': 'other.url',
    'license': 'other.license',
    'barcode': 'other.barcode',
    'catalognumber': 'other.catalog_number',
  };
  static const _losslessCodecs = {
    'mp4a.40.35',
    'mp4a.40.36',
    'mp4a.40.37',
    'mp4a.40.38',
  };
  static const _decoderSpecificInfoTag = 0x05;
  static const _versionedAtoms = {'meta', 'stsd'};
  static const _flaggedAtoms = {'stsd'};
  static const _ilstPath = ['ftyp', 'moov', 'udta', 'meta', 'ilst'];
  static const _covrPath = ['ftyp', 'moov', 'udta', 'meta', 'ilst', 'covr'];

  static Map<String, Object>? _combined;

  static Map<String, Object> _combinedTree() => _combined ??= {
    'moov': {
      'mvhd': _parseMvhd,
      'trak': {
        'mdia': {
          'minf': {
            'stbl': {
              'stsd': {'mp4a': _parseMp4a, 'alac': _parseAlac},
            },
          },
        },
      },
      'udta': {
        'meta': {
          'ilst': {
            '©ART': {'data': _dataParser('artist')},
            '©alb': {'data': _dataParser('album')},
            '©cmt': {'data': _dataParser('comment')},
            '©com': {'data': _dataParser('composer')},
            '©con': {'data': _dataParser('other.conductor')},
            '©day': {'data': _dataParser('year')},
            '©des': {'data': _dataParser('other.description')},
            '©dir': {'data': _dataParser('other.director')},
            '©gen': {'data': _dataParser('genre')},
            '©grp': {'data': _dataParser('other.grouping')},
            '©lyr': {'data': _dataParser('other.lyrics')},
            '©mvc': {'data': _dataParser('other.movement_total')},
            '©mvi': {'data': _dataParser('other.movement')},
            '©mvn': {'data': _dataParser('other.movement_name')},
            '©nam': {'data': _dataParser('title')},
            '©pub': {'data': _dataParser('other.publisher')},
            '©too': {'data': _dataParser('other.encoded_by')},
            '©wrk': {'data': _dataParser('other.work')},
            '©wrt': {'data': _dataParser('composer')},
            'aART': {'data': _dataParser('albumartist')},
            'cprt': {'data': _dataParser('other.copyright')},
            'desc': {'data': _dataParser('other.description')},
            'disk': {'data': _numsParser('disc', 'disc_total')},
            'gnre': {'data': _parseId3v1Genre},
            'shwm': {'data': _dataParser('other.show_movement')},
            'trkn': {'data': _numsParser('track', 'track_total')},
            'tmpo': {'data': _dataParser('other.bpm')},
            'covr': {'data': _parseCoverImage},
            '----': _parseCustomField,
          },
        },
      },
    },
    'uuid': _parseUuid,
  };

  @override
  void _parse(_Fh fh) {
    // Both tags and duration, always (TinyTag.get's defaults).
    _traverseAtoms(fh, _combinedTree());
    _durationParsed = _parseDuration;
    _tagsParsed = _parseTags;
  }

  /// (atom type, header size, atom size or null).
  static (Uint8List?, int, int?) _readAtomHeader(_Fh fh) {
    Uint8List? atomType;
    int? atomSize;
    final atomHeader = fh.read(8);
    var headerSize = atomHeader.length;
    if (headerSize < 8) return (atomType, headerSize, atomSize);
    atomSize = _u32be(atomHeader, 0);
    atomType = Uint8List.fromList(_sl(atomHeader, 4));
    if (atomSize == 1) {
      final ext = fh.read(8);
      headerSize += ext.length;
      if (headerSize == 16) atomSize = _u64be(ext, 0);
    }
    if (atomSize < headerSize) {
      atomSize = null;
    } else {
      atomSize -= headerSize;
    }
    return (atomType, headerSize, atomSize);
  }

  static bool _pathEq(List<String>? a, List<String> b) {
    if (a == null || a.length != b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (a[i] != b[i]) return false;
    }
    return true;
  }

  int _traverseAtoms(
    _Fh fh,
    Map<String, Object> path, {
    int? currPos,
    int? stopPos,
    List<String>? currPath,
  }) {
    currPos ??= fh.tell();
    var (atomTypeB, headerSize, atomSize) = _readAtomHeader(fh);
    if (currPath == null) {
      if (atomTypeB == null || !_isAsciiBytes(atomTypeB)) {
        throw ParseError('Invalid MP4 header');
      }
      _f['mime_type'] = 'audio/mp4';
    }
    while (atomTypeB != null && atomSize != null) {
      final atomType = _atom(atomTypeB);
      var size = atomSize;
      currPos = currPos! + headerSize;
      currPath ??= [atomType];
      if (size >= 4) {
        if (_versionedAtoms.contains(atomType)) {
          currPos = fh.seek(4, _seekCur);
          size -= 4;
        }
        if (_flaggedAtoms.contains(atomType)) {
          currPos = fh.seek(4, _seekCur);
          size -= 4;
        }
      }
      final subPath = path[atomType];
      if (subPath is _AtomParser) {
        if (_pathEq(currPath, _covrPath)) {
          // Images are never loaded.
          currPos = fh.seek(size, _seekCur);
        } else {
          final data = fh.read(size);
          currPos = currPos + data.length;
          for (final (fieldname, value) in subPath(data)) {
            if (value is _Image) {
              // images are not kept
            } else if (value is List<String>) {
              for (final v in value) {
                _setField(fieldname, v);
              }
            } else if (fieldname == 'codec') {
              _f['mime_type'] = 'audio/mp4; codecs="$value"';
            } else {
              _setField(fieldname, value);
            }
          }
        }
      } else if (subPath is Map<String, Object>) {
        currPos = _traverseAtoms(
          fh,
          subPath,
          currPos: currPos,
          stopPos: currPos + size,
          currPath: [...currPath, atomType],
        );
      } else if (_pathEq(currPath, _ilstPath)) {
        final fieldName = _otherPrefix + pyLower(atomType);
        fh.seek(-headerSize, _seekCur);
        currPos = currPos - headerSize;
        currPos = _traverseAtoms(
          fh,
          {
            atomType: {'data': _dataParser(fieldName)},
          },
          currPos: currPos,
          stopPos: currPos + size + headerSize,
          currPath: [...currPath, atomType],
        );
      } else {
        currPos = fh.seek(size, _seekCur);
      }
      if (stopPos != null && stopPos != 0 && currPos >= stopPos) {
        return currPos;
      }
      (atomTypeB, headerSize, atomSize) = _readAtomHeader(fh);
    }
    return currPos!;
  }

  static _AtomParser _dataParser(String fieldname) => (Uint8List data) sync* {
    final dataType = _u32be(data, 0);
    data = _sl(data, 8);
    var value = '';
    if (dataType == 1) {
      value = _utf8(data);
    } else if (dataType == 21) {
      final bd = _bd(data);
      switch (data.length) {
        case 1:
          value = '${bd.getInt8(0)}';
        case 2:
          value = '${bd.getInt16(0, Endian.big)}';
        case 4:
          value = '${bd.getInt32(0, Endian.big)}';
        case 8:
          value = '${bd.getInt64(0, Endian.big)}';
      }
    }
    yield (fieldname, value);
  };

  static _AtomParser _numsParser(String f1, String f2) =>
      (Uint8List data) sync* {
        final n = _exact(_sl(data, 8, 14), 6);
        yield (f1, _u16be(n, 2));
        yield (f2, _u16be(n, 4));
      };

  static Iterable<_Field> _parseId3v1Genre(Uint8List data) sync* {
    final idx = _u16be(data, 8) - 1;
    const g = _ID3._id3v1Genres;
    if (idx < g.length) yield ('genre', idx < 0 ? g[g.length + idx] : g[idx]);
  }

  static Iterable<_Field> _parseCoverImage(Uint8List data) sync* {
    _u32be(data, 0); // raises on a short atom, as unpack_from does
    yield ('front_cover', const _Image());
  }

  static Iterable<_Field> _parseCustomField(Uint8List data) sync* {
    final fh = _BytesFh(data);
    String? fieldName;
    final values = <String>[];
    var (atomType, _, atomSize) = _readAtomHeader(fh);
    while (atomType != null && atomSize != null) {
      if (_eq(atomType, _ascii('name'))) {
        final v = _bytesLower(_sl(fh.read(atomSize), 4));
        final name = _utf8(v);
        fieldName = _customFieldNameMapping[name] ?? _otherPrefix + name;
      } else if (_eq(atomType, _ascii('data')) && fieldName != null) {
        final dataAtom = fh.read(atomSize);
        for (final (_, value) in _dataParser(fieldName)(dataAtom)) {
          values.add(value as String);
          break;
        }
      } else {
        fh.seek(atomSize, _seekCur);
      }
      (atomType, _, atomSize) = _readAtomHeader(fh);
    }
    if (fieldName != null && values.isNotEmpty) yield (fieldName, values);
  }

  static const _xmpUuid = [
    0xBE, 0x7A, 0xCF, 0xCB, 0x97, 0xA9, 0x42, 0xE8, //
    0x9C, 0x71, 0x99, 0x94, 0x91, 0xE3, 0xAF, 0xAC,
  ];

  static Iterable<_Field> _parseUuid(Uint8List data) sync* {
    if (_eq(_sl(data, 0, 16), _xmpUuid)) {
      yield ('other.xmp', _utf8(_sl(data, 16)));
    }
  }

  static Iterable<_Field> _parseMp4a(Uint8List data) sync* {
    final channels = _u16be(data, 16);
    final samplerate = _u32be(data, 22);
    if (channels > 0) yield ('channels', channels);
    if (samplerate > 0) yield ('samplerate', samplerate);

    (int, int) readDescriptorSize(Uint8List d, int offset) {
      var size = 0;
      for (var i = 0; i < 4; i++) {
        final byte = _at(d, offset);
        offset += 1;
        size = (size << 7) | (byte & 0x7F);
        if (byte & 0x80 == 0) break;
      }
      return (size, offset);
    }

    var offset = 36 + 5;
    (_, offset) = readDescriptorSize(data, offset);
    offset += 4;
    (_, offset) = readDescriptorSize(data, offset);
    final objectType = _at(data, offset);
    var codec = 'mp4a.${objectType.toRadixString(16).toUpperCase()}';
    offset += 9;
    final avgBr = _u32be(data, offset);
    if (avgBr > 0) yield ('bitrate', avgBr / 1000);
    offset += 4;
    if (data.length > offset) {
      final descriptorTag = data[offset];
      offset += 1;
      if (descriptorTag == _decoderSpecificInfoTag) {
        (_, offset) = readDescriptorSize(data, offset);
        final first = _at(data, offset);
        var audioObjectType = first >> 3;
        if (audioObjectType == 31) {
          final extended = ((first & 0x07) << 3) | (_at(data, offset + 1) >> 5);
          audioObjectType = 32 + extended;
        }
        codec += '.$audioObjectType';
      }
    }
    if (objectType != 0) {
      yield ('codec', codec);
      yield ('is_lossless', _losslessCodecs.contains(codec));
    }
  }

  static Iterable<_Field> _parseAlac(Uint8List data) sync* {
    yield ('codec', 'alac');
    yield ('is_lossless', true);
    final bitdepth = _at(data, 45);
    final channels = _at(data, 49);
    final avgBr = _u32be(data, 56);
    final samplerate = _u32be(data, 60);
    if (bitdepth > 0) yield ('bitdepth', bitdepth);
    if (channels > 0) yield ('channels', channels);
    if (avgBr > 0) yield ('bitrate', avgBr / 1000);
    if (samplerate != 0) yield ('samplerate', samplerate);
  }

  static Iterable<_Field> _parseMvhd(Uint8List data) sync* {
    final version = _at(data, 0);
    if (version == 0) {
      final timeScale = _u32be(data, 12);
      final duration = _u32be(data, 16);
      if (timeScale > 0) yield ('duration', duration / timeScale);
    } else if (version == 1) {
      final timeScale = _u32be(data, 20);
      final duration = _u64be(data, 24);
      if (timeScale > 0) yield ('duration', duration / timeScale);
    }
  }
}

/// Stands in for tinytag's `Image` where a parser yields one; never kept.
class _Image {
  const _Image();
}

// ── ID3 ─────────────────────────────────────────────────────────────────────

class _ID3 extends TinyTag {
  _ID3() : super._();

  static const _id3Mapping = {
    'COMM': 'comment', 'COM': 'comment', //
    'TRCK': 'track', 'TRK': 'track',
    'TYER': 'year', 'TYE': 'year', 'TDRC': 'year',
    'TALB': 'album', 'TAL': 'album',
    'TPE1': 'artist', 'TP1': 'artist',
    'TIT2': 'title', 'TT2': 'title',
    'TCON': 'genre', 'TCO': 'genre',
    'TPOS': 'disc', 'TPA': 'disc',
    'TPE2': 'albumartist', 'TP2': 'albumartist',
    'TCOM': 'composer', 'TCM': 'composer',
    'WOAR': 'other.url', 'WAR': 'other.url',
    'TSRC': 'other.isrc', 'TRC': 'other.isrc',
    'TCOP': 'other.copyright', 'TCR': 'other.copyright',
    'TBPM': 'other.bpm', 'TBP': 'other.bpm',
    'TKEY': 'other.initial_key', 'TKE': 'other.initial_key',
    'TLAN': 'other.language', 'TLA': 'other.language',
    'TPUB': 'other.publisher', 'TPB': 'other.publisher',
    'USLT': 'other.lyrics', 'ULT': 'other.lyrics',
    'TPE3': 'other.conductor', 'TP3': 'other.conductor',
    'TEXT': 'other.lyricist', 'TXT': 'other.lyricist',
    'TSST': 'other.set_subtitle',
    'TENC': 'other.encoded_by', 'TEN': 'other.encoded_by',
    'TSSE': 'other.encoder_settings', 'TSS': 'other.encoder_settings',
    'TMED': 'other.media', 'TMT': 'other.media',
    'WCOP': 'other.license',
    'MVNM': 'other.movement_name',
    'MVIN': 'other.movement',
    'GRP1': 'modern_grouping', 'GP1': 'modern_grouping',
    'TIT1': 'legacy_grouping', 'TT1': 'legacy_grouping',
  };
  static const _id3MappingCustom = {
    'artists': 'artist',
    'director': 'other.director',
    'license': 'other.license',
    'barcode': 'other.barcode',
    'catalognumber': 'other.catalog_number',
    'showmovement': 'other.show_movement',
  };
  static const _emptyFrameIds = {'\x00\x00\x00\x00', '\x00\x00\x00'};
  static const _customFrameIds = {'TXXX', 'TXX'};
  static const _syncedLyricsFrameIds = {'SYLT', 'SLT'};
  static const _ignoredFrameIds = {
    'AENC', 'CRA', 'APIC', 'PIC', 'ASPI', 'ATXT', 'CHAP', 'COMR', 'CRM', //
    'CTOC', 'ENCR', 'EQU2', 'EQU', 'ETCO', 'ETC', 'GEOB', 'GEO', 'GRID',
    'LINK', 'LNK', 'MCDI', 'MCI', 'MLLT', 'MLL', 'OWNE', 'PCNT', 'CNT',
    'POPM', 'POP', 'POSS', 'RBUF', 'BUF', 'RGAD', 'RVA2', 'RVA', 'RVRB',
    'REV', 'SEEK', 'SIGN', 'SYTC', 'STC', 'USER', 'WXXX', 'WXX',
  };
  static const _validStringEncodings = {0x00, 0x01, 0x02, 0x03};
  static const _id3v1TagSize = 128;
  static const _id3v2HeaderSize = 10;
  static const _id3v1Genres = [
    'Blues', 'Classic Rock', 'Country', 'Dance', 'Disco', //
    'Funk', 'Grunge', 'Hip-Hop', 'Jazz', 'Metal', 'New Age', 'Oldies',
    'Other', 'Pop', 'R&B', 'Rap', 'Reggae', 'Rock', 'Techno', 'Industrial',
    'Alternative', 'Ska', 'Death Metal', 'Pranks', 'Soundtrack',
    'Euro-Techno', 'Ambient', 'Trip-Hop', 'Vocal', 'Jazz+Funk', 'Fusion',
    'Trance', 'Classical', 'Instrumental', 'Acid', 'House', 'Game',
    'Sound Clip', 'Gospel', 'Noise', 'AlternRock', 'Bass', 'Soul', 'Punk',
    'Space', 'Meditative', 'Instrumental Pop', 'Instrumental Rock',
    'Ethnic', 'Gothic', 'Darkwave', 'Techno-Industrial', 'Electronic',
    'Pop-Folk', 'Eurodance', 'Dream', 'Southern Rock', 'Comedy', 'Cult',
    'Gangsta', 'Top 40', 'Christian Rap', 'Pop/Funk', 'Jungle',
    'Native American', 'Cabaret', 'New Wave', 'Psychadelic', 'Rave',
    'Showtunes', 'Trailer', 'Lo-Fi', 'Tribal', 'Acid Punk', 'Acid Jazz',
    'Polka', 'Retro', 'Musical', 'Rock & Roll', 'Hard Rock',
    // Wimamp Extended Genres
    'Folk', 'Folk-Rock', 'National Folk', 'Swing', 'Fast Fusion', 'Bebob',
    'Latin', 'Revival', 'Celtic', 'Bluegrass', 'Avantgarde', 'Gothic Rock',
    'Progressive Rock', 'Psychedelic Rock', 'Symphonic Rock', 'Slow Rock',
    'Big Band', 'Chorus', 'Easy listening', 'Acoustic', 'Humour', 'Speech',
    'Chanson', 'Opera', 'Chamber Music', 'Sonata', 'Symphony',
    'Booty Bass', 'Primus', 'Porn Groove', 'Satire', 'Slow Jam', 'Club',
    'Tango', 'Samba', 'Folklore', 'Ballad', 'Power Ballad',
    'Rhythmic Soul', 'Freestyle', 'Duet', 'Punk Rock', 'Drum Solo',
    'A capella', 'Euro-House', 'Dance Hall', 'Goa', 'Drum & Bass',
    'Club-House', 'Hardcore Techno', 'Terror', 'Indie', 'BritPop',
    'Afro-Punk', 'Polsk Punk', 'Beat', 'Christian Gangsta Rap',
    'Heavy Metal', 'Black Metal', 'Contemporary Christian',
    'Christian Rock',
    // WinAmp 1.91
    'Merengue', 'Salsa', 'Thrash Metal', 'Anime', 'Jpop', 'Synthpop',
    // WinAmp 5.6
    'Abstract', 'Art Rock', 'Baroque', 'Bhangra', 'Big Beat', 'Breakbeat',
    'Chillout', 'Downtempo', 'Dub', 'EBM', 'Eclectic', 'Electro',
    'Electroclash', 'Emo', 'Experimental', 'Garage', 'Illbient',
    'Industro-Goth', 'Jam Band', 'Krautrock', 'Leftfield', 'Lounge',
    'Math Rock', 'New Romantic', 'Nu-Breakz', 'Post-Punk', 'Post-Rock',
    'Psytrance', 'Shoegaze', 'Space Rock', 'Trop Rock', 'World Music',
    'Neoclassical', 'Audiobook', 'Audio Theatre', 'Neue Deutsche Welle',
    'Podcast', 'Indie Rock', 'G-Funk', 'Dubstep', 'Garage Rock',
    'Psybient',
  ];

  bool _onlyId3 = false;
  final _modernGroupingValues = <String>[];
  final _legacyGroupingValues = <String>[];

  @override
  void _parse(_Fh fh) {
    var audioOffset = fh.tell();
    var tagSize = 0;
    if (_parseTags) {
      tagSize = _parseId3v2(fh);
    } else if (_parseDuration) {
      (tagSize, _, _) = _parseId3v2Header(fh);
    }
    if (tagSize > 0) audioOffset += _id3v2HeaderSize + tagSize;
    fh.seek(audioOffset);
    if (_onlyId3) return;
    final header = fh.read(4);
    if ((filename != null && filename!.endsWith('.flac')) ||
        _eq(header, _ascii('fLaC'))) {
      _f['mime_type'] = 'audio/flac';
      fh.seek(audioOffset);
      final flac = _child(_Flac.new, fh)
        .._load(tags: _parseTags, duration: _parseDuration);
      _update(flac);
    } else {
      _f['mime_type'] = 'audio/mpeg';
      var endPadding = 0;
      if (filesize >= _id3v1TagSize) {
        fh.seek(-_id3v1TagSize, _seekEnd);
        if (_parseId3v1(fh)) endPadding = _id3v1TagSize;
      }
      fh.seek(audioOffset);
      final mpeg = _child(_MPEG.new, fh) as _MPEG;
      mpeg._endPadding = endPadding;
      mpeg._load(tags: false, duration: _parseDuration);
      _update(mpeg);
    }
    _tagsParsed = _parseTags;
    _durationParsed = _parseDuration;
  }

  (int, bool, int) _parseId3v2Header(_Fh fh) {
    var size = 0, major = 0;
    var extended = false;
    final header = fh.read(_id3v2HeaderSize);
    if (header.length == _id3v2HeaderSize &&
        _startsWith(header, _ascii('ID3'))) {
      major = header[3];
      extended = (header[5] & 0x40) > 0;
      size += _unsynchsafe(header, 6);
    }
    return (size, extended, major);
  }

  int _parseId3v2(_Fh fh) {
    final (size, extended, major) = _parseId3v2Header(fh);
    if (size <= 0) return size;
    var parsedSize = _id3v2HeaderSize;
    if (extended) {
      final extdSize = _unsynchsafe(fh.read(6));
      parsedSize += extdSize;
      fh.seek(extdSize - 6, _seekCur);
    }
    while (parsedSize < size) {
      final frameSize = _parseFrame(fh, size, id3version: major);
      if (frameSize == -1) break;
      parsedSize += frameSize;
    }
    _setGroupingWorkFields();
    return size;
  }

  bool _parseId3v1(_Fh fh) {
    final content = fh.read(_parseTags ? 128 : 3);
    if (!_startsWith(content, _ascii('TAG'))) return false;
    if (!_parseTags) return true;

    String asciidecode(Uint8List x) =>
        _rstripNul(_decodeNamed(x, _defaultEncoding ?? 'latin1'));
    bool empty(Object? v) => v == null || v == '' || v == 0 || v == false;

    if (empty(title)) _setField('title', asciidecode(_sl(content, 3, 33)));
    if (empty(artist)) _setField('artist', asciidecode(_sl(content, 33, 63)));
    if (empty(album)) _setField('album', asciidecode(_sl(content, 63, 93)));
    if (empty(year)) _setField('year', asciidecode(_sl(content, 93, 97)));
    var comment = _sl(content, 97, 127);
    if (_at(comment, -2) == 0 && _at(comment, -1) != 0) {
      if (track == null) _setField('track', _at(comment, -1));
      comment = _sl(comment, 0, -2);
    }
    if (empty(this.comment)) _setField('comment', asciidecode(comment));
    if (empty(genre)) {
      final genreId = _at(content, 127);
      if (genreId < _id3v1Genres.length) {
        _setField('genre', _id3v1Genres[genreId]);
      }
    }
    return true;
  }

  void _setCustomField(String customFieldName, String value) {
    final lower = pyLower(customFieldName);
    _setField(_id3MappingCustom[lower] ?? _otherPrefix + lower, value);
  }

  void _setGroupingWorkFields() {
    if (_modernGroupingValues.isNotEmpty) {
      for (final v in _modernGroupingValues) {
        _setOtherField('grouping', v);
      }
      for (final v in _legacyGroupingValues) {
        _setOtherField('work', v);
      }
      return;
    }
    for (final v in _legacyGroupingValues) {
      _setOtherField('grouping', v);
    }
  }

  static String _lrcTimestamp(double seconds) {
    var cs = (seconds * 100).truncate();
    final minutes = cs ~/ 6000;
    cs = cs % 6000;
    final secs = cs ~/ 100;
    cs = cs % 100;
    String two(int v) => v.toString().padLeft(2, '0');
    return '${two(minutes)}:${two(secs)}.${two(cs)}';
  }

  String _parseSyncedLyrics(Uint8List content) {
    var lyrics = '';
    final contentLength = content.length;
    final encoding = _at(content, 0);
    final timestampFormat = _at(content, 4);
    var endPos = _findStringEndPos(content, encoding, 6);
    var offset = endPos;
    var foundLine = false;
    while (offset < contentLength) {
      endPos = _findStringEndPos(content, encoding, offset);
      var value = _decodeString(_sl(content, offset, endPos), encoding);
      value = value.replaceFirst(RegExp(r'^\n+'), '');
      offset = endPos;
      if (offset + 4 > contentLength) break;
      final time = _u32be(content, offset);
      offset += 4;
      if (foundLine) lyrics += '\n';
      foundLine = true;
      if (timestampFormat == 0x02) {
        lyrics += '[${_lrcTimestamp(time / 1000)}]$value';
      } else {
        lyrics += value;
      }
    }
    return lyrics;
  }

  int _parseFrame(_Fh fh, int totalSize, {int? id3version}) {
    final headerLen = id3version == 2 ? 6 : 10;
    var parsedSize = headerLen;
    final frameSizeBytes = id3version == 2 ? 3 : 4;
    final isSynchsafeInt = id3version == 4;
    final header = fh.read(headerLen);
    if (header.length != headerLen) return -1;
    final frameIdB = _sl(header, 0, frameSizeBytes);
    final frameId = _latin1(frameIdB);
    if (_emptyFrameIds.contains(frameId)) return -1;
    final int frameSize;
    if (frameSizeBytes == 3) {
      frameSize = (header[3] << 16) | (header[4] << 8) | header[5];
    } else if (isSynchsafeInt) {
      frameSize = _unsynchsafe(header, 4);
    } else {
      frameSize = _u32be(header, 4);
    }
    parsedSize += frameSize;
    if (frameSize == 0) return parsedSize;
    if (frameSize > totalSize) return -1;
    final fieldname = _id3Mapping[frameId];
    if (_parseTags && fieldname != null) {
      var content = fh.read(frameSize);
      if (fieldname == 'comment' || fieldname == 'other.lyrics') {
        final encoding = _at(content, 0);
        content = _sl(content, 4);
        final endPos = _findStringEndPos(content, encoding);
        var value = _decodeString(_sl(content, 0, endPos), encoding);
        if (endPos < content.length) {
          final contentDescriptor = value;
          value = _decodeString(_sl(content, endPos), encoding);
          if (fieldname == 'comment' &&
              contentDescriptor.isNotEmpty &&
              value.isNotEmpty) {
            _setCustomField(contentDescriptor, value);
            return parsedSize;
          }
        }
        _setField(fieldname, value);
        return parsedSize;
      }
      if (frameId.startsWith('W')) {
        _setField(fieldname, _decodeString(content));
        return parsedSize;
      }
      final encoding = _at(content, 0);
      content = _sl(content, 1);
      final contentLength = content.length;
      var offset = 0;
      while (offset < contentLength) {
        var endPos = _findStringEndPos(content, encoding, offset);
        if (endPos <= offset) endPos = contentLength;
        var value = _decodeString(_sl(content, offset, endPos), encoding);
        offset = endPos;
        if (fieldname == 'track' ||
            fieldname == 'disc' ||
            fieldname == 'other.movement') {
          if (value.contains('/')) {
            final parts = value.split('/');
            value = parts[0];
            final total = parts[1];
            if (pyIsDecimal(total)) {
              _setField('${fieldname}_total', pyDecimalInt(total));
            }
          }
          if (pyIsDecimal(value)) _setField(fieldname, pyDecimalInt(value));
        } else if (fieldname == 'genre') {
          var genreId = 255;
          if (pyIsDecimal(value)) {
            genreId = pyDecimalInt(value);
          } else if (value.startsWith('(')) {
            final end = value.indexOf(')');
            // value[1:end_pos] with end_pos == -1 drops the last character.
            final parensText = end < 0
                ? (value.length > 1 ? value.substring(1, value.length - 1) : '')
                : (end >= 1 ? value.substring(1, end) : '');
            if (end > 0 && pyIsDecimal(parensText)) {
              genreId = pyDecimalInt(parensText);
            }
          }
          if (genreId >= 0 && genreId < _id3v1Genres.length) {
            value = _id3v1Genres[genreId];
          }
          _setField(fieldname, value);
        } else if (fieldname == 'modern_grouping') {
          _modernGroupingValues.add(value);
        } else if (fieldname == 'legacy_grouping') {
          _legacyGroupingValues.add(value);
        } else {
          _setField(fieldname, value);
        }
      }
    } else if (_parseTags && _syncedLyricsFrameIds.contains(frameId)) {
      final content = fh.read(frameSize);
      _setOtherField('lyrics', _parseSyncedLyrics(content));
    } else if (_parseTags && _customFrameIds.contains(frameId)) {
      final content = fh.read(frameSize);
      final encoding = _at(content, 0);
      final endPos = _findStringEndPos(content, encoding, 1);
      final description = _decodeString(_sl(content, 1, endPos), encoding);
      final value = _decodeString(_sl(content, endPos), encoding);
      if (description.isNotEmpty && value.isNotEmpty) {
        _setCustomField(description, value);
      }
    } else if (_parseTags && frameId == 'PRIV') {
      final content = fh.read(frameSize);
      final ownerIdEndPos = _findStringEndPos(content);
      final ownerId = _sl(content, 0, ownerIdEndPos - 1);
      if (_eq(ownerId, _ascii('XMP'))) {
        _setOtherField(
          'xmp',
          _utf8(_rstripNulBytes(_sl(content, ownerIdEndPos))),
        );
      }
    } else if (_parseTags && !_ignoredFrameIds.contains(frameId)) {
      final content = fh.read(frameSize);
      final encoding = _at(content, 0);
      final value = _validStringEncodings.contains(encoding)
          ? _decodeString(_sl(content, 1), encoding)
          : _decodeString(content);
      _setField(_otherPrefix + pyLower(frameId), value);
    } else {
      fh.seek(frameSize, _seekCur);
    }
    return parsedSize;
  }

  static int _findStringEndPos(
    Uint8List content, [
    int encoding = 0x00,
    int startPos = 0,
  ]) {
    if (encoding == 0x00 || encoding == 0x03) {
      final end = _find(content, const [0], startPos);
      return end < 0 ? startPos : end + 1;
    }
    var end = _find(content, const [0, 0], startPos);
    while (end >= 0 && (end - startPos) % 2 != 0) {
      end = _find(content, const [0, 0], end + 1);
    }
    return end < 0 ? startPos : end + 2;
  }

  String _decodeString(Uint8List value, [int? encoding]) {
    final String decoded;
    if (encoding == 0x00) {
      decoded = _decodeNamed(value, _defaultEncoding ?? 'ISO-8859-1');
    } else if (encoding == 0x01) {
      final be = _startsWith(value, const [0xFE, 0xFF]);
      if (be || _startsWith(value, const [0xFF, 0xFE])) {
        value = value.length.isEven ? _sl(value, 2) : _sl(value, 2, -1);
      }
      decoded = _utf16(value, bigEndian: be);
    } else if (encoding == 0x02) {
      if (value.length.isOdd) value = _sl(value, 0, -1);
      decoded = _utf16(value, bigEndian: true);
    } else if (encoding == 0x03) {
      decoded = _utf8(value);
    } else {
      decoded = _latin1(value);
    }
    return _rstripNul(decoded);
  }

  static int _unsynchsafe(Uint8List data, [int offset = 0]) =>
      (_at(data, offset) << 21) |
      (_at(data, offset + 1) << 14) |
      (_at(data, offset + 2) << 7) |
      _at(data, offset + 3);
}

/// `bytes.decode(name, 'replace')` for the encodings tinytag is given: its
/// latin-1 default and a caller's override (the scanner passes none).
String _decodeNamed(Uint8List b, String name) {
  final n = name.toLowerCase().replaceAll('_', '-');
  if (n == 'utf-8' || n == 'utf8') return _utf8(b);
  if (n == 'latin1' || n == 'latin-1' || n == 'iso-8859-1') return _latin1(b);
  throw ArgumentError('encoding $name is not supported');
}

// ── MPEG ────────────────────────────────────────────────────────────────────

class _MPEG extends TinyTag {
  _MPEG() : super._();

  static const _maxEstimationSec = 30;
  static const _cbrDetectionFrameCount = 5;
  static const _sampleRates = [
    [11025, 12000, 8000],
    [0, 0, 0],
    [22050, 24000, 16000],
    [44100, 48000, 32000],
  ];
  static const _v1l1 = [
    0,
    32,
    64,
    96,
    128,
    160,
    192,
    224,
    256,
    288,
    320,
    352,
    384,
    416,
    448,
    0,
  ];
  static const _v1l2 = [
    0,
    32,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
    384,
    0,
  ];
  static const _v1l3 = [
    0,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
    0,
  ];
  static const _v2l1 = [
    0,
    32,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    144,
    160,
    176,
    192,
    224,
    256,
    0,
  ];
  static const _v2l2 = [
    0,
    8,
    16,
    24,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    144,
    160,
    0,
  ];
  static const _v2l3 = _v2l2;
  static const _none = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
  static const _bitrateVersionLayers = [
    [_none, _v2l3, _v2l2, _v2l1],
    [_none, _none, _none, _none],
    [_none, _v2l3, _v2l2, _v2l1],
    [_none, _v1l3, _v1l2, _v1l1],
  ];
  static const _samplesPerFrame = [
    [0, 576, 1152, 384],
    [0, 0, 0, 0],
    [0, 576, 1152, 384],
    [0, 1152, 1152, 384],
  ];
  static const _slotSizes = [0, 1, 1, 4];
  static const _channelsPerChannelMode = [2, 2, 2, 1];

  int _endPadding = 0;

  @override
  void _parse(_Fh fh) {
    _f['mime_type'] = 'audio/mpeg';
    if (!_parseDuration) return;
    _f['is_lossless'] = false;
    var samplesPf = 0;
    var maxEstimationFrames = 0;
    var frameSizeAccu = 0;
    var frames = 0;
    var bitrateAccu = 0;
    final lastBitrates = <int>{};
    int? firstId;
    var audioOffset = fh.tell();
    while (true) {
      final header = fh.read(4);
      final headerLen = header.length;
      if (headerLen < 4) {
        if (frames > 0) _f['bitrate'] = bitrateAccu / frames;
        break;
      }
      final idByte = header[1];
      final brSrByte = header[2];
      final brId = (brSrByte >> 4) & 0xF;
      final srId = (brSrByte >> 2) & 0x3;
      final padding = (brSrByte >> 1) & 0x1;
      final mpegId = (idByte >> 3) & 0x3;
      final layerId = (idByte >> 1) & 0x3;
      final versionId = (idByte >> 1) & 0xF;
      final channelMode = (header[3] >> 6) & 0x3;
      if (header[0] != 0xFF ||
          (idByte & 0xE0) != 0xE0 ||
          (firstId != null && firstId != versionId) ||
          brId > 14 ||
          brId == 0 ||
          srId == 3 ||
          layerId == 0 ||
          mpegId == 1) {
        final idx = _find(header, const [0xFF], 1);
        var nextOffset = headerLen;
        if (idx != -1) {
          nextOffset = idx;
          fh.seek(idx - headerLen, _seekCur);
        }
        if (frames == 0) audioOffset += nextOffset;
        continue;
      }
      _f['channels'] = _channelsPerChannelMode[channelMode];
      final frameBr = _bitrateVersionLayers[mpegId][layerId][brId];
      final samplerate = _sampleRates[mpegId][srId];
      _f['samplerate'] = samplerate;
      samplesPf = _samplesPerFrame[mpegId][layerId];
      final slotSize = _slotSizes[layerId];
      final coefficient = (samplesPf ~/ 8 ~/ slotSize) * 1000;
      final frameLength =
          ((coefficient * frameBr) ~/ samplerate + padding) * slotSize;
      if (firstId == null) {
        firstId = versionId;
        maxEstimationFrames = (_maxEstimationSec * samplerate) ~/ samplesPf;
      }
      if (frames == 0) {
        var xframes = 0, byteCount = 0;
        final prevOffset = headerLen + audioOffset;
        final frameContent = fh.read(math.min(50, frameLength));
        var xing = _find(frameContent, _ascii('Xing'));
        if (xing == -1) xing = _find(frameContent, _ascii('Info'));
        if (xing != -1) {
          (xframes, byteCount) = _parseXingHeader(frameContent, xing);
          byteCount -= frameLength;
        } else {
          final vbri = _find(frameContent, _ascii('VBRI'));
          if (vbri != -1) {
            (xframes, byteCount) = _parseVbriHeader(frameContent, vbri);
          }
        }
        if (xframes > 0 && byteCount > 0) {
          final dur = xframes * samplesPf / samplerate;
          _f['duration'] = dur;
          _f['bitrate'] = byteCount * 8 / dur / 1000;
          _durationParsed = true;
          return;
        }
        fh.seek(prevOffset);
      }
      frames += 1;
      bitrateAccu += frameBr;
      if (frames <= _cbrDetectionFrameCount) lastBitrates.add(frameBr);
      frameSizeAccu += frameLength;
      final isCbr =
          frames == _cbrDetectionFrameCount && lastBitrates.length == 1;
      if (frames == maxEstimationFrames || isCbr) {
        final streamSize = filesize - audioOffset - _endPadding;
        final estFrameCount = (streamSize / (frameSizeAccu / frames) + 0.5)
            .truncate();
        final samples = estFrameCount * samplesPf;
        _f['duration'] = samples / samplerate;
        _f['bitrate'] = bitrateAccu / frames;
        _durationParsed = true;
        return;
      }
      if (frameLength > 1) fh.seek(frameLength - headerLen, _seekCur);
    }
    final sr = samplerate;
    if (sr != null && sr != 0) _f['duration'] = frames * samplesPf / sr;
    _durationParsed = true;
  }

  static (int, int) _parseXingHeader(Uint8List content, int offset) {
    offset += 4;
    final flags = _i32be(content, offset);
    offset += 4;
    var frames = 0, byteCount = 0;
    if (flags & 1 != 0) {
      frames = _i32be(content, offset);
      offset += 4;
    }
    if (flags & 2 != 0) {
      byteCount = _i32be(content, offset);
      offset += 4;
    }
    return (frames, byteCount);
  }

  static (int, int) _parseVbriHeader(Uint8List content, int offset) {
    offset += 10;
    final byteCount = _u32be(content, offset);
    offset += 4;
    final frames = _u32be(content, offset);
    return (frames, byteCount);
  }
}

// ── Ogg ─────────────────────────────────────────────────────────────────────

class _Ogg extends TinyTag {
  _Ogg() : super._();

  static const _vorbisMapping = {
    'album': 'album',
    'albumartist': 'albumartist',
    'title': 'title',
    'artist': 'artist',
    'artists': 'artist',
    'author': 'artist',
    'date': 'year',
    'tracknumber': 'track',
    'tracktotal': 'track_total',
    'totaltracks': 'track_total',
    'discnumber': 'disc',
    'disctotal': 'disc_total',
    'totaldiscs': 'disc_total',
    'genre': 'genre',
    'description': 'comment',
    'comment': 'comment',
    'comments': 'comment',
    'composer': 'composer',
    'bpm': 'other.bpm',
    'copyright': 'other.copyright',
    'isrc': 'other.isrc',
    'lyrics': 'other.lyrics',
    'unsyncedlyrics': 'other.lyrics',
    'publisher': 'other.publisher',
    'language': 'other.language',
    'director': 'other.director',
    'website': 'other.url',
    'conductor': 'other.conductor',
    'lyricist': 'other.lyricist',
    'discsubtitle': 'other.set_subtitle',
    'setsubtitle': 'other.set_subtitle',
    'initialkey': 'other.initial_key',
    'key': 'other.initial_key',
    'encodedby': 'other.encoded_by',
    'encodersettings': 'other.encoder_settings',
    'media': 'other.media',
    'license': 'other.license',
    'barcode': 'other.barcode',
    'catalognumber': 'other.catalog_number',
    'movementname': 'other.movement_name',
    'movement': 'other.movement',
    'movementtotal': 'other.movement_total',
    'showmovement': 'other.show_movement',
    'grouping': 'other.grouping',
    'contentgroup': 'other.grouping',
    'work': 'other.work',
  };

  int _granulePos = 0;
  int _audioSize = 0;
  int? _granulePosSerial;
  int? _audioSizeSerial;

  @override
  void _parse(_Fh fh) {
    var checkFlacSecondPacket = false;
    var checkSpeexSecondPacket = false;
    var preskip = 0;
    for (final (packet, serial) in _parsePages(fh)) {
      if (_startsWith(packet, const [0x01, ..._vorbisAscii])) {
        if (_parseDuration) {
          final channels = _at(packet, 11);
          final samplerate = _i32le(packet, 12);
          final bitrate = _i32le(packet, 20);
          if (channels > 0) _f['channels'] = channels;
          if (samplerate > 0) _f['samplerate'] = samplerate;
          if (bitrate > 0) _f['bitrate'] = bitrate / 1000;
          _f['mime_type'] = 'audio/ogg; codecs="vorbis"';
          _f['is_lossless'] = false;
          _granulePosSerial = serial;
          _durationParsed = true;
        }
      } else if (_startsWith(packet, const [0x03, ..._vorbisAscii])) {
        if (_parseTags) {
          final walker = _BytesFh(packet)..seek(7);
          _setVorbisCommentFields(walker);
          _tagsParsed = true;
        }
      } else if (_startsWith(packet, _ascii('OpusHead'))) {
        if (_parseDuration) {
          final version = _at(packet, 8);
          final channels = _at(packet, 9);
          preskip = _u16le(packet, 10);
          if ((version & 0xF0) == 0) {
            if (channels > 0) _f['channels'] = channels;
            _f['samplerate'] = 48000;
          }
          _f['mime_type'] = 'audio/ogg; codecs="opus"';
          _f['is_lossless'] = false;
          _durationParsed = true;
          _granulePosSerial = serial;
        }
      } else if (_startsWith(packet, _ascii('OpusTags'))) {
        if (_parseTags) {
          final walker = _BytesFh(packet)..seek(8);
          _setVorbisCommentFields(walker);
          _tagsParsed = true;
        }
        _audioSizeSerial = serial;
      } else if (_startsWith(packet, const [0x7F, 0x46, 0x4C, 0x41, 0x43])) {
        final walker = _BytesFh(packet)..seek(9);
        final flac = _child(_Flac.new, walker)
          .._load(tags: false, duration: _parseDuration);
        _update(flac);
        if (_parseDuration) {
          _f['mime_type'] = 'audio/ogg; codecs="flac"';
          _f['is_lossless'] = true;
        }
        _durationParsed = _parseDuration;
        checkFlacSecondPacket = true;
      } else if (checkFlacSecondPacket) {
        if (_parseTags) {
          final walker = _BytesFh(packet);
          final metaHeader = walker.read(4);
          final blockType = _at(metaHeader, 0) & 0x7f;
          if (blockType == _Flac._vorbisComment) {
            _setVorbisCommentFields(walker);
          }
          _tagsParsed = true;
        }
        checkFlacSecondPacket = false;
      } else if (_startsWith(packet, _ascii('Speex   '))) {
        if (_parseDuration) {
          final samplerate = _i32le(packet, 36);
          final channels = _i32le(packet, 48);
          final bitrate = _i32le(packet, 52);
          if (samplerate > 0) _f['samplerate'] = samplerate;
          if (channels > 0) _f['channels'] = channels;
          if (bitrate > 0) _f['bitrate'] = bitrate / 1000;
          _f['mime_type'] = 'audio/ogg; codecs="speex"';
          _f['is_lossless'] = false;
          _granulePosSerial = serial;
          _durationParsed = true;
        }
        checkSpeexSecondPacket = true;
      } else if (checkSpeexSecondPacket) {
        if (_parseTags) {
          _setVorbisCommentFields(_BytesFh(packet));
          _tagsParsed = true;
        }
        checkSpeexSecondPacket = false;
      }
      if (_tagsParsed && !_parseDuration) break;
    }
    final sr = samplerate;
    if (_f['duration'] != null || sr == null || sr == 0) return;
    final d = (_granulePos - preskip) / sr;
    // Python's max(x, 0) keeps the int 0.
    final Object dur = d > 0 ? d : 0;
    _f['duration'] = dur;
    if (_audioSize != 0 && d > 0) _f['bitrate'] = _audioSize * 8 / d / 1000;
  }

  static const _vorbisAscii = [0x76, 0x6F, 0x72, 0x62, 0x69, 0x73];

  static Uint8List _exactRead(_Fh fh, int n) => _exact(fh.read(n), n);

  static Iterable<_Field> _parseVorbisComment(
    _Fh fh, {
    bool readData = true,
  }) sync* {
    final vendorLength = _u32le(_exactRead(fh, 4), 0);
    fh.seek(vendorLength, _seekCur);
    final elements = _u32le(_exactRead(fh, 4), 0);
    for (var i = 0; i < elements; i++) {
      final length = _u32le(_exactRead(fh, 4), 0);
      if (!readData) {
        fh.seek(length, _seekCur);
        continue;
      }
      final keyvalpair = _utf8(fh.read(length));
      final eq = keyvalpair.indexOf('=');
      if (eq < 0) continue;
      final key = keyvalpair.substring(0, eq);
      var value = keyvalpair.substring(eq + 1);
      final keyLower = pyLower(key);
      if (keyLower == 'metadata_block_picture') continue; // images not loaded
      final fieldname = _vorbisMapping[keyLower] ?? _otherPrefix + keyLower;
      if (const {
        'track',
        'disc',
        'track_total',
        'disc_total',
      }.contains(fieldname)) {
        if ((fieldname == 'track' || fieldname == 'disc') &&
            value.contains('/')) {
          final parts = value.split('/');
          value = parts[0];
          final total = parts[1];
          if (pyIsDecimal(total)) {
            yield ('${fieldname}_total', pyDecimalInt(total));
          }
        }
        if (pyIsDecimal(value)) yield (fieldname, pyDecimalInt(value));
      } else {
        yield (fieldname, value);
      }
    }
  }

  void _setVorbisCommentFields(_Fh fh) {
    for (final (fieldname, value) in _parseVorbisComment(fh)) {
      _setField(fieldname, value);
    }
  }

  Iterable<(Uint8List, int)> _parsePages(_Fh fh) sync* {
    final packets = <int, BytesBuilder>{};
    var lastGranulePos = 0;
    var lastAudioSize = 0;
    const headerLen = 27;
    var pageHeader = fh.read(headerLen);
    if (!_startsWith(pageHeader, _ascii('OggS')) ||
        !_startsWith(pageHeader, const [0], 4)) {
      throw ParseError('Invalid OGG header');
    }
    _f['mime_type'] = 'audio/ogg';
    while (pageHeader.length == headerLen) {
      final version = pageHeader[4];
      if (!_startsWith(pageHeader, _ascii('OggS')) || version != 0) break;
      final headerType = pageHeader[5];
      final eos = headerType & 0x04;
      final granulePos = _i64le(pageHeader, 6);
      final serial = _u32le(pageHeader, 14);
      final packetData = packets.putIfAbsent(
        serial,
        () => BytesBuilder(copy: true),
      );
      final audioSizeSerialMatch = serial == _audioSizeSerial;
      if (serial == _granulePosSerial) {
        if (eos != 0) {
          _granulePos = granulePos;
        } else {
          _granulePos = lastGranulePos;
          lastGranulePos = granulePos;
        }
      }
      final segments = pageHeader[26];
      final segSizes = fh.read(segments);
      var readSize = 0;
      var audioSize = 0;
      for (final segSize in segSizes) {
        readSize += segSize;
        if (audioSizeSerialMatch) audioSize += segSize;
        if (segSize < 255 && !_tagsParsed) {
          packetData.add(fh.read(readSize));
          yield (packetData.takeBytes(), serial);
          readSize = 0;
        }
      }
      if (readSize > 0) {
        if (_tagsParsed && _durationParsed) {
          fh.seek(readSize, _seekCur);
        } else {
          packetData.add(fh.read(readSize));
        }
      }
      if (audioSizeSerialMatch) {
        if (eos != 0) {
          _audioSize += lastAudioSize + audioSize;
        } else {
          _audioSize += lastAudioSize;
          lastAudioSize = audioSize;
        }
      }
      pageHeader = fh.read(headerLen);
    }
  }
}

// ── WAVE ────────────────────────────────────────────────────────────────────

class _Wave extends TinyTag {
  _Wave() : super._();

  static const _riffMapping = {
    'INAM': 'title',
    'TITL': 'title',
    'IPRD': 'album',
    'IART': 'artist',
    'IBPM': 'other.bpm',
    'ICMT': 'comment',
    'IMUS': 'composer',
    'ICOP': 'other.copyright',
    'ICRD': 'year',
    'IGNR': 'genre',
    'ILNG': 'other.language',
    'ISRC': 'other.isrc',
    'IPUB': 'other.publisher',
    'IPRT': 'track',
    'ITRK': 'track',
    'TRCK': 'track',
    'IBSU': 'other.url',
    'YEAR': 'year',
    'IWRI': 'other.lyricist',
    'IENC': 'other.encoded_by',
    'IMED': 'other.media',
  };
  static const _losslessFormats = {0x01, 0x03, 0x163, 0x8ae, 0x1971, 0xf1ac};

  @override
  void _parse(_Fh fh) {
    final header = fh.read(12);
    if (!_startsWith(header, _ascii('RIFF')) ||
        !_startsWith(header, _ascii('WAVE'), 8)) {
      throw ParseError('Invalid WAV header');
    }
    _f['mime_type'] = 'audio/wav';
    var blockAlign = 0;
    var audioSize = 0;
    var numSamples = 0;
    const headerLen = 8;
    var chunkHeader = fh.read(headerLen);
    while (chunkHeader.length == headerLen) {
      var subchunkSize = _u32le(chunkHeader, 4);
      final subchunkSizeUnpadded = subchunkSize;
      subchunkSize += subchunkSize % 2;
      bool id(String s) => _startsWith(chunkHeader, _ascii(s));
      if (_parseDuration && id('fmt ')) {
        final chunk = fh.read(subchunkSize);
        var formatTag = _u16le(chunk, 0);
        final channels = _u16le(chunk, 2);
        final samplerate = _u32le(chunk, 4);
        blockAlign = _u16le(chunk, 12);
        final bitdepth = _u16le(chunk, 14);
        if (formatTag == 0xFFFE) formatTag = _u16le(chunk, 24);
        if (channels > 0) _f['channels'] = channels;
        if (samplerate > 0) _f['samplerate'] = samplerate;
        if (bitdepth > 0) _f['bitdepth'] = bitdepth;
        if (formatTag != 0) {
          _f['mime_type'] =
              'audio/wav; codecs="${formatTag.toRadixString(16).toUpperCase()}"';
          _f['is_lossless'] = _losslessFormats.contains(formatTag);
        }
      } else if (_parseDuration && id('data')) {
        audioSize = subchunkSizeUnpadded;
        fh.seek(subchunkSize, _seekCur);
      } else if (_parseDuration && id('fact')) {
        final chunk = fh.read(subchunkSize);
        numSamples = _u32le(chunk, 0);
      } else if (_parseTags && id('LIST')) {
        final chunk = fh.read(subchunkSize);
        if (_startsWith(chunk, _ascii('INFO'))) {
          final walker = _BytesFh(chunk)..seek(4);
          var field = walker.read(4);
          while (field.length == 4) {
            var dataLength = _u32le(_exact(walker.read(4), 4), 0);
            dataLength += dataLength % 2;
            final data = _rstripNulBytes(walker.read(dataLength));
            final key = _latin1(field);
            final fieldname = _riffMapping[key] ?? pyLower(_otherPrefix + key);
            final value = _utf8(data);
            if (fieldname == 'track') {
              if (pyIsDecimal(value)) _setField(fieldname, pyDecimalInt(value));
            } else {
              _setField(fieldname, value);
            }
            field = walker.read(4);
          }
        }
      } else if (_parseTags && (id('id3 ') || id('ID3 '))) {
        final id3 =
            _child(
                  _ID3.new,
                  _BytesFh(Uint8List.fromList(fh.read(subchunkSize))),
                )
                as _ID3;
        id3._onlyId3 = true;
        id3._load(tags: true, duration: false);
        _update(id3);
      } else if (_parseTags && id('_PMX')) {
        final chunk = _rstripNulBytes(fh.read(subchunkSize));
        _setOtherField('xmp', _utf8(chunk));
      } else {
        fh.seek(subchunkSize, _seekCur);
      }
      chunkHeader = fh.read(headerLen);
    }
    final sr = samplerate;
    if (isLossless != true && sr != null && sr != 0) {
      if (audioSize == 0) numSamples = 0;
      final d = numSamples / sr;
      _f['duration'] = d;
      if (d != 0) _f['bitrate'] = audioSize * 8 / d / 1000;
    } else if (blockAlign != 0 && sr != null && sr != 0) {
      _f['duration'] = audioSize / (blockAlign * sr);
      _f['bitrate'] = blockAlign * sr * 8 / 1000;
    }
    _durationParsed = _parseDuration;
    _tagsParsed = _parseTags;
  }
}

// ── FLAC ────────────────────────────────────────────────────────────────────

class _Flac extends TinyTag {
  _Flac() : super._();

  static const _streaminfo = 0;
  static const _vorbisComment = 4;
  static const _picture = 6;

  @override
  void _parse(_Fh fh) {
    final header = fh.read(4);
    if (!_startsWith(header, _ascii('fLaC'))) {
      throw ParseError('Invalid FLAC header');
    }
    _f['mime_type'] = 'audio/flac';
    var foundStreaminfo = false;
    const headerLen = 4;
    var blockHeader = fh.read(headerLen);
    while (blockHeader.length == headerLen) {
      final blockType = blockHeader[0] & 0x7f;
      final isLastBlock = (blockHeader[0] & 0x80) != 0;
      final isStreaminfoBlock = blockType == _streaminfo;
      final size =
          (blockHeader[1] << 16) | (blockHeader[2] << 8) | blockHeader[3];
      if (_parseDuration && isStreaminfoBlock && !foundStreaminfo) {
        final head = fh.read(size);
        final infoByte = _u64be(head, 10);
        // >>> on the 64-bit pattern: unpack('>Q') is unsigned.
        final samplerate = (infoByte >>> 44) & ((1 << 20) - 1);
        final channels = ((infoByte >>> 41) & 0x7) + 1;
        final bitdepth = ((infoByte >>> 36) & 0x1F) + 1;
        final totalSamples = infoByte & ((1 << 36) - 1);
        if (samplerate > 0) {
          _f['samplerate'] = samplerate;
          final duration = totalSamples / samplerate;
          _f['duration'] = duration;
          if (duration > 0) _f['bitrate'] = filesize * 8 / duration / 1000;
        }
        _f['channels'] = channels;
        _f['bitdepth'] = bitdepth;
        _f['is_lossless'] = true;
      } else if (blockType == _vorbisComment) {
        for (final (fieldname, value) in _Ogg._parseVorbisComment(
          fh,
          readData: _parseTags,
        )) {
          _setField(fieldname, value);
        }
      } else if (blockType == _picture) {
        _skipImage(fh);
      } else {
        fh.seek(size, _seekCur);
      }
      if (isStreaminfoBlock && !foundStreaminfo) foundStreaminfo = true;
      if (isLastBlock) break;
      blockHeader = fh.read(headerLen);
    }
    final d = duration;
    if (d != null && d != 0) {
      final audioOffset = fh.tell();
      final bitrate = (filesize - audioOffset) * 8 / d / 1000;
      if (bitrate > 0) _f['bitrate'] = bitrate;
    }
    _durationParsed = _parseDuration;
    _tagsParsed = _parseTags;
  }

  /// `_parse_image(fh, read_data=False)`.
  static void _skipImage(_Fh fh) {
    final h = _exact(fh.read(8), 8);
    fh.seek(_u32be(h, 4), _seekCur);
    final descriptionLen = _u32be(_exact(fh.read(4), 4), 0);
    fh.seek(descriptionLen, _seekCur);
    fh.seek(16, _seekCur);
    final picLen = _u32be(_exact(fh.read(4), 4), 0);
    fh.seek(picLen, _seekCur);
  }
}
