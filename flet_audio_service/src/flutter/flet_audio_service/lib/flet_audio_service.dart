import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:audio_service/audio_service.dart';
import 'package:audio_session/audio_session.dart';
import 'package:flet/flet.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:just_audio/just_audio.dart';
import 'package:permission_handler/permission_handler.dart';

// Bridge to the native PCM decoder implemented in FletAudioServicePlugin.kt.
// We keep this MethodChannel separate from audio_service's own channels so a
// hung decode can never wedge playback.
const MethodChannel _decodeChannel =
    MethodChannel('com.flet.flet_audio_service/decode');


class Extension extends FletExtension {
  @override
  FletService? createService(Control control) {
    debugPrint("createService called for type: ${control.type}");
    if (control.type == "flet_audio_service") {
      debugPrint("Creating FletAudioService for ${control.id}");
      final service = FletAudioService(control: control);
      // Workaround for Flet 0.84.0 Android: FletService.init() can be invoked
      // late or skipped under certain timings. Force-invoke it here so the
      // method-channel listener and AudioService init begin immediately. The
      // service's init() is guarded against double-invocation.
      service.init();
      return service;
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

  // Position throttling: just_audio emits ~5x/sec which is wasted battery on
  // mobile (each emit = IPC → Python → observer dispatch → Flet rebuild).
  // Coalesce to ≤ 1 emit/sec, but always emit on a large jump so seeks update
  // the UI immediately.
  static const int _positionEmitMinMs = 1000;
  static const int _positionJumpMs = 1500;
  int _lastEmittedPositionMs = -1;
  int _lastEmitWallClockMs = 0;

  bool _initRan = false;

  @override
  void init() {
    if (_initRan) {
      debugPrint("FletAudioService(${control.id}).init — already ran, skipping");
      return;
    }
    _initRan = true;

    super.init();
    debugPrint("FletAudioService(${control.id}).init");
    // Register the invoke-method listener immediately so Python method calls
    // can resolve as soon as the handler is ready. flet_audio (the working
    // reference) does the same.
    control.addInvokeMethodListener(_invokeMethod);

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

  Future<dynamic> _invokeMethod(String name, dynamic args) async {
    debugPrint("FletAudioService.$name($args)");
    // Wait for the handler to be ready before executing any method.
    if (_handlerReady != null) {
      await _handlerReady!.future;
    }
    // All method handlers are fire-and-forget: the Flet method-channel has a
    // 10-second timeout, but ExoPlayer/just_audio operations on Android can
    // exceed that on cold start (codec init, file-source validation). The
    // operations still run; their result is surfaced via state_change /
    // error events, not via the method-call return value.
    // Methods like play/pause/stop send no args, so `args` arrives as null.
    // Methods like seek/set_media_item send a Map. Normalise to a Map either way.
    final Map<String, dynamic> a = args == null
        ? <String, dynamic>{}
        : args is Map<String, dynamic>
            ? args
            : Map<String, dynamic>.from(args as Map);
    switch (name) {
      case 'play':
        _handler?.play();

      case 'pause':
        _handler?.pause();

      case 'stop':
        _handler?.stop();

      case 'seek':
        final ms = (a['position'] as num?)?.toInt() ?? 0;
        _handler?.seek(Duration(milliseconds: ms));

      case 'set_media_item':
        final item = _mediaItemFromMap(a);
        _handler?.setMediaItem(item, a['src'] as String?);

      case 'set_playlist':
        final rawItems = (a['items'] as List<dynamic>?) ?? [];
        final startIndex = (a['start_index'] as num?)?.toInt() ?? 0;
        // Items arrive as Map<dynamic, dynamic> from the Flet protocol; we
        // need Map<String, dynamic>. .cast<>() can't bridge that — manually
        // re-key each map.
        final items = rawItems.map((raw) {
          final m = raw is Map<String, dynamic>
              ? raw
              : Map<String, dynamic>.from(raw as Map);
          return _mediaItemFromMap(m);
        }).toList();
        _handler?.setPlaylist(items, startIndex);

      case 'add_queue_item':
        final item = _mediaItemFromMap(a);
        final index = (a['index'] as num?)?.toInt() ??
            (_handler?.queue.value.length ?? 0);
        _handler?.addQueueItemAt(item, index);

      case 'remove_queue_item':
        final index = (a['index'] as num?)?.toInt() ?? 0;
        _handler?.removeQueueItemAt(index);

      case 'move_queue_item':
        final from = (a['from_index'] as num?)?.toInt() ?? 0;
        final to = (a['to_index'] as num?)?.toInt() ?? 0;
        _handler?.moveQueueItem(from, to);

      case 'skip_to_next':
        _handler?.skipToNext();

      case 'skip_to_previous':
        _handler?.skipToPrevious();

      case 'skip_to_index':
        final index = (a['index'] as num?)?.toInt() ?? 0;
        _handler?.skipToQueueItem(index);

      case 'decode_pcm':
        // Fire-and-forget: the actual reply comes back via the
        // 'decode_complete' event. Python correlates by `request_id`.
        // We MUST NOT await here — decoding a 60s clip can exceed the Flet
        // method-channel's 10s timeout.
        final reqId = (a['request_id'] as String?) ?? '';
        final path = (a['path'] as String?) ?? '';
        if (reqId.isEmpty || path.isEmpty) {
          control.triggerEvent(
            'decode_complete',
            jsonEncode({
              'request_id': reqId,
              'ok': false,
              'error': 'missing request_id or path',
            }),
          );
        } else {
          _runDecode(reqId, path);
        }

      default:
        throw Exception("Unknown FletAudioService method: $name");
    }
    return null;
  }

  Future<void> _runDecode(String requestId, String path) async {
    try {
      final res = await _decodeChannel.invokeMethod<Map<dynamic, dynamic>>(
        'decodePcm',
        {'path': path},
      );
      final m = (res ?? <dynamic, dynamic>{}).map(
        (k, v) => MapEntry(k.toString(), v),
      );
      m['request_id'] = requestId;
      control.triggerEvent('decode_complete', jsonEncode(m));
    } catch (e) {
      control.triggerEvent(
        'decode_complete',
        jsonEncode({
          'request_id': requestId,
          'ok': false,
          'error': e.toString(),
        }),
      );
    }
  }

  void _setupListeners() {
    if (_handler == null) return;

    _playerStateSub?.cancel();
    _positionSub?.cancel();
    _durationSub?.cancel();
    _errorSub?.cancel();

    _playerStateSub = _handler!._player.playerStateStream.listen((state) {
      // currentIndex can transiently be null during seek/buffer; emitting `?? 0`
      // confuses Python into thinking the user moved to track 0. Omit the key
      // entirely when null so Python keeps its existing index.
      final currentIdx = _handler!._player.currentIndex;
      final payload = <String, dynamic>{
        'status': state.playing ? 'playing' : 'paused',
        'processing_state': state.processingState.name,
      };
      if (currentIdx != null) payload['queue_index'] = currentIdx;
      control.triggerEvent("state_change", jsonEncode(payload));
    });

    _positionSub = _handler!._player.positionStream.listen((position) {
      if (!_handler!._player.playing) return;
      final posMs = position.inMilliseconds;
      final nowMs = DateTime.now().millisecondsSinceEpoch;
      final jumped = (_lastEmittedPositionMs >= 0) &&
          ((posMs - _lastEmittedPositionMs).abs() > _positionJumpMs);
      if (nowMs - _lastEmitWallClockMs >= _positionEmitMinMs || jumped) {
        _lastEmitWallClockMs = nowMs;
        _lastEmittedPositionMs = posMs;
        control.triggerEvent("position_change", posMs.toString());
      }
    });

    _durationSub = _handler!._player.durationStream.listen((duration) {
      if (duration == null) return;
      final currentIdx = _handler!._player.currentIndex;
      final payload = <String, dynamic>{
        'status': _handler!._player.playing ? 'playing' : 'paused',
        'processing_state': _handler!._player.processingState.name,
        'duration_ms': duration.inMilliseconds,
      };
      if (currentIdx != null) payload['queue_index'] = currentIdx;
      control.triggerEvent("state_change", jsonEncode(payload));
    });

    _errorSub = _handler!._player.playbackEventStream.listen(
      (_) {},
      onError: (Object e, StackTrace st) {
        control.triggerEvent("error", e.toString());
      },
    );
  }

  Future<void> _requestRuntimePermissions() async {
    if (!Platform.isAndroid) return;
    // Request all runtime permissions audio_service / file-source playback
    // need on modern Android. Without this the user has to flip them in
    // Settings manually. Each permission's request() is a no-op if already
    // granted or if the OS doesn't apply that permission to this API level.
    try {
      final results = await [
        Permission.notification,        // POST_NOTIFICATIONS — Android 13+
        Permission.audio,               // READ_MEDIA_AUDIO  — Android 13+
        Permission.storage,             // READ/WRITE_EXTERNAL_STORAGE — ≤ Android 12
      ].request();
      results.forEach((perm, status) {
        debugPrint("FletAudioService: permission $perm = $status");
      });
    } catch (e) {
      debugPrint("FletAudioService: permission request failed: $e");
    }
  }

  Future<void> _initHandler() async {
    debugPrint("FletAudioService._initHandler() starting AudioService.init");
    await _requestRuntimePermissions();
    if (_handler == null) {
      _handler = await AudioService.init(
        builder: () => AudioPlayerHandler(),
        config: const AudioServiceConfig(
          androidNotificationChannelId:
              'com.example.flet_audio_service.channel.audio',
          androidNotificationChannelName: 'Audio Playback',
          androidStopForegroundOnPause: false,
          // Android 14+ MediaStyle notifications require an explicit icon;
          // omitting this can cause SystemUI to kill the foreground service
          // on screen-lock. mipmap/ic_launcher always exists in a Flet app.
          androidNotificationIcon: 'mipmap/ic_launcher',
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
    control.removeInvokeMethodListener(_invokeMethod);
    _playerStateSub?.cancel();
    _positionSub?.cancel();
    _durationSub?.cancel();
    _errorSub?.cancel();
    super.dispose();
  }

  MediaItem _mediaItemFromMap(Map<String, dynamic> map) {
    final src = (map['src'] as String?) ?? 'flet_audio';
    final artUrl = map['album_art'] as String?;
    Uri? artUri;
    if (artUrl != null && artUrl.isNotEmpty) {
      try {
        final parsed = Uri.parse(artUrl);
        // Only set artUri if it has a real scheme + host or is a valid file://
        // path. Empty or relative URIs crash flutter_cache_manager with
        // "No host specified in URI" when audio_service tries to load them.
        if (parsed.hasScheme &&
            (parsed.hasAuthority || parsed.scheme == 'file')) {
          artUri = parsed;
        }
      } catch (_) {
        // ignore — leave artUri null
      }
    }
    return MediaItem(
      id: src,
      album: 'Flet Music',
      title: (map['title'] as String?) ?? '',
      artist: (map['artist'] as String?) ?? '',
      artUri: artUri,
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
    await _player.play();
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

  Future<void> setPlaylist(List<MediaItem> items, [int startIndex = 0]) async {
    final sources = items
        .map((item) => AudioSource.uri(Uri.parse(item.id), tag: item))
        .toList();
    _playlist = ConcatenatingAudioSource(
      children: sources,
      useLazyPreparation: true,
    );
    queue.add(items);
    final clampedStart = items.isEmpty
        ? 0
        : startIndex.clamp(0, items.length - 1);
    if (items.isNotEmpty) mediaItem.add(items[clampedStart]);
    await _player.stop();
    // Setting initialIndex inside setAudioSource avoids the race where a
    // separate seek call would be clobbered by the source-load defaulting
    // back to index 0.
    //
    // preload: true (combined with useLazyPreparation on the parent) eagerly
    // loads only the initial child. This is critical for session-restore:
    // without it, the source isn't decoded until play() is called, so
    // durationStream/processingState=ready never fire — Python's
    // _is_loaded stays False, the slider's max stays 0, and any user scrub
    // gets stuffed into _restore_position instead of seeking. The first
    // play() then applies that stale scrub target — which can exceed the
    // actual track duration and trigger auto-advance ("skip song").
    await _player.setAudioSource(
      _playlist,
      preload: true,
      initialIndex: clampedStart,
    );
  }

  Future<void> addQueueItemAt(MediaItem item, int index) async {
    // In-place insert on the live ConcatenatingAudioSource — does NOT call
    // _player.setAudioSource, so the currently-playing source is not torn
    // down. _rebuildPlaylist (the previous approach) reloaded the player
    // from position 0 on every queue mutation, which the user perceived
    // as playback "crashing" on swipe-to-queue / reorder.
    final clamped = index.clamp(0, _playlist.children.length);
    await _playlist.insert(
      clamped,
      AudioSource.uri(Uri.parse(item.id), tag: item),
    );
    final updated = List<MediaItem>.from(queue.value);
    updated.insert(index.clamp(0, updated.length), item);
    queue.add(updated);
  }

  Future<void> removeQueueItemAt(int index) async {
    if (index < 0 || index >= _playlist.children.length) return;
    // In-place remove. just_audio auto-advances to the next source if the
    // active item was removed, and decrements currentIndex for items
    // before the active one. No player rebuild → no playback interruption.
    await _playlist.removeAt(index);
    final updated = List<MediaItem>.from(queue.value);
    if (index < updated.length) {
      updated.removeAt(index);
      queue.add(updated);
    }
  }

  Future<void> moveQueueItem(int fromIndex, int toIndex) async {
    final n = _playlist.children.length;
    if (fromIndex < 0 || fromIndex >= n) return;
    if (toIndex < 0 || toIndex >= n) return;
    if (fromIndex == toIndex) return;
    // ConcatenatingAudioSource.move handles all index-shift cases internally
    // and updates _player.currentIndex if the active source moved. No source
    // reload → playback continues uninterrupted.
    await _playlist.move(fromIndex, toIndex);
    final updated = List<MediaItem>.from(queue.value);
    if (fromIndex < updated.length) {
      final item = updated.removeAt(fromIndex);
      updated.insert(toIndex.clamp(0, updated.length), item);
      queue.add(updated);
    }
  }

  Future<void> setMediaItem(MediaItem item, String? url) async {
    mediaItem.add(item);
    if (url != null) {
      try {
        // preload: false — just_audio will load lazily when play() is called.
        // No _player.stop() first: setAudioSource replaces the previous
        // source, and stop() can deadlock against a previously-pending source.
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
