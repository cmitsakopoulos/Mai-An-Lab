import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:mime/mime.dart';

/// One part of a multipart/form-data upload, streamed.
class FormPart {
  FormPart(this.name, this.filename, this.body);
  final String name;
  final String? filename;
  final Stream<List<int>> body;

  Future<String> text() => utf8.decodeStream(body);
}

/// The parts of [req], in order. Each part's [FormPart.body] must be
/// consumed (or skipped) before the next is requested.
Stream<FormPart> multipartParts(HttpRequest req) {
  final boundary = req.headers.contentType?.parameters['boundary'];
  if (boundary == null) {
    throw const FormatException('not a multipart request');
  }
  return MimeMultipartTransformer(boundary).bind(req).map((m) {
    final d = parseDisposition(m.headers['content-disposition'] ?? '');
    return FormPart(d.name, d.filename, m);
  });
}

/// `Content-Disposition: form-data; name="file"; filename*=UTF-8''%CE%A9.flac`.
/// Handles quoted values and RFC 5987 `filename*` (what the phone sends:
/// a plain percent-encoded filename would not survive Greek or accented
/// names).
({String name, String? filename}) parseDisposition(String header) {
  final params = <String, String>{};
  final re = RegExp(r'''([\w-]+\*?)\s*=\s*("(?:[^"\\]|\\.)*"|[^;]*)''');
  for (final m in re.allMatches(header)) {
    var v = m.group(2)!.trim();
    if (v.startsWith('"') && v.endsWith('"') && v.length >= 2) {
      v = v.substring(1, v.length - 1).replaceAllMapped(
        RegExp(r'\\(.)'),
        (x) => x.group(1)!,
      );
    }
    params[m.group(1)!.toLowerCase()] = v;
  }
  String? filename = params['filename'];
  final ext = params['filename*'];
  if (ext != null) {
    // charset'language'percent-encoded
    final i = ext.indexOf("'");
    final j = i < 0 ? -1 : ext.indexOf("'", i + 1);
    if (j > 0) {
      try {
        filename = Uri.decodeComponent(ext.substring(j + 1));
      } catch (_) {}
    }
  }
  return (name: params['name'] ?? '', filename: filename);
}

/// Write [body] to [file]; returns the byte count.
Future<int> saveStream(Stream<List<int>> body, File file) async {
  var n = 0;
  final sink = file.openWrite();
  try {
    await sink.addStream(
      body.map((c) {
        n += c.length;
        return c;
      }),
    );
  } finally {
    await sink.close();
  }
  return n;
}
