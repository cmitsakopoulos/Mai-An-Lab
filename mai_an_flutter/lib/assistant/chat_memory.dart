import 'dart:convert';
import 'dart:io';

import '../core/py_json.dart';

/// `chat_memory.ChatMemoryManager`: the Jarvis conversation and the graph
/// build state, as JSON files in the data folder (`chat_history.json`,
/// `graph_state.json`), written as `json.dump(…, indent=4)` writes them so
/// the Flet app and the port read each other's.
///
/// A session expires lazily: loading it after [timeoutSeconds] without
/// activity deletes the file and starts afresh.
class ChatMemory {
  ChatMemory(
    this.dataDir, {
    this.filename = 'chat_history.json',
    this.timeoutSeconds = 900,
    double Function()? clock,
  }) : _clock = clock ?? _now;

  final String dataDir;
  final String filename;
  final int timeoutSeconds;
  final double Function() _clock;

  static double _now() => DateTime.now().microsecondsSinceEpoch / 1e6;

  /// The most messages kept on disk (`messages[-50:]`).
  static const maxMessages = 50;

  String get historyPath => '$dataDir${Platform.pathSeparator}$filename';
  String get graphStatePath =>
      '$dataDir${Platform.pathSeparator}graph_state.json';

  /// `load_session`: the active session, or an empty one when there is
  /// none, it can't be read, or it has expired (which also deletes it).
  ChatSession loadSession() {
    final f = File(historyPath);
    if (!f.existsSync()) return ChatSession.empty();
    final Object? data;
    try {
      data = jsonDecode(f.readAsStringSync());
    } catch (_) {
      return ChatSession.empty();
    }
    // Python would raise on a file that isn't an object; an unreadable
    // session is an empty one here.
    if (data is! Map<String, Object?>) return ChatSession.empty();
    final last = data['last_active_timestamp'];
    final lastActive = last is num ? last.toDouble() : 0.0;
    if (_clock() - lastActive > timeoutSeconds) {
      clearSession();
      return ChatSession.empty();
    }
    final raw = data['messages'];
    return ChatSession(
      messages: [
        if (raw is List)
          for (final m in raw)
            if (m is Map<String, Object?>) m,
      ],
      initGreeted: data['init_greeted'] == true,
      lastActive: lastActive,
    );
  }

  /// `save_session`: the newest [maxMessages] messages, stamped now.
  /// Failures are swallowed, as Flet logs and carries on (a message that
  /// can't be serialised leaves the previous file in place).
  void saveSession(List<Map<String, Object?>> messages, bool initGreeted) {
    final keep = messages.length > maxMessages
        ? messages.sublist(messages.length - maxMessages)
        : messages;
    try {
      final text = pyJsonDumps({
        'messages': keep,
        'init_greeted': initGreeted,
        'last_active_timestamp': _clock(),
      }, indent: 4);
      File(historyPath).writeAsStringSync(text);
    } catch (_) {}
  }

  /// `touch_session`: keeps a non-empty session alive.
  void touchSession() {
    final s = loadSession();
    if (s.messages.isEmpty) return;
    saveSession(s.messages, s.initGreeted);
  }

  /// `clear_session`: deletes the file.
  void clearSession() {
    try {
      final f = File(historyPath);
      if (f.existsSync()) f.deleteSync();
    } catch (_) {}
  }

  /// `load_graph_state`: `{total_tracks, missing_count}` (-1 each when
  /// unknown).
  Map<String, Object?> loadGraphState() {
    const unknown = {'total_tracks': -1, 'missing_count': -1};
    final f = File(graphStatePath);
    if (!f.existsSync()) return Map.of(unknown);
    try {
      final data = jsonDecode(f.readAsStringSync());
      if (data is Map<String, Object?>) return data;
    } catch (_) {}
    return Map.of(unknown);
  }

  /// `save_graph_state`.
  void saveGraphState(int totalTracks, int missingCount) {
    try {
      File(graphStatePath).writeAsStringSync(
        pyJsonDumps({
          'total_tracks': totalTracks,
          'missing_count': missingCount,
          'timestamp': _clock(),
        }, indent: 4),
      );
    } catch (_) {}
  }
}

/// One loaded session: `{"messages", "init_greeted",
/// "last_active_timestamp"}`.
class ChatSession {
  ChatSession({
    required this.messages,
    required this.initGreeted,
    required this.lastActive,
  });

  ChatSession.empty() : messages = [], initGreeted = false, lastActive = 0.0;

  final List<Map<String, Object?>> messages;
  final bool initGreeted;
  final double lastActive;
}
