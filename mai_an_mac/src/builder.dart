import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'home.dart';
import 'updates.dart';

/// Builds the phone app on this Mac (`flutter build apk --release`) and
/// offers the result to the phone, all from the control page.
///
/// A Finder-launched app gets a bare environment (no flutter on PATH, no
/// JAVA_HOME or ANDROID_HOME), so builds run with the login shell's
/// environment, read once.
class AppBuilder {
  AppBuilder(
    this.settings,
    this.updates, {
    this.log,
    this.onChange,
    Future<Map<String, String>> Function()? shellEnv,
  }) : shellEnv = shellEnv ?? loginShellEnv;

  final Settings settings;
  final AppUpdates updates;
  final void Function(String)? log;
  final void Function()? onChange;

  /// The environment builds run with (tests swap it).
  Future<Map<String, String>> Function() shellEnv;

  // ── options (persisted) ───────────────────────────────────────────────────

  /// The Flutter project (the folder with pubspec.yaml); null = unknown.
  String? get project {
    final p = settings.get('flutter_project');
    return p is String && p.isNotEmpty ? p : null;
  }

  void setProject(String? p) {
    settings.set('flutter_project', p ?? '');
    _emit();
  }

  /// Only the 64-bit ARM code (every recent phone): less than half the size.
  bool get arm64 => settings.get('build_arm64') != false;

  /// Raise pubspec.yaml's build number (+N) before building.
  bool get bump => settings.get('build_bump') == true;

  /// Stop Gradle's daemon after a build: it otherwise sits on ~1-2 GB of
  /// memory for hours.
  bool get stopGradle => settings.get('build_stop_gradle') != false;

  void setOption(String key, bool on) {
    const keys = {
      'arm64': 'build_arm64',
      'bump': 'build_bump',
      'stop_gradle': 'build_stop_gradle',
    };
    final k = keys[key];
    if (k == null) throw FormatException('Unknown build option $key');
    settings.set(k, on);
    _emit();
  }

  /// "0.3.0+4" from the project's pubspec.yaml.
  String? get projectVersion {
    final p = project;
    if (p == null) return null;
    try {
      final m = _versionLine.firstMatch(
        File('$p/pubspec.yaml').readAsStringSync(),
      );
      return m?.group(1);
    } catch (_) {
      return null;
    }
  }

  static final _versionLine = RegExp(r'^version:\s*(\S+)\s*$', multiLine: true);

  // ── state ─────────────────────────────────────────────────────────────────

  /// idle, running, publishing, done, failed, cancelled.
  String phase = 'idle';
  String error = '';
  int? startedAt, endedAt;

  /// "0.3.0 (5)" after a successful build.
  String? built;
  final lines = <String>[];
  Process? _proc;
  bool _cancel = false;

  bool get running => phase == 'running' || phase == 'publishing';

  /// The last good build's duration, for a progress estimate.
  int? get lastMs => (settings.get('build_last_ms') as num?)?.toInt();

  Map<String, Object?> toJson() => {
    'project': project,
    'version': projectVersion,
    'phase': phase,
    'error': error,
    'started_at': startedAt,
    'ended_at': endedAt,
    'built': built,
    'last_ms': lastMs,
    'arm64': arm64,
    'bump': bump,
    'stop_gradle': stopGradle,
    'lines': lines.length > 150 ? lines.sublist(lines.length - 150) : lines,
  };

  Timer? _emitTimer;

  /// Flutter can print hundreds of lines a second: the page hears about
  /// them at most four times a second.
  void _emit({bool now = false}) {
    if (now) {
      _emitTimer?.cancel();
      _emitTimer = null;
      onChange?.call();
      return;
    }
    _emitTimer ??= Timer(const Duration(milliseconds: 250), () {
      _emitTimer = null;
      onChange?.call();
    });
  }

  void _line(String s) {
    final clean = s.replaceAll(_ansi, '').trimRight();
    if (clean.isEmpty) return;
    lines.add(clean);
    if (lines.length > 2000) lines.removeRange(0, lines.length - 2000);
    _emit();
  }

  static final _ansi = RegExp(r'\x1b\[[0-9;?]*[A-Za-z]');

  // ── building ──────────────────────────────────────────────────────────────

  /// Build, then offer. Returns when done; check [phase].
  Future<void> build() async {
    if (running) return;
    final dir = project;
    phase = 'running';
    error = '';
    built = null;
    lines.clear();
    _cancel = false;
    startedAt = DateTime.now().millisecondsSinceEpoch;
    endedAt = null;
    updates.hold = true;
    _emit(now: true);
    Map<String, String> env = const {};
    try {
      if (dir == null || !File('$dir/pubspec.yaml').existsSync()) {
        throw const _BuildError(
          'Choose the Flutter project first (the folder with pubspec.yaml)',
        );
      }
      env = await shellEnv();
      final flutter = findExecutable('flutter', env);
      if (flutter == null) {
        throw const _BuildError(
          'Flutter is not on your shell\'s PATH (checked ~/.zprofile and '
          '~/.zshrc)',
        );
      }
      if (bump) _bumpBuildNumber(dir);
      final args = [
        'build',
        'apk',
        '--release',
        if (arm64) ...['--target-platform', 'android-arm64'],
      ];
      log?.call('Building the phone app: flutter ${args.join(' ')}');
      _line('\$ flutter ${args.join(' ')}');
      final p = await Process.start(
        flutter,
        args,
        workingDirectory: dir,
        environment: env,
        includeParentEnvironment: false,
      );
      _proc = p;
      unawaited(p.stdin.close());
      // Both outputs, read to the end (their futures made before either
      // can finish).
      final drained = Future.wait([
        for (final s in [p.stdout, p.stderr])
          s
              .transform(utf8.decoder)
              .transform(const LineSplitter())
              .listen(_line)
              .asFuture<void>(),
      ]);
      final code = await p.exitCode;
      // A cancelled build's grandchildren may hold the pipes a moment longer.
      await drained.timeout(
        Duration(seconds: _cancel ? 2 : 30),
        onTimeout: () => const [],
      );
      _proc = null;
      if (_cancel) throw const _Cancelled();
      if (code != 0) throw _BuildError(_failureReason(code));
      final apk = File('$dir/build/app/outputs/flutter-apk/app-release.apk');
      if (!apk.existsSync() ||
          apk.lastModifiedSync().millisecondsSinceEpoch < startedAt! - 2000) {
        throw const _BuildError('Flutter finished but wrote no new APK');
      }
      phase = 'publishing';
      _emit(now: true);
      final meta = await updates.publish(apk.path, source: 'built here');
      built = '${meta['version_name']} (${meta['version_code']})';
      // Where the next build's "From git" notes start.
      final head = await _git(dir, ['rev-parse', 'HEAD']);
      if (head != null) settings.set('notes_last_commit', head.trim());
      phase = 'done';
      endedAt = DateTime.now().millisecondsSinceEpoch;
      settings.set('build_last_ms', endedAt! - startedAt!);
      log?.call(
        'Built $built in ${_took(endedAt! - startedAt!)}: offered to the phone',
      );
    } on _Cancelled {
      phase = 'cancelled';
      endedAt = DateTime.now().millisecondsSinceEpoch;
      log?.call('Build cancelled');
    } catch (e) {
      phase = 'failed';
      error = e is _BuildError ? e.message : '$e';
      endedAt = DateTime.now().millisecondsSinceEpoch;
      log?.call('Build failed: $error');
    } finally {
      _proc = null;
      updates.hold = false;
      _emit(now: true);
      if (stopGradle && dir != null && env.isNotEmpty) {
        unawaited(_stopGradle(dir, env));
      }
    }
  }

  /// Release notes from git: the project's commit subjects since the last
  /// build made here (the last 10 before the first one), one per line.
  Future<String> gitNotes() async {
    final dir = project;
    if (dir == null)
      throw const FormatException('Choose the Flutter project first');
    final last = settings.get('notes_last_commit');
    final known =
        last is String &&
        last.isNotEmpty &&
        await _git(dir, ['cat-file', '-e', '$last^{commit}']) != null;
    final out = await _git(dir, [
      'log',
      '--no-merges',
      '--format=%s',
      if (known) '$last..HEAD' else ...['-n', '10'],
      '--',
      '.',
    ]);
    if (out == null)
      throw const FormatException('The project is not in a git repository');
    return [
      for (final l in out.split('\n'))
        if (l.trim().isNotEmpty) '- ${l.trim()}',
    ].join('\n');
  }

  static Future<String?> _git(String dir, List<String> args) async {
    try {
      final r = await Process.run('git', ['-C', dir, ...args]);
      return r.exitCode == 0 ? '${r.stdout}' : null;
    } catch (_) {
      return null;
    }
  }

  void cancel() {
    if (!running) return;
    _cancel = true;
    final p = _proc;
    if (p != null) unawaited(_killTree(p.pid));
  }

  /// flutter starts Gradle (and Gradle its workers): stop the children
  /// first, then flutter itself.
  static Future<void> _killTree(int pid) async {
    try {
      final r = await Process.run('pgrep', ['-P', '$pid']);
      for (final c in '${r.stdout}'.split('\n')) {
        final child = int.tryParse(c.trim());
        if (child != null) await _killTree(child);
      }
    } catch (_) {}
    Process.killPid(pid);
  }

  /// The most telling line of a failed build: Gradle's "What went wrong"
  /// answer, an "Error:" line, or the last thing printed.
  String _failureReason(int code) {
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].contains('What went wrong') && i + 1 < lines.length) {
        return lines[i + 1].trim();
      }
    }
    for (final l in lines.reversed) {
      if (RegExp(r'^(Error|error|FAILURE|Target .* failed)').hasMatch(l)) {
        return l.trim();
      }
    }
    return 'flutter exited with code $code';
  }

  void _bumpBuildNumber(String dir) {
    final f = File('$dir/pubspec.yaml');
    final text = f.readAsStringSync();
    final m = RegExp(
      r'^version:\s*([^\s+]+)\+(\d+)\s*$',
      multiLine: true,
    ).firstMatch(text);
    if (m == null) {
      _line('pubspec.yaml has no +build number: not bumped');
      return;
    }
    final next = '${m.group(1)}+${int.parse(m.group(2)!) + 1}';
    f.writeAsStringSync(text.replaceRange(m.start, m.end, 'version: $next'));
    _line('pubspec.yaml: version ${m.group(1)}+${m.group(2)} → $next');
    log?.call('Build number raised to $next');
  }

  Future<void> _stopGradle(String dir, Map<String, String> env) async {
    final gradlew = File('$dir/android/gradlew');
    if (!gradlew.existsSync()) return;
    try {
      await Process.run(
        gradlew.path,
        ['--stop'],
        workingDirectory: '$dir/android',
        environment: env,
        includeParentEnvironment: false,
      ).timeout(const Duration(seconds: 30));
      log?.call('Gradle stopped (frees its memory until the next build)');
    } catch (_) {}
  }

  void dispose() {
    cancel();
    _emitTimer?.cancel();
  }
}

class _BuildError implements Exception {
  const _BuildError(this.message);
  final String message;
}

class _Cancelled implements Exception {
  const _Cancelled();
}

String _took(int ms) {
  final s = (ms / 1000).round();
  return s < 60 ? '$s s' : '${s ~/ 60} min ${s % 60} s';
}

/// [name] on [env]'s PATH, or null.
String? findExecutable(String name, Map<String, String> env) {
  for (final d in (env['PATH'] ?? '').split(':')) {
    if (d.isEmpty) continue;
    final f = File('$d/$name');
    if (f.existsSync()) return f.path;
  }
  return null;
}

Map<String, String>? _envCache;

/// The user's login shell environment (what Terminal would have), so a
/// Finder-launched app can find flutter, Java and the Android SDK. Read once;
/// falls back to this process's own environment.
Future<Map<String, String>> loginShellEnv() async {
  final cached = _envCache;
  if (cached != null) return cached;
  final own = Platform.environment;
  const marker = '__MAIAN_ENV__';
  try {
    final shell = own['SHELL'] ?? '/bin/zsh';
    final p = await Process.start(shell, ['-lic', 'echo $marker; env -0']);
    unawaited(p.stdin.close());
    unawaited(p.stderr.drain<void>());
    final out = p.stdout.transform(utf8.decoder).join();
    final code = await p.exitCode.timeout(
      const Duration(seconds: 20),
      onTimeout: () {
        p.kill();
        return -1;
      },
    );
    final text = await out;
    final i = text.indexOf('$marker\n');
    if (code == 0 && i >= 0) {
      final env = <String, String>{};
      for (final kv in text.substring(i + marker.length + 1).split('\x00')) {
        final eq = kv.indexOf('=');
        if (eq > 0) env[kv.substring(0, eq)] = kv.substring(eq + 1);
      }
      if (env.containsKey('PATH')) return _envCache = env;
    }
  } catch (_) {}
  return own;
}

/// The Flutter app next to this checkout (`../mai_an_flutter`), if any.
String? guessFlutterProject(String fromDir) {
  final apkDir = guessBuildDir(fromDir);
  if (apkDir == null) return null;
  return apkDir.substring(0, apkDir.indexOf('/build/app/outputs'));
}
