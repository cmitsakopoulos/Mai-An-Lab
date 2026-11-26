import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';

import 'apk.dart';
import 'home.dart';

/// The phone app's build the Mac is offering, and the watcher that picks up
/// new builds on its own.
///
/// One APK at a time lives in `updates/current.apk`, a copy: rebuilding the
/// Flutter app mid-download must not change the bytes under the phone. The
/// phone sees the offer in every ping (version, size, SHA-256), downloads
/// `/api/v1/app/apk`, checks the hash, and hands it to Android's installer,
/// which also refuses anything not signed with the installed app's key.
class AppUpdates {
  AppUpdates(this.home, this.settings, {this.log, this.onChange}) {
    dir.createSync(recursive: true);
    try {
      final v = jsonDecode(_meta.readAsStringSync());
      if (v is Map<String, Object?> && apk.existsSync()) current = v;
    } catch (_) {}
  }

  final MaiAnHome home;
  final Settings settings;
  final void Function(String)? log;
  final void Function()? onChange;

  Directory get dir => Directory('${home.root.path}/updates');
  File get apk => File('${dir.path}/current.apk');
  File get _meta => File('${dir.path}/current.json');

  /// The offered build's metadata, or null.
  Map<String, Object?>? current;

  /// What the phone last told us about the offered build
  /// (`downloading`, `installing`, `installed`, `failed`) and when.
  Map<String, Object?>? phoneStatus;
  int downloads = 0;

  /// Where the watcher looks for `app-release.apk`; null = not watching.
  String? get watchDir {
    final d = settings.get('apk_watch_dir');
    return d is String && d.isNotEmpty ? d : null;
  }

  bool get auto => settings.get('apk_auto') != false;

  Timer? _timer;
  String? _lastKey;

  /// A build on this Mac is writing the APK and will offer it itself.
  bool hold = false;
  bool _busy = false;
  String? watchError;

  /// The payload the phone sees in `/ping`.
  Map<String, Object?>? get offer {
    final c = current;
    if (c == null) return null;
    return {
      'package': c['package'],
      'version_code': c['version_code'],
      'version_name': c['version_name'],
      'sha256': c['sha256'],
      'size': c['size'],
      'debuggable': c['debuggable'],
      'published_at': c['published_at'],
      'notes': c['notes'] ?? '',
      'path': '/api/v1/app/apk',
    };
  }

  /// Offer [path] to the phone. Throws [FormatException] if it isn't an APK.
  Future<Map<String, Object?>> publish(
    String path, {
    String source = 'manual',
    String? notes,
  }) async {
    final info = ApkInfo.read(path);
    // The notes written for "the next build" go with this one.
    notes ??= draftNotes;
    final tmp = File('${dir.path}/incoming.apk.tmp');
    await File(path).copy(tmp.path);
    final digest = await sha256.bind(tmp.openRead()).first;
    tmp.renameSync(apk.path);
    final meta = <String, Object?>{
      'package': info.package,
      'version_code': info.versionCode,
      'version_name': info.versionName,
      'debuggable': info.debuggable,
      'min_sdk': info.minSdk,
      'sha256': '$digest',
      'size': apk.lengthSync(),
      'source': source,
      'file_name': path.split('/').last,
      'published_at': DateTime.now().millisecondsSinceEpoch,
      'notes': notes.trim(),
    };
    atomicWrite(_meta, jsonEncode(meta));
    settings.set('update_notes_draft', '');
    current = meta;
    phoneStatus = null;
    downloads = 0;
    log?.call(
      'Offering app ${info.label}${info.debuggable ? ' (debug build)' : ''} '
      'to the phone',
    );
    onChange?.call();
    return meta;
  }

  /// "What's new" for the next build offered (written on the page before
  /// building).
  String get draftNotes => '${settings.get('update_notes_draft') ?? ''}';

  void setDraftNotes(String text) {
    settings.set('update_notes_draft', _clip(text));
    onChange?.call();
  }

  /// Change the offered build's notes; the phone sees them in its next ping.
  void setNotes(String text) {
    final c = current;
    if (c == null) throw const FormatException('No app update on offer');
    c['notes'] = _clip(text).trim();
    atomicWrite(_meta, jsonEncode(c));
    onChange?.call();
  }

  static String _clip(String s) => s.length > 4000 ? s.substring(0, 4000) : s;

  void withdraw() {
    for (final f in [apk, _meta]) {
      try {
        f.deleteSync();
      } catch (_) {}
    }
    current = null;
    phoneStatus = null;
    log?.call('App update withdrawn');
    onChange?.call();
  }

  void setPhoneStatus(Map<String, Object?> s) {
    phoneStatus = {...s, 'at': DateTime.now().millisecondsSinceEpoch};
    onChange?.call();
  }

  // ── watching a build folder ───────────────────────────────────────────────

  void setWatchDir(String? d) {
    // '' rather than removed: "never watch" must survive the launcher's
    // default on the next start.
    settings.set('apk_watch_dir', d ?? '');
    _lastKey = null;
    watchError = null;
    onChange?.call();
    unawaited(poll());
  }

  void setAuto(bool v) {
    settings.set('apk_auto', v);
    _lastKey = null;
    onChange?.call();
    unawaited(poll());
  }

  void start({Duration every = const Duration(seconds: 4)}) {
    _timer ??= Timer.periodic(every, (_) => poll());
    unawaited(poll());
  }

  void stop() {
    _timer?.cancel();
    _timer = null;
  }

  /// The newest build in the watched folder, if auto-publish is on and it
  /// differs from what is offered. A file still being written (its size or
  /// time changed since the last poll) waits for the next one.
  Future<void> poll() async {
    final d = watchDir;
    if (_busy || hold || d == null || !auto) return;
    final f = File('$d/app-release.apk');
    if (!f.existsSync()) return;
    _busy = true;
    try {
      final st = f.statSync();
      final key = '${st.modified.millisecondsSinceEpoch}:${st.size}';
      if (key == _lastKey) return;
      final settled = _pendingKey == key;
      _pendingKey = key;
      if (!settled) return;
      _lastKey = key;
      final digest = '${await sha256.bind(f.openRead()).first}';
      if (digest == current?['sha256']) return;
      final info = ApkInfo.read(f.path);
      final cur = current;
      if (cur != null &&
          cur['package'] == info.package &&
          (cur['version_code'] as int? ?? 0) > info.versionCode) {
        log?.call(
          'New build ${info.label} is older than the offered '
          '${cur['version_name']} (${cur['version_code']}): not offered',
        );
        return;
      }
      await publish(f.path, source: 'build folder');
      watchError = null;
    } catch (e) {
      watchError = '$e';
      log?.call('Could not read the new build: $e');
      onChange?.call();
    } finally {
      _busy = false;
    }
  }

  String? _pendingKey;
}

/// The Flutter app's release output next to this checkout, if there is one:
/// `../mai_an_flutter/build/app/outputs/flutter-apk`.
String? guessBuildDir(String fromDir) {
  var d = Directory(fromDir).absolute;
  for (var i = 0; i < 6; i++) {
    final cand = Directory(
      '${d.path}/mai_an_flutter/build/app/outputs/flutter-apk',
    );
    if (Directory('${d.path}/mai_an_flutter').existsSync()) return cand.path;
    final up = d.parent;
    if (up.path == d.path) break;
    d = up;
  }
  return null;
}
