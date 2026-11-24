// The scan as the Library screen runs it: banner state, the reload of
// every view, and library.py's completion messages.
import 'dart:io';

import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:maian_native/maian_native.dart';
import 'package:mai_an_lab/core/progress_notify.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/browse_controller.dart';
import 'package:mai_an_lab/library/library_controller.dart';

class _Alerts implements NotificationBridge {
  final shown = <(String, String)>[];
  final cancelled = <String>[];
  @override
  Stream<NotificationAction> get actions => const Stream.empty();
  @override
  Future<void> showProgress({
    required String job,
    required String title,
    required String content,
    required int progress,
    required int total,
    required bool done,
    required bool cancellable,
    String? actionLabel,
    String? actionTarget,
  }) async {}
  @override
  Future<void> showAlert({
    required String key,
    required String title,
    required String content,
    String navigateTo = '',
    String? actionLabel,
    String? actionTarget,
    bool urgent = false,
    String icon = '',
  }) async => shown.add((key, content));
  @override
  Future<void> cancelAlert(String key) async => cancelled.add(key);
}

void main() {
  late Directory dir;

  setUp(() => dir = Directory.systemTemp.createTempSync('scanctl'));
  tearDown(() => dir.deleteSync(recursive: true));

  void put(String rel, String src) {
    final f = File('${dir.path}/music/$rel')
      ..parent.createSync(recursive: true);
    f.writeAsBytesSync(File('test/goldens/tags/$src').readAsBytesSync());
  }

  test('scan indexes, reloads every view, then reports', () async {
    put('a/one.flac', 'tagged.flac');
    put('a/two.mp3', 'id3v22.mp3');
    put('skip.mp3', 'plain_cbr.mp3'); // no title, no artist
    final c = LibraryController(
      () => LibraryRepository.open('${dir.path}/library.db', writable: true),
      libraryFolder: () async => '${dir.path}/music',
    );
    final browse = BrowseController(c, views: LibraryView.values);
    await c.load();
    expect(c.total, 0);
    expect(c.canScan, isTrue);

    final messages = <String>[];
    c.messages.listen(messages.add);
    final states = <String>[];
    c.addListener(() {
      if (c.scanning) states.add(c.scanStatus);
    });
    final gen = c.dataGeneration;
    await c.scan();

    expect(c.scanning, isFalse);
    expect(states.first, 'Initializing active scan…');
    expect(states, contains('Searching: Calculating library size...'));
    expect(states.last, startsWith('3/3 — '));
    expect(c.total, 2);
    expect(c.dataGeneration, gen + 1);
    expect({for (final t in c.allTracks) t.title}, {'Flac Title', 'Old Title'});

    browse.setView(LibraryView.artists);
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(browse.total, 2);

    // Nothing changed on disk, but the untagged file never got a row, so
    // it is read (and counted) again, as in Flet.
    await c.scan();
    File('${dir.path}/music/skip.mp3').deleteSync();
    await c.scan();
    await Future<void>.delayed(Duration.zero);
    expect(messages, [
      'Scan complete. Indexed 3 items.',
      'Scan complete. Indexed 1 items.',
      'Library is up to date.',
    ]);
    browse.dispose();
    c.dispose();
    await _closed();
  });

  test(
    'a quiet scan: no banner, no snackbar, the "not analysed" card',
    () async {
      final alerts = _Alerts();
      final saved = NotificationBridge.instance;
      NotificationBridge.instance = alerts;
      addTearDown(() => NotificationBridge.instance = saved);
      put('a/one.flac', 'tagged.flac');
      put('a/two.mp3', 'id3v22.mp3');
      final c = LibraryController(
        () => LibraryRepository.open('${dir.path}/library.db', writable: true),
        libraryFolder: () async => '${dir.path}/music',
      );
      await c.load();
      final messages = <String>[];
      c.messages.listen(messages.add);
      var banner = false;
      c.addListener(() => banner |= c.scanning);

      await c.scan(quiet: true);
      await Future<void>.delayed(Duration.zero);
      expect(banner, isFalse);
      expect(messages, isEmpty);
      expect(c.total, 2);
      expect(alerts.shown, [
        (
          'scan',
          "2 tracks are not analysed yet, so auto-play and similar-track "
              "picks can't use them.",
        ),
      ]);

      // Features that came with a transfer (server.py persist_features), in
      // the wire form stored_features reads back.
      final repo = c.repository!;
      final path = c.allTracks.first.path;
      final wire = <String, Object?>{
        'bpm': 120.5,
        'energy': 0.25,
        'brightness': 2000.0,
        'rolloff': 0.5,
        'beat_strength': 0.75,
        'spectral_flatness': 0.125,
        'spectral_contrast': 3.5,
        'key_index': 7,
        'timbre_b64': 'AAECAw==',
        'features_version': kFeaturesVersion,
      };
      await repo.persistFeatures(path, wire);
      expect(await repo.storedFeatures(path), wire);

      put('b/three.flac', 'tagged.flac');
      await c.scan(quiet: true);
      expect(alerts.shown.last.$2, startsWith('2 tracks are not analysed'));
      await repo.persistFeatures(c.allTracks[0].path, wire);
      await repo.persistFeatures(c.allTracks[1].path, wire);
      await repo.persistFeatures(c.allTracks[2].path, wire);
      File('${dir.path}/music/b/three.flac').deleteSync();
      put('b/four.flac', 'tagged.flac');
      await c.scan(quiet: true);
      // Only the new file is unanalysed now.
      expect(
        alerts.shown.last.$2,
        "1 track is not analysed yet, so auto-play and similar-track picks "
        "can't use it.",
      );
      c.dispose();
      await _closed();
    },
  );

  test('a read-only library has no scan', () async {
    final c = LibraryController(() async {
      final p = '${dir.path}/ro.db';
      await (await LibraryRepository.open(p, writable: true)).close();
      return LibraryRepository.open(p);
    }, libraryFolder: () async => dir.path);
    await c.load();
    expect(c.canScan, isFalse);
    c.dispose();
    await _closed();
  });
}

/// `dispose` closes the database without waiting; SQLite removes its -wal
/// and -shm files as it does, which races the temp folder's deletion.
Future<void> _closed() =>
    Future<void>.delayed(const Duration(milliseconds: 50));
