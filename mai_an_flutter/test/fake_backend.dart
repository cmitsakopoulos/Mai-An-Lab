import 'dart:async';

import 'package:mai_an_lab/player/play_queue.dart';
import 'package:mai_an_lab/player/playback_backend.dart';
import 'package:mai_an_lab/player/queue_entry.dart';

/// The native playlist as a list, like tests/test_queue_divergence.py's
/// FakeDart: applies ops (optionally slowly) and reports what it holds.
class FakeBackend implements PlaybackBackend {
  final rows = <QueueEntry>[];
  final ops = <String>[];
  Duration latency = Duration.zero;

  /// Makes the next splice throw, like Dart's RangeError on a bad edit.
  bool failNextSplice = false;

  @override
  bool playing = false;
  @override
  Processing processing = Processing.idle;
  @override
  Duration position = Duration.zero;
  @override
  Duration? duration;
  @override
  int? currentIndex;
  bool shuffleOn = false;
  List<int> order = const [];
  Repeat repeat = Repeat.none;
  String? ledgerPath;
  String? resumePath;

  final _changes = StreamController<void>.broadcast(sync: true);
  final _failures = StreamController<PlaybackFailure>.broadcast(sync: true);

  void emit() => _changes.add(null);

  @override
  Stream<void> get changes => _changes.stream;
  @override
  Stream<PlaybackFailure> get failures => _failures.stream;

  final listenEvents = StreamController<Listen>.broadcast(sync: true);
  @override
  Stream<Listen> get listens => listenEvents.stream;
  @override
  List<int> get shuffleIndices => shuffleOn ? order : const [];
  @override
  int get length => rows.length;
  @override
  String? idAt(int? i) =>
      (i == null || i < 0 || i >= rows.length) ? null : rows[i].src;
  @override
  String? artFor(String src) => null;

  Future<void> _lag() =>
      latency == Duration.zero ? Future.value() : Future.delayed(latency);

  /// The player moving on by itself (gapless auto-advance, a notification
  /// skip).
  void advance([int by = 1]) {
    currentIndex = currentIndex! + by;
    position = Duration.zero;
    emit();
  }

  @override
  void setPaths({required String ledgerPath, required String resumePath}) {
    this.ledgerPath = ledgerPath;
    this.resumePath = resumePath;
  }

  @override
  Future<void> setPlaylist(
    List<QueueEntry> items,
    int start, {
    required bool autoplay,
    bool? shuffle,
    bool keepListen = false,
  }) async {
    await _lag();
    ops.add('set_playlist');
    rows
      ..clear()
      ..addAll(items);
    if (shuffle != null) shuffleOn = shuffle;
    currentIndex = items.isEmpty ? null : start.clamp(0, items.length - 1);
    if (shuffleOn) order = _shuffled(currentIndex ?? 0);
    position = Duration.zero;
    processing = Processing.ready;
    if (autoplay) playing = true;
    emit();
  }

  List<int> _shuffled(int first) => [
    first,
    for (var i = rows.length - 1; i >= 0; i--)
      if (i != first) i,
  ];

  @override
  Future<void> splice(List<SpliceEdit> edits) async {
    await _lag();
    ops.add('splice');
    if (failNextSplice) {
      failNextSplice = false;
      throw RangeError('injected');
    }
    for (final e in edits) {
      if (e.start + e.deleteCount > rows.length) throw RangeError('splice');
      rows.removeRange(e.start, e.start + e.deleteCount);
      rows.insertAll(e.start, e.items);
      _shift(e);
    }
    emit();
  }

  /// How just_audio's index follows a ConcatenatingAudioSource edit: rows
  /// removed or inserted before the playing one shift it; removing the
  /// playing row leaves the index on the row that slid into its place.
  void _shift(SpliceEdit e) {
    var cur = currentIndex;
    if (cur == null) return;
    if (e.deleteCount > 0) {
      if (e.start + e.deleteCount <= cur) {
        cur -= e.deleteCount;
      } else if (e.start <= cur) {
        cur = e.start;
      }
    }
    if (e.items.isNotEmpty && e.start <= cur) cur += e.items.length;
    currentIndex = rows.isEmpty ? null : cur.clamp(0, rows.length - 1);
  }

  @override
  Future<void> move(int from, int to) async {
    await _lag();
    ops.add('move');
    final cur = currentIndex;
    rows.insert(to, rows.removeAt(from));
    if (cur != null) {
      if (from == cur) {
        currentIndex = to;
      } else if (from < cur && cur <= to) {
        currentIndex = cur - 1;
      } else if (to <= cur && cur < from) {
        currentIndex = cur + 1;
      }
    }
    emit();
  }

  @override
  Future<void> skipTo(int index, {bool autoplay = true}) async {
    await _lag();
    ops.add('skip');
    currentIndex = index;
    position = Duration.zero;
    if (autoplay) playing = true;
    emit();
  }

  @override
  Future<void> setShuffle(bool enabled) async {
    ops.add('shuffle');
    shuffleOn = enabled;
    order = enabled ? _shuffled(currentIndex ?? 0) : const [];
    emit();
  }

  @override
  Future<void> setRepeat(Repeat mode) async => repeat = mode;

  @override
  Future<void> play() async {
    ops.add('play');
    playing = true;
    emit();
  }

  @override
  Future<void> pause() async {
    ops.add('pause');
    playing = false;
    emit();
  }

  @override
  Future<void> stop() async {
    ops.add('stop');
    playing = false;
    processing = Processing.idle;
    emit();
  }

  @override
  Future<void> seek(Duration p) async {
    ops.add('seek');
    position = p;
    emit();
  }

  double boostDb = 0;
  final eq = List<double>.filled(5, 0);

  @override
  Future<void> setLoudnessBoost(double gainDb) async => boostDb = gainDb;

  @override
  Future<void> setEqBandGain(int index, double gainDb) async =>
      eq[index] = gainDb;

  @override
  Future<void> skipToNext() async => advance();
  @override
  Future<void> skipToPrevious() async => advance(-1);
}
