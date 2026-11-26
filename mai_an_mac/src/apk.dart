import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

/// What the Mac needs to know about an Android build before offering it to
/// the phone: read straight from the APK (a zip) and its binary
/// AndroidManifest.xml, so no Android SDK (aapt) is needed on the Mac.
class ApkInfo {
  const ApkInfo({
    required this.package,
    required this.versionCode,
    required this.versionName,
    required this.debuggable,
    this.minSdk,
  });

  final String package;
  final int versionCode;
  final String versionName;
  final bool debuggable;
  final int? minSdk;

  /// "0.3.0 (4)".
  String get label => '$versionName ($versionCode)';

  /// Read [path]. Throws [FormatException] for anything that isn't an APK.
  static ApkInfo read(String path) {
    final raf = File(path).openSync();
    try {
      final xml = _zipEntry(raf, 'AndroidManifest.xml');
      if (xml == null) {
        throw const FormatException('Not an APK: no AndroidManifest.xml');
      }
      return _parseManifest(xml);
    } finally {
      raf.closeSync();
    }
  }
}

// ── zip ─────────────────────────────────────────────────────────────────────

/// One file out of a zip, or null. Stored and deflated entries only (all an
/// APK uses); no zip64 (an APK under 4 GB never needs it).
Uint8List? _zipEntry(RandomAccessFile raf, String wanted) {
  final len = raf.lengthSync();
  if (len < 22) throw const FormatException('Not a zip file');
  // The end-of-central-directory record sits in the last 64 KiB + 22 bytes.
  final tailLen = len < 65557 ? len : 65557;
  raf.setPositionSync(len - tailLen);
  final tail = ByteData.sublistView(raf.readSync(tailLen));
  var eocd = -1;
  for (var i = tailLen - 22; i >= 0; i--) {
    if (tail.getUint32(i, Endian.little) == 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw const FormatException('Not a zip file');
  final count = tail.getUint16(eocd + 10, Endian.little);
  final cdSize = tail.getUint32(eocd + 12, Endian.little);
  final cdOffset = tail.getUint32(eocd + 16, Endian.little);
  if (cdOffset + cdSize > len) throw const FormatException('Corrupt zip');
  raf.setPositionSync(cdOffset);
  final cd = ByteData.sublistView(raf.readSync(cdSize));
  var p = 0;
  for (var n = 0; n < count && p + 46 <= cd.lengthInBytes; n++) {
    if (cd.getUint32(p, Endian.little) != 0x02014b50) break;
    final method = cd.getUint16(p + 10, Endian.little);
    final compSize = cd.getUint32(p + 20, Endian.little);
    final nameLen = cd.getUint16(p + 28, Endian.little);
    final extraLen = cd.getUint16(p + 30, Endian.little);
    final commentLen = cd.getUint16(p + 32, Endian.little);
    final local = cd.getUint32(p + 42, Endian.little);
    final name = utf8.decode(
      Uint8List.sublistView(cd, p + 46, p + 46 + nameLen),
      allowMalformed: true,
    );
    p += 46 + nameLen + extraLen + commentLen;
    if (name != wanted) continue;
    raf.setPositionSync(local);
    final lh = ByteData.sublistView(raf.readSync(30));
    if (lh.getUint32(0, Endian.little) != 0x04034b50) {
      throw const FormatException('Corrupt zip entry');
    }
    final skip =
        lh.getUint16(26, Endian.little) + lh.getUint16(28, Endian.little);
    raf.setPositionSync(local + 30 + skip);
    final data = raf.readSync(compSize);
    return switch (method) {
      0 => data,
      8 => Uint8List.fromList(ZLibDecoder(raw: true).convert(data)),
      _ => throw FormatException('Unsupported zip method $method'),
    };
  }
  return null;
}

// ── binary XML (AXML) ───────────────────────────────────────────────────────

const _resVersionCode = 0x0101021b;
const _resVersionName = 0x0101021c;
const _resDebuggable = 0x0101000f;
const _resMinSdk = 0x0101020c;

ApkInfo _parseManifest(Uint8List bytes) {
  final b = ByteData.sublistView(bytes);
  if (bytes.length < 8 || b.getUint16(0, Endian.little) != 0x0003) {
    throw const FormatException('Not a binary manifest');
  }
  var strings = <String>[];
  var resIds = <int>[];
  String? package, versionName;
  int? versionCode, minSdk;
  var debuggable = false;

  var p = b.getUint16(2, Endian.little);
  while (p + 8 <= bytes.length) {
    final type = b.getUint16(p, Endian.little);
    final headerSize = b.getUint16(p + 2, Endian.little);
    final size = b.getUint32(p + 4, Endian.little);
    if (size < 8 || p + size > bytes.length) break;
    switch (type) {
      case 0x0001:
        strings = _stringPool(b, p);
      case 0x0180:
        resIds = [
          for (var i = p + headerSize; i + 4 <= p + size; i += 4)
            b.getUint32(i, Endian.little),
        ];
      case 0x0102:
        final tag = _str(strings, b.getUint32(p + 20, Endian.little));
        final attrStart = b.getUint16(p + 24, Endian.little);
        final attrSize = b.getUint16(p + 26, Endian.little);
        final attrCount = b.getUint16(p + 28, Endian.little);
        for (var i = 0; i < attrCount; i++) {
          final a = p + headerSize + attrStart + i * attrSize;
          final nameIdx = b.getUint32(a + 4, Endian.little);
          final raw = b.getUint32(a + 8, Endian.little);
          final dataType = b.getUint8(a + 15);
          final data = b.getUint32(a + 16, Endian.little);
          final name = _str(strings, nameIdx);
          final res = nameIdx < resIds.length ? resIds[nameIdx] : 0;
          String? text() => dataType == 0x03
              ? _str(strings, data)
              : (raw != 0xffffffff ? _str(strings, raw) : null);
          int? number() => (dataType >= 0x10 && dataType <= 0x1f)
              ? data.toSigned(32)
              : int.tryParse(text() ?? '');
          if (tag == 'manifest') {
            if (name == 'package') package = text();
            if (res == _resVersionCode || name == 'versionCode') {
              versionCode = number();
            }
            if (res == _resVersionName || name == 'versionName') {
              versionName = text() ?? (number()?.toString());
            }
          } else if (tag == 'uses-sdk') {
            if (res == _resMinSdk || name == 'minSdkVersion') minSdk = number();
          } else if (tag == 'application') {
            if (res == _resDebuggable || name == 'debuggable') {
              debuggable = dataType == 0x12 ? data != 0 : text() == 'true';
            }
          }
        }
    }
    p += size;
  }
  if (package == null || versionCode == null) {
    throw const FormatException('Manifest has no package or versionCode');
  }
  return ApkInfo(
    package: package,
    versionCode: versionCode,
    versionName: versionName ?? '$versionCode',
    debuggable: debuggable,
    minSdk: minSdk,
  );
}

String _str(List<String> s, int i) => i < s.length ? s[i] : '';

List<String> _stringPool(ByteData b, int chunk) {
  final count = b.getUint32(chunk + 8, Endian.little);
  final utf8Pool = (b.getUint32(chunk + 16, Endian.little) & 0x100) != 0;
  final start = chunk + b.getUint32(chunk + 20, Endian.little);
  final out = <String>[];
  for (var i = 0; i < count; i++) {
    var o = start + b.getUint32(chunk + 28 + i * 4, Endian.little);
    if (utf8Pool) {
      // utf-16 length, then utf-8 length, each 1 or 2 bytes.
      o += (b.getUint8(o) & 0x80) != 0 ? 2 : 1;
      var n = b.getUint8(o);
      if ((n & 0x80) != 0) {
        n = ((n & 0x7f) << 8) | b.getUint8(o + 1);
        o += 2;
      } else {
        o += 1;
      }
      out.add(
        utf8.decode(Uint8List.sublistView(b, o, o + n), allowMalformed: true),
      );
    } else {
      var n = b.getUint16(o, Endian.little);
      if ((n & 0x8000) != 0) {
        n = ((n & 0x7fff) << 16) | b.getUint16(o + 2, Endian.little);
        o += 4;
      } else {
        o += 2;
      }
      out.add(
        String.fromCharCodes([
          for (var k = 0; k < n; k++) b.getUint16(o + k * 2, Endian.little),
        ]),
      );
    }
  }
  return out;
}
