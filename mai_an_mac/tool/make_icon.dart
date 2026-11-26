// Draws the app icon: a dark macOS squircle with the Mai An yellow
// waveform. Pure Dart (signed-distance shapes, anti-aliased, written as a
// PNG), so the build needs no image tools beyond macOS's own sips/iconutil.
//
//   dart run tool/make_icon.dart OUT.png [SIZE]
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';

void main(List<String> args) {
  final out = args.isNotEmpty ? args[0] : 'icon.png';
  final n = args.length > 1 ? int.parse(args[1]) : 1024;
  File(out).writeAsBytesSync(png(n, n, draw(n)));
}

/// Signed distance to a rounded rectangle centred at (cx, cy).
double rrect(
  double x,
  double y,
  double cx,
  double cy,
  double hw,
  double hh,
  double r,
) {
  final qx = (x - cx).abs() - hw + r, qy = (y - cy).abs() - hh + r;
  final ox = math.max(qx, 0.0), oy = math.max(qy, 0.0);
  return math.sqrt(ox * ox + oy * oy) + math.min(math.max(qx, qy), 0.0) - r;
}

double clamp01(double v) => v < 0 ? 0 : (v > 1 ? 1 : v);

Uint8List draw(int n) {
  final s = n / 1024;
  final px = Uint8List(n * n * 4);
  // Apple's grid: an 824 body in a 1024 canvas, corner radius ~185.
  const body = 824 / 2, radius = 185.0, c = 512.0;
  // Waveform bars (x centre, half height), yellow.
  const bars = [
    (304.0, 84.0),
    (408.0, 190.0),
    (512.0, 270.0),
    (616.0, 150.0),
    (720.0, 64.0),
  ];
  const barHalfW = 34.0;
  for (var j = 0; j < n; j++) {
    for (var i = 0; i < n; i++) {
      final x = (i + .5) / s, y = (j + .5) / s;
      var r = 0.0, g = 0.0, b = 0.0, a = 0.0;
      void over(double sr, double sg, double sb, double sa) {
        final na = sa + a * (1 - sa);
        if (na <= 0) return;
        r = (sr * sa + r * a * (1 - sa)) / na;
        g = (sg * sa + g * a * (1 - sa)) / na;
        b = (sb * sa + b * a * (1 - sa)) / na;
        a = na;
      }

      // Soft shadow under the body.
      final ds = rrect(x, y, c, c + 14, body, body, radius);
      if (ds > -40) {
        final t = clamp01(1 - (ds + 10) / 46);
        over(0, 0, 0, 0.32 * t * t);
      }
      final d = rrect(x, y, c, c, body, body, radius);
      final cov = clamp01(0.5 - d * s);
      if (cov > 0) {
        // Near-black, lifting toward the top, with a faint warm glow
        // behind the waveform.
        final ty = (y - (c - body)) / (2 * body);
        var base = 0.13 - 0.10 * ty;
        final glow = math.exp(
          -(math.pow(x - 512, 2) + math.pow(y - 520, 2)) / (2 * 230 * 230),
        );
        over(base + 0.20 * glow, base + 0.16 * glow, base, cov);
        // Hairline rim, brighter at the top.
        final rim = clamp01(1 - (d + 3).abs() / 2.2) * (0.10 + 0.14 * (1 - ty));
        if (rim > 0) over(1, 1, 1, rim * cov);
        // The bars.
        var bd = double.infinity;
        for (final (bx, hh) in bars) {
          bd = math.min(bd, rrect(x, y, bx, 512, barHalfW, hh, barHalfW));
        }
        final bc = clamp01(0.5 - bd * s);
        if (bc > 0) {
          // Yellow with a slight vertical gradient.
          final k = clamp01((y - 244) / 536);
          over(1.0, 0.86 - 0.08 * k, 0.16 - 0.12 * k, bc * cov);
        }
      }
      final o = (j * n + i) * 4;
      px[o] = (r * 255).round().clamp(0, 255);
      px[o + 1] = (g * 255).round().clamp(0, 255);
      px[o + 2] = (b * 255).round().clamp(0, 255);
      px[o + 3] = (a * 255).round().clamp(0, 255);
    }
  }
  return px;
}

// ── PNG ─────────────────────────────────────────────────────────────────────

final _crcTable = List<int>.generate(256, (n) {
  var c = n;
  for (var k = 0; k < 8; k++) {
    c = (c & 1) != 0 ? 0xedb88320 ^ (c >> 1) : c >> 1;
  }
  return c;
});

int _crc(List<int> bytes) {
  var c = 0xffffffff;
  for (final b in bytes) {
    c = _crcTable[(c ^ b) & 0xff] ^ (c >> 8);
  }
  return c ^ 0xffffffff;
}

Uint8List png(int w, int h, Uint8List rgba) {
  final out = BytesBuilder();
  void chunk(String type, List<int> data) {
    final len = ByteData(4)..setUint32(0, data.length);
    final td = [...type.codeUnits, ...data];
    final crc = ByteData(4)..setUint32(0, _crc(td));
    out
      ..add(len.buffer.asUint8List())
      ..add(td)
      ..add(crc.buffer.asUint8List());
  }

  out.add([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  final ihdr = ByteData(13)
    ..setUint32(0, w)
    ..setUint32(4, h)
    ..setUint8(8, 8)
    ..setUint8(9, 6);
  chunk('IHDR', ihdr.buffer.asUint8List());
  final raw = BytesBuilder();
  for (var y = 0; y < h; y++) {
    raw.addByte(0);
    raw.add(Uint8List.sublistView(rgba, y * w * 4, (y + 1) * w * 4));
  }
  chunk('IDAT', ZLibEncoder(level: 9).convert(raw.toBytes()));
  chunk('IEND', const []);
  return out.toBytes();
}
