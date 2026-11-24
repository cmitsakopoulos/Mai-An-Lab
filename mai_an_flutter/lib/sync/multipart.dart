import 'dart:convert';
import 'dart:io';
import 'dart:math';

import '../core/py_text.dart';

/// One part of a multipart/form-data body: a text field or a file streamed
/// from disk.
sealed class FormPart {
  const FormPart(this.name);
  final String name;
}

class TextPart extends FormPart {
  const TextPart(super.name, this.value, {this.contentType});
  final String value;

  /// aiohttp's FormData sends `text/plain; charset=utf-8` for a str field
  /// unless told otherwise (`meta` goes as application/json).
  final String? contentType;
}

class FilePart extends FormPart {
  const FilePart(
    super.name,
    this.file, {
    required this.filename,
    this.contentType = 'application/octet-stream',
  });
  final File file;
  final String filename;
  final String contentType;
}

/// multipart/form-data as aiohttp's FormData writes it, streamed with a
/// Content-Length so a whole FLAC never sits in memory.
///
/// One deliberate difference (PORT_MAP §7): the file name goes as RFC 5987
/// `filename*=UTF-8''…`. aiohttp's client percent-encodes a plain
/// `filename="…"` and its server never decodes that form, so Flet's Send to
/// Mac saves "Άλφα.flac" as "%CE%86%CE%BB%CF%86%CE%B1.flac". The server
/// decodes `filename*`, which is the form RFC 7578 allows for this.
class MultipartBody {
  MultipartBody(this.parts) : boundary = _boundary();

  final List<FormPart> parts;
  final String boundary;

  String get contentType => 'multipart/form-data; boundary=$boundary';

  static String _boundary() {
    final r = Random.secure();
    return List.generate(32, (_) => r.nextInt(16).toRadixString(16)).join();
  }

  List<int> _head(FormPart p) {
    final b = StringBuffer('--$boundary\r\n');
    switch (p) {
      case TextPart():
        b
          ..write(
            'Content-Type: ${p.contentType ?? 'text/plain; charset=utf-8'}\r\n',
          )
          ..write('Content-Disposition: form-data; name="${p.name}"\r\n');
      case FilePart():
        b
          ..write('Content-Type: ${p.contentType}\r\n')
          ..write(
            'Content-Disposition: form-data; name="${p.name}"; '
            "filename*=utf-8''${pyQuote(p.filename, safe: '')}\r\n",
          );
    }
    b.write('\r\n');
    return utf8.encode(b.toString());
  }

  List<int> get _tail => utf8.encode('--$boundary--\r\n');

  Future<int> length() async {
    var n = _tail.length;
    for (final p in parts) {
      n += _head(p).length + 2; // + the CRLF after the content
      n += switch (p) {
        TextPart() => utf8.encode(p.value).length,
        FilePart() => await p.file.length(),
      };
    }
    return n;
  }

  Stream<List<int>> stream() async* {
    const crlf = [13, 10];
    for (final p in parts) {
      yield _head(p);
      switch (p) {
        case TextPart():
          yield utf8.encode(p.value);
        case FilePart():
          yield* p.file.openRead();
      }
      yield crlf;
    }
    yield _tail;
  }
}
