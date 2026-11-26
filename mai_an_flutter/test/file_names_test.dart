// File-name helpers. (The live checks of NFC, clean_filename and pathlib
// parts against Flet's Python are on the `flet` branch.)
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/downloads/companion_download.dart';
import 'package:mai_an_lab/downloads/file_names.dart';

void main() {
  test('companion file names follow the Mac name and its encode', () {
    expect(
      companionFileName(
        'http://h:1/api/v1/tracks/stream?path=%2Fm%2F01%20A.flac&quality=original&token=t',
      ),
      '01 A.flac',
    );
    expect(
      companionFileName(
        'http://h:1/api/v1/tracks/stream?path=%2Fm%2F01+A.flac&quality=aac_256k',
      ),
      '01 A.m4a',
    );
    expect(
      companionFileName('http://h:1/api/v1/tracks/stream?path=&quality=x'),
      'track.audio',
    );
    expect(
      companionFileName('http://h:1/api/v1/tracks/stream?path=%2Fm%2Fnoext'),
      'noext.flac',
    );
  });
}

String nfc(String s) => normalizeNfc(s);
