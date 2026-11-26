import 'dart:convert';
import 'dart:io';

/// Where the Mac side keeps its files: one folder, nothing else on disk.
///
///   state.json        pairing token, receive folder, last phone
///   features.jsonl    DSP features already computed (the "Mac cache")
///   transfers.json    tracks queued for the phone
///   received.json     tracks the phone sent us (the UI's list)
///   state_backups/    state bundles the phone uploaded
///   outbox/           files dropped into the web UI, waiting to go out
///   updates/          the phone app build on offer (current.apk + .json)
///   cache/            AAC transcodes, scratch audio
class MaiAnHome {
  MaiAnHome(String root) : root = Directory(root) {
    for (final d in [this.root, backups, outbox, cache, tmp]) {
      d.createSync(recursive: true);
    }
  }

  /// `$MAIAN_HOME`, else `~/Library/Application Support/MaiAn` on macOS and
  /// `~/.maian` elsewhere.
  factory MaiAnHome.resolve([String? override]) {
    final env = Platform.environment;
    final home = env['HOME'] ?? env['USERPROFILE'] ?? Directory.systemTemp.path;
    final path =
        override ??
        env['MAIAN_HOME'] ??
        (Platform.isMacOS
            ? '$home/Library/Application Support/MaiAn'
            : '$home/.maian');
    return MaiAnHome(path);
  }

  final Directory root;

  Directory get backups => Directory('${root.path}/state_backups');
  Directory get outbox => Directory('${root.path}/outbox');
  Directory get cache => Directory('${root.path}/cache');
  Directory get tmp => Directory('${root.path}/cache/tmp');
  File get stateFile => File('${root.path}/state.json');
  File get featuresFile => File('${root.path}/features.jsonl');
  File get transfersFile => File('${root.path}/transfers.json');
  File get receivedFile => File('${root.path}/received.json');

  /// The control page's port while `maian serve` runs, so `maian send` and
  /// `maian status` find it.
  File get uiPortFile => File('${root.path}/ui.port');

  /// `~/Music/MaiAn`: where tracks sent from the phone land.
  String get defaultReceiveDir {
    final env = Platform.environment;
    final home = env['HOME'] ?? env['USERPROFILE'] ?? root.path;
    return '$home/Music/MaiAn';
  }
}

/// The few settings that outlive a run. The token is the important one: a
/// token minted per launch would unpair every phone each time the Mac
/// program restarts (the phone would look connected and then 401).
class Settings {
  Settings._(this._file, this._data);

  final File _file;
  final Map<String, Object?> _data;

  static Settings load(MaiAnHome home) {
    Map<String, Object?> data = {};
    try {
      final v = jsonDecode(home.stateFile.readAsStringSync());
      if (v is Map<String, Object?>) data = v;
    } catch (_) {}
    return Settings._(home.stateFile, data);
  }

  String? get token => _data['token'] as String?;
  String? get receiveDir => _data['receive_dir'] as String?;
  String get lastClient => (_data['last_client'] as String?) ?? '';
  String get quality => (_data['quality'] as String?) ?? 'aac_vbr_max';
  bool get notify => _data['notify'] != false;

  Object? get(String key) => _data[key];

  void set(String key, Object? value) {
    if (value == null) {
      _data.remove(key);
    } else {
      _data[key] = value;
    }
    atomicWrite(_file, jsonEncode(_data));
  }
}

/// Write-then-rename, so a crash never leaves half a JSON file.
void atomicWrite(File f, String text) {
  final tmp = File('${f.path}.tmp');
  tmp.writeAsStringSync(text);
  tmp.renameSync(f.path);
}
