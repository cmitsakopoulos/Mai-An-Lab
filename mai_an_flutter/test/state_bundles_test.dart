// Phase 4f: the app's state bundles. The service (export, import with the
// library reopened, the boot snapshot and the boot import) on real files
// and isolates, and Settings → Advanced's Export / Import with the bundle
// browser.
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/app_config.dart';
import 'package:mai_an_lab/core/config_file.dart';
import 'package:mai_an_lab/core/haptics.dart';
import 'package:mai_an_lab/core/zip_archive.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/data/state_export.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/settings/config_controller.dart';
import 'package:mai_an_lab/settings/folder_browser.dart';
import 'package:mai_an_lab/settings/settings_page.dart';
import 'package:mai_an_lab/sync/state_bundles.dart';
import 'package:mai_an_lab/ui/tokens.dart';

final _template = File('assets/config_template.toml').readAsStringSync();

Future<void> _makeDb(String path, List<String> titles) async {
  final repo = await LibraryRepository.open(path, writable: true);
  await repo.debugExecute([
    ("INSERT INTO artists (id, name) VALUES (1, 'A')", const []),
    ("INSERT INTO albums (id, artist_id, title) VALUES (1, 1, 'Al')", const []),
    for (final (i, t) in titles.indexed)
      (
        'INSERT INTO tracks (album_id, title, path, added_date) '
            'VALUES (1, ?, ?, ?)',
        [t, '/m/$i.flac', i],
      ),
  ]);
  await repo.close();
}

/// Records what Settings asks for; no isolates (widget tests run on a fake
/// clock).
class _FakeBundles extends StateBundles {
  _FakeBundles(String dataDir, ConfigController config)
    : super(
        dataDir: dataDir,
        dbPath: '$dataDir/library.db',
        library: LibraryController(() => throw UnimplementedError()),
        config: config,
      );

  final calls = <String>[];
  Object? importError;

  @override
  Future<String> export({String? outDir}) async {
    calls.add('export $outDir');
    return '$outDir/mai_an_lab_state_20261124_120000.zip';
  }

  @override
  Future<ImportResult> import(String zipPath) async {
    calls.add('import $zipPath');
    if (importError case final e?) throw e;
    return (
      replaced: {'library.db': 'x', 'config.toml': 'y'},
      manifest: <String, Object?>{},
    );
  }
}

void main() {
  late Directory dir;
  late String dataDir, dbPath;

  setUp(() async {
    dir = Directory.systemTemp.createTempSync('state_bundles_test');
    dataDir = '${dir.path}/data';
    Directory(dataDir).createSync();
    dbPath = '$dataDir/library.db';
  });
  tearDown(() => dir.deleteSync(recursive: true));

  test("Python's .0f: half to even", () {
    expect(bundleSizeLabel(0), '0 KB');
    expect(bundleSizeLabel(511), '0 KB');
    expect(bundleSizeLabel(512), '0 KB');
    expect(bundleSizeLabel(513), '1 KB');
    expect(bundleSizeLabel(1536), '2 KB');
    expect(bundleSizeLabel(2560), '2 KB');
    expect(bundleSizeLabel(1700000), '1660 KB');
  });

  group('the service', () {
    late ConfigController config;
    late LibraryController library;
    late StateBundles bundles;
    late String downloads, musicDir;

    setUp(() async {
      downloads = '${dir.path}/Download';
      musicDir = '${dir.path}/Music';
      Directory(downloads).createSync();
      Directory(musicDir).createSync();
      await _makeDb(dbPath, ['Old']);
      config = await ConfigController.load(
        ConfigStore(dataDir, template: () async => _template),
      );
      library = LibraryController(
        () => LibraryRepository.open(dbPath, writable: true),
      );
      await library.load();
      bundles = StateBundles(
        dataDir: dataDir,
        dbPath: dbPath,
        library: library,
        config: config,
        libraryFolder: () async => musicDir,
        bundleDir: () => downloads,
      );
    });
    // The database closed before the folder goes (dispose doesn't wait,
    // and SQLite removes its -wal and -shm files on close).
    tearDown(() async {
      await library.repository?.close();
      library.dispose();
    });

    /// A bundle from another phone: two tracks and its own accent.
    Future<String> otherBundle() async {
      final other = '${dir.path}/other';
      Directory(other).createSync();
      await _makeDb('$other/library.db', ['New 1', 'New 2']);
      File('$other/config.toml').writeAsStringSync(
        _template.replaceFirst(
          RegExp(r'accent_color = "[^"]*"'),
          'accent_color = "#FF2D55"',
        ),
      );
      return exportState(
        dbPath: '$other/library.db',
        configPath: '$other/config.toml',
        outDir: other,
      );
    }

    test('export: a timestamped bundle in the default folder', () async {
      final out = await bundles.export();
      expect(out, startsWith('$downloads/mai_an_lab_state_'));
      final z = ZipReader(out);
      expect(z.names, ['library.db', 'config.toml', 'manifest.json']);
      z.close();
    });

    test('import: the library reopens on the new database, the config is '
        'read again', () async {
      expect(library.allTracks.map((t) => t.title), ['Old']);
      final gen = library.dataGeneration;
      final r = await bundles.import(await otherBundle());
      expect(r.replaced.keys, ['library.db', 'config.toml']);
      expect(library.allTracks.map((t) => t.title).toSet(), {'New 1', 'New 2'});
      expect(library.dataGeneration, greaterThan(gen));
      expect(library.canWrite, isTrue);
      expect(config.config.accent, const Color(0xFFFF2D55));
    });

    test('a failed import leaves the library as it was', () async {
      final junk = File('${dir.path}/junk.zip')..writeAsStringSync('no');
      await expectLater(bundles.import(junk.path), throwsA(isA<BadZipFile>()));
      expect(library.allTracks.map((t) => t.title), ['Old']);
      expect(library.canWrite, isTrue);
    });

    test('the boot snapshot: Downloads, then a copy in the library '
        'folder; the data dir when Downloads is missing', () async {
      final out = await bundles.autoExport();
      expect(out, '$downloads/$kSnapshotName');
      expect(inspectBundle(out!)['auto_snapshot'], isTrue);
      expect(File('$musicDir/$kSnapshotName').existsSync(), isTrue);

      Directory(downloads).deleteSync(recursive: true);
      File('$musicDir/$kSnapshotName').deleteSync();
      expect(await bundles.autoExport(), '$dataDir/$kSnapshotName');
      expect(File('$musicDir/$kSnapshotName').existsSync(), isTrue);
    });

    test('overlapping boot snapshots run one at a time', () async {
      final outs = await Future.wait([
        for (var i = 0; i < 4; i++) bundles.autoExport(),
      ]);
      expect(outs.toSet(), {'$downloads/$kSnapshotName'});
      expect(Directory(downloads).listSync(), hasLength(1));
    });

    test('a failed boot snapshot is only logged', () async {
      // Neither folder can be made: under a file.
      final b = StateBundles(
        dataDir: '$dbPath/data',
        dbPath: dbPath,
        library: library,
        config: config,
        bundleDir: () => '$dbPath/Download',
      );
      expect(await b.autoExport(), isNull);
    });
  });

  group('the boot import', () {
    late String a, b;
    setUp(() async {
      a = '${dir.path}/sdcard';
      b = '${dir.path}/emulated';
      Directory(a).createSync();
      Directory(b).createSync();
      await _makeDb(dbPath, ['Old']);
    });

    Future<bool> boot() =>
        StateBundles.bootImport(dataDir: dataDir, dbPath: dbPath, dirs: [a, b]);

    test('nothing there, or an empty file: nothing happens', () async {
      expect(await boot(), isFalse);
      File('$a/$kAutoImportName').createSync();
      File('$b/$kAutoImportName').createSync();
      expect(await boot(), isFalse);
      expect(File('$a/$kAutoImportName').existsSync(), isTrue);
      expect(File('$dataDir/.onboarded').existsSync(), isFalse);
    });

    test('a bundle is ingested, deleted, and marks the install onboarded '
        '(the second folder when the first has none)', () async {
      final other = '${dir.path}/other';
      Directory(other).createSync();
      await _makeDb('$other/library.db', ['New']);
      File('$other/config.toml').writeAsStringSync('# restored\n');
      final zip = await exportState(
        dbPath: '$other/library.db',
        configPath: '$other/config.toml',
        outDir: other,
      );
      File(zip).renameSync('$b/$kAutoImportName');
      expect(await boot(), isTrue);
      expect(File('$b/$kAutoImportName').existsSync(), isFalse);
      expect(File('$dataDir/.onboarded').readAsStringSync(), '1');
      expect(
        File(AppConfig.pathFor(dataDir)).readAsStringSync(),
        '# restored\n',
      );
      final repo = await LibraryRepository.open(dbPath);
      expect((await repo.allTracks()).map((t) => t.title), ['New']);
      await repo.close();
    });

    test('a bad bundle stays for the next start', () async {
      File('$a/$kAutoImportName').writeAsStringSync('not a zip');
      expect(await boot(), isFalse);
      expect(File('$a/$kAutoImportName').existsSync(), isTrue);
      expect(File('$dataDir/.onboarded').existsSync(), isFalse);
    });
  });

  group('Settings → Advanced', () {
    late ConfigController config;
    late _FakeBundles bundles;
    late String appStorage;

    Future<void> pump(WidgetTester tester) async {
      tester.view.physicalSize = const Size(390 * 3, 844 * 3);
      tester.view.devicePixelRatio = 3;
      addTearDown(tester.view.reset);
      appStorage = dataDir;
      await tester.runAsync(() async {
        config = await ConfigController.load(
          ConfigStore(dataDir, template: () async => _template),
        );
      });
      bundles = _FakeBundles(dataDir, config);
      await tester.pumpWidget(
        MaterialApp(
          theme: Tokens.theme(),
          home: SettingsScreen(
            initial: SettingsPage.advanced,
            deps: SettingsDeps(
              dataDir: dataDir,
              config: config,
              library: LibraryController(() => throw UnimplementedError()),
              player: ValueNotifier<PlayerController?>(null),
              haptics: Haptics(const HapticsConfig(enabled: false)),
              bundles: bundles,
            ),
          ),
        ),
      );
      await tester.pump();
    }

    testWidgets('the section, and Export through the bundle browser', (
      tester,
    ) async {
      await pump(tester);
      expect(find.text('BACKUP & MIGRATION'), findsOneWidget);
      expect(find.textContaining(kSnapshotName), findsOneWidget);

      await tester.tap(find.text('Export State'));
      await tester.pumpAndSettle();
      expect(find.text('Export State Bundle'), findsOneWidget);
      expect(find.text('Pick a destination folder'), findsOneWidget);
      for (final b in ['Downloads', 'Internal Storage', 'SD Card', 'Music']) {
        expect(find.text(b), findsOneWidget);
      }
      Directory('$appStorage/sub').createSync();
      await tester.tap(find.text('App Storage'));
      await tester.pumpAndSettle();
      expect(find.text('sub'), findsOneWidget);
      await tester.tap(find.text('Export here'));
      await tester.pumpAndSettle();
      expect(bundles.calls, ['export $appStorage']);
      expect(
        find.text(
          'Exported to $appStorage/mai_an_lab_state_20261124_120000.zip',
        ),
        findsOneWidget,
      );
    });

    testWidgets('Import lists the bundles, then reports what was replaced', (
      tester,
    ) async {
      await pump(tester);
      File('$appStorage/b.zip').writeAsBytesSync(List.filled(1536, 0));
      File('$appStorage/notes.txt').writeAsStringSync('x');
      Directory('$appStorage/.hidden').createSync();

      await tester.tap(find.text('Import State'));
      await tester.pumpAndSettle();
      expect(find.text('Import State Bundle'), findsOneWidget);
      expect(find.text('Pick a .zip bundle'), findsOneWidget);
      await tester.tap(find.text('App Storage'));
      await tester.pumpAndSettle();
      expect(find.text('Export here'), findsNothing);
      expect(find.text('b.zip'), findsOneWidget);
      expect(find.text('2 KB'), findsOneWidget);
      expect(find.text('notes.txt'), findsNothing);
      expect(find.text('.hidden'), findsNothing);
      await tester.tap(find.text('b.zip'));
      await tester.pumpAndSettle();
      expect(bundles.calls, ['import $appStorage/b.zip']);
      expect(
        find.text('Imported library.db, config.toml. Library reloaded.'),
        findsOneWidget,
      );

      bundles.importError = BundleError('bundle_version 1 != expected 3');
      // The snackbar leaves first (it covers the button).
      await tester.pump(const Duration(seconds: 5));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Import State'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('App Storage'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('b.zip'));
      await tester.pumpAndSettle();
      expect(
        find.text('Import failed: bundle_version 1 != expected 3'),
        findsOneWidget,
      );
    });

    testWidgets('an empty folder, and Cancel', (tester) async {
      await pump(tester);
      Directory('$appStorage/empty').createSync();
      await tester.tap(find.text('Import State'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('App Storage'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('empty'));
      await tester.pumpAndSettle();
      expect(find.text('(no .zip bundles or sub-folders)'), findsOneWidget);
      // Up, back to App Storage.
      await tester.tap(find.byTooltip('Up'));
      await tester.pumpAndSettle();
      expect(find.text('empty'), findsOneWidget);
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(bundles.calls, isEmpty);
    });
  });
}
