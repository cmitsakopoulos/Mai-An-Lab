import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart' as crypto;

/// Clean-room pure-Dart cryptography for Deezer CDN streams and fallback URLs:
/// - Per-track Blowfish key derivation (MD5 + XOR with Deezer's constant)
/// - Blowfish-CBC decryption (Schneier 1993) with initial P/S tables derived
///   at runtime from Machin's formula for pi
/// - 6144-byte stripe stream decryption (2048 encrypted + 4096 plaintext)
/// - AES-128-ECB encryption (FIPS 197) for Deezer's encrypted fallback URL

const kDeezerBlowfishSecret = 'g4el58wc0zvf9na1';
const kDeezerEncryptChunkSize = 3 * 2048; // 6144 bytes

final Uint8List _kDeezerIv = Uint8List.fromList(const [0, 1, 2, 3, 4, 5, 6, 7]);
final Uint8List _kDeezerAesKey = Uint8List.fromList(
  utf8.encode('jo6aey6haid2Teih'),
);

/// Derive the 16-byte per-track Blowfish key from [trackId].
/// Matches `DeezerDownloadable._generate_blowfish_key`.
Uint8List deezerBlowfishKey(String trackId) {
  final md5Hex = crypto.md5.convert(utf8.encode(trackId)).toString();
  final out = Uint8List(16);
  for (var i = 0; i < 16; i++) {
    out[i] =
        md5Hex.codeUnitAt(i) ^
        md5Hex.codeUnitAt(i + 16) ^
        kDeezerBlowfishSecret.codeUnitAt(i);
  }
  return out;
}

/// Decrypt a 2048-byte (or any multiple-of-8-byte) Deezer Blowfish-CBC stripe.
Uint8List deezerBlowfishCbcDecrypt(Uint8List key, Uint8List data) {
  if (data.length % 8 != 0) {
    throw ArgumentError('Blowfish CBC data length must be a multiple of 8');
  }
  final bf = _Blowfish(key);
  return bf.decryptCbc(data, _kDeezerIv);
}

/// Stream-decrypt a Deezer CDN response into [sink], keeping peak memory to
/// one 6144-byte block. Mirrors `DeezerDownloadable._download`.
Future<void> deezerDecryptStripeStream({
  required Uint8List key,
  required Stream<List<int>> chunks,
  required IOSink sink,
  required void Function(int bytes) onBytes,
  bool Function()? isCancelled,
}) async {
  final bf = _Blowfish(key);
  final carry = BytesBuilder(copy: false);

  await for (final data in chunks) {
    if (isCancelled != null && isCancelled()) return;
    if (data.isEmpty) continue;
    carry.add(data);
    onBytes(data.length);
    if (carry.length >= kDeezerEncryptChunkSize) {
      final buf = carry.takeBytes();
      var offset = 0;
      while (buf.length - offset >= kDeezerEncryptChunkSize) {
        final enc = Uint8List.sublistView(buf, offset, offset + 2048);
        sink.add(bf.decryptCbc(enc, _kDeezerIv));
        sink.add(
          Uint8List.sublistView(
            buf,
            offset + 2048,
            offset + kDeezerEncryptChunkSize,
          ),
        );
        offset += kDeezerEncryptChunkSize;
      }
      if (offset < buf.length) {
        carry.add(Uint8List.sublistView(buf, offset));
      }
    }
  }

  if (carry.isNotEmpty) {
    final rem = carry.takeBytes();
    if (rem.length >= 2048) {
      final enc = Uint8List.sublistView(rem, 0, 2048);
      sink.add(bf.decryptCbc(enc, _kDeezerIv));
      if (rem.length > 2048) {
        sink.add(Uint8List.sublistView(rem, 2048));
      }
    } else {
      sink.add(rem);
    }
  }
}

/// Compute Deezer's legacy AES-128-ECB encrypted CDN URL fallback.
/// Matches `DeezerClient._get_encrypted_file_url`.
String deezerEncryptedFileUrl({
  required String trackId,
  required String md5Origin,
  required String mediaVersion,
  int formatNumber = 1,
}) {
  const sep = 0xa4;
  final urlBytes = BytesBuilder(copy: false)
    ..add(utf8.encode(md5Origin))
    ..addByte(sep)
    ..add(utf8.encode('$formatNumber'))
    ..addByte(sep)
    ..add(utf8.encode(trackId))
    ..addByte(sep)
    ..add(utf8.encode(mediaVersion));
  final rawUrlBytes = urlBytes.takeBytes();
  final urlHash = crypto.md5.convert(rawUrlBytes).toString();

  final info = BytesBuilder(copy: false)
    ..add(utf8.encode(urlHash))
    ..addByte(sep)
    ..add(rawUrlBytes)
    ..addByte(sep);
  final padLen = 16 - (info.length % 16);
  for (var i = 0; i < padLen; i++) {
    info.addByte(0x2e); // '.'
  }
  final encrypted = aes128EcbEncrypt(_kDeezerAesKey, info.takeBytes());
  final hexBuf = StringBuffer();
  for (final b in encrypted) {
    hexBuf.write(b.toRadixString(16).padLeft(2, '0'));
  }
  return 'https://e-cdns-proxy-${md5Origin[0]}.dzcdn.net/mobile/1/$hexBuf';
}

/// Encrypt [plaintext] (length multiple of 16) with AES-128 in ECB mode.
Uint8List aes128EcbEncrypt(Uint8List key, Uint8List plaintext) {
  if (key.length != 16) {
    throw ArgumentError('AES-128 requires a 16-byte key');
  }
  if (plaintext.length % 16 != 0) {
    throw ArgumentError('AES-ECB plaintext length must be a multiple of 16');
  }
  final roundKeys = _aesExpandKey128(key);
  final out = Uint8List(plaintext.length);
  for (var off = 0; off < plaintext.length; off += 16) {
    _aesEncryptBlock(plaintext, off, out, off, roundKeys);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Blowfish (Schneier 1993) with pi-derived initial P/S tables.

class _Blowfish {
  _Blowfish(Uint8List key)
    : _p = Uint32List.fromList(_piTables.$1),
      _s0 = Uint32List.fromList(_piTables.$2),
      _s1 = Uint32List.fromList(_piTables.$3),
      _s2 = Uint32List.fromList(_piTables.$4),
      _s3 = Uint32List.fromList(_piTables.$5) {
    if (key.isEmpty || key.length > 56) {
      throw ArgumentError('Blowfish key must be 1..56 bytes');
    }
    var j = 0;
    for (var i = 0; i < 18; i++) {
      var word = 0;
      for (var k = 0; k < 4; k++) {
        word = ((word << 8) | key[j]) & 0xFFFFFFFF;
        j = (j + 1) % key.length;
      }
      _p[i] = (_p[i] ^ word) & 0xFFFFFFFF;
    }

    var l = 0;
    var r = 0;
    for (var i = 0; i < 18; i += 2) {
      (l, r) = _encryptPair(l, r);
      _p[i] = l;
      _p[i + 1] = r;
    }
    for (final s in [_s0, _s1, _s2, _s3]) {
      for (var i = 0; i < 256; i += 2) {
        (l, r) = _encryptPair(l, r);
        s[i] = l;
        s[i + 1] = r;
      }
    }
  }

  final Uint32List _p;
  final Uint32List _s0;
  final Uint32List _s1;
  final Uint32List _s2;
  final Uint32List _s3;

  int _f(int x) {
    final a = (x >>> 24) & 0xFF;
    final b = (x >>> 16) & 0xFF;
    final c = (x >>> 8) & 0xFF;
    final d = x & 0xFF;
    var y = (_s0[a] + _s1[b]) & 0xFFFFFFFF;
    y = (y ^ _s2[c]) & 0xFFFFFFFF;
    return (y + _s3[d]) & 0xFFFFFFFF;
  }

  (int, int) _encryptPair(int l, int r) {
    for (var i = 0; i < 16; i++) {
      l = (l ^ _p[i]) & 0xFFFFFFFF;
      r = (r ^ _f(l)) & 0xFFFFFFFF;
      final tmp = l;
      l = r;
      r = tmp;
    }
    final tmp = l;
    l = r;
    r = tmp;
    r = (r ^ _p[16]) & 0xFFFFFFFF;
    l = (l ^ _p[17]) & 0xFFFFFFFF;
    return (l, r);
  }

  (int, int) _decryptPair(int l, int r) {
    for (var i = 17; i > 1; i--) {
      l = (l ^ _p[i]) & 0xFFFFFFFF;
      r = (r ^ _f(l)) & 0xFFFFFFFF;
      final tmp = l;
      l = r;
      r = tmp;
    }
    final tmp = l;
    l = r;
    r = tmp;
    r = (r ^ _p[1]) & 0xFFFFFFFF;
    l = (l ^ _p[0]) & 0xFFFFFFFF;
    return (l, r);
  }

  Uint8List decryptCbc(Uint8List data, Uint8List iv) {
    final out = Uint8List(data.length);
    final inView = ByteData.sublistView(data);
    final outView = ByteData.sublistView(out);
    final ivView = ByteData.sublistView(iv);
    var prevL = ivView.getUint32(0, Endian.big);
    var prevR = ivView.getUint32(4, Endian.big);

    for (var off = 0; off < data.length; off += 8) {
      final cL = inView.getUint32(off, Endian.big);
      final cR = inView.getUint32(off + 4, Endian.big);
      final (dL, dR) = _decryptPair(cL, cR);
      outView.setUint32(off, (dL ^ prevL) & 0xFFFFFFFF, Endian.big);
      outView.setUint32(off + 4, (dR ^ prevR) & 0xFFFFFFFF, Endian.big);
      prevL = cL;
      prevR = cR;
    }
    return out;
  }
}

/// Lazily computes the 18 + 4*256 = 1042 32-bit fractional words of pi
/// using Machin's formula: pi/4 = 4*atan(1/5) - atan(1/239).
final (Uint32List, Uint32List, Uint32List, Uint32List, Uint32List) _piTables =
    _computePiTables();

(Uint32List, Uint32List, Uint32List, Uint32List, Uint32List)
_computePiTables() {
  const totalWords = 18 + 1024;
  const guardBits = 64;
  final scale = BigInt.one << (totalWords * 32 + guardBits);

  BigInt atanRecip(int m) {
    final mBig = BigInt.from(m);
    final m2 = mBig * mBig;
    var power = scale ~/ mBig;
    var sum = power;
    var k = 3;
    var negative = true;
    while (power > BigInt.zero) {
      power = power ~/ m2;
      final term = power ~/ BigInt.from(k);
      if (term == BigInt.zero) break;
      sum = negative ? sum - term : sum + term;
      negative = !negative;
      k += 2;
    }
    return sum;
  }

  final piScaled = (atanRecip(5) * BigInt.from(4) - atanRecip(239)) * BigInt.from(4);
  final fracBits = (piScaled - BigInt.from(3) * scale) >> guardBits;
  final mask32 = BigInt.from(0xFFFFFFFF);

  final words = Uint32List(totalWords);
  var remaining = fracBits;
  for (var i = totalWords - 1; i >= 0; i--) {
    words[i] = (remaining & mask32).toInt();
    remaining = remaining >> 32;
  }

  return (
    Uint32List.fromList(words.sublist(0, 18)),
    Uint32List.fromList(words.sublist(18, 274)),
    Uint32List.fromList(words.sublist(274, 530)),
    Uint32List.fromList(words.sublist(530, 786)),
    Uint32List.fromList(words.sublist(786, 1042)),
  );
}

// ---------------------------------------------------------------------------
// AES-128 (FIPS 197) with algebraically generated GF(2^8) S-box.

final Uint8List _aesSbox = _computeAesSbox();

Uint8List _computeAesSbox() {
  int gfMul(int a, int b) {
    var p = 0;
    var aa = a;
    var bb = b;
    for (var i = 0; i < 8; i++) {
      if ((bb & 1) != 0) p ^= aa;
      final hi = (aa & 0x80) != 0;
      aa = (aa << 1) & 0xFF;
      if (hi) aa ^= 0x1B;
      bb >>= 1;
    }
    return p;
  }

  int gfInv(int a) {
    if (a == 0) return 0;
    var res = 1;
    var base = a;
    var exp = 254;
    while (exp > 0) {
      if ((exp & 1) != 0) res = gfMul(res, base);
      base = gfMul(base, base);
      exp >>= 1;
    }
    return res;
  }

  int rotl8(int v, int shift) => ((v << shift) | (v >> (8 - shift))) & 0xFF;

  final box = Uint8List(256);
  for (var i = 0; i < 256; i++) {
    final x = gfInv(i);
    box[i] = x ^ rotl8(x, 1) ^ rotl8(x, 2) ^ rotl8(x, 3) ^ rotl8(x, 4) ^ 0x63;
  }
  return box;
}

Uint8List _aesExpandKey128(Uint8List key) {
  final rk = Uint8List(176);
  rk.setRange(0, 16, key);
  var rcon = 1;
  for (var i = 16; i < 176; i += 4) {
    var t0 = rk[i - 4];
    var t1 = rk[i - 3];
    var t2 = rk[i - 2];
    var t3 = rk[i - 1];
    if (i % 16 == 0) {
      final tmp = t0;
      t0 = _aesSbox[t1] ^ rcon;
      t1 = _aesSbox[t2];
      t2 = _aesSbox[t3];
      t3 = _aesSbox[tmp];
      rcon = (rcon << 1) ^ ((rcon & 0x80) != 0 ? 0x1B : 0);
      rcon &= 0xFF;
    }
    rk[i] = rk[i - 16] ^ t0;
    rk[i + 1] = rk[i - 15] ^ t1;
    rk[i + 2] = rk[i - 14] ^ t2;
    rk[i + 3] = rk[i - 13] ^ t3;
  }
  return rk;
}

int _xtime(int a) => ((a << 1) ^ ((a & 0x80) != 0 ? 0x1B : 0)) & 0xFF;

void _aesEncryptBlock(
  Uint8List input,
  int inOff,
  Uint8List output,
  int outOff,
  Uint8List rk,
) {
  final s = Uint8List(16);
  for (var i = 0; i < 16; i++) {
    s[i] = input[inOff + i] ^ rk[i];
  }

  for (var round = 1; round <= 10; round++) {
    // SubBytes
    for (var i = 0; i < 16; i++) {
      s[i] = _aesSbox[s[i]];
    }
    // ShiftRows (column-major state)
    var tmp = s[1];
    s[1] = s[5];
    s[5] = s[9];
    s[9] = s[13];
    s[13] = tmp;

    tmp = s[2];
    s[2] = s[10];
    s[10] = tmp;
    tmp = s[6];
    s[6] = s[14];
    s[14] = tmp;

    tmp = s[15];
    s[15] = s[11];
    s[11] = s[7];
    s[7] = s[3];
    s[3] = tmp;

    // MixColumns (rounds 1..9)
    if (round < 10) {
      for (var c = 0; c < 16; c += 4) {
        final a0 = s[c], a1 = s[c + 1], a2 = s[c + 2], a3 = s[c + 3];
        final t = a0 ^ a1 ^ a2 ^ a3;
        s[c] = a0 ^ t ^ _xtime(a0 ^ a1);
        s[c + 1] = a1 ^ t ^ _xtime(a1 ^ a2);
        s[c + 2] = a2 ^ t ^ _xtime(a2 ^ a3);
        s[c + 3] = a3 ^ t ^ _xtime(a3 ^ a0);
      }
    }

    // AddRoundKey
    final rOff = round * 16;
    for (var i = 0; i < 16; i++) {
      s[i] ^= rk[rOff + i];
    }
  }

  output.setRange(outOff, outOff + 16, s);
}
