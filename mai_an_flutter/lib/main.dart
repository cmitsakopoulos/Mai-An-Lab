import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:maian_native/maian_native.dart';
import 'package:path_provider/path_provider.dart';
import 'package:permission_handler/permission_handler.dart';

import 'core/app_config.dart';
import 'core/haptics.dart';
import 'data/library_repository.dart';
import 'library/library_controller.dart';
import 'library/library_screen.dart';
import 'player/audio_handler.dart';
import 'player/player_controller.dart';
import 'player/queue_store.dart';
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

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final dataDir = await resolveDataDir();
  final config = await AppConfig.load(dataDir);
  final dbPath = _dbOverride.isNotEmpty
      ? _dbOverride
      : '$dataDir${Platform.pathSeparator}library.db';
  final library = LibraryController(
    () => LibraryRepository.open(dbPath),
    sort: config.librarySort,
  )..load();
  final player = ValueNotifier<PlayerController?>(null);
  runApp(
    MaiAnApp(
      library: library,
      player: player,
      accent: config.accent ?? Tokens.accent,
      haptics: Haptics(config.haptics),
    ),
  );
  player.value = await _startPlayer(dataDir, library);
}

/// Permissions first, as the Flet extension did (`_requestRuntimePermissions`
/// before AudioService.init): restoring the queue preloads the saved track,
/// and without file access that load fails and the skip-on-error logic would
/// walk the queue forward.
Future<PlayerController> _startPlayer(
  String dataDir,
  LibraryController library,
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
    required this.player,
    required this.accent,
    required this.haptics,
  });

  final LibraryController library;
  final ValueNotifier<PlayerController?> player;
  final Color accent;
  final Haptics haptics;

  @override
  State<MaiAnApp> createState() => _MaiAnAppState();
}

class _MaiAnAppState extends State<MaiAnApp> {
  late final AppLifecycleListener _lifecycle;
  StreamSubscription<NotificationAction>? _notifications;

  @override
  void initState() {
    super.initState();
    // Flet's _on_lifecycle: hide/pause = background, resume = foreground.
    // Flutter stops drawing by itself; the player stops its position ticker
    // and saves where it is.
    _lifecycle = AppLifecycleListener(
      onHide: () => widget.player.value?.setForeground(false),
      onShow: () => widget.player.value?.setForeground(true),
    );
    // Notification taps arrive here. With one screen there is nowhere to
    // route them yet; listening (and draining a cold-start tap) keeps the
    // Kotlin side from holding them. Routing lands with the other screens.
    _notifications = MaianNative.notificationActions.listen((_) {});
    MaianNative.takePendingNavigate();
  }

  @override
  void dispose() {
    _lifecycle.dispose();
    _notifications?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Mai An Lab',
      debugShowCheckedModeBanner: false,
      theme: Tokens.theme(accentColor: widget.accent),
      home: LibraryScreen(
        controller: widget.library,
        player: widget.player,
        haptics: widget.haptics,
      ),
    );
  }
}
