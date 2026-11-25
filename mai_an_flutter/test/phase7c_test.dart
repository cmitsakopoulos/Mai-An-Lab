import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/app_shell.dart';
import 'package:mai_an_lab/core/config_file.dart';
import 'package:mai_an_lab/onboarding/onboarding.dart';
import 'package:mai_an_lab/onboarding/onboarding_wizard.dart';
import 'package:mai_an_lab/player/queue_store.dart';
import 'package:mai_an_lab/scan/storage_paths.dart';
import 'package:mai_an_lab/settings/config_controller.dart';
import 'package:mai_an_lab/ui/tokens.dart';

/// Phase 7c: the onboarding wizard's finish against Flet's own
/// (`tool/export_phase7c_goldens.py`): the config.toml update, the folder
/// prefs, the marker and the tab it lands on; then the wizard itself, walked
/// through on a phone-sized screen.

final Map<String, Object?> g =
    jsonDecode(File('test/goldens/phase7c.json').readAsStringSync())
        as Map<String, Object?>;

const _android = StoragePaths(
  StoragePlatform(
    isAndroid: true,
    realHome: '/storage/emulated/0',
    appHome: '/data/user/0/x/files',
    privateRoots: ['/data/user/0/x/files'],
  ),
);

Future<ConfigController> _config(String dir) => ConfigController.load(
  ConfigStore(
    dir,
    template: () => File('assets/config_template.toml').readAsString(),
  ),
);

/// Flet's `tab_map` index for the shell's tabs.
const _tabIndex = {AppTab.jarvis: 0, AppTab.search: 1, AppTab.library: 2};

void main() {
  late Directory tmp;
  setUp(() => tmp = Directory.systemTemp.createTempSync('mai_an_7c_'));
  tearDown(() => tmp.deleteSync(recursive: true));

  test('options match Flet', () {
    expect([
      for (final (h, n) in kOnboardingAccents) [h, n],
    ], g['accents']);
    expect([
      for (final (k, l) in kOnboardingStartups) [k, l],
    ], g['startups']);
  });

  for (final c in (g['cases']! as List).cast<Map<String, Object?>>()) {
    test('finish: ${c['name']}', () async {
      final input = c['input']! as Map<String, Object?>;
      final choices = OnboardingChoices(
        accent: input['accent'] as String? ?? '#FFD60A',
        startup: input['startup'] as String? ?? 'Library',
        folder:
            input['folder'] as String? ?? '/storage/emulated/0/Music/Streamrip',
        qobuzUser: input['qobuz_user'] as String? ?? '',
        qobuzToken: input['qobuz_token'] as String? ?? '',
        deezerArl: input['deezer_arl'] as String? ?? '',
      );
      expect(
        jsonDecode(jsonEncode(onboardingConfigUpdate(choices, choices.folder))),
        c['update'],
      );

      expect(await needsOnboarding(tmp.path), isTrue);
      final config = await _config(tmp.path);
      final saved = await finishOnboarding(
        dataDir: tmp.path,
        config: config,
        storage: _android,
        choices: choices,
      );
      expect(await needsOnboarding(tmp.path), isFalse);
      expect(
        File(onboardedMarkerPath(tmp.path)).readAsStringSync(),
        c['marker'],
      );
      final prefs = Prefs(tmp.path);
      await prefs.load();
      expect([
        ['folder_path', prefs['folder_path']],
        ['library_path', prefs['library_path']],
      ], c['prefs']);
      expect(saved, choices.folder);
      // config.toml now says what Flet wrote.
      final update = c['update']! as Map<String, Object?>;
      for (final MapEntry(key: section, value: values) in update.entries) {
        for (final MapEntry(:key, :value) in (values! as Map).entries) {
          expect(
            jsonDecode(jsonEncode(config.value(section, key as String))),
            value,
            reason: '$section.$key',
          );
        }
      }
      final restart = c['restart']! as Map<String, Object?>;
      expect(
        _tabIndex[AppTab.fromStartupPage(choices.startup)],
        restart['target_tab'],
      );
      expect(kOnboardingWelcome, restart['post_message']);
    });
  }

  test('a sandboxed folder falls back to the public Music folder', () async {
    final config = await _config(tmp.path);
    final saved = await finishOnboarding(
      dataDir: tmp.path,
      config: config,
      storage: _android,
      choices: const OnboardingChoices(folder: '/data/user/0/x/files/Music'),
    );
    expect(saved, StoragePlatform.androidMusicDirs.first);
    expect(config.value('downloads', 'folder'), saved);
  });

  testWidgets('the wizard on a phone: three slides, then saved', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(360 * 3, 720 * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    final config = (await tester.runAsync(() => _config(tmp.path)))!;
    (String, String)? result;
    await tester.pumpWidget(
      MaterialApp(
        theme: Tokens.theme(),
        home: Builder(
          builder: (context) => Scaffold(
            body: Center(
              child: TextButton(
                onPressed: () async => result = await showOnboardingWizard(
                  context,
                  dataDir: tmp.path,
                  config: config,
                  storage: _android,
                ),
                child: const Text('open'),
              ),
            ),
          ),
        ),
      ),
    );
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    expect(find.text('Welcome to Mai An Lab'), findsOneWidget);
    expect(find.text('STEP 1 OF 3'), findsOneWidget);

    // Back can't dismiss it.
    await tester.binding.handlePopRoute();
    await tester.pumpAndSettle();
    expect(find.text('Welcome to Mai An Lab'), findsOneWidget);

    await tester.tap(find.text('GET STARTED'));
    await tester.pumpAndSettle();
    expect(find.text('Music Sources'), findsOneWidget);
    await tester.enterText(
      find.byKey(const ValueKey('onboarding-folder')),
      '  /storage/emulated/0/Music/Mine  ',
    );
    await tester.enterText(
      find.widgetWithText(TextField, 'Your Qobuz Email or User ID'),
      '12345',
    );
    await tester.enterText(
      find.widgetWithText(TextField, 'Your Password or Session Token'),
      'tok',
    );
    await tester.ensureVisible(find.text('Deezer'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Deezer'));
    await tester.pumpAndSettle();
    expect(find.text('Your Deezer ARL Cookie'), findsOneWidget);
    await tester.ensureVisible(find.text('Your Deezer ARL Cookie'));
    await tester.tap(find.text('PERSONALIZE'));
    await tester.pumpAndSettle();
    expect(find.text('Make It Yours'), findsOneWidget);
    await tester.tap(find.byTooltip('Violet'));
    await tester.tap(find.text('Search'));
    await tester.pump();
    expect(tester.takeException(), isNull);

    await tester.tap(find.text('ENTER MAI-AN LAB'));
    // The saves are real file I/O.
    for (var i = 0; i < 100 && result == null; i++) {
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 50)),
      );
      await tester.pump();
    }
    await tester.pumpAndSettle();
    expect(result, ('Search', '/storage/emulated/0/Music/Mine'));
    expect(find.text('Make It Yours'), findsNothing);
    expect(config.value('appearance', 'accent_color'), '#BF5AF2');
    expect(config.value('general', 'startup_page'), 'Search');
    expect(config.value('qobuz', 'email_or_userid'), '12345');
    expect(config.value('qobuz', 'password_or_token'), 'tok');
    expect(File(onboardedMarkerPath(tmp.path)).existsSync(), isTrue);
  });
}
