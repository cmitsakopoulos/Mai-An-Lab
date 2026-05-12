import 'dart:async';
import 'dart:convert';

import 'package:audio_service/audio_service.dart';
import 'package:audio_session/audio_session.dart';
import 'package:flet/flet.dart';
import 'package:flutter/foundation.dart';
import 'package:just_audio/just_audio.dart';


class Extension extends FletExtension {
  @override
  FletService? createService(Control control) {
    debugPrint("createService called for type: ${control.type}");
    if (control.type == "flet_audio_service") {
      debugPrint("Creating FletAudioService for ${control.id}");
      return FletAudioService(control: control);
    }
    return null;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// FletAudioService — Flet 0.84.0 FletService bridge
// ─────────────────────────────────────────────────────────────────────────────

class FletAudioService extends FletService {
  FletAudioService({required super.control});

  static AudioPlayerHandler? _handler;

  /// Completed once _initHandler() finishes. Any invoked method that arrives
  /// before the handler is ready will await this before proceeding.
  static Completer<void>? _handlerReady;

  StreamSubscription? _playerStateSub;
  StreamSubscription? _positionSub;
  StreamSubscription? _durationSub;
  StreamSubscription? _errorSub;



  @override
  void init() {
    super.init();
    debugPrint("FletAudioService(${control.id}).init");
    // Only start initialisation once; subsequent instances share the handler.
    if (_handlerReady == null || _handlerReady!.isCompleted) {
      debugPrint("FletAudioService: Starting fresh _initHandler...");
      _handlerReady = Completer<void>();
      _initHandler().then((_) {
        debugPrint("FletAudioService: _initHandler COMPLETED successfully");
        if (!_handlerReady!.isCompleted) _handlerReady!.complete();
        _setupListeners();
        debugPrint("FletAudioService: Triggering 'ready' event to Python");
        control.triggerEvent("ready", "true");
      }).catchError((e) {
        debugPrint("FletAudioService: _initHandler FAILED: $e");
        if (!_handlerReady!.isCompleted) _handlerReady!.completeError(e);
      });
    } else {
      debugPrint("FletAudioService: Handler already/still initializing, waiting...");
      _handlerReady!.future.then((_) {
        debugPrint("FletAudioService: Shared handler is now ready, wiring listeners");
        _setupListeners();
        control.triggerEvent("ready", "true");
      });
    }
  }

  @override
  Future<dynamic> onMethodCall(String name, Map<String, dynamic> args) async {
    debugPrint("FletAudioService.$name($args)");
    // Wait for the handler to be ready before executing any method.
    if (_handlerReady != null) {
      await _handlerReady!.future;
    }
    final a = args;
    switch (name) {
      case 'play':
        await _handler?.play();

      case 'pause':
        await _handler?.pause();

      case 'stop':
        await _handler?.stop();

      case 'seek':
        final ms = (a['position'] as num?)?.toInt() ?? 0;
        await _handler?.seek(Duration(milliseconds: ms));

      case 'set_media_item':
        final item = _mediaItemFromMap(a);
        await _handler?.setMediaItem(item, a['src'] as String?);

      case 'set_playlist':
        final rawItems = (a['items'] as List<dynamic>?) ?? [];
        final items = rawItems
            .cast<Map<String, dynamic>>()
            .map(_mediaItemFromMap)
            .toList();
        await _handler?.setPlaylist(items);

      case 'add_queue_item':
        final item = _mediaItemFromMap(a);
        final index = (a['index'] as num?)?.toInt() ??
            (_handler?.queue.value.length ?? 0);
        await _handler?.addQueueItemAt(item, index);

      case 'remove_queue_item':
        final index = (a['index'] as num?)?.toInt() ?? 0;
        await _handler?.removeQueueItemAt(index);

      case 'skip_to_next':
        await _handler?.skipToNext();

      case 'skip_to_previous':
        await _handler?.skipToPrevious();

      case 'skip_to_index':
        final index = (a['index'] as num?)?.toInt() ?? 0;
        await _handler?.skipToQueueItem(index);

      default:
        throw Exception("Unknown FletAudioService method: $name");
    }
    return null;
  }

  void _setupListeners() {
    if (_handler == null) return;

    _playerStateSub?.cancel();
    _positionSub?.cancel();
    _durationSub?.cancel();
    _errorSub?.cancel();

    _playerStateSub = _handler!._player.playerStateStream.listen((state) {
      control.triggerEvent(
          "state_change",
          jsonEncode({
            'status': state.playing ? 'playing' : 'paused',
            'processing_state': state.processingState.name,
            'queue_index': _handler!._player.currentIndex ?? 0,
          }));
    });

    _positionSub = _handler!._player.createPositionStream(
      minPeriod: const Duration(milliseconds: 200),
      maxPeriod: const Duration(milliseconds: 500),
    ).listen((position) {
      if (_handler!._player.playing) {
        control.triggerEvent(
            "position_change", position.inMilliseconds.toString());
      }
    });

    _durationSub = _handler!._player.durationStream.listen((duration) {
      if (duration != null) {
        control.triggerEvent(
            "state_change",
            jsonEncode({
              'status': _handler!._player.playing ? 'playing' : 'paused',
              'processing_state': _handler!._player.processingState.name,
              'queue_index': _handler!._player.currentIndex ?? 0,
              'duration_ms': duration.inMilliseconds,
            }));
      }
    });

    _errorSub = _handler!._player.playbackEventStream.listen(
      (_) {},
      onError: (Object e, StackTrace st) {
        control.triggerEvent("error", e.toString());
      },
    );
  }

  Future<void> _initHandler() async {
    debugPrint("FletAudioService._initHandler() starting AudioService.init");
    if (_handler == null) {
      _handler = await AudioService.init(
        builder: () => AudioPlayerHandler(),
        config: const AudioServiceConfig(
          androidNotificationChannelId:
              'com.example.flet_audio_service.channel.audio',
          androidNotificationChannelName: 'Audio Playback',
          androidStopForegroundOnPause: false,
        ),
      );
    }
    debugPrint("FletAudioService: AudioService.init done");

    final src = control.getString('src');
    if (src != null) {
      debugPrint("FletAudioService: Initial src found: $src");
      final item = MediaItem(
        id: src,
        album: 'Flet Music',
        title: control.getString('title') ?? 'Unknown',
        artist: control.getString('artist') ?? 'Unknown',
        artUri: control.getString('album_art') != null
            ? Uri.parse(control.getString('album_art')!)
            : null,
      );
      await _handler?.setMediaItem(item, src);
    }
  }

  @override
  void dispose() {
    debugPrint("FletAudioService(${control.id}).dispose()");
    _playerStateSub?.cancel();
    _positionSub?.cancel();
    _durationSub?.cancel();
    _errorSub?.cancel();
    super.dispose();
  }

  MediaItem _mediaItemFromMap(Map<String, dynamic> map) {
    final src = (map['src'] as String?) ?? 'flet_audio';
    final artUrl = map['album_art'] as String?;
    return MediaItem(
      id: src,
      album: 'Flet Music',
      title: (map['title'] as String?) ?? '',
      artist: (map['artist'] as String?) ?? '',
      artUri: artUrl != null ? Uri.parse(artUrl) : null,
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// AudioPlayerHandler
// ─────────────────────────────────────────────────────────────────────────────

class AudioPlayerHandler extends BaseAudioHandler with QueueHandler, SeekHandler {
  final _player = AudioPlayer();

  ConcatenatingAudioSource _playlist = ConcatenatingAudioSource(
    children: [],
    useLazyPreparation: true,
  );

  AudioPlayerHandler() {
    _player.playbackEventStream.map(_transformEvent).pipe(playbackState);

    _player.sequenceStateStream.listen((state) {
      if (state == null) return;
      final items = state.effectiveSequence
          .map((source) => source.tag as MediaItem)
          .toList();
      queue.add(items);
      final current = state.currentSource?.tag as MediaItem?;
      if (current != null) mediaItem.add(current);
    });

    _initAudioSession();
  }

  Future<void> _initAudioSession() async {
    final session = await AudioSession.instance;
    await session.configure(const AudioSessionConfiguration.music());

    session.interruptionEventStream.listen((event) {
      if (event.type == AudioInterruptionType.duck) {
        _player.setVolume(event.begin ? 0.5 : 1.0);
      } else if (event.type == AudioInterruptionType.pause) {
        if (event.begin) {
          _player.pause();
        } else {
          _player.play();
        }
      } else if (event.type == AudioInterruptionType.unknown && event.begin) {
        _player.pause();
      }
    });
  }

  /// androidCompactActionIndices [0, 1, 3] = previous, play/pause, next
  PlaybackState _transformEvent(PlaybackEvent event) {
    return PlaybackState(
      controls: [
        MediaControl.skipToPrevious,
        if (_player.playing) MediaControl.pause else MediaControl.play,
        MediaControl.stop,
        MediaControl.skipToNext,
      ],
      systemActions: const {
        MediaAction.seek,
        MediaAction.skipToPrevious,
        MediaAction.skipToNext,
      },
      androidCompactActionIndices: const [0, 1, 3],
      processingState: const {
            ProcessingState.idle: AudioProcessingState.idle,
            ProcessingState.loading: AudioProcessingState.loading,
            ProcessingState.buffering: AudioProcessingState.buffering,
            ProcessingState.ready: AudioProcessingState.ready,
            ProcessingState.completed: AudioProcessingState.completed,
          }[_player.processingState] ??
          AudioProcessingState.idle,
      playing: _player.playing,
      updatePosition: event.updatePosition,
      bufferedPosition: event.bufferedPosition,
      speed: _player.speed,
      queueIndex: event.currentIndex,
    );
  }

  @override
  Future<void> play() async {
    final session = await AudioSession.instance;
    await session.setActive(true);
    _player.play();
  }

  @override
  Future<void> pause() => _player.pause();

  @override
  Future<void> stop() async {
    await _player.stop();
    await super.stop();
  }

  @override
  Future<void> seek(Duration position) async {
    final duration = _player.duration;
    if (duration != null && position >= duration) {
      position = duration - const Duration(milliseconds: 500);
    }
    await _player.seek(position);
  }

  @override
  Future<void> skipToNext() => _player.seekToNext();

  @override
  Future<void> skipToPrevious() => _player.seekToPrevious();

  @override
  Future<void> skipToQueueItem(int index) =>
      _player.seek(Duration.zero, index: index);

  Future<void> setPlaylist(List<MediaItem> items) async {
    final sources = items
        .map((item) => AudioSource.uri(Uri.parse(item.id), tag: item))
        .toList();
    _playlist = ConcatenatingAudioSource(
      children: sources,
      useLazyPreparation: true,
    );
    queue.add(items);
    if (items.isNotEmpty) mediaItem.add(items.first);
    await _player.stop();
    await _player.setAudioSource(_playlist, preload: false);
  }

  Future<void> addQueueItemAt(MediaItem item, int index) async {
    final sources = List<AudioSource>.from(_playlist.children);
    sources.insert(
      index.clamp(0, sources.length),
      AudioSource.uri(Uri.parse(item.id), tag: item),
    );
    await _rebuildPlaylist(sources);
    final updated = List<MediaItem>.from(queue.value);
    updated.insert(index.clamp(0, updated.length), item);
    queue.add(updated);
  }

  Future<void> removeQueueItemAt(int index) async {
    final sources = List<AudioSource>.from(_playlist.children);
    if (index < 0 || index >= sources.length) return;
    sources.removeAt(index);
    await _rebuildPlaylist(sources);
    final updated = List<MediaItem>.from(queue.value);
    if (index < updated.length) {
      updated.removeAt(index);
      queue.add(updated);
    }
  }

  Future<void> setMediaItem(MediaItem item, String? url) async {
    mediaItem.add(item);
    if (url != null) {
      await _player.stop();
      try {
        // preload: false — return immediately; just_audio will buffer lazily
        // when play() is called. This prevents blocking the Flet invoke_method
        // channel (10-second timeout) while the OS resolves the audio source.
        await _player.setAudioSource(
          AudioSource.uri(Uri.parse(url), tag: item),
          preload: false,
        );
      } catch (e) {
        debugPrint('flet_audio_service: Error setting audio source: $e');
      }
    }
  }

  Future<void> _rebuildPlaylist(List<AudioSource> sources) async {
    final currentIndex = _player.currentIndex ?? 0;
    final currentPosition = _player.position;
    _playlist = ConcatenatingAudioSource(
      children: sources,
      useLazyPreparation: true,
    );
    await _player.setAudioSource(
      _playlist,
      initialIndex:
          currentIndex.clamp(0, sources.isEmpty ? 0 : sources.length - 1),
      initialPosition: currentPosition,
      preload: false,
    );
  }
}
