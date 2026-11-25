import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:audio_service/audio_service.dart';
import 'package:audio_session/audio_session.dart';
import 'package:flutter/widgets.dart';
import 'package:just_audio/just_audio.dart';
import 'package:maian_native/maian_native.dart';

import 'play_queue.dart';
import 'playback_backend.dart';
import 'queue_entry.dart';

/// Starts the media service (notification, lockscreen, headset buttons) with
/// the Flet app's configuration. Android only.
Future<AudioPlayerHandler> startAudioService() => AudioService.init(
  builder: AudioPlayerHandler.new,
  config: const AudioServiceConfig(
    // Same channel id as the Flet app: after cutover the user's notification
    // settings for it carry over.
    androidNotificationChannelId:
        'com.example.flet_audio_service.channel.audio',
    androidNotificationChannelName: 'Audio Playback',
    // false: EVERY playing→paused edge would otherwise drop the foreground and
    // the wakelock, including the ones nobody chose (a transient focus loss, a
    // playlist reload). The backgrounded app was then frozen, and getting the
    // foreground back from the background is refused on Android 12+. The
    // handler's idle timer releases the service after a long pause instead.
    androidStopForegroundOnPause: false,
    // Monochrome, shipped by the shared Kotlin plugin (res/drawable).
    androidNotificationIcon: 'drawable/ic_stat_playback',
    artDownscaleWidth: 512,
    artDownscaleHeight: 512,
  ),
);

/// The Flet extension's AudioPlayerHandler (flet_audio_service.dart, from
/// line 1122), lifted with the Flet bridge removed. Behaviour is unchanged:
/// resume record, play ledger, now-playing art, skip-on-error, idle release,
/// audio focus. What the Flet `FletAudioService` wrapper did (serialised ops,
/// epochs, events to Python) is now the player controller's job, in the same
/// isolate, so it needs no protocol.
class AudioPlayerHandler extends BaseAudioHandler
    with QueueHandler, SeekHandler
    implements PlaybackBackend {
  AudioPlayerHandler() {
    final effects = <AndroidAudioEffect>[];
    if (Platform.isAndroid) {
      effects
        ..add(_equalizer)
        ..add(_loudnessEnhancer);
      _equalizer.setEnabled(true);
      _loudnessEnhancer.setEnabled(true);
      _equalizer.parameters
          .then((p) {
            _equalizerBands = p.bands;
            // Gains set before the bands were known (the first track's DSP
            // at startup) land now instead of being dropped.
            for (var i = 0; i < _eqGains.length && i < p.bands.length; i++) {
              p.bands[i].setGain(_eqGains[i]);
            }
            return p.bands;
          })
          .catchError((Object e) {
            debugPrint('AudioPlayerHandler: equalizer parameters failed: $e');
            return <AndroidEqualizerBand>[];
          });
    }
    _player = AudioPlayer(
      audioPipeline: AudioPipeline(androidAudioEffects: effects),
    );

    // NOT `.pipe(playbackState)`: that holds an addStream open on the subject
    // for the handler's whole life, and any other add() (ours, or
    // audio_service's own on stop/skip) then throws.
    _player.playbackEventStream
        .map(_transformEvent)
        .listen(
          (state) {
            if (!playbackState.isClosed) playbackState.add(state);
          },
          onError: (Object e, StackTrace st) {
            debugPrint('AudioPlayerHandler: playbackState transform error: $e');
          },
        );

    _player.sequenceStateStream.listen((state) {
      // whereType: a source with a null/non-MediaItem tag (mid-load sequence
      // swap) would otherwise throw and kill this listener for good.
      final items = state.effectiveSequence
          .map((source) => source.tag)
          .whereType<MediaItem>()
          .toList();
      queue.add(items);
      final current = state.currentSource?.tag;
      if (current is MediaItem) {
        mediaItem.add(_withArt(current));
        _ledgerOnItem(current);
        // Keyed by item, not index: a reload can keep the index and change
        // the item, and a splice above the playing row the reverse.
        if (current.id != _resumeSrc) {
          _resumeSrc = current.id;
          _saveResume();
        }
        _ensureArt(current);
        final next = _player.nextIndex;
        final seq = _player.sequence;
        if (next != null && next < seq.length) {
          final nextTag = seq[next].tag;
          if (nextTag is MediaItem) _ensureArt(nextTag);
        }
      }
      _changed();
    });

    // Play ledger inputs. The clock runs only while audio is actually audible
    // (playing AND ready), so pauses, buffering stalls and the post-queue
    // `completed` state (where `playing` stays true) are not counted.
    _player.playerStateStream.listen((s) {
      _ledgerCounting = s.playing && s.processingState == ProcessingState.ready;
      if (_ledgerCounting) {
        _ledgerClock.start();
      } else {
        _ledgerClock.stop();
      }
      // Before the ledger's early return: a repair reload's pause still moves
      // the resume point.
      if (s.playing != _resumePlaying) {
        _resumePlaying = s.playing;
        _saveResume();
      }
      _syncIdleStop(s);
      _changed();
      if (s.processingState == ProcessingState.idle && _ledgerHold) return;
      if (s.processingState == ProcessingState.completed ||
          s.processingState == ProcessingState.idle) {
        _ledgerFinalize();
      }
    });

    // A loop (repeat-one, or repeat-all over one item) replays the SAME item,
    // so the sequence listener sees no change; autoAdvance onto the ledger's
    // own item is the only signal that a fresh listen began.
    _player.positionDiscontinuityStream.listen((d) {
      _changed();
      if (d.reason != PositionDiscontinuityReason.autoAdvance) return;
      final tag = _player.sequenceState.currentSource?.tag;
      if (tag is MediaItem && tag.id == _ledgerItem?.id) {
        _ledgerFinalize();
        _ledgerOnItem(tag);
      }
    });

    _player.currentIndexStream.listen((_) => _changed());
    _player.durationStream.listen((_) => _changed());

    _player.errorStream.listen(_onPlayerError);
    _player.processingStateStream.listen((state) {
      if (state == ProcessingState.ready) _errorsInRow = 0;
      // A new load after giving up (the user picked another track) starts a
      // fresh count.
      if (state == ProcessingState.loading && _gaveUpOnError) {
        _gaveUpOnError = false;
        _errorsInRow = 0;
      }
    });

    _initAudioSession();
  }

  final _equalizer = AndroidEqualizer();
  final _loudnessEnhancer = AndroidLoudnessEnhancer();
  late final AudioPlayer _player;
  List<AndroidEqualizerBand>? _equalizerBands;

  // ignore: deprecated_member_use
  ConcatenatingAudioSource _playlist = ConcatenatingAudioSource(
    children: [],
    useLazyPreparation: true,
  );

  final _changes = StreamController<void>.broadcast(sync: true);
  final _failures = StreamController<PlaybackFailure>.broadcast();

  void _changed() {
    if (!_changes.isClosed) _changes.add(null);
  }

  // ── PlaybackBackend ───────────────────────────────────────────────────────

  @override
  Stream<void> get changes => _changes.stream;
  @override
  Stream<PlaybackFailure> get failures => _failures.stream;

  final _listens = StreamController<Listen>.broadcast();
  @override
  Stream<Listen> get listens => _listens.stream;
  @override
  bool get playing => _player.playing;
  @override
  Processing get processing =>
      Processing.values.byName(_player.processingState.name);
  @override
  Duration get position => _player.position;
  @override
  Duration? get duration => _player.duration;
  @override
  int? get currentIndex => _player.currentIndex;
  @override
  List<int> get shuffleIndices =>
      _player.shuffleModeEnabled ? _player.shuffleIndices : const [];
  @override
  int get length => _playlist.children.length;
  @override
  String? artFor(String src) => _artById[src]?.toFilePath();

  @override
  void setPaths({required String ledgerPath, required String resumePath}) {
    setPlayLogPath(ledgerPath);
    setResumePath(resumePath);
  }

  @override
  Future<void> setPlaylist(
    List<QueueEntry> items,
    int start, {
    required bool autoplay,
    bool? shuffle,
    bool keepListen = false,
  }) async {
    if (shuffle != null) await _player.setShuffleModeEnabled(shuffle);
    await _setPlaylist(
      [for (final e in items) mediaItemFor(e)],
      start,
      keepListen: keepListen,
      autoplay: autoplay,
    );
    // Not awaited: just_audio's play() completes only when playback pauses or
    // ends, which would hold up the caller (and its op chain) for the whole
    // track.
    if (autoplay) unawaited(play());
  }

  @override
  Future<void> splice(List<SpliceEdit> edits) => _spliceQueue(edits);

  @override
  Future<void> move(int from, int to) => moveQueueItem(from, to);

  @override
  Future<void> skipTo(int index, {bool autoplay = true}) async {
    await skipToQueueItem(index);
    if (autoplay) unawaited(play());
  }

  @override
  Future<void> setShuffle(bool enabled) async {
    // Enabling regenerates a fresh order anchored at the current item, so the
    // playing track stays put and the tail is randomised.
    await _player.setShuffleModeEnabled(enabled);
    if (enabled) await _player.shuffle();
  }

  @override
  Future<void> setRepeat(Repeat mode) => setRepeatMode(switch (mode) {
    Repeat.one => AudioServiceRepeatMode.one,
    Repeat.all => AudioServiceRepeatMode.all,
    Repeat.none => AudioServiceRepeatMode.none,
  });

  // ── EQ and loudness (Flet: set_eq_band_gain, set_loudness_boost) ─────────

  final _eqGains = List<double>.filled(5, 0);

  @override
  Future<void> setLoudnessBoost(double gainDb) async {
    // engine._update_player_volume: never a cut.
    if (Platform.isAndroid) {
      await _loudnessEnhancer.setTargetGain(gainDb < 0 ? 0 : gainDb);
    }
  }

  @override
  Future<void> setEqBandGain(int index, double gainDb) async {
    if (index >= 0 && index < _eqGains.length) _eqGains[index] = gainDb;
    final bands = _equalizerBands;
    if (bands == null || index < 0 || index >= bands.length) return;
    await bands[index].setGain(gainDb);
  }

  @override
  Future<EqualizerInfo?> equalizerInfo() async {
    if (!Platform.isAndroid) return null;
    try {
      final p = await _equalizer.parameters;
      return (
        centerHz: [for (final b in p.bands) b.centerFrequency],
        minDb: p.minDecibels,
        maxDb: p.maxDecibels,
      );
    } catch (e) {
      debugPrint('AudioPlayerHandler: equalizer parameters failed: $e');
      return null;
    }
  }

  // ── MediaItem ─────────────────────────────────────────────────────────────

  /// `AudioEngine._track_to_playlist_item` + the extension's
  /// `_mediaItemFromMap`. Sidecar art is looked up lazily in [_ensureArt],
  /// for the rows that are about to play, not for the whole queue.
  static MediaItem mediaItemFor(QueueEntry e) {
    final ms = (e.duration * 1000).round();
    return MediaItem(
      id: e.src,
      title: e.title,
      artist: e.artist,
      album: e.album,
      duration: ms > 0 ? Duration(milliseconds: ms) : null,
    );
  }

  // ── Playback failures ─────────────────────────────────────────────────────
  // A file that can't play (deleted, moved, storage access revoked, corrupt)
  // leaves ExoPlayer idle. Two problems with that, both fixed here:
  //  * audio_service treats ANY move to idle as a full stop: it tears down the
  //    media service and its notification. While a skip is possible,
  //    _transformEvent reports buffering instead, which keeps the service.
  //  * just_audio's own maxSkipsOnError can't help: the whole queue is ONE
  //    ConcatenatingAudioSource child, so it sees a one-item playlist and just
  //    pauses. An errored player ignores seeks until reloaded, so the skip is
  //    a reload at the next index.
  static const _maxSkipsOnError = 3;
  int _errorsInRow = 0;
  bool _gaveUpOnError = false;

  bool get _canSkipOnError =>
      !_gaveUpOnError &&
      _errorsInRow < _maxSkipsOnError &&
      _player.nextIndex != null;

  void _onPlayerError(PlayerException e) {
    final idx = e.index ?? _player.currentIndex;
    final failedId = idAt(idx);
    final tag = _player.sequenceState.currentSource?.tag;
    final title = tag is MediaItem ? tag.title : '';
    final artist = tag is MediaItem ? (tag.artist ?? '') : '';
    debugPrint('AudioPlayerHandler: playback error at $idx ($title): $e');
    // A beat first: an error from a source a queue op is replacing clears on
    // its own. Only a player still idle on the same item is a dead track.
    Future.delayed(const Duration(milliseconds: 300), () async {
      if (_player.processingState != ProcessingState.idle) return;
      if (_player.currentIndex != idx ||
          idAt(_player.currentIndex) != failedId) {
        return;
      }
      var action = 'stopped';
      if (_canSkipOnError) {
        _errorsInRow++;
        try {
          await _reloadAt(_player.nextIndex!);
          action = 'skipped';
        } catch (err) {
          debugPrint('AudioPlayerHandler: skip after error failed: $err');
        }
      }
      if (action == 'stopped') {
        _gaveUpOnError = true;
        try {
          await _player.pause();
        } catch (_) {}
        // Report the idle held back, so audio_service releases the service
        // instead of showing "buffering" forever.
        playbackState.add(
          playbackState.value.copyWith(
            processingState: AudioProcessingState.idle,
            playing: false,
          ),
        );
      }
      _reportPlaybackError(
        e.message ?? 'error ${e.code}',
        title,
        artist,
        action,
      );
    });
  }

  Future<void> _reloadAt(int index) async {
    final wasPlaying = _player.playing;
    await _player.setAudioSource(_playlist, preload: true, initialIndex: index);
    if (wasPlaying && !_player.playing) unawaited(_player.play());
  }

  void _reportPlaybackError(
    String message,
    String title,
    String artist,
    String action,
  ) {
    final heard = _failures.hasListener;
    _failures.add((
      message: message,
      title: title,
      artist: artist,
      action: action,
    ));
    // In the app, the snackbar says it; otherwise a card, since nobody saw the
    // music stop or jump.
    final foreground =
        WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed;
    if (foreground && heard) return;
    final name = title.isEmpty ? 'a track' : '“$title”';
    MaianNative.showAlert(
      key: 'playback_error',
      icon: 'playback',
      title: action == 'skipped' ? "Couldn't play $name" : 'Playback stopped',
      content: action == 'skipped'
          ? 'Skipped to the next track. The file may have been moved, deleted '
                'or be unreadable.'
          : "Couldn't play $name or the tracks after it. Check that the music "
                'folder is still there and the app can read it.',
      navigateTo: 'home',
    );
  }

  /// Id (= [QueueEntry.src]) of live playlist row [idx], or null. Every child
  /// is a leaf source, so a logical index is a children index. O(1).
  @override
  String? idAt(int? idx) {
    if (idx == null) return null;
    final children = _playlist.children;
    if (idx < 0 || idx >= children.length) return null;
    final child = children[idx];
    if (child is IndexedAudioSource) {
      final tag = child.tag;
      if (tag is MediaItem) return tag.id;
    }
    return null;
  }

  // ── Resume record ─────────────────────────────────────────────────────────
  // Written by the handler because it hears every track change, including
  // background auto-advance. One ~120-byte atomic write per track change and
  // per play/pause; nothing periodic.
  String? _resumePath;
  Future<void> _resumeWrites = Future<void>.value();
  String? _resumeSrc;
  bool _resumePlaying = false;

  void setResumePath(String path) {
    _resumePath = path;
    _saveResume();
  }

  void _saveResume() {
    final path = _resumePath;
    if (path == null) return;
    final idx = _player.currentIndex;
    final src = idAt(idx);
    if (src == null) return;
    final record = jsonEncode({
      'src': src,
      'index': idx,
      'position_ms': _player.position.inMilliseconds,
      'playing': _player.playing,
      'at': _nowSecs(),
    });
    _resumeWrites = _resumeWrites.then((_) async {
      try {
        final tmp = File('$path.tmp');
        await tmp.writeAsString(record, flush: true);
        await tmp.rename(path);
      } catch (e) {
        debugPrint('AudioPlayerHandler: resume record write failed: $e');
      }
    });
  }

  // ── Now-playing artwork ───────────────────────────────────────────────────
  // The plugin's resolveArtwork pulls embedded art (sidecar as fallback) into
  // a downscaled JPEG in the app cache. Done here so it keeps working through
  // background auto-advance. Keyed by item id; null = no art found.
  final Map<String, Uri?> _artById = {};
  final Set<String> _artPending = {};
  final Map<String, String?> _sidecarByFolder = {};

  MediaItem _withArt(MediaItem item) {
    final art = _artById[item.id];
    return art == null ? item : item.copyWith(artUri: art);
  }

  void _ensureArt(MediaItem item) {
    final src = item.id;
    if (_artById.containsKey(src) || _artPending.contains(src)) return;
    final String path;
    if (src.startsWith('file://')) {
      path = srcToPath(src);
    } else if (src.startsWith('/')) {
      path = src;
    } else {
      return; // network stream
    }
    final album = item.album ?? '';
    // Album-level key so one decode serves every track on the album; the file
    // itself when the album is unknown (singles folders etc.).
    final key =
        (album.isEmpty || album == 'Unknown Album' || album == 'Flet Music')
        ? path
        : '${item.artist ?? ''}\u0000$album';
    _artPending.add(src);
    () async {
      final out = await MaianNative.resolveArtwork(
        path: path,
        sidecar: await _sidecarFor(path),
        key: key,
      );
      _artPending.remove(src);
      if (_artById.length > 4000) _artById.clear();
      _artById[src] = out == null ? null : Uri.file(out);
      final cur = mediaItem.value;
      if (out != null && cur != null && cur.id == src) {
        mediaItem.add(cur.copyWith(artUri: Uri.file(out)));
      }
      _changed();
    }();
  }

  /// `AudioEngine._get_artwork_path`'s folder lookup, cached per folder.
  Future<String?> _sidecarFor(String path) async {
    final slash = path.lastIndexOf('/');
    if (slash <= 0) return null;
    final folder = path.substring(0, slash);
    if (_sidecarByFolder.containsKey(folder)) return _sidecarByFolder[folder];
    String? found;
    for (final name in const [
      'cover.jpg',
      'folder.jpg',
      'cover.png',
      'front.jpg',
    ]) {
      try {
        if (await File('$folder/$name').exists()) {
          found = '$folder/$name';
          break;
        }
      } catch (_) {}
    }
    if (_sidecarByFolder.length > 2000) _sidecarByFolder.clear();
    return _sidecarByFolder[folder] = found;
  }

  // ── Play ledger ───────────────────────────────────────────────────────────
  // How long each item was actually heard: one JSON line per finished listen,
  // appended to play_ledger.jsonl. Same format as the Flet app, so its
  // drain (utils/play_ledger.py) ingests either. The port drains it once it
  // writes the database (phase 3); until then lines accumulate on disk.
  static const int _ledgerMinMs = 1000;
  static const int _ledgerPendingCap = 500;
  MediaItem? _ledgerItem;
  // Set while a repair re-push reloads the item already playing: an `idle`
  // seen during that reload must not close the listen.
  bool _ledgerHold = false;
  final Stopwatch _ledgerClock = Stopwatch();
  bool _ledgerCounting = false;
  int _ledgerStartedAt = 0;
  String? _ledgerPath;
  final List<String> _ledgerPending = [];
  Future<void> _ledgerWrites = Future<void>.value();

  static int _nowSecs() => DateTime.now().millisecondsSinceEpoch ~/ 1000;

  /// Same id = same listen continuing (queue inserts/moves and shuffle re-emit
  /// the sequence without changing what is playing); a new id closes the
  /// previous listen and opens one.
  void _ledgerOnItem(MediaItem item) {
    if (_ledgerItem != null && _ledgerItem!.id == item.id) {
      _ledgerItem = item;
      return;
    }
    _ledgerFinalize();
    _ledgerItem = item;
    _ledgerStartedAt = _nowSecs();
    if (_ledgerCounting) _ledgerClock.start();
  }

  void _ledgerFinalize() {
    final item = _ledgerItem;
    _ledgerItem = null;
    final ms = _ledgerClock.elapsedMilliseconds;
    _ledgerClock
      ..stop()
      ..reset();
    if (item == null || ms < _ledgerMinMs) return;
    final dur = item.duration?.inMilliseconds ?? 0;
    final path = srcToPath(item.id);
    if (path.isNotEmpty) {
      _listens.add((
        path: path,
        listenedS: ms / 1000.0,
        durationS: dur / 1000.0,
      ));
    }
    _ledgerAppend(
      jsonEncode({
        'id': item.id,
        'ms': ms,
        'dur': dur,
        'start': _ledgerStartedAt,
        'end': _nowSecs(),
      }),
    );
  }

  void _ledgerAppend(String line) {
    final path = _ledgerPath;
    if (path == null) {
      if (_ledgerPending.length < _ledgerPendingCap) _ledgerPending.add(line);
      return;
    }
    _ledgerWrites = _ledgerWrites.then((_) async {
      try {
        await File(
          path,
        ).writeAsString('$line\n', mode: FileMode.append, flush: true);
      } catch (e) {
        debugPrint('AudioPlayerHandler: play-ledger append failed: $e');
      }
    });
  }

  void setPlayLogPath(String path) {
    _ledgerPath = path;
    if (_ledgerPending.isEmpty) return;
    final lines = List<String>.of(_ledgerPending);
    _ledgerPending.clear();
    lines.forEach(_ledgerAppend);
  }

  // ── Idle release ──────────────────────────────────────────────────────────
  // The service stays in the foreground while paused (see the config), which
  // pins the process and holds the wakelock. After this long without audible
  // playback the handler stops and releases it; a reclaimed process
  // cold-starts into the saved queue. A transient interruption never counts.
  static const Duration _idleStopAfter = Duration(minutes: 10);
  Timer? _idleStopTimer;
  bool _holdsService = false;
  bool _transientPause = false;

  void _syncIdleStop(PlayerState s) {
    final audible =
        s.playing &&
        s.processingState != ProcessingState.completed &&
        s.processingState != ProcessingState.idle;
    if (audible) {
      _holdsService = true;
      _idleStopTimer?.cancel();
      _idleStopTimer = null;
      return;
    }
    if (!_holdsService || _transientPause) {
      _idleStopTimer?.cancel();
      _idleStopTimer = null;
      return;
    }
    _idleStopTimer ??= Timer(_idleStopAfter, () {
      _idleStopTimer = null;
      debugPrint(
        'AudioPlayerHandler: idle for $_idleStopAfter, releasing service',
      );
      stop();
    });
  }

  bool _wasPlayingBeforeInterruption = false;

  Future<void> _initAudioSession() async {
    final session = await AudioSession.instance;
    await session.configure(const AudioSessionConfiguration.music());

    session.interruptionEventStream.listen((event) {
      if (event.type == AudioInterruptionType.duck) {
        _player.setVolume(event.begin ? 0.5 : 1.0);
      } else if (event.type == AudioInterruptionType.pause) {
        // Transient focus loss: Android will hand focus back.
        if (event.begin) {
          _wasPlayingBeforeInterruption = _player.playing;
          // Set before pausing so the paused state doesn't arm the idle timer.
          _transientPause = _wasPlayingBeforeInterruption;
          _player.pause();
        } else {
          _transientPause = false;
          if (_wasPlayingBeforeInterruption) {
            _wasPlayingBeforeInterruption = false;
            _player.play();
          } else {
            _syncIdleStop(_player.playerState);
          }
        }
      } else if (event.type == AudioInterruptionType.unknown && event.begin) {
        // Permanent focus loss (another player took over): no resume is
        // coming, so this pause is idle like a user pause.
        _wasPlayingBeforeInterruption = false;
        _transientPause = false;
        _player.pause();
      }
    });
    // Headphones unplugged: just_audio pauses by itself (handleInterruptions
    // defaults to true), as in the Flet app.
  }

  /// A user play/pause supersedes any pending interruption.
  void _clearInterruption() {
    _transientPause = false;
    _wasPlayingBeforeInterruption = false;
  }

  /// androidCompactActionIndices [0, 1, 3] = previous, play/pause, next
  PlaybackState _transformEvent(PlaybackEvent event) {
    final repeatMode =
        const {
          LoopMode.off: AudioServiceRepeatMode.none,
          LoopMode.one: AudioServiceRepeatMode.one,
          LoopMode.all: AudioServiceRepeatMode.all,
        }[_player.loopMode] ??
        AudioServiceRepeatMode.none;

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
        MediaAction.setRepeatMode,
      },
      androidCompactActionIndices: const [0, 1, 3],
      // An error leaves the player idle; reporting idle would make
      // audio_service stop the service before _onPlayerError can skip.
      processingState:
          (_player.processingState == ProcessingState.idle &&
              event.errorCode != null &&
              _canSkipOnError)
          ? AudioProcessingState.buffering
          : const {
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
      repeatMode: repeatMode,
    );
  }

  @override
  Future<void> play() async {
    _clearInterruption();
    final session = await AudioSession.instance;
    await session.setActive(true);
    await _player.play();
  }

  @override
  Future<void> pause() {
    _clearInterruption();
    return _player.pause();
  }

  /// Every stop path (the app, the notification's stop button, swiping the
  /// notification away, the idle timer) ends here.
  @override
  Future<void> stop() async {
    _idleStopTimer?.cancel();
    _idleStopTimer = null;
    _clearInterruption();
    await _player.stop();
    await super.stop();
    // audio_service's own stop can't leave the foreground (see
    // releasePlaybackService in the plugin).
    if (_holdsService && Platform.isAndroid) {
      await MaianNative.releasePlaybackService();
    }
    _holdsService = false;
    _changed();
  }

  @override
  Future<void> seek(Duration position) async {
    final duration = _player.duration;
    if (duration != null && position >= duration) {
      position = duration - const Duration(milliseconds: 500);
    }
    await _player.seek(position);
  }

  RemoteControls? _remote;
  @override
  set remoteControls(RemoteControls? controls) => _remote = controls;

  // audio_service calls these for the notification, lockscreen, headset and
  // car buttons (the app's own skips use skipTo). Moving just_audio directly
  // went behind the controller's back: the queue only caught up when the
  // player's index was mirrored, Previous always left the track (no 3 s
  // restart), and Next at the end of the queue never asked auto-play.
  @override
  Future<void> skipToNext() async {
    final remote = _remote;
    if (remote != null) return remote.next();
    await _player.seekToNext();
  }

  @override
  Future<void> skipToPrevious() async {
    final remote = _remote;
    if (remote != null) return remote.previous();
    await _player.seekToPrevious();
  }

  @override
  Future<void> skipToQueueItem(int index) =>
      _player.seek(Duration.zero, index: index);

  @override
  Future<void> setRepeatMode(AudioServiceRepeatMode repeatMode) async {
    final loopMode =
        const {
          AudioServiceRepeatMode.none: LoopMode.off,
          AudioServiceRepeatMode.one: LoopMode.one,
          AudioServiceRepeatMode.all: LoopMode.all,
        }[repeatMode] ??
        LoopMode.off;
    await _player.setLoopMode(loopMode);
  }

  AudioSource _buildAudioSource(String src, MediaItem item) {
    // A bare filesystem path is a local file; AudioSource.uri would treat it
    // as a schemeless network URI and fail to load.
    if (src.startsWith('/')) return AudioSource.file(src, tag: item);
    if (src.startsWith('file://')) {
      return AudioSource.file(srcToPath(src), tag: item);
    }
    final uri = Uri.tryParse(src);
    if (uri == null || !uri.hasScheme) return AudioSource.file(src, tag: item);
    return AudioSource.uri(uri, tag: item);
  }

  Future<void> _setPlaylist(
    List<MediaItem> items,
    int startIndex, {
    bool keepListen = false,
    bool autoplay = false,
  }) async {
    final sources = [
      for (final item in items) _buildAudioSource(item.id, item),
    ];
    // ignore: deprecated_member_use
    _playlist = ConcatenatingAudioSource(
      children: sources,
      useLazyPreparation: true,
    );
    queue.add(items);
    final clampedStart = items.isEmpty
        ? 0
        : startIndex.clamp(0, items.length - 1);
    if (items.isNotEmpty) mediaItem.add(_withArt(items[clampedStart]));
    _ledgerHold =
        keepListen &&
        items.isNotEmpty &&
        _ledgerItem?.id == items[clampedStart].id;
    try {
      // No stop() first: setAudioSource replaces the source and keeps playing
      // if the player was playing. stop() went through `idle`, which
      // audio_service treats as a full stop. A push that must not play still
      // pauses first.
      if (!autoplay && _player.playing) await _player.pause();
      // initialIndex inside setAudioSource avoids a separate seek being
      // clobbered by the load defaulting back to index 0. preload: true loads
      // the initial child now, so a restored session reaches `ready` (and can
      // take its saved-position seek) without play().
      await _player.setAudioSource(
        _playlist,
        preload: true,
        initialIndex: clampedStart,
      );
    } finally {
      _ledgerHold = false;
    }
    // The shuffle flag persists across setAudioSource, but the new source
    // starts with an identity order. Re-randomise, anchored at the start item.
    if (_player.shuffleModeEnabled) await _player.shuffle();
    _changed();
  }

  /// Queue edits in order on the live source. In place: the player is not
  /// reloaded, so playback continues unless an edit removes the active item
  /// (just_audio then advances). An edit outside the playlist throws, so the
  /// controller repairs instead of silently diverging.
  Future<void> _spliceQueue(List<SpliceEdit> edits) async {
    final updated = List<MediaItem>.from(queue.value);
    for (final e in edits) {
      final n = _playlist.children.length;
      if (e.start < 0 || e.deleteCount < 0 || e.start + e.deleteCount > n) {
        throw RangeError(
          'splice(${e.start}, ${e.deleteCount}) outside playlist of $n',
        );
      }
      if (e.deleteCount > 0) {
        await _playlist.removeRange(e.start, e.start + e.deleteCount);
        if (e.start + e.deleteCount <= updated.length) {
          updated.removeRange(e.start, e.start + e.deleteCount);
        }
      }
      if (e.items.isNotEmpty) {
        final items = [for (final t in e.items) mediaItemFor(t)];
        await _playlist.insertAll(e.start, [
          for (final item in items) _buildAudioSource(item.id, item),
        ]);
        updated.insertAll(e.start.clamp(0, updated.length), items);
      }
    }
    queue.add(updated);
    _changed();
  }

  Future<void> moveQueueItem(int fromIndex, int toIndex) async {
    final n = _playlist.children.length;
    if (fromIndex < 0 || fromIndex >= n) return;
    if (toIndex < 0 || toIndex >= n) return;
    if (fromIndex == toIndex) return;
    // Handles every index-shift case and updates currentIndex if the active
    // source moved. No reload, so playback continues uninterrupted.
    await _playlist.move(fromIndex, toIndex);
    final updated = List<MediaItem>.from(queue.value);
    if (fromIndex < updated.length) {
      final item = updated.removeAt(fromIndex);
      updated.insert(toIndex.clamp(0, updated.length), item);
      queue.add(updated);
    }
    _changed();
  }
}
