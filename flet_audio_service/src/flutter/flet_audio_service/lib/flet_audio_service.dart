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
import 'package:flutter_tts/flutter_tts.dart';
import 'package:speech_to_text/speech_to_text.dart';
import 'package:flutter/widgets.dart';


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
// FletAudioService; Flet 0.84.0 FletService bridge
// ─────────────────────────────────────────────────────────────────────────────

class FletAudioService extends FletService with WidgetsBindingObserver {
  FletAudioService({required super.control});

  static AudioPlayerHandler? _handler;

  /// Completed once _initHandler() finishes. Any invoked method that arrives
  /// before the handler is ready will await this before proceeding.
  static Completer<void>? _handlerReady;

  StreamSubscription? _playerStateSub;
  StreamSubscription? _positionSub;
  StreamSubscription? _durationSub;
  StreamSubscription? _errorSub;
  // Fires on EVERY currentIndex change (including gapless auto-advance, which
  // playerStateStream/durationStream miss). This is the authoritative signal
  // that Python mirrors current_index from; without it Python only learned of
  // an advance when durationStream happened to emit a different duration.
  StreamSubscription? _indexSub;

  // ── Queue-generation handshake ────────────────────────────────────────────
  // Every queue-mutating command from Python carries a monotonically
  // increasing `epoch`. We adopt it only AFTER the op has actually been applied
  // (inside _enqueueOp), then stamp it on every emitted event. Python accepts a
  // mirrored queue_index only when the event's epoch equals the latest epoch it
  // sent — i.e. Dart has caught up to Python's most recent intent. This
  // replaces the old wall-clock suppression gate with a deterministic,
  // timing-independent reconciliation.
  int _epoch = 0;
  // Serializes all playlist mutations + skips so a rapid add-then-skip (Play
  // Similar replenishment) can never interleave against the live
  // ConcatenatingAudioSource. Each link emits an `op_complete` ack.
  Future<void> _opChain = Future<void>.value();

  // Position pacing. This 1 Hz ticker is the SINGLE source of position
  // events (Python and the UI don't re-throttle), and it only EXISTS while
  // audio is playing and the app is visible — with the screen off nothing
  // ticks, so the CPU can sleep between the audio path's ~1 s buffer fills.
  // just_audio's positionStream was used before: it ticks every duration/800
  // clamped to 16–200 ms (5 Hz for a typical song, up to 60 Hz for a short
  // one), kept ticking in the background, and its internal timer is never
  // stopped when the listener cancels, so every session re-entry leaked one.
  // Seeks and track changes still emit immediately (discontinuity stream).
  static const Duration _positionTick = Duration(seconds: 1);
  Timer? _positionTimer;
  bool _isBackground = false;

  // TTS singleton: lazily initialised on first speak so cold-start cost is
  // paid only by users who actually invoke the assistant. Pause/resume of
  // music playback is the caller's responsibility — flutter_tts mixes by
  // default which is the right behaviour for short assistant utterances on
  // top of a paused player.
  static FlutterTts? _tts;
  static Completer<void>? _ttsCompleter;
  // 0.5 is the flutter_tts default, but on Android the system TTS engine
  // tends to interpret it as 'fast' (especially Google TTS on Pixel/Samsung
  // devices). 0.4 is a comfortable narration pace for short assistant
  // utterances; the Python side can override via tts_set_voice.
  double _ttsRate = 0.4;
  double _ttsPitch = 1.0;

  // STT (push-to-talk). Lazy singleton mirroring the TTS pattern. We hold
  // an in-flight request_id so a second start_listening cancels the first
  // gracefully — recognition sessions are serialised, not concurrent.
  static SpeechToText? _stt;
  static bool _sttAvailable = false;
  String? _activeSttRequestId;



  bool _initRan = false;

  @override
  void init() {
    if (_initRan) {
      debugPrint("FletAudioService(${control.id}).init; already ran, skipping");
      return;
    }
    _initRan = true;

    super.init();
    debugPrint("FletAudioService(${control.id}).init");
    WidgetsBinding.instance.addObserver(this);
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
        _fireAndReport(_handler?.play());

      case 'pause':
        _fireAndReport(_handler?.pause());

      case 'stop':
        _fireAndReport(_handler?.stop());

      case 'seek':
        final ms = (a['position'] as num?)?.toInt() ?? 0;
        _fireAndReport(_handler?.seek(Duration(milliseconds: ms)));

      case 'set_media_item':
        final item = _mediaItemFromMap(a);
        _fireAndReport(_handler?.setMediaItem(item, a['src'] as String?));

      case 'set_playlist':
        final rawItems = (a['items'] as List<dynamic>?) ?? [];
        final startIndex = (a['start_index'] as num?)?.toInt() ?? 0;
        final autoplay = (a['autoplay'] as bool?) ?? false;
        // Desired shuffle state, applied atomically with the push so the source
        // and the shuffle flag can never disagree. null = leave as-is.
        final shuffle = a['shuffle'] as bool?;
        // Items arrive as Map<dynamic, dynamic> from the Flet protocol; we
        // need Map<String, dynamic>. .cast<>() can't bridge that; manually
        // re-key each map.
        final items = rawItems.map((raw) {
          final m = raw is Map<String, dynamic>
              ? raw
              : Map<String, dynamic>.from(raw as Map);
          return _mediaItemFromMap(m);
        }).toList();
        _enqueueOp(a['request_id'] as String?, (a['epoch'] as num?)?.toInt(),
            () async {
          if (shuffle != null) {
            await _handler!._player.setShuffleModeEnabled(shuffle);
          }
          // setPlaylist re-shuffles internally when shuffle mode is enabled.
          await _handler!.setPlaylist(items, startIndex);
          // CRITICAL: do NOT await play(). just_audio's play() Future does not
          // complete until playback is paused or the track ends, so awaiting it
          // here would block this op (and the whole serialized _opChain) for the
          // entire duration of playback — delaying the epoch adoption + op ack
          // by seconds and freezing Python's index mirror. Fire-and-forget so
          // the op completes as soon as the source is loaded.
          if (autoplay) unawaited(_handler!.play());
        });

      case 'add_queue_item':
        final item = _mediaItemFromMap(a);
        final index = (a['index'] as num?)?.toInt() ??
            (_handler?.queue.value.length ?? 0);
        _enqueueOp(a['request_id'] as String?, (a['epoch'] as num?)?.toInt(),
            () => _handler!.addQueueItemAt(item, index));

      case 'add_queue_items':
        // Batch insert: Python sends the whole block in one call, each item
        // carrying its own insertion index (computed in append order). Dart
        // performs the N inserts locally so the IPC cost is one round-trip.
        final rawItems = (a['items'] as List<dynamic>?) ?? [];
        final batchItems = <MediaItem>[];
        final batchIndices = <int>[];
        for (final raw in rawItems) {
          final m = raw is Map<String, dynamic>
              ? raw
              : Map<String, dynamic>.from(raw as Map);
          batchItems.add(_mediaItemFromMap(m));
          batchIndices.add((m['index'] as num?)?.toInt() ??
              (_handler?.queue.value.length ?? 0));
        }
        _enqueueOp(a['request_id'] as String?, (a['epoch'] as num?)?.toInt(),
            () => _handler!.addQueueItemsAt(batchItems, batchIndices));

      case 'remove_queue_item':
        final index = (a['index'] as num?)?.toInt() ?? 0;
        _enqueueOp(a['request_id'] as String?, (a['epoch'] as num?)?.toInt(),
            () => _handler!.removeQueueItemAt(index));

      case 'move_queue_item':
        final from = (a['from_index'] as num?)?.toInt() ?? 0;
        final to = (a['to_index'] as num?)?.toInt() ?? 0;
        _enqueueOp(a['request_id'] as String?, (a['epoch'] as num?)?.toInt(),
            () => _handler!.moveQueueItem(from, to));

      case 'set_shuffle':
        // Ownership of shuffle order moves to just_audio. Enabling regenerates
        // a fresh order anchored at the current item (shuffle()'s initialIndex
        // behaviour), so the currently-playing track stays put and the tail is
        // randomised — matching the previous "[current] + shuffled_rest".
        final enabled = (a['enabled'] as bool?) ?? false;
        _enqueueOp(a['request_id'] as String?, (a['epoch'] as num?)?.toInt(),
            () async {
          await _handler!._player.setShuffleModeEnabled(enabled);
          if (enabled) await _handler!._player.shuffle();
        });

      case 'skip_to_next':
        _enqueueOp(a['request_id'] as String?, (a['epoch'] as num?)?.toInt(),
            () async {
          await _handler!.skipToNext();
          // Fire-and-forget: awaiting play() blocks until pause/end (see set_playlist).
          unawaited(_handler!.play());
        });

      case 'skip_to_previous':
        _enqueueOp(a['request_id'] as String?, (a['epoch'] as num?)?.toInt(),
            () async {
          await _handler!.skipToPrevious();
          // Fire-and-forget: awaiting play() blocks until pause/end (see set_playlist).
          unawaited(_handler!.play());
        });

      case 'skip_to_index':
        final index = (a['index'] as num?)?.toInt() ?? 0;
        final autoplay = (a['autoplay'] as bool?) ?? true;
        _enqueueOp(a['request_id'] as String?, (a['epoch'] as num?)?.toInt(),
            () async {
          await _handler!.skipToQueueItem(index);
          // Fire-and-forget: awaiting play() blocks until pause/end (see set_playlist).
          if (autoplay) unawaited(_handler!.play());
        });

      case 'set_play_log_path':
        final path = a['path'] as String?;
        if (path != null && path.isNotEmpty) _handler?.setPlayLogPath(path);

      case 'set_repeat_mode':
        final mode = a['mode'] as String? ?? 'none';
        final repeatMode = const {
          'one': AudioServiceRepeatMode.one,
          'all': AudioServiceRepeatMode.all,
          'none': AudioServiceRepeatMode.none,
        }[mode] ?? AudioServiceRepeatMode.none;
        _fireAndReport(_handler?.setRepeatMode(repeatMode));

      case 'show_progress_notification':
        final job = (a['job'] as String?) ?? 'dsp';
        final title = (a['title'] as String?) ?? '';
        final content = (a['content'] as String?) ?? '';
        final progress = (a['progress'] as num?)?.toInt() ?? 0;
        final total = (a['total'] as num?)?.toInt() ?? 0;
        final done = (a['done'] as bool?) ?? false;
        _decodeChannel.invokeMethod('showProgressNotification', {
          'job': job,
          'title': title,
          'content': content,
          'progress': progress,
          'total': total,
          'done': done,
        });

      case 'decode_pcm':
        // Fire-and-forget: the actual reply comes back via the
        // 'decode_complete' event. Python correlates by `request_id`.
        // We MUST NOT await here; decoding a 60s clip can exceed the Flet
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

      case 'query_permissions':
        // Async-but-fast: each .status read returns within a few ms. We still
        // route the result through an event so Python's correlation pattern
        // matches decode_pcm and we don't depend on the method-channel return
        // value (which Flet treats as fire-and-forget on Android).
        final reqId = (a['request_id'] as String?) ?? '';
        _runQueryPermissions(reqId);

      case 'request_permission':
        // For Permission.manageExternalStorage this opens the Android Settings
        // page (not a dialog) and resolves once the user returns to the app.
        final reqId = (a['request_id'] as String?) ?? '';
        final permName = (a['name'] as String?) ?? '';
        _runRequestPermission(reqId, permName);

      case 'open_app_settings':
        // Fire-and-forget. Used as a last-resort link from in-app prompts.
        openAppSettings();

      case 'tts_speak':
        // Fire-and-forget. Callers (the assistant) await a completion event
        // if they need to know when speaking finishes; play/pause coordination
        // is handled Python-side, not here.
        final reqId = (a['request_id'] as String?) ?? '';
        final text = (a['text'] as String?) ?? '';
        _runTtsSpeak(reqId, text);

      case 'tts_stop':
        _runTtsStop();

      case 'tts_set_voice':
        final rate = (a['rate'] as num?)?.toDouble();
        final pitch = (a['pitch'] as num?)?.toDouble();
        if (rate != null) _ttsRate = rate.clamp(0.1, 1.5);
        if (pitch != null) _ttsPitch = pitch.clamp(0.5, 2.0);
        _applyTtsVoice();

      case 'stt_listen':
        final reqId = (a['request_id'] as String?) ?? '';
        final timeout = (a['timeout'] as num?)?.toDouble() ?? 10.0;
        _runSttListen(reqId, timeout);

      case 'stt_stop':
        _runSttStop();

      case 'set_loudness_boost':
        final gain = (a['gain'] as num?)?.toDouble() ?? 0.0;
        if (Platform.isAndroid) {
          _handler?._loudnessEnhancer.setTargetGain(gain);
        }

      case 'set_eq_band_gain':
        final index = (a['index'] as num?)?.toInt() ?? 0;
        final gain = (a['gain'] as num?)?.toDouble() ?? 0.0;
        if (Platform.isAndroid && _handler?._equalizerBands != null) {
          if (index >= 0 && index < _handler!._equalizerBands!.length) {
            _handler!._equalizerBands![index].setGain(gain);
          }
        }

      case 'get_equalizer_bands':
        final reqId = (a['request_id'] as String?) ?? '';
        if (Platform.isAndroid && _handler?._equalizerBands != null) {
          final bandsJson = _handler!._equalizerBands!.map((b) => {
            'index': b.index,
            'center_frequency': b.centerFrequency,
            'gain': b.gain,
          }).toList();

          double minDb = -15.0;
          double maxDb = 15.0;
          _handler!._equalizer.parameters.then((params) {
            control.triggerEvent('equalizer_bands_result', jsonEncode({
              'request_id': reqId,
              'ok': true,
              'min_db': params.minDecibels,
              'max_db': params.maxDecibels,
              'bands': bandsJson,
            }));
          }).catchError((e) {
            control.triggerEvent('equalizer_bands_result', jsonEncode({
              'request_id': reqId,
              'ok': true,
              'min_db': minDb,
              'max_db': maxDb,
              'bands': bandsJson,
            }));
          });
        } else {
          control.triggerEvent('equalizer_bands_result', jsonEncode({
            'request_id': reqId,
            'ok': false,
            'error': 'Equalizer not supported or not ready',
            'bands': [],
          }));
        }

      default:
        throw Exception("Unknown FletAudioService method: $name");
    }
    return null;
  }

  /// Fire a handler Future without awaiting it (so _invokeMethod returns
  /// immediately and never trips the Flet method-channel's 10s timeout on a
  /// slow cold-start operation), but surface any rejection to Python as an
  /// instant "error" event. Before this, a thrown setPlaylist/setAudioSource
  /// (e.g. an unplayable stream URL) was swallowed, and the Python side only
  /// discovered the failure by timing out its own wait loop.
  void _fireAndReport(Future<dynamic>? future) {
    future?.catchError((Object e, StackTrace st) {
      debugPrint("FletAudioService: handler error → $e");
      control.triggerEvent("error", e.toString());
    });
  }

  Future<FlutterTts> _ensureTts() async {
    if (_tts != null) return _tts!;
    final tts = FlutterTts();

    tts.setCompletionHandler(() {
      if (_ttsCompleter != null && !_ttsCompleter!.isCompleted) {
        _ttsCompleter!.complete();
      }
    });
    tts.setErrorHandler((msg) {
      if (_ttsCompleter != null && !_ttsCompleter!.isCompleted) {
        _ttsCompleter!.completeError(msg);
      }
    });
    tts.setCancelHandler(() {
      if (_ttsCompleter != null && !_ttsCompleter!.isCompleted) {
        _ttsCompleter!.complete();
      }
    });
    // Force Android to wait for the utterance to finish via completion handler
    // instead of block-waiting inside speak(), preventing cold-start engine hangs.
    try {
      if (Platform.isAndroid) {
        // Force the high-quality Google engine if available.
        await tts.setEngine("com.google.android.tts");
      }
    } catch (_) {}
    try {
      await tts.setLanguage('en-GB');
    } catch (_) {}
    try {
      // Attempt to find a higher-quality British male voice (Jarvis style)
      await _applyJarvisVoice(tts);
    } catch (_) {}
    try {
      await tts.setSpeechRate(_ttsRate);
      await tts.setPitch(_ttsPitch);
    } catch (_) {}
    _tts = tts;
    return tts;
  }

  Future<void> _applyJarvisVoice(FlutterTts tts) async {
    try {
      // 1. Scan available voices for a British male voice
      dynamic voices = await tts.getVoices;
      if (voices is List) {
        // Preference: British English Male
        for (var voice in voices) {
          String name = voice["name"].toString().toLowerCase();
          String locale = voice["locale"].toString().toLowerCase();
          // Heuristic for British Male: common Android tags include 'male', 'man', 'low', 'rjs', 'gb-local'
          if ((locale.contains("en-gb") || locale.contains("en_gb")) && 
              (name.contains("male") || name.contains("man") || name.contains("low") || 
               name.contains("rjs") || name.contains("x-gb-local") || name.contains("gb-x-fis-local"))) {
            await tts.setVoice({"name": voice["name"], "locale": voice["locale"]});
            return;
          }
        }
        // Fallback 1: Any British English voice
        for (var voice in voices) {
          String locale = voice["locale"].toString().toLowerCase();
          if (locale.contains("en-gb") || locale.contains("en_gb")) {
            await tts.setVoice({"name": voice["name"], "locale": voice["locale"]});
            return;
          }
        }
        // Fallback 2: Any male voice
        for (var voice in voices) {
          String name = voice["name"].toString().toLowerCase();
          if (name.contains("male") || name.contains("man")) {
            await tts.setVoice({"name": voice["name"], "locale": voice["locale"]});
            return;
          }
        }
      }
      // If no specific voice found, just stick to en-GB
      await tts.setLanguage('en-GB');
    } catch (e) {
      debugPrint("Jarvis voice selection error: $e");
    }
  }

  Future<void> _applyTtsVoice() async {
    if (_tts == null) return;
    try {
      await _tts!.setSpeechRate(_ttsRate);
      await _tts!.setPitch(_ttsPitch);
    } catch (_) {}
  }

  Future<void> _runTtsSpeak(String requestId, String text) async {
    if (text.trim().isEmpty) {
      control.triggerEvent('tts_complete', jsonEncode({
        'request_id': requestId, 'ok': true, 'skipped': true,
      }));
      return;
    }
    try {
      final tts = await _ensureTts();
      // Complete any existing completer to prevent leaks/hangs
      if (_ttsCompleter != null && !_ttsCompleter!.isCompleted) {
        _ttsCompleter!.complete();
      }
      _ttsCompleter = Completer<void>();
      await tts.speak(text);
      
      // Await actual speech completion on all platforms, since tts.speak()
      // now resolves instantly or can hang under specific hardware profiles.
      // A 30-second safety timeout ensures the completion event is always sent.
      try {
        await _ttsCompleter!.future.timeout(const Duration(seconds: 30));
      } catch (e) {
        debugPrint("FletAudioService: TTS speech completion wait exception/timeout: $e");
      }
      
      control.triggerEvent('tts_complete', jsonEncode({
        'request_id': requestId, 'ok': true,
      }));
    } catch (e) {
      control.triggerEvent('tts_complete', jsonEncode({
        'request_id': requestId, 'ok': false, 'error': e.toString(),
      }));
    }
  }

  Future<void> _runTtsStop() async {
    if (_tts == null) return;
    try {
      await _tts!.stop();
    } catch (_) {}
    if (_ttsCompleter != null && !_ttsCompleter!.isCompleted) {
      _ttsCompleter!.complete();
    }
  }

  Future<SpeechToText> _ensureStt() async {
    if (_stt != null) return _stt!;
    final stt = SpeechToText();
    final ok = await stt.initialize(
      onError: (err) => debugPrint("STT Error: $err"),
      onStatus: (stat) => debugPrint("STT Status: $stat"),
    );
    if (!ok) throw Exception("Speech recognition not available on this device");
    _stt = stt;
    return stt;
  }

  Future<void> _runSttListen(String requestId, double timeout) async {
    _activeSttRequestId = requestId;
    try {
      final stt = await _ensureStt();
      // listen() resolves when it successfully starts listening. 
      // We then await the result in the onResult callback.
      final maxDuration = Duration(seconds: timeout.toInt());
      await stt.listen(
        onResult: (result) {
          if (result.finalResult) {
            control.triggerEvent('stt_result', jsonEncode({
              'request_id': requestId,
              'ok': true,
              'text': result.recognizedWords,
            }));
          }
        },
        listenFor: maxDuration,
        // Match pauseFor to listenFor so the plugin's silence detector
        // doesn't auto-terminate mid-utterance. For push-to-talk we
        // rely on the explicit stt_stop() call (fired on button release)
        // to finalise the session.
        pauseFor: maxDuration,
        cancelOnError: true,
      );
    } catch (e) {
      control.triggerEvent('stt_result', jsonEncode({
        'request_id': requestId,
        'ok': false,
        'error': e.toString(),
      }));
    }
  }

  Future<void> _runSttStop() async {
    if (_stt == null) return;
    try {
      await _stt!.stop();
    } catch (_) {}
  }

  Future<void> _runQueryPermissions(String requestId) async {
    try {
      final notif = await Permission.notification.status;
      final audio = await Permission.audio.status;
      final storage = await Permission.storage.status;
      final mes = await Permission.manageExternalStorage.status;
      final microphone = await Permission.microphone.status;
      control.triggerEvent('permissions_result', jsonEncode({
        'request_id': requestId,
        'ok': true,
        'notification': notif.name,
        'audio': audio.name,
        'storage': storage.name,
        'manage_external_storage': mes.name,
        'record_audio': microphone.name,
      }));
    } catch (e) {
      control.triggerEvent('permissions_result', jsonEncode({
        'request_id': requestId,
        'ok': false,
        'error': e.toString(),
      }));
    }
  }

  Future<void> _runRequestPermission(String requestId, String name) async {
    try {
      PermissionStatus status;
      switch (name) {
        case 'notification':
          status = await Permission.notification.request();
        case 'audio':
          status = await Permission.audio.request();
        case 'storage':
          status = await Permission.storage.request();
        case 'manage_external_storage':
          status = await Permission.manageExternalStorage.request();
        case 'record_audio':
          status = await Permission.microphone.request();
        default:
          throw Exception("Unknown permission name: $name");
      }
      control.triggerEvent('permission_request_result', jsonEncode({
        'request_id': requestId,
        'ok': true,
        'name': name,
        'status': status.name,
      }));
    } catch (e) {
      control.triggerEvent('permission_request_result', jsonEncode({
        'request_id': requestId,
        'ok': false,
        'name': name,
        'error': e.toString(),
      }));
    }
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

  /// Build the common state payload (status/processing/index/epoch/shuffle) and
  /// emit it. `extra` overlays extra keys (e.g. duration_ms). currentIndex can
  /// transiently be null during seek/buffer; we omit the key entirely rather
  /// than send `?? 0`, which would confuse Python into thinking the user jumped
  /// to track 0. Every event carries the current epoch so Python can reject
  /// index mirrors that predate its latest queue mutation.
  void _emitState({Map<String, dynamic>? extra}) {
    final handler = _handler;
    if (handler == null) return;
    final player = handler._player;
    final payload = <String, dynamic>{
      'status': player.playing ? 'playing' : 'paused',
      'processing_state': player.processingState.name,
      'epoch': _epoch,
    };
    final currentIdx = player.currentIndex;
    if (currentIdx != null) payload['queue_index'] = currentIdx;
    // Identity of the row at that index, so Python can verify its mirror
    // points at the track actually playing (not just a same-numbered row).
    final currentSrc = _srcAt(currentIdx);
    if (currentSrc != null) {
      payload['current_src'] = currentSrc;
      // The now-playing art the native resolver produced (embedded art, sidecar
      // fallback, downscaled + cached). Python shows this instead of decoding
      // the same image a second time with PIL.
      final art = handler._artById[currentSrc];
      if (art != null) payload['current_art'] = art.toFilePath();
    }
    // shuffleIndices maps play-position → original(logical) index. Python uses
    // it to render "up next" and to compute next/previous targets in shuffle
    // mode, so it no longer maintains its own permutation.
    if (player.shuffleModeEnabled) {
      payload['shuffle_indices'] = player.shuffleIndices;
    }
    if (extra != null) payload.addAll(extra);
    control.triggerEvent("state_change", jsonEncode(payload));
  }

  /// Id (= the src Python sent) of the live playlist row at [idx], or null.
  /// Every child is a leaf source, so a logical index is a children index.
  /// O(1): reads our own ConcatenatingAudioSource, not player.sequence (which
  /// rebuilds a list of the whole queue on each read).
  String? _srcAt(int? idx) {
    final handler = _handler;
    if (handler == null || idx == null) return null;
    final children = handler._playlist.children;
    if (idx < 0 || idx >= children.length) return null;
    final child = children[idx];
    if (child is IndexedAudioSource) {
      final tag = child.tag;
      if (tag is MediaItem) return tag.id;
    }
    return null;
  }

  void _emitPosition() {
    final handler = _handler;
    if (handler == null) return;
    control.triggerEvent(
        "position_change", handler._player.position.inMilliseconds.toString());
  }

  /// Run the position ticker only while playing AND visible; otherwise stop it.
  void _syncPositionTicker() {
    final handler = _handler;
    final want = handler != null && handler._player.playing && !_isBackground;
    if (want && _positionTimer == null) {
      _emitPosition();
      _positionTimer = Timer.periodic(_positionTick, (_) => _emitPosition());
    } else if (!want && _positionTimer != null) {
      _positionTimer!.cancel();
      _positionTimer = null;
    }
  }

  void _setupListeners() {
    if (_handler == null) return;

    _playerStateSub?.cancel();
    _positionSub?.cancel();
    _durationSub?.cancel();
    _errorSub?.cancel();
    _indexSub?.cancel();

    // Authoritative index mirroring: fires on skip, notification next/previous,
    // AND gapless auto-advance (the case the other two streams miss).
    _indexSub = _handler!._player.currentIndexStream.listen((idx) {
      if (idx == null) return;
      _emitState();
    });

    _playerStateSub = _handler!._player.playerStateStream.listen((state) {
      _emitState();
      _syncPositionTicker();
    });

    // Seek / track change / loop: snap the slider now rather than on the
    // next tick.
    _positionSub = _handler!._player.positionDiscontinuityStream.listen((_) {
      if (!_isBackground) _emitPosition();
    });
    _syncPositionTicker();

    _durationSub = _handler!._player.durationStream.listen((duration) {
      if (duration == null) return;
      _emitState(extra: {'duration_ms': duration.inMilliseconds});
    });

    _errorSub = _handler!._player.playbackEventStream.listen(
      (_) {},
      onError: (Object e, StackTrace st) {
        control.triggerEvent("error", e.toString());
      },
    );

    // Art resolves asynchronously after the index change; re-emit so Python
    // gets it for the track that is actually playing.
    _handler!.onCurrentArtResolved = _emitState;
  }

  // ── Serialized queue-op execution + ack ─────────────────────────────────────

  /// Run a queue-mutating op strictly after all previously-submitted ops
  /// complete, then adopt its epoch and emit an `op_complete` ack. Serialization
  /// guarantees a rapid add-then-skip can't race against the live playlist;
  /// the ack lets Python await the landed state (authoritative index + length)
  /// before issuing the follow-up.
  void _enqueueOp(String? requestId, int? epoch, Future<void> Function() op) {
    _opChain = _opChain.then((_) async {
      try {
        await op();
        // Adopt the epoch only after the mutation has landed, so any events
        // emitted between command-receipt and here still carry the OLD epoch
        // and are correctly rejected by Python.
        if (epoch != null) _epoch = epoch;
        _emitOpComplete(requestId, true, null);
      } catch (e) {
        debugPrint("FletAudioService: queue op error → $e");
        // Adopt on failure too: the op was PROCESSED, and the ack reports where
        // the player really is. Keeping the old epoch made Python reject every
        // later event (a frozen index mirror) until some other op succeeded.
        if (epoch != null) _epoch = epoch;
        _emitOpComplete(requestId, false, e.toString());
        control.triggerEvent("error", e.toString());
      }
    });
  }

  void _emitOpComplete(String? requestId, bool ok, String? error) {
    if (requestId == null) return;
    final handler = _handler;
    if (handler == null) return;
    final player = handler._player;
    final payload = <String, dynamic>{
      'request_id': requestId,
      'ok': ok,
      'epoch': _epoch,
      'queue_len': handler.queue.value.length,
      // Length of the LIVE source (updated synchronously by insert/remove),
      // unlike queue_len, which audio_service's stream rewrites asynchronously.
      // Python compares it with its own queue to detect divergence.
      'playlist_len': handler._playlist.children.length,
    };
    final currentIdx = player.currentIndex;
    if (currentIdx != null) payload['current_index'] = currentIdx;
    final currentSrc = _srcAt(currentIdx);
    if (currentSrc != null) payload['current_src'] = currentSrc;
    if (player.shuffleModeEnabled) {
      payload['shuffle_indices'] = player.shuffleIndices;
    }
    if (error != null) payload['error'] = error;
    control.triggerEvent("op_complete", jsonEncode(payload));
  }

  Future<void> _requestRuntimePermissions() async {
    if (!Platform.isAndroid) return;
    // Request all runtime permissions audio_service / file-source playback
    // need on modern Android. Without this the user has to flip them in
    // Settings manually. Each permission's request() is a no-op if already
    // granted or if the OS doesn't apply that permission to this API level.
    try {
      final results = await [
        Permission.notification,        // POST_NOTIFICATIONS: Android 13+
        Permission.audio,               // READ_MEDIA_AUDIO:  Android 13+
        Permission.storage,             // READ/WRITE_EXTERNAL_STORAGE: ≤ Android 12
        Permission.microphone,          // RECORD_AUDIO: Jarvis Voice Search
      ].request();
      results.forEach((perm, status) {
        debugPrint("FletAudioService: permission $perm = $status");
      });

      // MANAGE_EXTERNAL_STORAGE is a special permission on Android 11+ —
      // request() launches the system Settings activity rather than showing
      // an in-app dialog. Gate on isGranted so we don't re-open Settings on
      // every cold start once the user has granted access. Required for
      // delete/metadata-edit operations on files under /storage/emulated/0.
      final mesGranted = await Permission.manageExternalStorage.isGranted;
      if (!mesGranted) {
        final mesStatus = await Permission.manageExternalStorage.request();
        debugPrint("FletAudioService: manageExternalStorage = $mesStatus");
      }
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
          // true: while PAUSED the service leaves the foreground, so Android
          // may freeze or reclaim the idle process instead of keeping it
          // pinned (and its timers firing) for as long as the user stays
          // paused. The notification becomes swipe-dismissible while paused;
          // a reclaimed process cold-starts into the saved queue.
          androidStopForegroundOnPause: true,
          // Android 14+ MediaStyle notifications require an explicit icon;
          // omitting this can cause SystemUI to kill the foreground service
          // on screen-lock. mipmap/ic_launcher always exists in a Flet app.
          androidNotificationIcon: 'mipmap/ic_launcher',
          // Guard for network artUris (local art arrives pre-scaled from the
          // plugin's resolveArtwork): never hand a full-resolution cover to
          // the notification / media session.
          artDownscaleWidth: 512,
          artDownscaleHeight: 512,
        ),
      );
    }
    debugPrint("FletAudioService: AudioService.init done");

    final src = control.getString('src');
    if (src != null) {
      debugPrint("FletAudioService: Initial src found: $src");
      final durationStr = control.getString('duration_ms');
      final durationMs = durationStr != null ? int.tryParse(durationStr) : null;
      final item = MediaItem(
        id: src,
        album: control.getString('album') ?? 'Flet Music',
        title: control.getString('title') ?? 'Unknown',
        artist: control.getString('artist') ?? 'Unknown',
        artUri: control.getString('album_art') != null
            ? Uri.parse(control.getString('album_art')!)
            : null,
        duration: durationMs != null ? Duration(milliseconds: durationMs) : null,
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
    _positionTimer?.cancel();
    _positionTimer = null;
    _durationSub?.cancel();
    _errorSub?.cancel();
    _indexSub?.cancel();
    if (_handler?.onCurrentArtResolved == _emitState) {
      _handler?.onCurrentArtResolved = null;
    }
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _isBackground = (state == AppLifecycleState.hidden ||
        state == AppLifecycleState.paused ||
        state == AppLifecycleState.detached);
    debugPrint("FletAudioService: Lifecycle state changed to $state, _isBackground=$_isBackground");
    _syncPositionTicker();
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
        // ignore: leave artUri null
      }
    }
    final durationMs = map['duration_ms'] as int?;
    return MediaItem(
      id: src,
      album: (map['album'] as String?) ?? 'Flet Music',
      title: (map['title'] as String?) ?? '',
      artist: (map['artist'] as String?) ?? '',
      artUri: artUri,
      duration: durationMs != null ? Duration(milliseconds: durationMs) : null,
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// AudioPlayerHandler
// ─────────────────────────────────────────────────────────────────────────────

class AudioPlayerHandler extends BaseAudioHandler with QueueHandler, SeekHandler {
  final _equalizer = AndroidEqualizer();
  final _loudnessEnhancer = AndroidLoudnessEnhancer();
  late final AudioPlayer _player;
  List<AndroidEqualizerBand>? _equalizerBands;

  ConcatenatingAudioSource _playlist = ConcatenatingAudioSource(
    children: [],
    useLazyPreparation: true,
  );

  AudioPlayerHandler() {
    final effects = <AndroidAudioEffect>[];
    if (Platform.isAndroid) {
      effects.add(_equalizer);
      effects.add(_loudnessEnhancer);
      _equalizer.setEnabled(true);
      _loudnessEnhancer.setEnabled(true);
      _equalizer.parameters.then((p) {
        _equalizerBands = p.bands;
      }).catchError((e) {
        debugPrint("FletAudioService: Failed to get equalizer parameters: $e");
      });
    }

    _player = AudioPlayer(
      audioPipeline: AudioPipeline(androidAudioEffects: effects),
    );

    // NOTE: `.pipe(playbackState)` opens an addStream on the playbackState
    // rxdart subject that stays in flight for the handler's entire life. Any
    // other `.add()` on that subject (ours or audio_service's own broadcast on
    // stop/skip) then throws "You cannot add items while items are being added
    // from addStream", which aborts the in-flight queue op → Python sees
    // ok=false, falls back to a full setAudioSource re-push, and the native
    // player desyncs from the UI. An explicit guarded listen adds per-event
    // without ever holding an addStream open.
    _player.playbackEventStream.map(_transformEvent).listen(
      (state) {
        if (!playbackState.isClosed) playbackState.add(state);
      },
      onError: (Object e, StackTrace st) {
        debugPrint("FletAudioService: playbackState transform error → $e");
      },
    );

    _player.sequenceStateStream.listen((state) {
      if (state == null) return;
      // Defensive: a source with a null/non-MediaItem tag (can happen during
      // a mid-load sequence swap) would throw on a forced `as MediaItem` cast
      // and kill this listener, freezing all future queue/mediaItem updates.
      // whereType filters those out instead.
      final items = state.effectiveSequence
          .map((source) => source.tag)
          .whereType<MediaItem>()
          .toList();
      queue.add(items);
      final current = state.currentSource?.tag;
      if (current is MediaItem) {
        mediaItem.add(_withArt(current));
        _ledgerOnItem(current);
        _ensureArt(current);
        final next = _player.nextIndex;
        final seq = _player.sequence;
        if (next != null && next < seq.length) {
          final nextTag = seq[next].tag;
          if (nextTag is MediaItem) _ensureArt(nextTag);
        }
      }
    });

    // Play ledger inputs. The clock runs only while audio is actually audible
    // (playing AND ready), so pauses, buffering stalls and the post-queue
    // `completed` state (where `playing` stays true) are not counted.
    _player.playerStateStream.listen((s) {
      _ledgerCounting =
          s.playing && s.processingState == ProcessingState.ready;
      if (_ledgerCounting) {
        _ledgerClock.start();
      } else {
        _ledgerClock.stop();
      }
      if (s.processingState == ProcessingState.completed ||
          s.processingState == ProcessingState.idle) {
        _ledgerFinalize();
      }
    });
    // A loop (repeat-one, or repeat-all over one item) replays the SAME item,
    // so the sequence listener sees no change; autoAdvance onto the ledger's
    // own item is the only signal that a fresh listen began.
    _player.positionDiscontinuityStream.listen((d) {
      if (d.reason != PositionDiscontinuityReason.autoAdvance) return;
      final tag = _player.sequenceState.currentSource?.tag;
      if (tag is MediaItem && tag.id == _ledgerItem?.id) {
        _ledgerFinalize();
        _ledgerOnItem(tag);
      }
    });

    _initAudioSession();
  }

  // ── Now-playing artwork ───────────────────────────────────────────────────
  // Python only knows sidecar images (cover.jpg), which Android 13+ won't let
  // us read with READ_MEDIA_AUDIO alone, and it never extracted embedded art.
  // The plugin's resolveArtwork pulls embedded art (sidecar as fallback) into
  // a downscaled JPEG in the app cache; we swap it into the MediaItem. Done
  // here, not in Python, so it keeps working through background auto-advance
  // while the Flet session is torn down. Keyed by item id; null = no art found.
  final Map<String, Uri?> _artById = {};
  final Set<String> _artPending = {};
  /// Set by the live FletAudioService: called when the CURRENT item's art
  /// finishes resolving, so it can be pushed to Python.
  void Function()? onCurrentArtResolved;

  MediaItem _withArt(MediaItem item) {
    final art = _artById[item.id];
    return art == null ? item : item.copyWith(artUri: art);
  }

  void _ensureArt(MediaItem item) {
    final src = item.id;
    if (_artById.containsKey(src) || _artPending.contains(src)) return;
    final String path;
    if (src.startsWith('file://')) {
      path = Uri.parse(src).toFilePath();
    } else if (src.startsWith('/')) {
      path = src;
    } else {
      return; // network stream: keep whatever artUri Python sent
    }
    final given = item.artUri;
    final sidecar =
        (given != null && given.isScheme('file')) ? given.toFilePath() : null;
    final album = item.album ?? '';
    // Album-level key so one decode serves every track on the album; fall back
    // to the file itself when the album is unknown (singles folders etc.).
    final key = (album.isEmpty || album == 'Unknown Album' || album == 'Flet Music')
        ? path
        : '${item.artist ?? ''}\u0000$album';
    _artPending.add(src);
    _decodeChannel.invokeMethod<String>('resolveArtwork', {
      'path': path,
      'sidecar': sidecar,
      'key': key,
    }).then((out) {
      _artPending.remove(src);
      if (_artById.length > 4000) _artById.clear();
      _artById[src] = out == null ? null : Uri.file(out);
      final cur = mediaItem.value;
      if (out != null && cur != null && cur.id == src) {
        mediaItem.add(cur.copyWith(artUri: Uri.file(out)));
        onCurrentArtResolved?.call();
      }
    }).catchError((Object e) {
      _artPending.remove(src);
      debugPrint("FletAudioService: resolveArtwork failed → $e");
    });
  }

  // ── Play ledger ───────────────────────────────────────────────────────────
  // Records how long each item was actually heard, independent of the Flet
  // session. Python used to count plays from its own mirror of the queue
  // index, which goes deaf when Android suspends the app long enough to tear
  // the session down — music keeps playing here, but nothing got logged. The
  // handler outlives the session, so the ledger lives here: one JSON line per
  // finished listen, appended to a file Python owns and drains into the DB
  // whenever it is connected. Cost: a Stopwatch toggle on play/pause and one
  // ~150-byte append per track. No per-tick work.
  static const int _ledgerMinMs = 1000;
  static const int _ledgerPendingCap = 500;
  MediaItem? _ledgerItem;
  final Stopwatch _ledgerClock = Stopwatch();
  bool _ledgerCounting = false;
  int _ledgerStartedAt = 0;
  String? _ledgerPath;
  final List<String> _ledgerPending = [];
  Future<void> _ledgerWrites = Future<void>.value();

  static int _nowSecs() => DateTime.now().millisecondsSinceEpoch ~/ 1000;

  /// Current item reported by the sequence. Same id = same listen continuing
  /// (queue inserts/moves and shuffle re-emit the sequence without changing
  /// what is playing); a new id closes the previous listen and opens one.
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
    _ledgerAppend(jsonEncode({
      'id': item.id,
      'ms': ms,
      'dur': item.duration?.inMilliseconds ?? 0,
      'start': _ledgerStartedAt,
      'end': _nowSecs(),
    }));
  }

  void _ledgerAppend(String line) {
    final path = _ledgerPath;
    if (path == null) {
      // Python hasn't told us where the ledger lives yet (cold start before
      // `ready`). Hold lines in memory; setPlayLogPath flushes them.
      if (_ledgerPending.length < _ledgerPendingCap) _ledgerPending.add(line);
      return;
    }
    // Chained so appends never interleave.
    _ledgerWrites = _ledgerWrites.then((_) async {
      try {
        await File(path).writeAsString('$line\n',
            mode: FileMode.append, flush: true);
      } catch (e) {
        debugPrint("FletAudioService: play-ledger append failed → $e");
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

  bool _wasPlayingBeforeInterruption = false;

  Future<void> _initAudioSession() async {
    final session = await AudioSession.instance;
    await session.configure(const AudioSessionConfiguration.music());

    session.interruptionEventStream.listen((event) {
      if (event.type == AudioInterruptionType.duck) {
        _player.setVolume(event.begin ? 0.5 : 1.0);
      } else if (event.type == AudioInterruptionType.pause) {
        if (event.begin) {
          _wasPlayingBeforeInterruption = _player.playing;
          _player.pause();
        } else {
          if (_wasPlayingBeforeInterruption) {
            _player.play();
          }
        }
      } else if (event.type == AudioInterruptionType.unknown && event.begin) {
        _wasPlayingBeforeInterruption = _player.playing;
        _player.pause();
      }
    });
  }

  /// androidCompactActionIndices [0, 1, 3] = previous, play/pause, next
  PlaybackState _transformEvent(PlaybackEvent event) {
    final repeatMode = const {
      LoopMode.off: AudioServiceRepeatMode.none,
      LoopMode.one: AudioServiceRepeatMode.one,
      LoopMode.all: AudioServiceRepeatMode.all,
    }[_player.loopMode] ?? AudioServiceRepeatMode.none;

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
      repeatMode: repeatMode,
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

  @override
  Future<void> setRepeatMode(AudioServiceRepeatMode repeatMode) async {
    final loopMode = const {
      AudioServiceRepeatMode.none: LoopMode.off,
      AudioServiceRepeatMode.one: LoopMode.one,
      AudioServiceRepeatMode.all: LoopMode.all,
    }[repeatMode] ?? LoopMode.off;
    await _player.setLoopMode(loopMode);
  }

  AudioSource _buildAudioSource(String src, MediaItem item) {
    try {
      // A bare filesystem path (leading '/') is a local file. Routing it
      // through AudioSource.uri makes ExoPlayer treat it as a schemeless
      // network URI and fail to load, so handle it explicitly first.
      if (src.startsWith('/')) {
        return AudioSource.file(src, tag: item);
      }
      final uri = Uri.parse(src);
      if (uri.isScheme('file')) {
        return AudioSource.file(uri.toFilePath(), tag: item);
      }
      // Any source that parsed without a scheme is also a local path.
      if (!uri.hasScheme) {
        return AudioSource.file(src, tag: item);
      }
      return AudioSource.uri(uri, tag: item);
    } catch (_) {
      return AudioSource.uri(Uri.parse(src), tag: item);
    }
  }

  Future<void> setPlaylist(List<MediaItem> items, [int startIndex = 0]) async {
    final sources = items
        .map((item) => _buildAudioSource(item.id, item))
        .toList();
    _playlist = ConcatenatingAudioSource(
      children: sources,
      useLazyPreparation: true,
    );
    queue.add(items);
    final clampedStart = items.isEmpty
        ? 0
        : startIndex.clamp(0, items.length - 1);
    if (items.isNotEmpty) mediaItem.add(_withArt(items[clampedStart]));
    await _player.stop();
    // Setting initialIndex inside setAudioSource avoids the race where a
    // separate seek call would be clobbered by the source-load defaulting
    // back to index 0.
    //
    // preload: true (combined with useLazyPreparation on the parent) eagerly
    // loads only the initial child. This is critical for session-restore:
    // without it, the source isn't decoded until play() is called, so
    // durationStream/processingState=ready never fire; Python's
    // _is_loaded stays False, the slider's max stays 0, and any user scrub
    // gets stuffed into _restore_position instead of seeking. The first
    // play() then applies that stale scrub target; which can exceed the
    // actual track duration and trigger auto-advance ("skip song").
    await _player.setAudioSource(
      _playlist,
      preload: true,
      initialIndex: clampedStart,
    );
    // The shuffle-mode flag persists across setAudioSource, but the new source
    // starts with an identity shuffle order. Re-randomise (anchored at the
    // start item) so re-pushes while shuffling stay shuffled instead of
    // silently reverting to sequential play order.
    if (_player.shuffleModeEnabled) {
      await _player.shuffle();
    }
  }

  Future<void> addQueueItemAt(MediaItem item, int index) async {
    // In-place insert on the live ConcatenatingAudioSource; does NOT call
    // _player.setAudioSource, so the currently-playing source is not torn
    // down. _rebuildPlaylist (the previous approach) reloaded the player
    // from position 0 on every queue mutation, which the user perceived
    // as playback "crashing" on swipe-to-queue / reorder.
    final clamped = index.clamp(0, _playlist.children.length);
    await _playlist.insert(
      clamped,
      _buildAudioSource(item.id, item),
    );
    final updated = List<MediaItem>.from(queue.value);
    updated.insert(index.clamp(0, updated.length), item);
    queue.add(updated);
  }

  /// Batch sibling of addQueueItemAt: insert several items in one pass with a
  /// single queue.add() at the end. Indices are applied in order (each against
  /// the growing playlist), matching N sequential addQueueItemAt calls, so the
  /// active source is never torn down and playback continues uninterrupted.
  Future<void> addQueueItemsAt(List<MediaItem> items, List<int> indices) async {
    if (items.isEmpty) return;
    final updated = List<MediaItem>.from(queue.value);
    for (var i = 0; i < items.length; i++) {
      final item = items[i];
      final idx = i < indices.length ? indices[i] : _playlist.children.length;
      await _playlist.insert(
        idx.clamp(0, _playlist.children.length),
        _buildAudioSource(item.id, item),
      );
      updated.insert(idx.clamp(0, updated.length), item);
    }
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
        // preload: false; just_audio will load lazily when play() is called.
        // No _player.stop() first: setAudioSource replaces the previous
        // source, and stop() can deadlock against a previously-pending source.
        await _player.setAudioSource(
          _buildAudioSource(url, item),
          preload: false,
        );
      } catch (e) {
        debugPrint('flet_audio_service: Error setting audio source: $e');
      }
    }
  }
}
