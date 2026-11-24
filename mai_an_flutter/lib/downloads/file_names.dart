import 'dart:convert';

import '../core/nfc.dart';
import '../core/py_text.dart';

/// filepath_utils.py's file-name helpers, written from their behaviour.

/// `normalize_nfc`: composed Unicode, so a name the Mac sent decomposed
/// (macOS NFD) matches the same name typed or scanned on the phone.
String normalizeNfc(String? text) => nfc(text ?? '');

final _illegal = RegExp(r'[\\/*?:"<>|]');

/// `clean_filename` (restrict off): drop the characters Windows or Unix
/// forbid, cut to 255 UTF-8 bytes (an incomplete last character is
/// dropped), strip whitespace, NFC.
String cleanFilename(String fn) {
  final sanitized = nfc(fn.replaceAll(_illegal, ''));
  return nfc(pyStrip(_truncate255(sanitized)));
}

/// `truncate_str`: the first 255 bytes of the UTF-8, decoded ignoring the
/// cut character.
String _truncate255(String s) {
  final bytes = utf8.encode(s);
  if (bytes.length <= 255) return s;
  var end = 255;
  // Back off to the start of the character the cut fell in.
  while (end > 0 && (bytes[end] & 0xC0) == 0x80) {
    end--;
  }
  return utf8.decode(bytes.sublist(0, end));
}

/// `Path(p).name`, `.stem` and `.suffix` for a POSIX path, as pathlib
/// reads them (trailing slashes and `.` parts dropped).
({String name, String stem, String suffix}) pyPathParts(String p) {
  final parts = p.split('/').where((x) => x.isNotEmpty && x != '.').toList();
  final name = parts.isEmpty ? '' : parts.last;
  final i = name.lastIndexOf('.');
  if (i > 0 && i < name.length - 1) {
    return (name: name, stem: name.substring(0, i), suffix: name.substring(i));
  }
  return (name: name, stem: name, suffix: '');
}
