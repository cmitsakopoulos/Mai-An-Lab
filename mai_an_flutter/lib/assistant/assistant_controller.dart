import 'dart:async';

import 'package:flutter/foundation.dart';

import '../core/app_config.dart';
import '../core/py_text.dart';
import '../core/py_values.dart';
import '../data/library_repository.dart';
import '../library/library_controller.dart';
import '../player/player_controller.dart';
import 'adapters.dart';
import 'chat_memory.dart';
import 'intent.dart';
import 'runner.dart';

/// State and lifecycle of `ui/views/assistant.py` (`AssistantView`).
///
/// Owns chat scrollback (50-message cap), `ChatMemoryManager` persistence
/// (`chat_history.json` / `graph_state.json`), the init flow (graph status,
/// edge builds, empty-library prompt), entity extraction for anaphora,
/// deferred playback, and the background/resume session expiry hooks.
class AssistantController extends ChangeNotifier {
  AssistantController({
    required this.memory,
    this.library,
    this.player,
    this.loadAssistantConfig,
    AssistantRunner? runner,
    this.onSpeak,
    this.onStopSpeak,
    this.onInitVoice,
    this.initDelay = const Duration(milliseconds: 200),
  }) : _customRunner = runner {
    final session = memory.loadSession();
    _initGreeted = session.initGreeted;
    _messages = [for (final m in session.messages) Map<String, Object?>.of(m)];
    _lastScanGen = library?.scanGeneration ?? 0;
    library?.addListener(_onLibraryChanged);
  }

  final ChatMemory memory;
  final LibraryController? library;
  final ValueListenable<PlayerController?>? player;
  final FutureOr<AssistantConfig?> Function()? loadAssistantConfig;
  final AssistantRunner? _customRunner;

  /// Voice hooks (wired in stage g): speak [text] and stop any in-flight TTS.
  Future<void> Function(String text)? onSpeak;
  Future<void> Function()? onStopSpeak;
  Future<void> Function()? onInitVoice;

  /// Initial delay before `_doInit` runs (200 ms in Flet; 0 in tests).
  final Duration initDelay;

  static const maxMessages = 50;
  static const _nonCanonicalSeeds = {'play_random', 'play_similar_bulk'};

  AssistantRunner? _runner;
  LibraryRepository? _runnerRepo;
  PlayerController? _runnerPlayer;

  List<Map<String, Object?>> _messages = [];
  bool _initGreeted = false;
  bool _initStarted = false;
  bool _thinking = false;
  bool _ttsEnabled = true;
  int _lastScanGen = 0;
  bool _disposed = false;

  List<Map<String, Object?>> get messages => List.unmodifiable(_messages);
  bool get initGreeted => _initGreeted;
  bool get thinking => _thinking;
  bool get ttsEnabled => _ttsEnabled;

  void _onLibraryChanged() {
    final gen = library?.scanGeneration ?? 0;
    if (gen != _lastScanGen) {
      _lastScanGen = gen;
      _initGreeted = false;
    }
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  AssistantRunner? _ensureRunner() {
    if (_customRunner != null) return _customRunner;
    final lib = library;
    final repo = lib?.repository;
    final pl = player?.value;
    if (repo == null || pl == null) return null;
    if (_runner != null &&
        identical(_runnerRepo, repo) &&
        identical(_runnerPlayer, pl)) {
      return _runner;
    }
    _runnerRepo = repo;
    _runnerPlayer = pl;
    _runner = AssistantRunner(
      library: RepoAssistantLibrary(repo, stations: lib!.stations),
      player: ControllerAssistantPlayer(pl),
      loadAssistantConfig: loadAssistantConfig,
    );
    return _runner;
  }

  /// `AssistantView.expand`: trigger lazy init whenever the Jarvis tab opens.
  Future<void> onShow() async {
    if (_initStarted || _disposed) return;
    _initStarted = true;
    try {
      await _doInit();
    } finally {
      _initStarted = false;
    }
  }

  /// `AssistantView.collapse`: stop any in-flight TTS when leaving the tab.
  void onHide() {
    final stop = onStopSpeak;
    if (stop != null) unawaited(stop());
  }

  Future<void> _doInit() async {
    if (initDelay > Duration.zero) {
      await Future<void>.delayed(initDelay);
    }
    if (_disposed) return;

    _ensureRunner();
    final initVoice = onInitVoice;
    if (initVoice != null) unawaited(initVoice());

    final lib = library;
    if (lib == null) return;

    final GraphStatus? status;
    try {
      status = await lib.graphStatus();
    } catch (exc) {
      await _appendBubble('assistant', "Couldn't read your library: $exc");
      return;
    }
    if (status == null || _disposed) return;

    if (status.totalTracks == 0) {
      if (!_initGreeted) {
        _initGreeted = true;
        memory.saveSession(_messages, _initGreeted);
        final hasEmptyMsg = _messages.any(
          (m) =>
              m['sender'] == 'assistant' &&
              '${m['text'] ?? ''}'.contains('Your library looks empty'),
        );
        if (!hasEmptyMsg) {
          await _appendBubble(
            'assistant',
            'Your library looks empty. Scan a music folder in Library → Scan to get started.',
          );
        }
      }
      return;
    }

    final graphState = memory.loadGraphState();
    final needsMetadata = status.artistEdges == 0 && status.albumEdges == 0;
    final needsAcoustic = status.coordTracks == 0 && status.totalTracks >= 2;
    final isUpToDate =
        graphState['total_tracks'] == status.totalTracks &&
        !needsAcoustic &&
        !needsMetadata;

    if (!isUpToDate && lib.canWrite) {
      try {
        await lib.rebuildSimilarityGraph(metadata: true);
        memory.saveGraphState(status.totalTracks, 0);
      } catch (exc) {
        debugPrint('AssistantController: edge build failed: $exc');
      }
    }

    if (!_initGreeted) {
      _initGreeted = true;
      memory.saveSession(_messages, _initGreeted);
    }
  }

  /// `AssistantView._toggle_tts`.
  void toggleTts() {
    _ttsEnabled = !_ttsEnabled;
    if (!_ttsEnabled) {
      final stop = onStopSpeak;
      if (stop != null) unawaited(stop());
    }
    _notify();
  }

  /// `AssistantView._handle_user_text`.
  Future<void> sendUserText(String rawText) async {
    final text = pyStrip(rawText);
    if (text.isEmpty || _disposed) return;

    await _appendBubble('user', text);

    final runner = _ensureRunner();
    if (runner == null) {
      await _appendBubble(
        'assistant',
        'Please stand by, sir. The system is still initializing.',
      );
      return;
    }

    _thinking = true;
    _notify();

    final AssistantResponse response;
    try {
      response = await runner.dispatchText(
        text,
        historyProvider: () => List<Object?>.of(_messages),
      );
    } finally {
      _thinking = false;
      _notify();
    }
    if (_disposed) return;

    // Entity extraction for anaphora (assistant.py lines 570-600).
    final entityIntent = response.extras['entity_intent'];
    final isNonCanonical = _nonCanonicalSeeds.contains(entityIntent);
    final isMulti = pyTruthy(response.extras['is_multi']);
    final rawTrack = response.extras['track'] ?? response.extras['first'];
    final rawTrackMap = rawTrack is Map<String, Object?> ? rawTrack : null;

    Map<String, Object?>? track;
    Object? artist;
    if (isNonCanonical) {
      track = null;
      artist = null;
    } else if (isMulti) {
      track = null;
      artist = rawTrackMap != null
          ? pyOr([rawTrackMap['artist'], rawTrackMap['artist_name'], null])
          : null;
    } else {
      track = rawTrackMap;
      artist = track != null
          ? pyOr([track['artist'], track['artist_name'], null])
          : response.extras['artist'];
    }

    final intent = response.intent;
    final agentProv = response.extras['agent'];
    if (pyTruthy(agentProv) && intent != null) {
      intent.extras['agent'] = agentProv;
    }
    final opts = response.extras['options'];
    if (pyTruthy(opts) && intent != null) {
      intent.extras['options'] = opts;
    }

    final entities = <String, Object?>{
      'track': track,
      'artist': artist,
      'playlist': response.extras['playlist'],
      'intent': intent?.name,
    };
    final hasEntities = entities.values.any(pyTruthy);

    await _appendBubble(
      'assistant',
      response.displayed,
      speak: response.success && response.spoken.isNotEmpty,
      speakText: response.spoken,
      entities: hasEntities ? entities : null,
      intent: intent,
    );

    if (response.success && response.deferredPlay) {
      try {
        final pl = player?.value;
        if (pl != null) {
          pl.play();
        } else {
          runner.player.play();
        }
      } catch (exc) {
        debugPrint('AssistantController: deferred play failed: $exc');
      }
    }
  }

  Future<void> _appendBubble(
    String sender,
    String text, {
    bool speak = false,
    String? speakText,
    Map<String, Object?>? entities,
    Intent? intent,
  }) async {
    final msg = <String, Object?>{'sender': sender, 'text': text};
    if (entities != null) msg['entities'] = entities;
    if (intent != null) {
      msg['intent'] = <String, Object?>{
        'name': intent.name,
        'query': intent.query,
        'raw': intent.raw,
        'extras': Map<String, Object?>.of(intent.extras),
      };
    }

    _messages.add(msg);
    if (_messages.length > maxMessages) {
      _messages.removeAt(0);
    }
    memory.saveSession(_messages, _initGreeted);
    _notify();

    if (speak && _ttsEnabled && onSpeak != null) {
      final pl = player?.value;
      final wasPlaying = pl?.playing ?? false;
      try {
        if (wasPlaying) pl!.pause();
        await onSpeak!(
          speakText != null && speakText.isNotEmpty ? speakText : text,
        );
      } catch (exc) {
        debugPrint('AssistantController: TTS speak failed: $exc');
      } finally {
        if (wasPlaying) pl!.play();
      }
    }
  }

  /// `AssistantView.clear_chat_manually`.
  void clearChat() {
    _messages = [];
    _initGreeted = false;
    memory.clearSession();
    _notify();
  }

  /// `AssistantView.handle_app_background`.
  void handleAppBackground() => memory.touchSession();

  /// `AssistantView.handle_app_resume`: clears the chat and re-runs init if
  /// the 15-minute session expired while backgrounded.
  void handleAppResume() {
    final session = memory.loadSession();
    if (session.messages.isEmpty && _messages.isNotEmpty) {
      _messages = [];
      _initGreeted = false;
      _notify();
      unawaited(onShow());
    }
  }

  @override
  void dispose() {
    _disposed = true;
    library?.removeListener(_onLibraryChanged);
    super.dispose();
  }
}
