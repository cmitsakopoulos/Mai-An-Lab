import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/sync/app_update.dart';
import 'package:mai_an_lab/sync/app_update_view.dart';
import 'package:mai_an_lab/sync/companion_client.dart';

import 'app_update_test.dart' show FakePlatform, offerFor;

/// "What's new" from the Mac: shown with the offer, and edits arrive.
/// (A widget test: its own file, as flutter_test fakes HTTP for the file.)
void main() {
  testWidgets('release notes: shown with the offer, edits arrive', (
    tester,
  ) async {
    final tmp = Directory.systemTemp.createTempSync('notes_');
    addTearDown(() => tmp.deleteSync(recursive: true));
    addTearDown(() => CompanionClient.appHeaders = const {});
    final u = AppUpdater(
      pref: (_) => null,
      savePref: (_, _) async {},
      platform: FakePlatform(
        tmp,
        const AppBuild(
          package: 'lab.maian.port',
          versionCode: 4,
          versionName: '0.3.0',
          apkPath: '/none',
        ),
      ),
    );
    var prompts = 0;
    u.onAvailable = (_) => prompts++;
    await tester.runAsync(u.init);
    final client = CompanionClient('127.0.0.1', authToken: 't');
    final offer = {
      ...offerFor([1, 2, 3]),
      'notes': '- Faster scans',
    };
    await tester.runAsync(() => u.handleOffer(client, offer));
    expect(u.state, AppUpdateState.available);
    expect(u.notes, '- Faster scans');
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: AppUpdatePanel(updater: u)),
      ),
    );
    expect(find.text("What's new"), findsOneWidget);
    expect(find.text('- Faster scans'), findsOneWidget);

    // Edited on the Mac after the offer: the same build, new notes.
    await tester.runAsync(
      () => u.handleOffer(client, {
        ...offer,
        'notes': '- Faster scans\n- Fixed the queue',
      }),
    );
    await tester.pump();
    expect(find.text('- Faster scans\n- Fixed the queue'), findsOneWidget);
    expect(prompts, 1, reason: 'notes alone do not prompt again');

    await tester.runAsync(() => u.handleOffer(client, {...offer, 'notes': ''}));
    await tester.pump();
    expect(find.text("What's new"), findsNothing);
  });
}
