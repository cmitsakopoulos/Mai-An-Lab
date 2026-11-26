import 'dart:io';

/// The bits of macOS the control page reaches through: native file pickers
/// (AppleScript, so the page gets real paths instead of uploaded copies) and
/// the login item (a LaunchAgent).

String _q(String s) => '"${s.replaceAll('\\', '\\\\').replaceAll('"', '\\"')}"';

/// Finder's "choose file" dialog. Empty when cancelled or not on macOS.
/// [extensions] limits the choice ("apk", "flac", …).
Future<List<String>> chooseFiles({
  String prompt = 'Choose files',
  List<String> extensions = const [],
  bool multiple = true,
}) async {
  if (!Platform.isMacOS) return const [];
  final types = extensions.isEmpty
      ? ''
      : ' of type {${extensions.map(_q).join(', ')}}';
  final script = [
    'set picked to choose file with prompt ${_q(prompt)}$types'
        '${multiple ? ' with multiple selections allowed' : ''}',
    if (multiple) ...[
      'set out to ""',
      'repeat with f in picked',
      'set out to out & POSIX path of f & linefeed',
      'end repeat',
      'return out',
    ] else
      'return POSIX path of picked',
  ];
  return _lines(await _osascript(script));
}

/// Finder's "choose folder" dialog; null when cancelled.
Future<String?> chooseFolder({String prompt = 'Choose a folder'}) async {
  if (!Platform.isMacOS) return null;
  final out = _lines(
    await _osascript([
      'return POSIX path of (choose folder with prompt ${_q(prompt)})',
    ]),
  );
  if (out.isEmpty) return null;
  final p = out.first;
  return p.length > 1 && p.endsWith('/') ? p.substring(0, p.length - 1) : p;
}

/// `activate` (osascript itself, so no Automation permission prompt)
/// brings the dialog in front of the browser.
Future<String> _osascript(List<String> lines) async {
  final args = <String>[
    '-e',
    'activate',
    for (final l in lines) ...['-e', l],
  ];
  try {
    final r = await Process.run('/usr/bin/osascript', args);
    return r.exitCode == 0 ? '${r.stdout}' : '';
  } catch (_) {
    return '';
  }
}

List<String> _lines(String s) => [
  for (final l in s.split('\n'))
    if (l.trim().isNotEmpty) l.trim(),
];

// ── login item ──────────────────────────────────────────────────────────────

const _label = 'lab.maian.companion';

File _agentFile() {
  final home = Platform.environment['HOME'] ?? '';
  return File('$home/Library/LaunchAgents/$_label.plist');
}

/// `…/Mai An Companion.app` when this binary runs from inside the app
/// bundle, else null (a bare `maian` from the build folder).
String? appBundlePath() {
  final exe = Platform.resolvedExecutable;
  final i = exe.indexOf('.app/Contents/');
  return i < 0 ? null : exe.substring(0, i + 4);
}

bool loginItemEnabled() => _agentFile().existsSync();

/// Start the companion at login (quietly: no browser tab). Through the app
/// bundle when there is one, so macOS attributes network access to it.
Future<void> setLoginItem(bool on) async {
  final f = _agentFile();
  if (!on) {
    try {
      f.deleteSync();
    } catch (_) {}
    return;
  }
  final app = appBundlePath();
  final args = app != null
      ? ['/usr/bin/open', '-g', '-a', app, '--args', '--no-open']
      : [Platform.resolvedExecutable, 'serve', '--no-open'];
  String esc(String s) => s
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;');
  f.parent.createSync(recursive: true);
  f.writeAsStringSync('''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$_label</string>
  <key>ProgramArguments</key>
  <array>
${args.map((a) => '    <string>${esc(a)}</string>').join('\n')}
  </array>
  <key>RunAtLoad</key><true/>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict>
</plist>
''');
}
