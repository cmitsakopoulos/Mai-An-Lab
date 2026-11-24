import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:maian_native/maian_native.dart';
import 'package:path_provider/path_provider.dart';
import 'package:permission_handler/permission_handler.dart';

import 'app_shell.dart';
import 'assistant/assistant_controller.dart';
import 'assistant/chat_memory.dart';
import 'assistant/voice_service.dart';
import 'core/app_config.dart';
import 'core/cache_pruner.dart';
import 'core/config_file.dart';
import 'core/haptics.dart';
import 'core/notification_router.dart';
import 'data/library_repository.dart';
import 'data/play_ledger.dart';
import 'downloads/app_download_host.dart';
import 'downloads/download_queue.dart';
import 'library/browse_controller.dart';
import 'library/library_controller.dart';
import 'player/audio_handler.dart';
import 'player/autoplay.dart';
import 'player/player_controller.dart';
import 'player/queue_store.dart';
import 'scan/library_folder.dart';
import 'search/landing_controller.dart';
import 'search/new_releases.dart';
import 'search/search_history.dart';
import 'settings/config_controller.dart';
import 'settings/settings_page.dart';
import 'sync/app_sync_host.dart';
import 'sync/state_bundles.dart';
import 'sync/sync_manager.dart';
import 'ui/tokens.dart';

/// Override with: --dart-define=LIBRARY_DB=/path/library.db
/// (Mac dev: the Flet app's live DB is StreamripApp/.flet/storage/data/library.db)
/// Only the database moves: the port's own files (queue, prefs) stay in its
/// data dir, so a desktop run never writes into the Flet app's.
const _dbOverride = String.fromEnvironment('LIBRARY_DB');

/// The same layout a Flet-built app uses: FLET_APP_STORAGE_DATA is
/// `<application support dir>/data`, and library.db, queue_state.json,
/// flet_prefs.json etc. sit directly in it. On Android that is
/// `/data/user/0/<pkg>/files/data`.
///
/// Keeping the layout identical is what makes the cutover free: once this app
/// ships under the Flet app's package ID, it finds the user's files where
/// they already are. Until then (package suffix `.port`), tool/push_db.sh
/// copies a database in.
Future<String> resolveDataDir() async {
  final support = await getApplicationSupportDirectory();
  final dir = Directory('${support.path}${Platform.pathSeparator}data');
  await dir.create(recursive: true);
  return dir.path;
}

/// Snackbars from outside the widget tree (the download queue, the
/// Companion manager).
final _messenger = GlobalKey<ScaffoldMessengerState>();

/// The root navigator: the Companion manager's PIN and transfer dialogs.
final _navigator = GlobalKey<NavigatorState>();

/// main.py `show_snackbar(text, icon, color)`: the icon in [color] (the
/// accent by default) before the text.
void _snack(String text, {IconData? icon, Color? color}) {
  final m = _messenger.currentState;
  if (m == null) return;
  final content = icon == null
      ? Text(text)
      : Row(
          children: [
            Icon(
              icon,
              size: 20,
              color: color ?? Theme.of(m.context).colorScheme.primary,
            ),
            const SizedBox(width: 10),
            Expanded(child: Text(text)),
          ],
        );
  m
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(content: content));
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final dataDir = await resolveDataDir();
  final dbPath = _dbOverride.isNotEmpty
      ? _dbOverride
      : '$dataDir${Platform.pathSeparator}library.db';
  // The port's own database is migrated and written (the ledger drain); an
  // override (e.g. the Flet app's live database on the Mac) is only read.
  final writable = _dbOverride.isEmpty;
  // main.py `_heavy_init`: a state bundle left in Downloads replaces the
  // state before the library opens. Here before config.toml is read too,
  // so the restored settings apply from the first frame.
  final imported =
      writable &&
      Platform.isAndroid &&
      await StateBundles.bootImport(dataDir: dataDir, dbPath: dbPath);
  // main.py loads config.toml first, creating it from the template when
  // missing (ensure_config_exists).
  final settings = await ConfigController.load(ConfigStore(dataDir));
  final config = settings.config;
  final library = LibraryController(
    () => LibraryRepository.open(dbPath, writable: writable),
    sort: config.librarySort,
    libraryFolder: writable ? () => resolveLibraryFolder(dataDir) : null,
  );
  final loaded = library.load();
  final ledger = writable ? LedgerDrain(dataDir, library.recordListens) : null;
  final browse = BrowseController(
    library,
    views: LibraryView.fromConfig(config.libraryTabs.visible),
  );
  final releasesScheduler = NewReleasesScheduler(
    repository: () => library.repository,
    isEnabled: () => settings.config.landing.newReleases,
  );
  final landing = LandingController(
    library,
    sections: config.landing,
    releasesScheduler: releasesScheduler,
  );
  final searchHistory = SearchHistoryStore(dataDir);
  final cachePruner = CachePruner(
    dataDir,
    nativeArtworkCacheDir: Platform.isAndroid
        ? () async => '${(await getTemporaryDirectory()).path}/np_art'
        : null,
  );
  final player = ValueNotifier<PlayerController?>(null);
  final assistant = AssistantController(
    memory: ChatMemory(dataDir),
    library: library,
    player: player,
    loadAssistantConfig: () => settings.config.assistant,
    voice: FlutterVoiceBackend(),
    onSnack: _snack,
  );
  final haptics = Haptics(config.haptics);
  // queue_controller.py: Mac transfers now, the sources' downloads later.
  final downloadHost = AppDownloadHost(
    dataDir: dataDir,
    library: library,
    haptics: haptics,
    onSnack: _snack,
  );
  final downloads = DownloadQueue(downloadHost);
  // main.py `initialize`: Companion auto-discovery (get_sync_manager, then
  // start when the user left it on). Desktop runs act as the phone until
  // the Mac side is ported (phase 8).
  final syncPrefs = Prefs(dataDir);
  await syncPrefs.load();
  final syncHost = AppSyncHost(
    prefs: syncPrefs,
    navigator: _navigator,
    onSnack: _snack,
    library: library,
    queue: downloads,
  );
  final sync = syncHost.manager = CompanionSyncManager(syncHost);
  downloadHost.sync = sync;
  final bundles = writable
      ? StateBundles(
          dataDir: dataDir,
          dbPath: dbPath,
          library: library,
          config: settings,
          libraryFolder: () => resolveLibraryFolder(dataDir),
        )
      : null;
  // Settings' writes reach the screens at once (Flet: restart_ui).
  settings.addListener(() {
    final c = settings.config;
    browse.setViews(LibraryView.fromConfig(c.libraryTabs.visible));
    landing.setSections(c.landing);
    haptics.config = c.haptics;
  });
  runApp(
    MaiAnApp(
      library: library,
      browse: browse,
      landing: landing,
      assistant: assistant,
      startTab: AppTab.fromStartupPage(
        config.startupPage,
        showJarvis: config.showJarvis,
      ),
      player: player,
      config: settings,
      haptics: haptics,
      ledger: ledger,
      downloads: downloads,
      syncHost: syncHost,
      searchHistory: searchHistory,
      cachePruner: cachePruner,
      settings: SettingsDeps(
        dataDir: dataDir,
        config: settings,
        library: library,
        player: player,
        haptics: haptics,
        sync: sync,
        bundles: bundles,
      ),
    ),
  );
  // After the first frame, so a PIN prompt has a navigator to open on.
  if (sync.enabled) {
    unawaited(WidgetsBinding.instance.endOfFrame.then((_) => sync.start()));
  }
  player.value = await _startPlayer(dataDir, library, ledger);
  // on_native_ready: listens logged while the app was closed.
  await loaded;
  ledger?.schedule(Duration.zero);
  // After an auto-import Flet rebuilds the similarity graph (the artist
  // tier, then the coordinates and the genre model).
  if (imported) {
    try {
      await library.rebuildSimilarityGraph();
    } catch (e) {
      debugPrint('Auto-import: graph rebuild failed: $e');
    }
  }
  // main.py `_ensure_graph_built_async`: analysed tracks without
  // coordinates get them, once, behind the UI.
  if (writable) unawaited(library.ensureGraphBuilt());
  // main.py `ensure_new_releases_refresh` + `_prune_caches_async`.
  if (writable) {
    releasesScheduler.ensureRefresh(delay: NewReleasesScheduler.bootDelay);
  }
  unawaited(cachePruner.pruneIfDue());
  // main.py `_auto_export_state_snapshot`, fire and forget. On the phone
  // only: desktop runs are development until phase 8 (§7).
  if (Platform.isAndroid) unawaited(bundles?.autoExport());
}

/// Permissions first, as the Flet extension did (`_requestRuntimePermissions`
/// before AudioService.init): restoring the queue preloads the saved track,
/// and without file access that load fails and the skip-on-error logic would
/// walk the queue forward.
Future<PlayerController> _startPlayer(
  String dataDir,
  LibraryController library,
  LedgerDrain? ledger,
) async {
  if (Platform.isAndroid) await _requestPermissions();
  final backend = Platform.isAndroid
      ? await startAudioService()
      : AudioPlayerHandler();
  final player = PlayerController(
    backend: backend,
    store: QueueStore(dataDir),
    prefs: Prefs(dataDir),
    libraryTracks: () async => library.allTracks,
    // Re-read per track change, like Flet's reapply_dsp.
    dspConfig: () async => (await AppConfig.load(dataDir)).dsp,
    dspFeatures: library.dspFeatures,
  );
  player.autoplay = AutoPlay(player: player, library: library);
  // Flet drains on every track change, 3 s later, once the outgoing
  // track's line has landed. A listen is exactly that line being written.
  if (ledger != null) backend.listens.listen((_) => ledger.schedule());
  await player.init();
  return player;
}

/// The Flet list minus RECORD_AUDIO, which waits for the voice features
/// (phase 5) instead of being asked for up front.
Future<void> _requestPermissions() async {
  try {
    await [
      Permission.notification, // POST_NOTIFICATIONS: Android 13+
      Permission.audio, // READ_MEDIA_AUDIO: Android 13+
      Permission.storage, // READ/WRITE_EXTERNAL_STORAGE: ≤ Android 12
    ].request();
    // All-files access is a Settings page, not a dialog: only while missing,
    // so it doesn't reopen on every cold start once granted.
    if (!await Permission.manageExternalStorage.isGranted) {
      await Permission.manageExternalStorage.request();
    }
  } catch (e) {
    debugPrint('permission request failed: $e');
  }
}

class MaiAnApp extends StatefulWidget {
  const MaiAnApp({
    super.key,
    required this.library,
    required this.browse,
    required this.landing,
    required this.player,
    required this.config,
    required this.haptics,
    required this.settings,
    this.assistant,
    this.startTab = AppTab.library,
    this.ledger,
    this.downloads,
    this.syncHost,
    this.searchHistory,
    this.cachePruner,
  });

  final LibraryController library;
  final BrowseController browse;
  final LandingController landing;
  final AssistantController? assistant;
  final AppTab startTab;
  final ValueNotifier<PlayerController?> player;

  /// config.toml: the accent and the pill style follow it.
  final ConfigController config;
  final Haptics haptics;
  final SettingsDeps settings;

  /// Null while the database is opened read-only.
  final LedgerDrain? ledger;

  /// The download queue (the dock and the `downloads` notification target).
  final DownloadQueue? downloads;

  /// The Companion manager's host: told when the app hides and shows.
  final AppSyncHost? syncHost;

  /// Persisted recent searches for the Search tab.
  final SearchHistoryStore? searchHistory;

  /// Periodic artwork & preview cache pruner (`_prune_caches_async`).
  final CachePruner? cachePruner;

  @override
  State<MaiAnApp> createState() => _MaiAnAppState();
}

class _MaiAnAppState extends State<MaiAnApp> {
  late final AppLifecycleListener _lifecycle;
  StreamSubscription<NotificationAction>? _notifications;
  final _router = NotificationRouter();

  @override
  void initState() {
    super.initState();
    // Flet's _on_lifecycle: hide/pause = background, resume = foreground.
    // Flutter stops drawing by itself; the player stops its position ticker
    // and saves where it is.
    _lifecycle = AppLifecycleListener(
      onHide: () {
        widget.player.value?.setForeground(false);
        widget.assistant?.handleAppBackground();
        if (widget.syncHost case final h?) {
          h.inBackground = true;
          h.manager.handleAppBackground();
        }
      },
      onShow: () {
        widget.player.value?.setForeground(true);
        widget.assistant?.handleAppResume();
        // Pick up listens that finished while in the background.
        widget.ledger?.schedule(Duration.zero);
        unawaited(widget.cachePruner?.pruneIfDue());
        // Re-checks a link that may have died while hidden, or re-probes
        // and restarts discovery, and offers transfers queued meanwhile.
        if (widget.syncHost case final h?) {
          h.inBackground = false;
          h.manager.handleAppResume();
        }
      },
    );
    // Notification taps: routed to the screen they name (main.py
    // `_on_notification_navigate`); a cold-start tap is drained once.
    _notifications = MaianNative.notificationActions.listen((a) {
      if (a.job == 'navigate') _router.route(a.action);
    });
    MaianNative.takePendingNavigate().then(_router.route);
  }

  @override
  void dispose() {
    _lifecycle.dispose();
    _notifications?.cancel();
    widget.ledger?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: widget.config,
      builder: (context, _) => MaterialApp(
        title: 'Mai An Lab',
        debugShowCheckedModeBanner: false,
        scaffoldMessengerKey: _messenger,
        navigatorKey: _navigator,
        theme: Tokens.theme(
          accentColor: widget.config.config.accent ?? Tokens.accent,
        ),
        home: AppShell(
          library: widget.library,
          browse: widget.browse,
          landing: widget.landing,
          assistant: widget.assistant,
          showJarvis: widget.config.config.showJarvis,
          unifiedPills: widget.config.config.libraryTabs.unified,
          player: widget.player,
          haptics: widget.haptics,
          startTab: widget.startTab,
          settings: widget.settings,
          router: _router,
          downloads: widget.downloads,
          searchHistory: widget.searchHistory,
        ),
      ),
    );
  }
}
