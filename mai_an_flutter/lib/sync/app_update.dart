import 'dart:async';
import 'dart:io';
import 'dart:isolate';

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';

import 'companion_client.dart';

/// Updates of this app, offered by the Mac Companion.
///
/// The Mac puts the offer in every ping (package, version, size, SHA-256).
/// A newer build, or the same version rebuilt (a different hash than the
/// APK we run from), becomes [AppUpdateState.available]; [update] downloads
/// it, checks the hash and hands it to Android's PackageInstaller, which
/// asks the user and refuses anything not signed with our own key.

/// This install, as Android reports it.
class AppBuild {
  const AppBuild({
    required this.package,
    required this.versionCode,
    required this.versionName,
    required this.apkPath,
    this.lastUpdateTime = 0,
  });
  final String package;
  final int versionCode;
  final String versionName;
  final String apkPath;
  final int lastUpdateTime;

  String get label => '$versionName ($versionCode)';
}

/// The Android side, behind an interface for tests.
abstract class AppUpdatePlatform {
  /// Null where updates can't happen (desktop, tests without a platform).
  Future<AppBuild?> info();
  Future<String> sha256Of(String path);
  Future<bool> canInstall();
  Future<void> openInstallSettings();

  /// Start the install; the outcome arrives on [results].
  Future<void> install(String path);

  /// (status, message): confirm, installed, declined, failed.
  Stream<(String, String?)> get results;
  Future<Directory> downloadDir();
}

class AndroidAppUpdatePlatform implements AppUpdatePlatform {
  AndroidAppUpdatePlatform() {
    _ch.setMethodCallHandler((call) async {
      if (call.method == 'appUpdateStatus') {
        final a = Map<String, Object?>.from(call.arguments as Map);
        _results.add(('${a['status']}', a['message'] as String?));
      }
      return null;
    });
  }

  static const _ch = MethodChannel('mai_an/app');
  final _results = StreamController<(String, String?)>.broadcast();

  @override
  Stream<(String, String?)> get results => _results.stream;

  @override
  Future<AppBuild?> info() async {
    if (!Platform.isAndroid) return null;
    try {
      final m = await _ch.invokeMapMethod<String, Object?>('appInfo');
      if (m == null) return null;
      return AppBuild(
        package: '${m['package']}',
        versionCode: (m['versionCode'] as num).toInt(),
        versionName: '${m['versionName']}',
        apkPath: '${m['apkPath']}',
        lastUpdateTime: (m['lastUpdateTime'] as num?)?.toInt() ?? 0,
      );
    } catch (e) {
      debugPrint('appInfo unavailable: $e');
      return null;
    }
  }

  @override
  Future<String> sha256Of(String path) => Isolate.run(
    () async => '${await sha256.bind(File(path).openRead()).first}',
  );

  @override
  Future<bool> canInstall() async =>
      await _ch.invokeMethod<bool>('canInstallPackages') ?? false;

  @override
  Future<void> openInstallSettings() => _ch.invokeMethod('openInstallSettings');

  @override
  Future<void> install(String path) => _ch.invokeMethod('installApk', path);

  @override
  Future<Directory> downloadDir() async {
    final d = Directory('${(await getTemporaryDirectory()).path}/app_update');
    await d.create(recursive: true);
    return d;
  }
}

enum AppUpdateState {
  /// Nothing offered, or nothing newer.
  none,

  /// The Mac offers exactly what we run.
  upToDate,
  available,
  downloading,
  verifying,

  /// "Install unknown apps" is off for this app.
  needsPermission,
  installing,
  failed,
}

class AppUpdater extends ChangeNotifier {
  AppUpdater({
    required this.pref,
    required this.savePref,
    AppUpdatePlatform? platform,
  }) : platform = platform ?? AndroidAppUpdatePlatform();

  final AppUpdatePlatform platform;
  final Object? Function(String key) pref;
  final Future<void> Function(String key, Object? value) savePref;

  /// A new build turned up (once per build): the app shows its dialog.
  void Function(AppUpdater updater)? onAvailable;

  AppBuild? build;
  AppUpdateState state = AppUpdateState.none;
  Map<String, Object?>? offer;
  CompanionClient? _client;
  double? progress;
  String error = '';
  File? _downloaded;
  bool _cancel = false;
  String? _ownSha;
  int _ownShaKey = -1;
  StreamSubscription<(String, String?)>? _sub;

  static const _reportedPref = 'app_update_reported_sha';
  static const _declinedPref = 'app_update_declined_sha';

  bool get enabled => build != null;
  bool get busy =>
      state == AppUpdateState.downloading ||
      state == AppUpdateState.verifying ||
      state == AppUpdateState.installing;

  String get offerLabel {
    final o = offer;
    return o == null ? '' : '${o['version_name']} (${o['version_code']})';
  }

  /// "What's new", as written on the Mac's page ('' when none).
  String get notes => '${offer?['notes'] ?? ''}'.trim();

  int get offerSize => (offer?['size'] as num?)?.toInt() ?? 0;
  bool get offerIsDebug => offer?['debuggable'] == true;

  /// Same version number as ours: a rebuild, not a newer release.
  bool get isRebuild =>
      (offer?['version_code'] as num?)?.toInt() == build?.versionCode;

  /// Read this install's version and tell [CompanionClient] to send it.
  Future<void> init() async {
    build = await platform.info();
    final b = build;
    if (b == null) return;
    CompanionClient.appHeaders = {
      'X-App-Version': '${b.versionName}+${b.versionCode}',
      'X-App-Package': b.package,
    };
    _sub = platform.results.listen(_onResult);
    // A previous run's download is useless once we've moved on.
    unawaited(_cleanup());
  }

  Future<void> _cleanup() async {
    try {
      final d = await platform.downloadDir();
      if (state == AppUpdateState.none || state == AppUpdateState.upToDate) {
        for (final f in d.listSync()) {
          f.deleteSync();
        }
      }
    } catch (_) {}
  }

  Future<String?> _ownHash() async {
    final b = build;
    if (b == null) return null;
    if (_ownShaKey == b.lastUpdateTime && _ownSha != null) return _ownSha;
    try {
      _ownSha = await platform.sha256Of(b.apkPath);
      _ownShaKey = b.lastUpdateTime;
    } catch (e) {
      debugPrint('hashing our own APK failed: $e');
      _ownSha = null;
    }
    return _ownSha;
  }

  bool _evaluating = false;
  Object? _promptedSha;

  /// Every ping's offer (null: none). Cheap when nothing changed.
  Future<void> handleOffer(
    CompanionClient client,
    Map<String, Object?>? o,
  ) async {
    _client = client;
    final b = build;
    if (b == null || _evaluating) return;
    final sameOffer = o?['sha256'] == offer?['sha256'];
    // Notes can be edited on the Mac after the build is offered.
    if (sameOffer &&
        o != null &&
        offer != null &&
        o['notes'] != offer!['notes']) {
      offer = {...offer!, 'notes': o['notes']};
      notifyListeners();
    }
    if (busy && sameOffer) return;
    if (sameOffer &&
        offer != null &&
        state != AppUpdateState.none &&
        state != AppUpdateState.upToDate) {
      return;
    }
    _evaluating = true;
    try {
      offer = o;
      final next = await _judge(b, o);
      if (next == AppUpdateState.upToDate) await _reportInstalled(client);
      if (!busy) {
        state = next;
        error = '';
      }
      notifyListeners();
      final sha = o?['sha256'];
      if (next == AppUpdateState.available &&
          sha != _promptedSha &&
          pref(_declinedPref) != sha) {
        _promptedSha = sha;
        onAvailable?.call(this);
      }
    } finally {
      _evaluating = false;
    }
  }

  Future<AppUpdateState> _judge(AppBuild b, Map<String, Object?>? o) async {
    if (o == null || o['package'] != b.package) return AppUpdateState.none;
    final code = (o['version_code'] as num?)?.toInt() ?? 0;
    if (code < b.versionCode) return AppUpdateState.none;
    if (code > b.versionCode) return AppUpdateState.available;
    final mine = await _ownHash();
    if (mine == null) return AppUpdateState.none;
    return mine == o['sha256']
        ? AppUpdateState.upToDate
        : AppUpdateState.available;
  }

  /// The Mac learns we run its build (after an install, the new process
  /// does this on its first ping). Once per build.
  Future<void> _reportInstalled(CompanionClient c) async {
    final sha = offer?['sha256'];
    if (sha == null || pref(_reportedPref) == sha) return;
    await savePref(_reportedPref, sha);
    await c.postAppStatus('installed');
  }

  /// "Not now": don't pop the dialog again for this build.
  Future<void> decline() async {
    final sha = offer?['sha256'];
    if (sha != null) await savePref(_declinedPref, sha);
    unawaited(_client?.postAppStatus('declined'));
  }

  void cancel() => _cancel = true;

  /// Download, verify, install.
  Future<void> update() async {
    final c = _client, o = offer;
    if (c == null || o == null || busy) return;
    _cancel = false;
    error = '';
    try {
      var f = _downloaded;
      if (f == null || !f.existsSync() || f.lengthSync() != offerSize) {
        state = AppUpdateState.downloading;
        progress = 0;
        notifyListeners();
        final dir = await platform.downloadDir();
        f = File('${dir.path}/mai_an_${o['version_code']}.apk');
        await c.downloadAppUpdate(
          '${o['path'] ?? '/api/v1/app/apk'}',
          f,
          cancelled: () => _cancel,
          progress: (got, total) {
            final t = total > 0 ? total : offerSize;
            progress = t > 0 ? got / t : null;
            notifyListeners();
          },
        );
        state = AppUpdateState.verifying;
        progress = null;
        notifyListeners();
        final digest = await platform.sha256Of(f.path);
        if (digest != o['sha256']) {
          try {
            f.deleteSync();
          } catch (_) {}
          throw const CompanionHttpError(
            'The download was damaged (checksum mismatch). Try again.',
          );
        }
        _downloaded = f;
        unawaited(c.postAppStatus('downloaded'));
      }
      if (!await platform.canInstall()) {
        state = AppUpdateState.needsPermission;
        notifyListeners();
        return;
      }
      await _install(c, f);
    } catch (e) {
      final cancelled = _cancel;
      _fail(cancelled ? '' : _describe(e), report: !cancelled);
      if (cancelled) {
        state = AppUpdateState.available;
        notifyListeners();
      }
    }
  }

  Future<void> _install(CompanionClient c, File f) async {
    state = AppUpdateState.installing;
    notifyListeners();
    unawaited(c.postAppStatus('installing'));
    await platform.install(f.path);
  }

  /// Settings → Install unknown apps; [resumed] continues on the way back.
  Future<void> allowInstalls() => platform.openInstallSettings();

  /// The app came back to the foreground: carry on if installs are now
  /// allowed.
  Future<void> resumed() async {
    if (state != AppUpdateState.needsPermission) return;
    final f = _downloaded, c = _client;
    if (f == null || c == null) return;
    if (await platform.canInstall()) {
      try {
        await _install(c, f);
      } catch (e) {
        _fail(_describe(e));
      }
    }
  }

  void _onResult((String, String?) r) {
    final (status, message) = r;
    switch (status) {
      case 'confirm':
        break; // Android's dialog is up
      case 'installed':
        state = AppUpdateState.upToDate;
        notifyListeners();
      case 'declined':
        state = AppUpdateState.available;
        notifyListeners();
        unawaited(_client?.postAppStatus('declined'));
      default:
        _fail(message ?? 'The install failed');
    }
  }

  void _fail(String why, {bool report = true}) {
    state = AppUpdateState.failed;
    error = why;
    progress = null;
    notifyListeners();
    if (report && why.isNotEmpty) {
      unawaited(_client?.postAppStatus('failed', error: why));
    }
  }

  String _describe(Object e) {
    if (e is CompanionAuthError) {
      return 'The Mac no longer accepts this phone. Pair again.';
    }
    if (e is CompanionHttpError) return e.message;
    if (isNetworkError(e)) {
      return 'Lost the connection to the Mac during the download.';
    }
    if (e is PlatformException) return e.message ?? e.code;
    return '$e';
  }

  @override
  void dispose() {
    unawaited(_sub?.cancel());
    super.dispose();
  }
}

String formatBytes(int n) => n >= 1 << 20
    ? '${(n / (1 << 20)).toStringAsFixed(1)} MB'
    : '${(n / 1024).toStringAsFixed(0)} kB';
