// Settings: the hub and its search, each group's writes into config.toml
// (the same params settings.py passes to update_config_params, whose output
// config_file_test pins), the changes reaching the running app, and the
// Database Management actions on a real database.
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/app_version.dart';
import 'package:mai_an_lab/core/config_file.dart';
import 'package:mai_an_lab/core/haptics.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/library/track_query.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/settings/config_controller.dart';
import 'package:mai_an_lab/settings/settings_page.dart';
import 'package:mai_an_lab/ui/tokens.dart';
import 'package:sqlite3/sqlite3.dart';
import 'package:toml/toml.dart';

void main() {
  late Directory dir;
  late String dbPath;
  final template = File('assets/config_template.toml').readAsStringSync();

  setUp(() async {
    dir = Directory.systemTemp.createTempSync('mai_an_settings');
    dbPath = '${dir.path}/library.db';
    final repo = await LibraryRepository.open(dbPath, writable: true);
    await repo.debugExecute([
      (
        "INSERT INTO artists (id, name, album_count, track_count) "
            "VALUES (1, 'A', 1, 2)",
        const [],
      ),
      (
        "INSERT INTO albums (id, artist_id, title, track_count) "
            "VALUES (1, 1, 'Al', 2)",
        const [],
      ),
      (
        "INSERT INTO tracks (id, album_id, title, path, added_date) VALUES "
            "(1, 1, 'One', '/m/1.flac', 1), (2, 1, 'Two', '/m/2.flac', 2)",
        const [],
      ),
      (
        "INSERT INTO play_counts (track_path, count, timbre, "
            "features_version, pca_coords) VALUES "
            "('/m/1.flac', 3, x'00', 5, x'00')",
        const [],
      ),
      ("INSERT INTO playlists (id, name) VALUES (1, 'P')", const []),
      (
        'INSERT INTO playlist_tracks (playlist_id, track_path, order_index) '
            "VALUES (1, '/m/1.flac', 0)",
        const [],
      ),
      (
        'INSERT INTO playback_history (track_path, played_at, event) '
            "VALUES ('/m/1.flac', 1, 'listen')",
        const [],
      ),
    ]);
    await repo.close();
  });

  tearDown(() => dir.deleteSync(recursive: true));

  int count(String table) {
    final db = sqlite3.open(dbPath, mode: OpenMode.readOnly);
    try {
      return db.select('SELECT COUNT(*) AS n FROM $table').first['n'] as int;
    } finally {
      db.close();
    }
  }

  Map<String, Object?> fileToml() => TomlDocument.parse(
    File(ConfigStore(dir.path).path).readAsStringSync(),
  ).toMap();

  group('maintenance (main.py)', () {
    Future<void> run(Future<void> Function(LibraryRepository) job) async {
      final repo = await LibraryRepository.open(dbPath, writable: true);
      await job(repo);
      await repo.close();
    }

    test('clear_library_index keeps play counts and history', () async {
      await run((r) => r.clearLibraryIndex());
      for (final t in ['tracks', 'albums', 'artists', 'playlists']) {
        expect(count(t), 0, reason: t);
      }
      expect(count('playlist_tracks'), 0);
      expect(count('play_counts'), 1);
      expect(count('playback_history'), 1);
    });

    test('clear_dsp_features keeps the index', () async {
      await run((r) => r.clearDspFeatures());
      expect(count('tracks'), 2);
      final db = sqlite3.open(dbPath, mode: OpenMode.readOnly);
      final row = db.select('SELECT * FROM play_counts').single;
      db.close();
      expect(row['timbre'], isNull);
      expect(row['features_version'], 0);
      expect(row['pca_coords'], isNull);
      expect(row['count'], 3);
    });

    test('wipe_database empties everything it lists', () async {
      await run((r) => r.wipeDatabase());
      for (final t in [
        'tracks',
        'albums',
        'artists',
        'playlists',
        'playlist_tracks',
        'play_counts',
        'playback_history',
      ]) {
        expect(count(t), 0, reason: t);
      }
    });
  });

  group('the Settings screens', () {
    late ConfigController config;
    late LibraryController library;

    Future<void> pumpSettings(
      WidgetTester tester, {
      SettingsPage? initial,
      double height = 844,
    }) async {
      tester.view.physicalSize = Size(390 * 3, height * 3);
      tester.view.devicePixelRatio = 3;
      addTearDown(tester.view.reset);
      await tester.runAsync(() async {
        config = await ConfigController.load(
          ConfigStore(dir.path, template: () async => template),
        );
        library = LibraryController(
          () => LibraryRepository.open(dbPath, writable: true),
        );
        await library.load();
      });
      await tester.pumpWidget(
        MaterialApp(
          theme: Tokens.theme(),
          home: SettingsScreen(
            initial: initial,
            deps: SettingsDeps(
              dataDir: dir.path,
              config: config,
              library: library,
              player: ValueNotifier<PlayerController?>(null),
              haptics: Haptics(const HapticsConfig(enabled: false)),
            ),
          ),
        ),
      );
      await tester.pump();
    }

    /// Real time for file and database IO, until [done] holds (or ~3 s).
    Future<void> settle(WidgetTester tester, [bool Function()? done]) async {
      var after = 0;
      for (var i = 0; i < 300; i++) {
        // A few more rounds once it holds: what the UI does next.
        if (done != null && done() && ++after > 5) break;
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 10)),
        );
        await tester.pump(const Duration(milliseconds: 50));
        if (done == null && i >= 25) break;
      }
    }

    testWidgets('the hub, its search, a group and back', (tester) async {
      await pumpSettings(tester, height: 1400);
      // A missing config.toml starts as the template.
      expect(File(config.store.path).readAsStringSync(), template);
      for (final t in [
        'Storage & Paths',
        'Authentication',
        'Appearance',
        'Audio & DSP',
        'Haptic Feedback',
        'Permissions',
        'Database Management',
        'Advanced',
        'About',
      ]) {
        expect(find.text(t), findsOneWidget, reason: t);
      }
      await tester.enterText(find.byType(TextField), 'wipe');
      await tester.pump();
      expect(find.text('SEARCH RESULTS (2)'), findsOneWidget);
      expect(
        find.text('[SYSTEM] Wipe database, compute DSP & PCA'),
        findsOneWidget,
      );
      await tester.enterText(find.byType(TextField), 'xyzzy');
      await tester.pump();
      expect(find.text("No settings found matching 'xyzzy'"), findsOneWidget);
      await tester.tap(find.text('CLEAR SEARCH'));
      await tester.pump();
      await tester.tap(find.text('About'));
      await tester.pumpAndSettle();
      expect(find.text('Version $kAppVersion'), findsOneWidget);
      await tester.tap(find.byTooltip('Back'));
      await tester.pumpAndSettle();
      expect(find.text('Settings'), findsOneWidget);
    });

    testWidgets(
      'Appearance saves immediately, plus library_sort & glass_opacity',
      (tester) async {
        await pumpSettings(tester, initial: SettingsPage.appearance);
        expect(find.text('SAVE APPEARANCE SETTINGS'), findsNothing);
        expect(find.text('Glass Opacity'), findsOneWidget);

        // Tap Green accent swatch (at top in Glass & Color card).
        await tester.tap(find.byTooltip('Green'));
        await tester.pumpAndSettle();

        // Open Default Library Sort frosted picker and pick Artist (A–Z).
        await tester.dragUntilVisible(
          find.text('Date Added'),
          find.byType(ListView).first,
          const Offset(0, -100),
        );
        await tester.tap(find.text('Date Added'));
        await tester.pumpAndSettle();
        await tester.tap(find.text('Artist (A–Z)').last);
        await tester.pumpAndSettle();

        // Toggle Albums chip in Library Views card.
        await tester.dragUntilVisible(
          find.text('Albums'),
          find.byType(ListView).first,
          const Offset(0, -100),
        );
        await tester.tap(find.text('Albums'));
        await settle(
          tester,
          () =>
              library.sort == LibrarySort.artist &&
              config.config.libraryTabs.visible.contains('albums') &&
              config.config.accent == const Color(0xFF30D158),
        );

        final t = fileToml();
        final app = t['appearance']! as Map;
        expect(app['show_albums'], true);
        expect(app['accent_color'], '#30D158');
        expect(app['show_jarvis'], true);
        expect(app['glass_opacity'], closeTo(Tokens.defaultGlassOpacity, 0.01));
        expect((t['landing']! as Map)['show_search_history'], true);
        expect(t['general'], {
          'startup_page': 'Library',
          'default_sort': 'artist',
          'library_sort': 'artist',
        });
        expect(config.config.libraryTabs.visible, contains('albums'));
        expect(config.config.accent, const Color(0xFF30D158));
        expect(library.sort, LibrarySort.artist);
      },
    );

    testWidgets('Haptics writes every change at once', (tester) async {
      await pumpSettings(tester, initial: SettingsPage.haptics);
      await tester.tap(find.text('Enable Haptic Feedback'));
      await settle(tester);
      expect((fileToml()['haptics']! as Map)['haptic_feedback_enabled'], false);
      await tester.tap(find.text('Heavy').first);
      await tester.pumpAndSettle();
      await tester.tap(find.text('None').last);
      await settle(tester);
      final h = fileToml()['haptics']! as Map;
      expect(h['long_press_intensity'], 'none');
      expect(h['eq_drag_intensity'], 'light');
      expect(config.config.haptics.intensityFor('long_press'), isNull);
    });

    testWidgets('Audio & DSP: EQ switch, custom preset save and delete', (
      tester,
    ) async {
      await pumpSettings(tester, initial: SettingsPage.audio, height: 2600);
      await tester.tap(find.text('Enable 5-Band Equalizer'));
      await settle(
        tester,
        () => (fileToml()['dsp']! as Map)['equalizer_enabled'] == true,
      );
      expect((fileToml()['dsp']! as Map)['equalizer_enabled'], true);

      await tester.tap(find.text('Custom Presets'));
      await settle(
        tester,
        () => (fileToml()['dsp']! as Map)['active_preset'] == 'Custom',
      );
      expect((fileToml()['dsp']! as Map)['active_preset'], 'Custom');

      // A gain typed into the 230 Hz field: one decimal, into Custom.
      await tester.enterText(find.text('+0.0 dB').at(1), '2.26 dB');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await settle(tester, () => fileToml().toString().contains('2.3'));
      expect(((fileToml()['dsp']! as Map)['custom_presets'] as Map)['Custom'], [
        0.0,
        2.3,
        0.0,
        0.0,
        0.0,
      ]);
      await tester.enterText(
        find.widgetWithText(TextField, 'Save Custom Preset'),
        'Rock',
      );
      await tester.tap(find.text('Save Preset'));
      await settle(tester);
      expect(find.text('Cannot overwrite default presets.'), findsOneWidget);

      await tester.enterText(
        find.widgetWithText(TextField, 'Save Custom Preset'),
        'Night',
      );
      await tester.tap(find.text('Save Preset'));
      // The file, then the screen re-read from it (the Delete button shows
      // for a custom preset): under a loaded test run the second can lag.
      await settle(
        tester,
        () =>
            (fileToml()['dsp']! as Map)['active_preset'] == 'Night' &&
            find.byTooltip('Delete Preset').evaluate().isNotEmpty,
      );
      var dsp = fileToml()['dsp']! as Map;
      expect(dsp['active_preset'], 'Night');
      expect((dsp['custom_presets'] as Map)['Night'], [
        0.0,
        2.3,
        0.0,
        0.0,
        0.0,
      ]);
      expect(
        File(config.store.path).readAsStringSync(),
        contains(
          'custom_presets = {Custom = [0.0, 2.3, 0.0, 0.0, 0.0],'
          'Night = [0.0, 2.3, 0.0, 0.0, 0.0]}',
        ),
      );
      await tester.tap(find.byTooltip('Delete Preset'));
      await tester.pumpAndSettle();
      expect(find.text("Delete 'Night'?"), findsOneWidget);
      await tester.tap(find.text('Delete'));
      await settle(
        tester,
        () => (fileToml()['dsp']! as Map)['active_preset'] == 'Flat',
      );
      dsp = fileToml()['dsp']! as Map;
      expect(dsp['active_preset'], 'Flat');
      expect((dsp['custom_presets'] as Map).keys, ['Custom']);
    });

    testWidgets('Audio & DSP: Rebuild All asks before recomputing', (
      tester,
    ) async {
      await pumpSettings(tester, initial: SettingsPage.audio, height: 1200);
      await settle(
        tester,
        () => find.text('Rebuild All').evaluate().isNotEmpty,
      );
      await tester.tap(find.text('Rebuild All'));
      await tester.pumpAndSettle();
      expect(find.text('Rebuild Acoustic Index?'), findsOneWidget);
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(find.text('Rebuild Acoustic Index?'), findsNothing);
      expect(library.analysis.running, isFalse);
      // Canceling leaves existing DSP features intact.
      final db = sqlite3.open(dbPath, mode: OpenMode.readOnly);
      final row = db.select('SELECT timbre FROM play_counts').single;
      db.close();
      expect(row['timbre'], isNotNull);
    });

    testWidgets('Database Management clears the index after asking', (
      tester,
    ) async {
      await pumpSettings(tester, initial: SettingsPage.database);
      await tester.tap(find.text('Library Index'));
      await tester.pumpAndSettle();
      expect(find.text('Clear Library Index?'), findsOneWidget);
      await tester.tap(find.text('Clear Index'));
      await settle(
        tester,
        () => find
            .text('Library index cleared successfully.')
            .evaluate()
            .isNotEmpty,
      );
      expect(find.text('Library index cleared successfully.'), findsOneWidget);
      expect(library.total, 0);
      expect(count('play_counts'), 1);
    });

    testWidgets('Advanced shows the file and saves it as typed', (
      tester,
    ) async {
      await pumpSettings(tester, initial: SettingsPage.advanced);
      await settle(
        tester,
        () => find.textContaining('[downloads]').evaluate().isNotEmpty,
      );
      expect(find.textContaining('[downloads]'), findsOneWidget);
      await tester.enterText(
        find.byType(TextField),
        '[general]\nstartup_page = "Search"\n',
      );
      await tester.dragUntilVisible(
        find.text('Save Configuration'),
        find.byType(ListView).first,
        const Offset(0, -200),
      );
      await tester.ensureVisible(find.text('Save Configuration'));
      await tester.pump();
      await tester.tap(find.text('Save Configuration'));
      await settle(tester, () => config.config.startupPage == 'Search');
      expect(
        File(config.store.path).readAsStringSync(),
        '[general]\nstartup_page = "Search"\n',
      );
      expect(config.config.startupPage, 'Search');
    });

    testWidgets(
      'Authentication saves Qobuz and Deezer credentials independently',
      (tester) async {
        await pumpSettings(tester, initial: SettingsPage.account, height: 1400);
        expect(find.text('Streaming Credentials'), findsOneWidget);
        expect(find.text('SAVE CREDENTIALS'), findsNothing);

        // Save Deezer ARL alone (without needing Qobuz credentials)
        await tester.enterText(
          find.widgetWithText(TextField, 'Deezer ARL Cookie'),
          'arl_secret_cookie_123',
        );
        await tester.pump();
        expect(find.text('SAVE CREDENTIALS'), findsOneWidget);
        await tester.tap(find.text('SAVE CREDENTIALS'));
        await settle(
          tester,
          () => config.config.sources.deezer.arl == 'arl_secret_cookie_123',
        );
        expect((fileToml()['deezer']! as Map)['arl'], 'arl_secret_cookie_123');
        expect(find.text('Deezer credentials updated.'), findsOneWidget);
        expect(find.text('SAVE CREDENTIALS'), findsNothing);

        // Clear floating SnackBar so it doesn't obscure the floating save pill.
        ScaffoldMessenger.of(
          tester.element(find.byType(SettingsScreen)),
        ).clearSnackBars();
        await tester.pumpAndSettle();

        // Save Qobuz credentials alongside Deezer
        await tester.enterText(
          find.widgetWithText(TextField, 'Qobuz User ID'),
          '987654',
        );
        await tester.enterText(
          find.widgetWithText(TextField, 'Auth Token / Password Hash'),
          'tok_abc',
        );
        await tester.pump();
        await tester.tap(find.text('SAVE CREDENTIALS'));
        await settle(
          tester,
          () =>
              config.config.sources.qobuz.emailOrUserid == '987654' &&
              find
                  .text('Qobuz & Deezer credentials updated.')
                  .evaluate()
                  .isNotEmpty,
        );
        final q = fileToml()['qobuz']! as Map;
        expect(q['email_or_userid'], '987654');
        expect(q['password_or_token'], 'tok_abc');
        expect(q['use_auth_token'], false);
        expect(q['app_id'], '312369995');
        expect(q['secrets'], ['e79f8b9be485692b0e5f9dd895826368']);
        expect(
          find.text('Qobuz & Deezer credentials updated.'),
          findsOneWidget,
        );
      },
    );

    testWidgets(
      'Storage Default Download Quality writes general.default_quality',
      (tester) async {
        final musicDir = Directory.systemTemp.createTempSync('mai_an_music');
        addTearDown(() => musicDir.deleteSync(recursive: true));
        const channel = MethodChannel('plugins.flutter.io/path_provider');
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
            .setMockMethodCallHandler(channel, (call) async {
              if (call.method == 'getApplicationSupportDirectory') {
                return '${dir.path}/support';
              }
              if (call.method == 'getApplicationDocumentsDirectory') {
                return musicDir.path;
              }
              return musicDir.path;
            });
        addTearDown(() {
          TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
              .setMockMethodCallHandler(channel, null);
        });

        await pumpSettings(tester, initial: SettingsPage.storage, height: 1200);
        await settle(
          tester,
          () => find.textContaining(musicDir.path).evaluate().isNotEmpty,
        );
        expect(find.text('Default Download Quality'), findsOneWidget);
        expect(find.text('Ask every time'), findsOneWidget);

        await tester.tap(find.text('Ask every time'));
        await tester.pumpAndSettle();
        await tester.tap(find.text('CD Quality').last);
        await tester.pumpAndSettle();
        // Applied at once: no Save button.
        expect(find.text('CD Quality'), findsOneWidget);
        await settle(
          tester,
          () => config.config.sources.defaultQuality == 'cd',
        );
        expect((fileToml()['general']! as Map)['default_quality'], 'cd');
      },
    );
  });
}
