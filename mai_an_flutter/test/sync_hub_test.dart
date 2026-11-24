// Phase 4e: Settings → Companion & Sync (sync_hub.py's Android view), the
// manager's PIN and transfer dialogs through the app's host, and Send to
// Mac (send.py) from the track menu. The manager runs on a fake Mac
// (sync_fakes.dart) and the test binding's clock.
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/app_shell.dart';
import 'package:mai_an_lab/core/app_config.dart';
import 'package:mai_an_lab/core/config_file.dart';
import 'package:mai_an_lab/core/haptics.dart';
import 'package:mai_an_lab/core/notification_router.dart';
import 'package:mai_an_lab/core/progress_notify.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/browse_controller.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/library/track_menu.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/player/queue_store.dart' show Prefs;
import 'package:mai_an_lab/search/landing_controller.dart';
import 'package:mai_an_lab/settings/config_controller.dart';
import 'package:mai_an_lab/settings/settings_page.dart';
import 'package:mai_an_lab/settings/sync_hub.dart';
import 'package:mai_an_lab/sync/app_sync_host.dart';
import 'package:mai_an_lab/sync/companion_client.dart'
    show CompanionAuthError, CompanionHttpError, FeatureRow;
import 'package:mai_an_lab/sync/offload_library.dart';
import 'package:mai_an_lab/sync/send.dart';
import 'package:mai_an_lab/sync/state_bundles.dart';
import 'package:mai_an_lab/sync/sync_manager.dart';
import 'package:mai_an_lab/ui/tokens.dart';

import 'sync_fakes.dart';

/// The manager's clock: the test binding's fake time.
class _BindingClock extends Stopwatch {
  _BindingClock(this.binding) : _t0 = binding.clock.now();
  final TestWidgetsFlutterBinding binding;
  final DateTime _t0;
  @override
  Duration get elapsed => binding.clock.now().difference(_t0);
}

Map<String, Object?> _t(String id, {bool dsp = false}) => {
  'id': id,
  'title': 'Title $id',
  'artist': 'Artist $id',
  'album': 'Album',
  'quality': 'aac_vbr_max',
  'extension': 'flac',
  'stream_path': '/api/v1/tracks/stream?path=%2F$id&quality=aac_vbr_max',
  'dsp_features': dsp ? {'bpm': 1} : null,
};

const _track = Track(
  id: 1,
  path: '/music/a.flac',
  title: 'A Song',
  artist: 'An Artist',
  album: 'An Album',
  duration: 200,
  format: 'flac',
);

/// export_state without the isolate: a file in [dir], as the real one
/// leaves in Downloads.
class _Bundles extends StateBundles {
  _Bundles(String dir)
    : super(
        dataDir: dir,
        dbPath: '$dir/library.db',
        library: LibraryController(() => throw UnimplementedError()),
        config: ConfigController(ConfigStore(dir)),
      );

  final exported = <String>[];

  @override
  Future<String> export({String? outDir}) async {
    final f = File('$dataDir/mai_an_lab_state_${exported.length}.zip')
      ..writeAsStringSync('zip');
    exported.add(f.path);
    return f.path;
  }
}

/// The library as the offload card sees it.
class _OffloadLib implements OffloadLibrary {
  _OffloadLib(this.missing);
  List<String> missing;
  final stored = <List<String>>[];
  int rebuilds = 0;

  @override
  Future<List<String>?> tracksMissingFeatures() async => List.of(missing);

  @override
  Future<void> storeFeatureRows(List<FeatureRow> rows) async {
    final paths = [for (final r in rows) r.path];
    stored.add(paths);
    missing = [
      for (final p in missing)
        if (!paths.contains(p)) p,
    ];
  }

  @override
  Future<void> rebuildSimilarityGraph() async => rebuilds++;
}

void main() {
  late Directory dir;
  late FakeMac mac;
  late FakeTransferQueue queue;
  late List<String> snacks;
  late List<String> listenerLog;
  late AppSyncHost host;
  late CompanionSyncManager m;
  final nav = GlobalKey<NavigatorState>();

  setUp(() {
    dir = Directory.systemTemp.createTempSync('sync_hub_test');
    mac = FakeMac();
    queue = FakeTransferQueue();
    snacks = [];
    listenerLog = [];
    NotificationBridge.instance = RecordingBridge();
  });
  tearDown(() => dir.deleteSync(recursive: true));

  /// [enabled]: the switch was left on (the manager reads it when built;
  /// its loops don't run until start).
  void make(WidgetTester tester, {bool enabled = false}) {
    host = AppSyncHost(
      prefs: Prefs(dir.path),
      navigator: nav,
      onSnack: (text, {icon, color}) =>
          snacks.add(color == Tokens.accentRed ? 'RED $text' : text),
      library: LibraryController(() => throw UnimplementedError()),
      queue: queue,
    );
    if (enabled) host.savePref('companion_sync_enabled', true);
    m = host.manager = CompanionSyncManager(
      host,
      clientFactory: (h, p, t, n) => FakeCompanionClient(mac, h, p, t),
      listenerFactory: (f) => FakeListener(f, listenerLog),
      localIp: () async => '192.168.1.20',
      sweep: (ip, port) async => const [],
      deviceName: () async => 'Pixel',
      clock: _BindingClock(tester.binding),
    );
  }

  void pair() {
    host
      ..savePref('companion_paired_token', 'tok')
      ..savePref('companion_paired_host', mac.host)
      ..savePref('companion_paired_port', mac.port)
      ..savePref('companion_paired_name', mac.name);
  }

  Future<void> pumpHub(
    WidgetTester tester, {
    StateBundles? bundles,
    OffloadLibrary? library,
  }) async {
    tester.view.physicalSize = const Size(390 * 3, 844 * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(
      MaterialApp(
        navigatorKey: nav,
        theme: Tokens.theme(),
        home: Scaffold(
          body: SyncHubGroup(manager: m, bundles: bundles, library: library),
        ),
      ),
    );
  }

  /// Ends the probe and heartbeat loops (their timers must not outlive
  /// the test).
  Future<void> drain(WidgetTester tester) async {
    await m.stop();
    await tester.pump(const Duration(seconds: 20));
  }

  Future<void> connect(WidgetTester tester) async {
    await m.handleDiscoveredServer({'ip': mac.host, 'port': mac.port});
    await tester.pumpAndSettle();
  }

  testWidgets('the switch: searching, then off', (tester) async {
    make(tester);
    await pumpHub(tester);
    expect(find.text('Offline · Inactive'), findsOneWidget);
    expect(find.text('Find Mac'), findsOneWidget);
    expect(find.text('Unpair Device'), findsNothing);

    await tester.tap(find.byKey(const ValueKey('sync-switch')));
    await tester.pump();
    await tester.pump();
    expect(m.enabled, isTrue);
    expect(host.prefs['companion_sync_enabled'], isTrue);
    expect(find.text('Searching for Mac on network or USB…'), findsOneWidget);
    expect(snacks, contains('Searching for Mac Companion…'));
    expect(listenerLog, ['start']);

    await tester.tap(find.byKey(const ValueKey('sync-switch')));
    await tester.pump();
    expect(m.enabled, isFalse);
    expect(host.prefs['companion_sync_enabled'], isFalse);
    expect(find.text('Offline · Inactive'), findsOneWidget);
    expect(snacks.last, 'Companion Sync disconnected');
    await drain(tester);
  });

  testWidgets('connected: status, pending count, the dialog, Download', (
    tester,
  ) async {
    make(tester, enabled: true);
    pair();
    host.prefs.set(askBeforeDownloadPref, true).ignore();
    mac.pending = [_t('1', dsp: true), _t('2', dsp: true)];
    await pumpHub(tester);
    await connect(tester);

    expect(find.text('Connected · Test Mac (192.168.1.7)'), findsOneWidget);
    expect(find.text('Test Connection'), findsOneWidget);
    expect(find.text('Unpair Device'), findsOneWidget);
    expect(find.text('2 tracks ready to download from Mac.'), findsOneWidget);

    // Ask first: the transfer dialog, once for this set.
    expect(find.text('Transfers from Test Mac'), findsOneWidget);
    expect(
      find.text('2 tracks · AAC VBR · acoustic features included'),
      findsOneWidget,
    );
    expect(find.text('Title 1'), findsOneWidget);
    expect(find.text('Artist 2'), findsOneWidget);
    await tester.tap(find.text('Later'));
    await tester.pumpAndSettle();
    expect(find.text('Transfers from Test Mac'), findsNothing);
    expect(m.transferPromptOpen, isFalse);

    await tester.tap(find.text('Download (2) from Mac'));
    await tester.pumpAndSettle();
    expect([for (final i in queue.items) i['id']], ['1', '2']);
    expect(snacks.last, 'Queued 2 tracks in Download Dock');
    // The queue change re-polls (as in Flet): in flight, so none left.
    expect(find.text('No transfers waiting from Mac.'), findsOneWidget);
    expect(find.text('Download from Mac'), findsOneWidget);
  });

  testWidgets('the dialog\'s Download hands the set to the queue', (
    tester,
  ) async {
    make(tester, enabled: true);
    pair();
    host.prefs.set(askBeforeDownloadPref, true).ignore();
    mac.pending = [_t('7')];
    await pumpHub(tester);
    await connect(tester);
    expect(find.text('1 track · AAC VBR'), findsOneWidget);
    await tester.tap(find.text('Download (1)'));
    await tester.pumpAndSettle();
    expect(find.text('Transfers from Test Mac'), findsNothing);
    expect([for (final i in queue.items) i['id']], ['7']);
    expect(snacks.last, 'Queued 1 track in Download Dock');
  });

  testWidgets('auto-accept (the default) skips the dialog', (tester) async {
    make(tester, enabled: true);
    pair();
    mac.pending = [_t('1')];
    await pumpHub(tester);
    await connect(tester);
    expect(find.text('Transfers from Test Mac'), findsNothing);
    expect([for (final i in queue.items) i['id']], ['1']);
    expect(snacks, contains('Receiving 1 track from Test Mac'));
    expect(find.text('No transfers waiting from Mac.'), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey('ask-switch')));
    await tester.pumpAndSettle();
    expect(host.prefs[askBeforeDownloadPref], isTrue);
  });

  testWidgets('PIN: bad input, a wrong PIN, the right one', (tester) async {
    make(tester, enabled: true);
    await pumpHub(tester);
    await connect(tester);
    expect(find.text('Pair with Test Mac'), findsOneWidget);
    expect(
      find.text(
        'Found Test Mac (192.168.1.7). Enter the 4-digit PIN shown in '
        'Settings → Companion & Sync on your Mac.',
      ),
      findsOneWidget,
    );

    final field = find.descendant(
      of: find.byType(AlertDialog),
      matching: find.byType(TextField),
    );
    await tester.enterText(field, '12');
    await tester.tap(find.text('Connect'));
    await tester.pumpAndSettle();
    expect(find.text('Please enter the 4-digit PIN.'), findsOneWidget);

    await tester.enterText(field, '1111');
    await tester.tap(find.text('Connect'));
    await tester.pumpAndSettle();
    expect(
      find.text(
        'Incorrect PIN. Check the code on your Mac (it changes after 5 '
        'wrong tries).',
      ),
      findsOneWidget,
    );

    await tester.enterText(field, '4321');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();
    expect(find.byType(AlertDialog), findsNothing);
    expect(m.isConnected, isTrue);
    expect(m.pinPromptOpen, isFalse);
    expect(host.prefs['companion_paired_token'], 'tok');
    expect(snacks, contains('Connected to Test Mac'));
    expect(find.text('Connected · Test Mac (192.168.1.7)'), findsOneWidget);
  });

  testWidgets('Not now snoozes that Mac; stop closes an open prompt', (
    tester,
  ) async {
    make(tester);
    await pumpHub(tester);
    await connect(tester);
    await tester.tap(find.text('Not now'));
    await tester.pumpAndSettle();
    expect(m.pinPromptOpen, isFalse);
    await connect(tester);
    expect(find.byType(AlertDialog), findsNothing, reason: 'snoozed');

    await tester.pump(const Duration(minutes: 3));
    await connect(tester);
    expect(find.text('Pair with Test Mac'), findsOneWidget);
    await m.stop();
    await tester.pumpAndSettle();
    expect(find.byType(AlertDialog), findsNothing);
    expect(m.pinPromptOpen, isFalse);
  });

  testWidgets('Find Mac: nothing answers, the diagnosis and Manual IP', (
    tester,
  ) async {
    make(tester);
    mac.up = false;
    await pumpHub(tester);
    expect(find.byKey(const ValueKey('manual-ip')), findsNothing);
    await tester.tap(find.text('Find Mac'));
    await tester.pump();
    expect(find.text('Searching…'), findsOneWidget);
    await tester.pump(const Duration(seconds: 5));
    await tester.pump();
    expect(find.textContaining('No Mac answered.'), findsOneWidget);
    expect(find.byKey(const ValueKey('manual-ip')), findsOneWidget);
    expect(find.text('Hide Manual IP'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Connect'));
    await tester.pump();
    expect(snacks.last, 'RED Please enter an IP address');
    await tester.enterText(find.byKey(const ValueKey('manual-ip')), '1.2:x');
    await tester.tap(find.widgetWithText(FilledButton, 'Connect'));
    await tester.pump();
    expect(snacks.last, 'RED Invalid port in 1.2:x');

    mac.up = true;
    await tester.enterText(
      find.byKey(const ValueKey('manual-ip')),
      '192.168.1.7',
    );
    await tester.tap(find.widgetWithText(FilledButton, 'Connect'));
    await tester.pumpAndSettle();
    expect(
      find.text(
        'Found Test Mac (192.168.1.7). Enter the PIN shown on the Mac.',
        findRichText: true,
      ),
      findsOneWidget,
    );
    await drain(tester);
  });

  group('Send to Mac', () {
    testWidgets('not paired: says what to do', (tester) async {
      make(tester);
      await pumpHub(tester);
      expect(await sendTracks(m, [_track]), 0);
      expect(snacks.last, startsWith('RED Not paired with a Mac yet.'));
    });

    testWidgets('paired but unreachable: wakes the link, then explains', (
      tester,
    ) async {
      make(tester);
      pair();
      mac.up = false;
      await pumpHub(tester);
      expect(await sendTracks(m, [_track]), 0);
      expect(snacks, [
        'Connecting to Test Mac…',
        startsWith("RED Can't reach Test Mac."),
      ]);
      expect(m.enabled, isTrue);
      await drain(tester);
    });

    testWidgets('from the track menu, named for the Mac', (tester) async {
      make(tester, enabled: true);
      pair();
      await pumpHub(tester);
      await connect(tester);
      final target = companionTarget(m);
      expect(target.label, 'Send to Test Mac');
      expect(target.paired, isTrue);

      final ctx = tester.element(find.byType(SyncHubGroup));
      showTrackMenu(
        ctx,
        _track,
        (
          select: null,
          playNext: null,
          addToQueue: null,
          startRadio: null,
          addToPlaylist: null,
          delete: null,
        ),
        sendLabel: target.label,
        send: () => sendTracks(m, [_track]),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('Send to Test Mac'));
      await tester.pumpAndSettle();
      expect(mac.pushed, ['/music/a.flac|0|null']);
      expect(snacks, contains('Sending 1 track to Test Mac…'));
      expect(snacks.last, 'Sent 1 track to Test Mac');
    });
  });

  testWidgets('in the app: the sync notification target, Send in the menu', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390 * 3, 844 * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    make(tester, enabled: true);
    pair();
    host.savePref(askBeforeDownloadPref, true);
    final dbPath = '${dir.path}/library.db';
    late ConfigController config;
    late LibraryController lib;
    await tester.runAsync(() async {
      final repo = await LibraryRepository.open(dbPath, writable: true);
      await repo.debugExecute([
        ("INSERT INTO artists (id, name) VALUES (1, 'An Artist')", const []),
        (
          "INSERT INTO albums (id, artist_id, title) VALUES (1, 1, 'An Album')",
          const [],
        ),
        (
          'INSERT INTO tracks (album_id, title, track_num, path, duration) '
              "VALUES (1, 'A Song', 1, '/music/a.flac', 200)",
          const [],
        ),
      ]);
      await repo.close();
      config = await ConfigController.load(
        ConfigStore(
          dir.path,
          template: () async =>
              File('assets/config_template.toml').readAsStringSync(),
        ),
      );
      lib = LibraryController(() => LibraryRepository.open(dbPath));
      await lib.load();
    });
    final router = NotificationRouter();
    final player = ValueNotifier<PlayerController?>(null);
    final haptics = Haptics(const HapticsConfig(enabled: false));
    await tester.pumpWidget(
      MaterialApp(
        navigatorKey: nav,
        theme: Tokens.theme(),
        home: AppShell(
          library: lib,
          browse: BrowseController(
            lib,
            views: LibraryView.fromConfig(const LibraryTabs().visible),
          ),
          landing: LandingController(lib),
          player: player,
          haptics: haptics,
          router: router,
          settings: SettingsDeps(
            dataDir: dir.path,
            config: config,
            library: lib,
            player: player,
            haptics: haptics,
            sync: m,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await connect(tester);

    await tester.longPress(find.text('A Song'));
    await tester.pumpAndSettle();
    expect(find.text('Send to Test Mac'), findsOneWidget);
    await tester.tap(find.text('Send to Test Mac'));
    await tester.pumpAndSettle();
    expect(mac.pushed, ['/music/a.flac|0|null']);

    // The background card's tap: Companion & Sync, then the dialog.
    mac.pending = [_t('9')];
    router.route('sync:transfers');
    await tester.pumpAndSettle();
    expect(find.byType(SyncHubGroup), findsOneWidget);
    expect(find.text('Transfers from Test Mac'), findsOneWidget);
    await tester.tap(find.text('Later'));
    await tester.pumpAndSettle();
    expect(find.text('Companion & Sync'), findsOneWidget);
    lib.dispose();
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 50)),
    );
  });

  group('Library State Backup', () {
    testWidgets('off: the card is there, its button disabled', (tester) async {
      make(tester);
      await pumpHub(tester, bundles: _Bundles(dir.path));
      expect(find.text('Library State Backup'), findsOneWidget);
      final button = tester.widget<TextButton>(
        find.ancestor(
          of: find.text('Send Backup to Mac'),
          matching: find.byWidgetPredicate((w) => w is TextButton),
        ),
      );
      expect(button.onPressed, isNull);
    });

    testWidgets('no bundles: no card', (tester) async {
      make(tester);
      await pumpHub(tester);
      expect(find.text('Library State Backup'), findsNothing);
    });

    testWidgets('connected: the bundle goes to the Mac and is deleted here, '
        'the sync notification says where it went', (tester) async {
      make(tester, enabled: true);
      pair();
      final bundles = _Bundles(dir.path);
      final bridge = NotificationBridge.instance as RecordingBridge;
      await pumpHub(tester, bundles: bundles);
      await connect(tester);
      await tester.scrollUntilVisible(
        find.text('Send Backup to Mac'),
        200,
        scrollable: find.byType(Scrollable).first,
      );
      // The connection's snackbar leaves first (it covers the card).
      await tester.pump(const Duration(seconds: 5));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Send Backup to Mac'));
      await tester.pumpAndSettle();
      expect(mac.uploads, bundles.exported);
      expect(File(bundles.exported.single).existsSync(), isFalse);
      expect(
        find.text('Backup saved on Mac: mai_an_lab_state_0.zip'),
        findsOneWidget,
      );
      final sync = [
        for (final c in bridge.calls)
          if (c['job'] == 'sync') '${c['done']} ${c['content']}',
      ];
      expect(sync, [
        'false Creating snapshot bundle…',
        'false Transmitting bundle to Mac…',
        'true Backup saved on Mac: mai_an_lab_state_0.zip',
      ]);
      expect(bridge.calls.map((c) => c['title']).toSet(), {
        'State Backup Upload',
      });
      await drain(tester);
    });

    testWidgets('a failed upload says why and keeps the bundle', (
      tester,
    ) async {
      make(tester, enabled: true);
      pair();
      mac.uploadError = const CompanionHttpError('Upload failed: HTTP 500');
      final bundles = _Bundles(dir.path);
      await pumpHub(tester, bundles: bundles);
      await connect(tester);
      await tester.scrollUntilVisible(
        find.text('Send Backup to Mac'),
        200,
        scrollable: find.byType(Scrollable).first,
      );
      // The connection's snackbar leaves first (it covers the card).
      await tester.pump(const Duration(seconds: 5));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Send Backup to Mac'));
      await tester.pumpAndSettle();
      expect(find.text('Backup failed: ${mac.uploadError}'), findsOneWidget);
      expect(File(bundles.exported.single).existsSync(), isTrue);
      final last = (NotificationBridge.instance as RecordingBridge).calls.last;
      expect(last['content'], 'Backup upload failed: ${mac.uploadError}');
      expect(last['done'], isTrue);
      await drain(tester);
    });
  });

  group('DSP Acoustic Offload', () {
    FilledButton offloadButton(WidgetTester tester) =>
        tester.widget<FilledButton>(
          find.ancestor(
            of: find.text('Offload to Mac'),
            matching: find.byWidgetPredicate((w) => w is FilledButton),
          ),
        );

    Future<void> showCard(WidgetTester tester) async {
      await tester.scrollUntilVisible(
        find.text('Offload to Mac'),
        200,
        scrollable: find.byType(Scrollable).first,
      );
      // The connection's snackbar leaves first (it covers the card).
      await tester.pump(const Duration(seconds: 5));
      await tester.pumpAndSettle();
    }

    List<String> syncNotes() => [
      for (final c in (NotificationBridge.instance as RecordingBridge).calls)
        if (c['job'] == 'sync') '${c['done']} ${c['content']}',
    ];

    testWidgets('off: no count yet, the button disabled', (tester) async {
      make(tester);
      await pumpHub(tester, library: _OffloadLib(['/m/a.flac']));
      expect(find.text('DSP Acoustic Offload'), findsOneWidget);
      expect(
        find.text('Checking library acoustic features...'),
        findsOneWidget,
      );
      expect(offloadButton(tester).onPressed, isNull);
    });

    testWidgets('no library: no card', (tester) async {
      make(tester);
      await pumpHub(tester);
      expect(find.text('DSP Acoustic Offload'), findsNothing);
    });

    testWidgets('connected: the count, the offload, the rebuild, the '
        'notification', (tester) async {
      make(tester, enabled: true);
      pair();
      final lib = _OffloadLib(['/m/1.flac', '/m/2.flac', '/m/3.flac']);
      await pumpHub(tester, library: lib);
      await connect(tester);
      await showCard(tester);
      expect(find.text('3 tracks require acoustic analysis.'), findsOneWidget);
      expect(offloadButton(tester).onPressed, isNotNull);

      await tester.tap(find.text('Offload to Mac'));
      await tester.pump();
      expect(find.text('Initiating offload...'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
      expect(offloadButton(tester).onPressed, isNull);
      await tester.pump(const Duration(seconds: 1));
      expect(
        find.text('Processed 1/3 (1.2 tracks/s) • 1.flac'),
        findsOneWidget,
      );
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();

      expect(mac.offloaded, ['/m/1.flac', '/m/2.flac', '/m/3.flac']);
      expect(lib.stored, [
        ['/m/1.flac', '/m/2.flac', '/m/3.flac'],
      ]);
      expect(lib.rebuilds, 1);
      expect(find.text('Offload complete: 3 tracks analysed.'), findsOneWidget);
      expect(
        find.text(
          'All tracks analyzed. Similarity walks and auto-play are ready.',
        ),
        findsOneWidget,
      );
      expect(find.text('Cancel'), findsNothing);
      expect(offloadButton(tester).onPressed, isNull);
      // About one update a second (the notifier's own clock): the first
      // track lands right after the stage line, the last always shows.
      expect(syncNotes(), [
        'false Sending 3 tracks to Mac…',
        'false 3/3 tracks (1.2 t/s) · 3.flac',
        'false Rebuilding similarity graph…',
        'true Offload complete: 3 tracks analysed.',
      ]);
      await drain(tester);
    });

    testWidgets('one track: "1 track require"', (tester) async {
      make(tester, enabled: true);
      pair();
      await pumpHub(tester, library: _OffloadLib(['/m/1.flac']));
      await connect(tester);
      await showCard(tester);
      expect(find.text('1 track require acoustic analysis.'), findsOneWidget);
      await drain(tester);
    });

    testWidgets('Cancel stops between tracks; what was done is kept and the '
        'graph rebuilt', (tester) async {
      make(tester, enabled: true);
      pair();
      final lib = _OffloadLib(['/m/1.flac', '/m/2.flac', '/m/3.flac']);
      await pumpHub(tester, library: lib);
      await connect(tester);
      await showCard(tester);
      await tester.tap(find.text('Offload to Mac'));
      await tester.pump(const Duration(milliseconds: 1500));
      await tester.tap(find.text('Cancel'));
      await tester.pump();
      expect(find.text('Cancelling offload...'), findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
      expect(mac.offloaded, ['/m/1.flac', '/m/2.flac']);
      expect(lib.rebuilds, 1);
      expect(find.text('DSP offload cancelled (2 of 3 done).'), findsOneWidget);
      expect(find.text('1 track require acoustic analysis.'), findsOneWidget);
      expect(offloadButton(tester).onPressed, isNotNull);
      await drain(tester);
    });

    testWidgets('the Mac refuses the token: unpaired, said so', (tester) async {
      make(tester, enabled: true);
      pair();
      mac.offloadError = const CompanionAuthError('Offload: not paired');
      final lib = _OffloadLib(['/m/1.flac']);
      await pumpHub(tester, library: lib);
      await connect(tester);
      await showCard(tester);
      await tester.tap(find.text('Offload to Mac'));
      await tester.pumpAndSettle();
      const text =
          'DSP offload stopped: the Mac no longer accepts this phone. '
          'Pair again.';
      expect(find.text(text), findsOneWidget);
      expect(host.pref('companion_paired_token'), isEmpty);
      expect(m.isConnected, isFalse);
      expect(lib.rebuilds, 0);
      expect(syncNotes().last, 'true $text');
      await drain(tester);
    });

    testWidgets('the link drops: progress kept, said so', (tester) async {
      make(tester, enabled: true);
      pair();
      mac
        ..offloadError = const SocketException('Lost connection to Mac')
        ..offloadErrorAt = 1;
      final lib = _OffloadLib(['/m/1.flac', '/m/2.flac']);
      await pumpHub(tester, library: lib);
      await connect(tester);
      await showCard(tester);
      await tester.tap(find.text('Offload to Mac'));
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
      const text =
          'DSP offload stopped: lost connection to the Mac. Progress so far '
          'was saved.';
      expect(find.text(text), findsOneWidget);
      expect(lib.stored, [
        ['/m/1.flac'],
      ]);
      // Flet rebuilds only after a run that finished.
      expect(lib.rebuilds, 0);
      await drain(tester);
    });

    testWidgets('nothing missing: the button stays off', (tester) async {
      make(tester, enabled: true);
      pair();
      await pumpHub(tester, library: _OffloadLib([]));
      await connect(tester);
      await showCard(tester);
      expect(
        find.text(
          'All tracks analyzed. Similarity walks and auto-play are ready.',
        ),
        findsOneWidget,
      );
      expect(offloadButton(tester).onPressed, isNull);
      await drain(tester);
    });
  });
}
