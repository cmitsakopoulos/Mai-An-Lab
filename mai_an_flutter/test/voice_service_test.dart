import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/assistant/adapters.dart';
import 'package:mai_an_lab/assistant/assistant_controller.dart';
import 'package:mai_an_lab/assistant/assistant_screen.dart';
import 'package:mai_an_lab/assistant/chat_memory.dart';
import 'package:mai_an_lab/assistant/runner.dart' hide Row;
import 'package:mai_an_lab/assistant/voice_service.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/ui/tokens.dart';

void main() {
  group('pickJarvisVoice selection heuristics (flet_audio_service)', () {
    test('prefers British English male voice', () {
      final picked = pickJarvisVoice([
        {'name': 'en-us-x-sfg-local', 'locale': 'en-US'},
        {'name': 'en-gb-x-gba-local', 'locale': 'en-GB'},
        {'name': 'en-gb-x-rjs-local', 'locale': 'en-GB'},
      ]);
      expect(picked, {'name': 'en-gb-x-rjs-local', 'locale': 'en-GB'});
    });

    test('falls back to any en-GB voice when no British male exists', () {
      final picked = pickJarvisVoice([
        {'name': 'en-us-male', 'locale': 'en-US'},
        {'name': 'en-gb-female', 'locale': 'en_GB'},
      ]);
      expect(picked, {'name': 'en-gb-female', 'locale': 'en_GB'});
    });

    test('falls back to any male voice when no en-GB exists', () {
      final picked = pickJarvisVoice([
        {'name': 'fr-fr-x-fra', 'locale': 'fr-FR'},
        {'name': 'en-us-man', 'locale': 'en-US'},
      ]);
      expect(picked, {'name': 'en-us-man', 'locale': 'en-US'});
    });

    test('returns null when nothing matches', () {
      expect(
        pickJarvisVoice([
          {'name': 'de-de-x-deb', 'locale': 'de-DE'},
        ]),
        isNull,
      );
    });
  });

  group('AssistantController voice TTS & push-to-talk STT', () {
    late Directory dir;
    late String dbPath;

    setUp(() {
      dir = Directory.systemTemp.createTempSync('mai_an_voice_');
      dbPath = '${dir.path}/library.db';
    });

    tearDown(() async {
      await Future<void>.delayed(const Duration(milliseconds: 50));
      if (dir.existsSync()) dir.deleteSync(recursive: true);
    });

    Future<LibraryRepository> createDb() async {
      final repo = await LibraryRepository.open(dbPath, writable: true);
      await repo.debugExecute([
        (
          "INSERT INTO artists (id, name, album_count, track_count) VALUES (1, 'Ride', 1, 0)",
          const [],
        ),
        (
          "INSERT INTO albums (id, artist_id, title, year, genre, track_count) VALUES (1, 1, 'Nowhere', 1990, 'Shoegaze', 0)",
          const [],
        ),
        (
          "INSERT INTO tracks (id, album_id, title, path, duration, format, track_num, added_date) VALUES (1, 1, 'Vapour Trail', '/m/vapour.flac', 258.0, 'FLAC', 1, 1.0)",
          const [],
        ),
      ]);
      return repo;
    }

    test(
      'onShow configures rate=0.75, pitch=0.75; onHide and toggleTts stop speech',
      () async {
        final voice = _FakeVoice();
        final ctrl = AssistantController(
          memory: ChatMemory(dir.path),
          voice: voice,
          initDelay: Duration.zero,
        );
        addTearDown(ctrl.dispose);

        await ctrl.onShow();
        expect(voice.lastRate, 0.75);
        expect(voice.lastPitch, 0.75);

        ctrl.onHide();
        expect(voice.stopSpeakCalls, 1);

        ctrl.toggleTts();
        expect(ctrl.ttsEnabled, isFalse);
        expect(voice.stopSpeakCalls, 2);
      },
    );

    test('permission denied shows snackbar and does not listen', () async {
      final voice = _FakeVoice()..grantPermission = false;
      final snacks = <String>[];
      final ctrl = AssistantController(
        memory: ChatMemory(dir.path),
        voice: voice,
        onSnack: snacks.add,
        initDelay: Duration.zero,
      );
      addTearDown(ctrl.dispose);

      ctrl.onMicDown();
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(voice.listenCalls, 0);
      expect(ctrl.micListening, isFalse);
      expect(snacks, ['Microphone permission is required for voice commands.']);
    });

    test('releasing mic during permission prompt aborts STT cleanly', () async {
      final permCompleter = Completer<bool>();
      final voice = _FakeVoice()..permissionCompleter = permCompleter;
      final ctrl = AssistantController(
        memory: ChatMemory(dir.path),
        voice: voice,
        initDelay: Duration.zero,
      );
      addTearDown(ctrl.dispose);

      ctrl.onMicDown();
      await Future<void>.delayed(const Duration(milliseconds: 5));
      ctrl.onMicUp();
      permCompleter.complete(true);
      await Future<void>.delayed(const Duration(milliseconds: 20));

      expect(voice.listenCalls, 0);
      expect(ctrl.micListening, isFalse);
    });

    test(
      'hold -> release -> Thinking -> transcription dispatches and speaks before deferred play',
      () async {
        final repo = await createDb();
        addTearDown(repo.close);

        final events = <String>[];
        final voice = _FakeVoice(onSpeakLog: (t) => events.add('speak:$t'));
        final player = _LoggingPlayer(() => events.add('play'));
        final runner = AssistantRunner(
          library: RepoAssistantLibrary(repo),
          player: player,
          now: () => DateTime.utc(2026, 11, 24, 15, 0),
        );
        final ctrl = AssistantController(
          memory: ChatMemory(dir.path),
          runner: runner,
          voice: voice,
          initDelay: Duration.zero,
        );
        addTearDown(ctrl.dispose);

        ctrl.onMicDown();
        await Future<void>.delayed(const Duration(milliseconds: 20));
        expect(ctrl.micListening, isTrue);
        expect(voice.lastMaxDuration, const Duration(seconds: 60));

        ctrl.onMicUp();
        expect(ctrl.micListening, isFalse);
        expect(ctrl.micProcessing, isTrue);
        expect(voice.stopListenCalls, 1);

        voice.completeListen((
          ok: true,
          text: 'play Vapour Trail',
          error: null,
        ));
        for (var i = 0; i < 50; i++) {
          await Future<void>.delayed(const Duration(milliseconds: 10));
          if (!ctrl.thinking &&
              ctrl.messages.length >= 2 &&
              events.length >= 2) {
            break;
          }
        }

        expect(ctrl.micListening, isFalse);
        expect(ctrl.micProcessing, isFalse);
        expect(ctrl.messages.length, 2);
        expect(ctrl.messages[0]['text'], 'play Vapour Trail');
        // Speech finishes BEFORE deferred play starts the track.
        expect(events.length, 2);
        expect(events[0], startsWith('speak:'));
        expect(events[1], 'play');
      },
    );

    test(
      'post-release timeout cancels stalled STT and ignores cancel/no_match; surfaces real errors',
      () async {
        final voice = _FakeVoice();
        final snacks = <String>[];
        final ctrl = AssistantController(
          memory: ChatMemory(dir.path),
          voice: voice,
          onSnack: snacks.add,
          initDelay: Duration.zero,
          postReleaseTimeout: const Duration(milliseconds: 40),
        );
        addTearDown(ctrl.dispose);

        ctrl.onMicDown();
        await Future<void>.delayed(const Duration(milliseconds: 15));
        ctrl.onMicUp();
        expect(ctrl.micProcessing, isTrue);

        // Don't resolve listen -> post-release timeout cancels silently.
        await Future<void>.delayed(const Duration(milliseconds: 80));
        expect(ctrl.micProcessing, isFalse);
        expect(snacks, isEmpty);

        // A real STT error surfaces a snackbar.
        ctrl.onMicDown();
        await Future<void>.delayed(const Duration(milliseconds: 15));
        voice.completeListen((ok: false, text: '', error: 'audio_device_busy'));
        await Future<void>.delayed(const Duration(milliseconds: 30));
        expect(snacks, ['Speech error: audio_device_busy']);
      },
    );

    testWidgets(
      'AssistantScreen renders mic button and listening/thinking bubbles',
      (tester) async {
        tester.view.physicalSize = const Size(390 * 3, 844 * 3);
        tester.view.devicePixelRatio = 3;
        addTearDown(tester.view.reset);

        final voice = _FakeVoice();
        final ctrl = AssistantController(
          memory: ChatMemory(dir.path),
          voice: voice,
          initDelay: Duration.zero,
        );
        addTearDown(ctrl.dispose);

        await tester.pumpWidget(
          MaterialApp(
            theme: Tokens.theme(),
            home: Scaffold(body: AssistantScreen(controller: ctrl)),
          ),
        );

        expect(find.byTooltip('Hold to Speak'), findsOneWidget);

        ctrl.onMicDown();
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 20)),
        );
        await tester.pump();
        expect(find.text('Listening, sir...'), findsOneWidget);
        expect(find.byTooltip('Release to Send'), findsOneWidget);

        ctrl.onMicUp();
        await tester.pump();
        expect(find.text('Thinking...'), findsOneWidget);

        voice.completeListen((ok: false, text: '', error: 'no_match'));
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 20)),
        );
        await tester.pump();
        expect(find.text('Thinking...'), findsNothing);
      },
    );
  });
}

class _FakeVoice implements VoiceBackend {
  _FakeVoice({this.onSpeakLog});

  final void Function(String text)? onSpeakLog;
  bool grantPermission = true;
  Completer<bool>? permissionCompleter;
  double? lastRate;
  double? lastPitch;
  Duration? lastMaxDuration;
  int stopSpeakCalls = 0;
  int listenCalls = 0;
  int stopListenCalls = 0;
  Completer<SttResult>? _listenCompleter;

  void completeListen(SttResult res) {
    final c = _listenCompleter;
    if (c != null && !c.isCompleted) c.complete(res);
  }

  @override
  Future<void> setVoice({double? rate, double? pitch}) async {
    lastRate = rate;
    lastPitch = pitch;
  }

  @override
  Future<void> speak(
    String text, {
    Duration timeout = const Duration(seconds: 30),
  }) async {
    onSpeakLog?.call(text);
  }

  @override
  Future<void> stopSpeaking() async {
    stopSpeakCalls++;
  }

  @override
  Future<bool> ensureMicPermission() async {
    if (permissionCompleter != null) return permissionCompleter!.future;
    return grantPermission;
  }

  @override
  Future<SttResult> listen({
    Duration maxDuration = const Duration(seconds: 60),
  }) async {
    listenCalls++;
    lastMaxDuration = maxDuration;
    final c = Completer<SttResult>();
    _listenCompleter = c;
    return c.future;
  }

  @override
  Future<void> stopListening() async {
    stopListenCalls++;
  }
}

class _LoggingPlayer implements AssistantPlayer {
  _LoggingPlayer(this.onPlay);
  final VoidCallback onPlay;
  final List<Map<String, Object?>> _queue = [];
  int _idx = 0;
  bool _playing = false;

  @override
  List<Map<String, Object?>> get queue => _queue;
  @override
  int get currentIndex => _idx;
  @override
  String get currentPath => '';
  @override
  String get currentTrack => '';
  @override
  String get currentArtist => '';
  @override
  String get currentAlbum => '';
  @override
  double get duration => 0;
  @override
  bool get isPlaying => _playing;
  @override
  bool isShuffle = false;

  @override
  void setQueue(List<Map<String, Object?>> tracks, {int startIndex = 0}) {
    _queue
      ..clear()
      ..addAll(tracks);
    _idx = startIndex;
  }

  @override
  void queueLast(Map<String, Object?> track) => _queue.add(track);
  @override
  void queueNext(Map<String, Object?> track) => _queue.insert(_idx + 1, track);
  @override
  void queueLastMany(List<Map<String, Object?>> tracks) =>
      _queue.addAll(tracks);
  @override
  void removeFromQueue(int idx) => _queue.removeAt(idx);
  @override
  void moveQueueItem(int oldIdx, int newIdx) {}
  @override
  void next() {}
  @override
  void previous() {}
  @override
  void pause() => _playing = false;
  @override
  void play() {
    _playing = true;
    onPlay();
  }

  @override
  void stop() => _playing = false;
  @override
  void clearQueue() => _queue.clear();
}
