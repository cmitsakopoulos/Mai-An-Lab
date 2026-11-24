import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

/// The part of Python's `zipfile` the state bundles use: writing a ZIP
/// with deflated members (`ZipFile(path, "w", ZIP_DEFLATED)`, `write`,
/// `writestr`) and reading one back (`namelist`, `read`, `open` to a file).
/// Members stream through dart:io's raw deflate, so a library.db never sits
/// in memory. Synchronous: callers run it on a background isolate.
///
/// No ZIP64: a member or archive past 4 GiB is refused (Python writes
/// ZIP64 records only past 2 GiB, and no bundle comes near that).

/// `zipfile.BadZipFile`: the file is not a ZIP this reader understands.
class BadZipFile implements Exception {
  BadZipFile(this.message);
  final String message;
  @override
  String toString() => message;
}

const _chunk = 1 << 16;
const _max32 = 0xFFFFFFFF;

/// One member as the central directory lists it.
class ZipEntry {
  ZipEntry._(
    this.name,
    this.method,
    this.crc,
    this.compressedSize,
    this.size,
    this.headerOffset,
  );
  final String name;
  final int method;
  final int crc;
  final int compressedSize;
  final int size;
  final int headerOffset;
}

/// `zipfile.ZipFile(path, "w", compression=ZIP_DEFLATED)`.
class ZipWriter {
  ZipWriter(String path) : _out = File(path).openSync(mode: FileMode.write);

  final RandomAccessFile _out;
  final _central = BytesBuilder(copy: false);
  int _count = 0;
  bool _closed = false;

  /// `write(src, name)`: the file's bytes, dated by its mtime.
  void addFile(String name, String src) {
    final f = File(src);
    final input = f.openSync();
    try {
      _add(name, f.lastModifiedSync(), 0x81A4, (feed) {
        final buf = Uint8List(_chunk);
        while (true) {
          final n = input.readIntoSync(buf);
          if (n == 0) break;
          feed(Uint8List.sublistView(buf, 0, n));
        }
      });
    } finally {
      input.closeSync();
    }
  }

  /// `writestr(name, data)`: dated now, mode 0600 as Python leaves it.
  void addBytes(String name, List<int> data) => _add(
    name,
    DateTime.now(),
    0x8180,
    (feed) => feed(data is Uint8List ? data : Uint8List.fromList(data)),
  );

  /// `writestr(name, text)`: UTF-8, as Python encodes a str.
  void addString(String name, String text) => addBytes(name, utf8.encode(text));

  void _add(
    String name,
    DateTime mtime,
    int mode,
    void Function(void Function(Uint8List)) produce,
  ) {
    if (_closed) throw StateError('ZIP already closed');
    final nameBytes = utf8.encode(name);
    final ascii = nameBytes.every((b) => b < 0x80);
    final flags = ascii ? 0 : 0x800;
    final (dosTime, dosDate) = _dos(mtime);
    final offset = _out.positionSync();
    // Sizes and CRC are patched in once the data is written (Python seeks
    // back the same way on a seekable file).
    _out.writeFromSync(
      _localHeader(flags, dosTime, dosDate, 0, 0, 0, nameBytes),
    );
    final deflate = RawZLibFilter.deflateFilter(raw: true);
    var crc = 0;
    var size = 0;
    var csize = 0;
    void drain() {
      while (true) {
        final out = deflate.processed(flush: false);
        if (out == null) break;
        _out.writeFromSync(out);
        csize += out.length;
      }
    }

    produce((data) {
      crc = crc32(data, crc);
      size += data.length;
      deflate.process(data, 0, data.length);
      drain();
    });
    while (true) {
      final out = deflate.processed(end: true);
      if (out == null) break;
      _out.writeFromSync(out);
      csize += out.length;
    }
    if (size > _max32 || csize > _max32 || offset > _max32) {
      throw BadZipFile('$name: too large for a ZIP without ZIP64');
    }
    final end = _out.positionSync();
    _out
      ..setPositionSync(offset + 14)
      ..writeFromSync(_u32s([crc, csize, size]))
      ..setPositionSync(end);
    _central.add(
      _centralHeader(
        flags,
        dosTime,
        dosDate,
        crc,
        csize,
        size,
        nameBytes,
        mode << 16,
        offset,
      ),
    );
    _count++;
  }

  void close() {
    if (_closed) return;
    _closed = true;
    try {
      final cdOffset = _out.positionSync();
      final cd = _central.takeBytes();
      _out.writeFromSync(cd);
      final eocd = ByteData(22)
        ..setUint32(0, 0x06054b50, Endian.little)
        ..setUint16(8, _count, Endian.little)
        ..setUint16(10, _count, Endian.little)
        ..setUint32(12, cd.length, Endian.little)
        ..setUint32(16, cdOffset, Endian.little);
      _out.writeFromSync(eocd.buffer.asUint8List());
    } finally {
      _out.closeSync();
    }
  }
}

/// `zipfile.ZipFile(path, "r")`.
class ZipReader {
  ZipReader._(this._in, this.entries);

  /// Opens [path]; [BadZipFile] when it has no readable central directory.
  factory ZipReader(String path) {
    final f = File(path).openSync();
    try {
      return ZipReader._(f, _readDirectory(f));
    } catch (_) {
      f.closeSync();
      rethrow;
    }
  }

  final RandomAccessFile _in;

  /// In archive order, as `infolist()`.
  final List<ZipEntry> entries;

  List<String> get names => [for (final e in entries) e.name];

  ZipEntry? _entry(String name) {
    for (final e in entries) {
      if (e.name == name) return e;
    }
    return null;
  }

  /// `read(name)`; null when there is no such member (Python's KeyError).
  Uint8List? read(String name) {
    final e = _entry(name);
    if (e == null) return null;
    final out = BytesBuilder(copy: false);
    _extract(e, out.add);
    return out.takeBytes();
  }

  /// `open(name)` copied into [dest] (`shutil.copyfileobj`); false when
  /// there is no such member.
  bool extractTo(String name, String dest) {
    final e = _entry(name);
    if (e == null) return false;
    final out = File(dest).openSync(mode: FileMode.write);
    try {
      _extract(e, out.writeFromSync);
    } finally {
      out.closeSync();
    }
    return true;
  }

  void _extract(ZipEntry e, void Function(Uint8List) sink) {
    _in.setPositionSync(e.headerOffset);
    final head = ByteData.sublistView(_in.readSync(30));
    if (head.lengthInBytes < 30 ||
        head.getUint32(0, Endian.little) != 0x04034b50) {
      throw BadZipFile('Bad magic number for file header');
    }
    final skip =
        head.getUint16(26, Endian.little) + head.getUint16(28, Endian.little);
    _in.setPositionSync(e.headerOffset + 30 + skip);
    final inflate = e.method == 8
        ? RawZLibFilter.inflateFilter(raw: true)
        : null;
    if (e.method != 0 && e.method != 8) {
      throw BadZipFile('That compression method is not supported');
    }
    var crc = 0;
    var size = 0;
    void emit(List<int> data) {
      final bytes = data is Uint8List ? data : Uint8List.fromList(data);
      crc = crc32(bytes, crc);
      size += bytes.length;
      sink(bytes);
    }

    var left = e.compressedSize;
    while (left > 0) {
      final data = _in.readSync(left < _chunk ? left : _chunk);
      if (data.isEmpty) throw BadZipFile('Truncated file');
      left -= data.length;
      if (inflate == null) {
        emit(data);
        continue;
      }
      inflate.process(data, 0, data.length);
      while (true) {
        final out = inflate.processed(flush: false);
        if (out == null) break;
        emit(out);
      }
    }
    if (inflate != null) {
      while (true) {
        final out = inflate.processed(end: true);
        if (out == null) break;
        emit(out);
      }
    }
    if (crc != e.crc || size != e.size) {
      throw BadZipFile("Bad CRC-32 for file '${e.name}'");
    }
  }

  void close() => _in.closeSync();

  static List<ZipEntry> _readDirectory(RandomAccessFile f) {
    final length = f.lengthSync();
    // The end record is the last 22 bytes, or sits before a comment of up
    // to 64 KiB.
    final tail = length < 22 + 0xFFFF ? length : 22 + 0xFFFF;
    f.setPositionSync(length - tail);
    final buf = f.readSync(tail);
    var at = -1;
    for (var i = buf.length - 22; i >= 0; i--) {
      if (buf[i] == 0x50 &&
          buf[i + 1] == 0x4b &&
          buf[i + 2] == 0x05 &&
          buf[i + 3] == 0x06) {
        at = i;
        break;
      }
    }
    if (at < 0) throw BadZipFile('File is not a zip file');
    final end = ByteData.sublistView(buf, at);
    final count = end.getUint16(10, Endian.little);
    final cdSize = end.getUint32(12, Endian.little);
    final cdOffset = end.getUint32(16, Endian.little);
    if (count == 0xFFFF || cdOffset == _max32) {
      throw BadZipFile('ZIP64 archives are not supported');
    }
    if (cdOffset + cdSize > length) {
      throw BadZipFile('File is not a zip file');
    }
    f.setPositionSync(cdOffset);
    final cd = f.readSync(cdSize);
    final d = ByteData.sublistView(cd);
    final out = <ZipEntry>[];
    var p = 0;
    for (var i = 0; i < count; i++) {
      if (p + 46 > cd.length || d.getUint32(p, Endian.little) != 0x02014b50) {
        throw BadZipFile('Bad magic number for central directory');
      }
      final flags = d.getUint16(p + 8, Endian.little);
      final n = d.getUint16(p + 28, Endian.little);
      final m = d.getUint16(p + 30, Endian.little);
      final k = d.getUint16(p + 32, Endian.little);
      final raw = Uint8List.sublistView(cd, p + 46, p + 46 + n);
      // Python decodes cp437 without the UTF-8 flag; bundle names are ASCII.
      final name = flags & 0x800 != 0 ? utf8.decode(raw) : latin1.decode(raw);
      out.add(
        ZipEntry._(
          name,
          d.getUint16(p + 10, Endian.little),
          d.getUint32(p + 16, Endian.little),
          d.getUint32(p + 20, Endian.little),
          d.getUint32(p + 24, Endian.little),
          d.getUint32(p + 42, Endian.little),
        ),
      );
      p += 46 + n + m + k;
    }
    return out;
  }
}

Uint8List _localHeader(
  int flags,
  int time,
  int date,
  int crc,
  int csize,
  int size,
  List<int> name,
) {
  final h = ByteData(30)
    ..setUint32(0, 0x04034b50, Endian.little)
    ..setUint16(4, 20, Endian.little)
    ..setUint16(6, flags, Endian.little)
    ..setUint16(8, 8, Endian.little)
    ..setUint16(10, time, Endian.little)
    ..setUint16(12, date, Endian.little)
    ..setUint32(14, crc, Endian.little)
    ..setUint32(18, csize, Endian.little)
    ..setUint32(22, size, Endian.little)
    ..setUint16(26, name.length, Endian.little);
  return (BytesBuilder(copy: false)
        ..add(h.buffer.asUint8List())
        ..add(name))
      .takeBytes();
}

Uint8List _centralHeader(
  int flags,
  int time,
  int date,
  int crc,
  int csize,
  int size,
  List<int> name,
  int externalAttr,
  int offset,
) {
  final h = ByteData(46)
    ..setUint32(0, 0x02014b50, Endian.little)
    // Made by: Unix (3), version 2.0, as Python writes on POSIX.
    ..setUint16(4, 0x0314, Endian.little)
    ..setUint16(6, 20, Endian.little)
    ..setUint16(8, flags, Endian.little)
    ..setUint16(10, 8, Endian.little)
    ..setUint16(12, time, Endian.little)
    ..setUint16(14, date, Endian.little)
    ..setUint32(16, crc, Endian.little)
    ..setUint32(20, csize, Endian.little)
    ..setUint32(24, size, Endian.little)
    ..setUint16(28, name.length, Endian.little)
    ..setUint32(38, externalAttr, Endian.little)
    ..setUint32(42, offset, Endian.little);
  return (BytesBuilder(copy: false)
        ..add(h.buffer.asUint8List())
        ..add(name))
      .takeBytes();
}

Uint8List _u32s(List<int> values) {
  final b = ByteData(4 * values.length);
  for (final (i, v) in values.indexed) {
    b.setUint32(4 * i, v, Endian.little);
  }
  return b.buffer.asUint8List();
}

/// MS-DOS time and date fields (local time, 2 s steps; 1980 at the
/// earliest, as zipfile clamps).
(int, int) _dos(DateTime t) {
  final l = t.toLocal();
  if (l.year < 1980) return (0, (1 << 5) | 1);
  return (
    (l.hour << 11) | (l.minute << 5) | (l.second ~/ 2),
    ((l.year - 1980) << 9) | (l.month << 5) | l.day,
  );
}

final _crcTable = () {
  final t = Uint32List(256);
  for (var n = 0; n < 256; n++) {
    var c = n;
    for (var k = 0; k < 8; k++) {
      c = (c & 1) != 0 ? 0xEDB88320 ^ (c >> 1) : c >> 1;
    }
    t[n] = c;
  }
  return t;
}();

/// `zlib.crc32(data, value)`.
int crc32(List<int> data, [int value = 0]) {
  var c = value ^ _max32;
  final t = _crcTable;
  for (final b in data) {
    c = t[(c ^ b) & 0xFF] ^ (c >> 8);
  }
  return c ^ _max32;
}
