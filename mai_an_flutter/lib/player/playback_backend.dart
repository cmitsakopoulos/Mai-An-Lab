import 'play_queue.dart';
import 'queue_entry.dart';

enum Repeat {
  none,
  one,
  all;

  static Repeat parse(Object? v) =>
      values.firstWhere((m) => m.name == v, orElse: () => none);
}

/// just_audio's processing state, by name (what the Flet engine mirrored as
/// `processing_state`).
enum Processing { idle, loading, buffering, ready, completed }

/// A failed track and what the handler did about it (Flet's `error` event:
/// {message, title, artist, action}).
typedef PlaybackFailure = ({
  String message,
  String title,
  String artist,
  String action, // skipped | stopped
});

/// What the player controller needs from the native player. The Android
/// implementation is [AudioPlayerHandler] (audio_handler.dart); tests use a
/// fake that mirrors the playlist in a list.
///
/// The playlist holds one item per queue row, in queue (logical) order;
/// shuffle order is the player's own and is read back via [shuffleIndices].
abstract interface class PlaybackBackend {
  /// Fires after any change to the properties below.
  Stream<void> get changes;

  /// Fires when the player gives up on, or skips past, an unplayable track.
  Stream<PlaybackFailure> get failures;

  bool get playing;
  Processing get processing;
  Duration get position;
  Duration? get duration;

  /// Logical index of the playing row, or null while nothing is loaded.
  int? get currentIndex;

  /// Play position → logical index. Empty unless shuffling.
  List<int> get shuffleIndices;

  int get length;

  /// MediaItem id (= [QueueEntry.src]) of row [index], or null.
  String? idAt(int? index);

  /// Resolved now-playing art for [src] (a local file path), if known.
  String? artFor(String src);

  Future<void> setPlaylist(
    List<QueueEntry> items,
    int start, {
    required bool autoplay,
    bool? shuffle,
    bool keepListen = false,
  });
  Future<void> splice(List<SpliceEdit> edits);
  Future<void> move(int from, int to);
  Future<void> skipTo(int index, {bool autoplay = true});
  Future<void> setShuffle(bool enabled);
  Future<void> setRepeat(Repeat mode);

  Future<void> play();
  Future<void> pause();
  Future<void> stop();
  Future<void> seek(Duration position);
  Future<void> skipToNext();
  Future<void> skipToPrevious();

  /// Where the handler appends finished listens and writes its resume
  /// record (Flet: `set_play_log_path`).
  void setPaths({required String ledgerPath, required String resumePath});
}
