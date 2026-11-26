import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:speech_to_text/speech_to_text.dart';

/// Result of one push-to-talk recognition attempt.
typedef SttResult = ({bool ok, String text, String? error});

/// Abstract TTS + STT + microphone-permission backend for [AssistantController]
/// so voice flows are testable without platform channels.
abstract class VoiceBackend {
  Future<void> setVoice({double? rate, double? pitch});
  Future<void> speak(
    String text, {
    Duration timeout = const Duration(seconds: 30),
  });
  Future<void> stopSpeaking();
  Future<bool> ensureMicPermission();
  Future<SttResult> listen({
    Duration maxDuration = const Duration(seconds: 60),
  });
  Future<void> stopListening();
}

/// Lifted from `flet_audio_service.dart` `_applyJarvisVoice`:
/// 1. British English male (`en-gb`/`en_gb` + `male`/`man`/`low`/`rjs`/
///    `x-gb-local`/`gb-x-fis-local`)
/// 2. Any British English voice (`en-gb`/`en_gb`)
/// 3. Any male voice (`male`/`man`)
/// 4. Otherwise null (caller sticks with `en-GB`).
Map<String, String>? pickJarvisVoice(List<Map<String, String>> voices) {
  bool isGb(String locale) =>
      locale.contains('en-gb') || locale.contains('en_gb');

  for (final v in voices) {
    final name = (v['name'] ?? '').toLowerCase();
    final locale = (v['locale'] ?? '').toLowerCase();
    if (isGb(locale) &&
        (name.contains('male') ||
            name.contains('man') ||
            name.contains('low') ||
            name.contains('rjs') ||
            name.contains('x-gb-local') ||
            name.contains('gb-x-fis-local'))) {
      return {'name': v['name'] ?? '', 'locale': v['locale'] ?? ''};
    }
  }
  for (final v in voices) {
    final locale = (v['locale'] ?? '').toLowerCase();
    if (isGb(locale)) {
      return {'name': v['name'] ?? '', 'locale': v['locale'] ?? ''};
    }
  }
  for (final v in voices) {
    final name = (v['name'] ?? '').toLowerCase();
    if (name.contains('male') || name.contains('man')) {
      return {'name': v['name'] ?? '', 'locale': v['locale'] ?? ''};
    }
  }
  return null;
}

/// Real TTS / STT backend lifted from `flet_audio_service.dart` (lines
/// 529–716): Google TTS engine on Android, `en-GB` British male voice
/// preference, rate/pitch clamped as in the extension, and push-to-talk
/// `speech_to_text` with `pauseFor == listenFor` so silence doesn't cut off
/// mid-hold.
class FlutterVoiceBackend implements VoiceBackend {
  FlutterTts? _tts;
  Completer<void>? _ttsCompleter;
  double _ttsRate = 0.4;
  double _ttsPitch = 1.0;

  SpeechToText? _stt;
  Completer<SttResult>? _sttCompleter;
  Timer? _doneGraceTimer;
  String _latestWords = '';

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
        _ttsCompleter!.completeError(msg.toString());
      }
    });
    tts.setCancelHandler(() {
      if (_ttsCompleter != null && !_ttsCompleter!.isCompleted) {
        _ttsCompleter!.complete();
      }
    });

    try {
      if (Platform.isAndroid) {
        await tts.setEngine('com.google.android.tts');
      }
    } catch (_) {}
    try {
      await tts.setLanguage('en-GB');
    } catch (_) {}
    try {
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
      final raw = await tts.getVoices;
      if (raw is List) {
        final voices = <Map<String, String>>[
          for (final item in raw)
            if (item is Map)
              {
                'name': '${item['name'] ?? ''}',
                'locale': '${item['locale'] ?? ''}',
              },
        ];
        final picked = pickJarvisVoice(voices);
        if (picked != null) {
          await tts.setVoice(picked);
          return;
        }
      }
      await tts.setLanguage('en-GB');
    } catch (e) {
      debugPrint('Jarvis voice selection error: $e');
    }
  }

  @override
  Future<void> setVoice({double? rate, double? pitch}) async {
    if (rate != null) _ttsRate = rate.clamp(0.1, 1.5);
    if (pitch != null) _ttsPitch = pitch.clamp(0.5, 2.0);
    if (_tts == null) return;
    try {
      await _tts!.setSpeechRate(_ttsRate);
      await _tts!.setPitch(_ttsPitch);
    } catch (_) {}
  }

  @override
  Future<void> speak(
    String text, {
    Duration timeout = const Duration(seconds: 30),
  }) async {
    if (text.trim().isEmpty) return;
    final tts = await _ensureTts();
    if (_ttsCompleter != null && !_ttsCompleter!.isCompleted) {
      _ttsCompleter!.complete();
    }
    final completer = Completer<void>();
    _ttsCompleter = completer;
    await tts.speak(text);
    try {
      await completer.future.timeout(timeout);
    } catch (e) {
      debugPrint('FlutterVoiceBackend: TTS wait ended: $e');
    }
  }

  @override
  Future<void> stopSpeaking() async {
    if (_tts == null) return;
    try {
      await _tts!.stop();
    } catch (_) {}
    if (_ttsCompleter != null && !_ttsCompleter!.isCompleted) {
      _ttsCompleter!.complete();
    }
  }

  @override
  Future<bool> ensureMicPermission() async {
    if (!Platform.isAndroid && !Platform.isIOS) return true;
    final status = await Permission.microphone.status;
    if (status.isGranted) return true;
    final requested = await Permission.microphone.request();
    return requested.isGranted;
  }

  void _scheduleDoneGrace() {
    _doneGraceTimer?.cancel();
    _doneGraceTimer = Timer(const Duration(milliseconds: 650), () {
      final c = _sttCompleter;
      if (c != null && !c.isCompleted) {
        final text = _latestWords.trim();
        c.complete((
          ok: text.isNotEmpty,
          text: text,
          error: text.isEmpty ? 'no_match' : null,
        ));
      }
    });
  }

  Future<SpeechToText> _ensureStt() async {
    if (_stt != null) return _stt!;
    final stt = SpeechToText();
    final ok = await stt.initialize(
      onError: (err) {
        debugPrint('STT Error: $err');
        _doneGraceTimer?.cancel();
        final c = _sttCompleter;
        if (c != null && !c.isCompleted) {
          final fallback = _latestWords.trim();
          if (fallback.isNotEmpty) {
            c.complete((ok: true, text: fallback, error: null));
          } else {
            c.complete((ok: false, text: '', error: err.errorMsg));
          }
        }
      },
      onStatus: (stat) {
        debugPrint('STT Status: $stat');
        // On Android, 'notListening' / 'done' often fires immediately on
        // stop() *before* SpeechRecognizer delivers the final onResult. Wait a
        // short grace window so the final result isn't dropped.
        if (stat == 'done' || stat == 'notListening') {
          final c = _sttCompleter;
          if (c != null && !c.isCompleted) {
            _scheduleDoneGrace();
          }
        }
      },
    );
    if (!ok) {
      throw StateError('Speech recognition not available on this device');
    }
    _stt = stt;
    return stt;
  }

  @override
  Future<SttResult> listen({
    Duration maxDuration = const Duration(seconds: 60),
  }) async {
    _doneGraceTimer?.cancel();
    if (_sttCompleter != null && !_sttCompleter!.isCompleted) {
      _sttCompleter!.complete((ok: false, text: '', error: 'cancelled'));
    }
    final completer = Completer<SttResult>();
    _sttCompleter = completer;
    _latestWords = '';
    try {
      final stt = await _ensureStt();
      await stt.listen(
        onResult: (result) {
          final words = result.recognizedWords.trim();
          if (words.isNotEmpty) {
            _latestWords = words;
          }
          if (result.finalResult && !completer.isCompleted) {
            _doneGraceTimer?.cancel();
            final text = words.isNotEmpty ? words : _latestWords;
            completer.complete((
              ok: text.isNotEmpty,
              text: text,
              error: text.isEmpty ? 'no_match' : null,
            ));
          }
        },
        listenOptions: SpeechListenOptions(
          partialResults: true,
          cancelOnError: false,
          listenFor: maxDuration,
          pauseFor: maxDuration,
        ),
      );
      return await completer.future.timeout(
        maxDuration + const Duration(seconds: 5),
        onTimeout: () {
          final text = _latestWords.trim();
          return (
            ok: text.isNotEmpty,
            text: text,
            error: text.isEmpty ? 'cancelled' : null,
          );
        },
      );
    } catch (e) {
      _doneGraceTimer?.cancel();
      if (!completer.isCompleted) {
        completer.complete((ok: false, text: '', error: '$e'));
      }
      return completer.future;
    }
  }

  @override
  Future<void> stopListening() async {
    if (_stt == null) return;
    try {
      await _stt!.stop();
    } catch (_) {}
    final c = _sttCompleter;
    if (c != null && !c.isCompleted) {
      _scheduleDoneGrace();
    }
  }
}
