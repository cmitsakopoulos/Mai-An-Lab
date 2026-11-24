// StreamripApp/tests/test_storage_paths.py, ported: user-facing folders
// never resolve into the app's sandbox. (The pick_download_dir tests come
// with downloads, phase 6.)
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/scan/storage_paths.dart';

const pkgFiles = '/data/user/0/com.mitsacopoulos.maianlab/files';

const android = StoragePaths(
  StoragePlatform(
    isAndroid: true,
    realHome: '/storage/emulated/0',
    appHome: '$pkgFiles/flet/storage/data',
    privateRoots: [pkgFiles, '$pkgFiles/flet/storage/data'],
  ),
);

void main() {
  group('android sandbox paths are private', () {
    for (final p in [
      '$pkgFiles/Music', // old onboarding default
      '$pkgFiles/flet/storage/data/StreamripDownloads', // old download default
      '/data/data/com.mitsacopoulos.maianlab/files',
      '/storage/emulated/0/Android/data/com.mitsacopoulos.maianlab/files/Music',
      '/storage/emulated/0/Android/obb/com.mitsacopoulos.maianlab',
      '~/Music', // HOME is the data dir
    ]) {
      test(p, () {
        expect(android.isAppPrivate(p), isTrue);
        expect(android.usable(p), isFalse);
      });
    }
  });

  group('android shared storage is usable', () {
    for (final p in [
      '/storage/emulated/0/Music',
      '/storage/emulated/0/Download/Music',
      '/storage/emulated/0/Android/media/whatever', // readable by files apps
      '/storage/1234-ABCD/Music', // SD card
    ]) {
      test(p, () => expect(android.usable(p), isTrue));
    }
  });

  test('android defaults to public music', () {
    final d = android.defaultMusicDir();
    expect(
      d.startsWith('/storage/emulated/0') || d.startsWith('/sdcard'),
      isTrue,
    );
    expect(android.isAppPrivate(d), isFalse);
  });

  test('private stored paths fall back to public music', () {
    final lib = android.resolveLibraryDir('$pkgFiles/Music', '$pkgFiles/Music');
    expect(lib, android.defaultMusicDir());
    expect(
      android.resolveDownloadDir(
        '$pkgFiles/flet/storage/data/StreamripDownloads',
        lib,
      ),
      lib,
    );
  });

  test('download folder follows library', () {
    expect(
      android.resolveDownloadDir('', '/storage/emulated/0/Music/Lib'),
      '/storage/emulated/0/Music/Lib',
    );
    expect(
      android.resolveDownloadDir(
        '/storage/emulated/0/DL',
        '/storage/emulated/0/Music',
      ),
      '/storage/emulated/0/DL',
    );
  });

  test('library falls back to download folder', () {
    expect(
      android.resolveLibraryDir('', '/storage/emulated/0/DL'),
      '/storage/emulated/0/DL',
    );
    expect(
      android.resolveLibraryDir(' /storage/emulated/0/Lib ', null),
      '/storage/emulated/0/Lib',
    );
  });

  test('desktop home is not private even when it is the app dir', () {
    final home = Directory.systemTemp
        .createTempSync('home')
        .resolveSymbolicLinksSync();
    addTearDown(() => Directory(home).deleteSync(recursive: true));
    var sp = StoragePaths(
      StoragePlatform(isAndroid: false, realHome: home, privateRoots: [home]),
    );
    expect(sp.isAppPrivate('$home/Music'), isFalse);
    sp = StoragePaths(
      StoragePlatform(
        isAndroid: false,
        realHome: home,
        privateRoots: [home, '$home/.flet/storage'],
      ),
    );
    expect(sp.isAppPrivate('$home/.flet/storage/Music'), isTrue);
    expect(sp.isAppPrivate('$home/Music'), isFalse);
  });

  test('normpath', () {
    expect(normpath('/a//b/./c/../d/'), '/a/b/d');
    expect(normpath('//a'), '//a');
    expect(normpath('///a'), '/a');
    expect(normpath('/..'), '/');
    expect(normpath('a/../..'), '..');
  });
}
