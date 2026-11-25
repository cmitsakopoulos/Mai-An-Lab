import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../data/library_repository.dart';
import 'autoplay.dart';
import 'dsp.dart';
import 'play_queue.dart';
import 'playback_backend.dart';
import 'queue_entry.dart';
import 'queue_store.dart';

/// Playback state for every screen: the queue, the transport, shuffle and
/// repeat, persistence. Port of the Android half of utils/audio_engine.py
/// plus the playback parts of main.py (`_play_track_core`, the shuffle
/// toggle, `cycle_repeat`, queue/position saving and session restore).
///
/// In the Flet app the queue lived in Python and the player in Dart, and an
/// epoch/ack protocol kept the two in step across a process boundary. Here
/// both share an isolate, but queue edits still reach the native playlist
/// asynchronously, so the same discipline survives in small form:
///
///  * every edit changes [queue] synchronously and queues ONE backend op;
///    ops run strictly in order ([_op]);
///  * a full playlist push supersedes ops queued before it (its snapshot
///    already contains their effect);
///  * the player's own index changes (auto-advance, notification skips) are
///    adopted only while no op is pending, and only when the row at that
///    index is the track the player says is playing;
///  * once the ops drain, a length or identity mismatch triggers a full
///    re-push (rate-limited), keeping the audible track and position.
class PlayerController extends ChangeNotifier {
  PlayerController({
    required this.backend,
    required this.store,
    required this.prefs,
    required this.libraryTracks,
    this.dspConfig,
    this.dspFeatures,
  });

  final PlaybackBackend backend;
  final QueueStore store;
  final Prefs prefs;

  /// Every library track, for shuffle (which plays the whole library).
  final Future<List<Track>> Function() libraryTracks;

  /// config.toml `[dsp]`, re-read on each track change as Flet's
  /// `reapply_dsp` does. Null: no per-track sound settings.
  final Future<DspConfig> Function()? dspConfig;

  /// The play_counts features of a track (null: no row).
  final Future<DspFeatures?> Function(String path)? dspFeatures;

  final PlayQueue queue = PlayQueue();

  /// Auto-play, when the app has one (set before [init]).
  AutoPlay? autoplay;

  /// At the end of the queue, ask auto-play for more instead of stopping
  /// (engine `continues_at_end`). Set by [AutoPlay.enabled].
  bool continuesAtEnd = false;

  /// Path of the row the queue is on ('' when empty).
  String get currentPath => queue.current?.path ?? '';

  Repeat _repeat = Repeat.none;
  Repeat get repeat => _repeat;
  bool get shuffle => queue.shuffle;

  bool playing = false;
  Processing processing = Processing.idle;
  Duration duration = Duration.zero;

  /// Position, ticked once a second while playing and visible. Separate from
  /// [notifyListeners] so the slider can rebuild alone.
  final ValueNotifier<Duration> position = ValueNotifier(Duration.zero);

  /// Resolved cover for the playing row (a local file), if any.
  String? art;

  /// Dynamism's loudness boost for the playing track, dB (0 = off). Drives
  /// Now Playing's "DYNAMISM ACTIVE" badge (Flet `loudness_boost_db`).
  final ValueNotifier<double> loudnessBoost = ValueNotifier(0);

  /// Play position → row while shuffling (the player owns the order).
  List<int> shuffleIndices = const [];

  /// After stop(): nothing is loaded, but the queue is kept (engine.stop).
  bool stopped = false;

  /// One-line messages for the snackbar (Flet's show_snackbar).
  final StreamController<String> _messages = StreamController.broadcast();
  Stream<String> get messages => _messages.stream;

  /// Set while the controller has never had anything to show.
  bool get hasEverPlayed => _lastShown != null;
  QueueEntry? _lastShown;

  /// The row shown as now playing; the last one when [stopped].
  QueueEntry? get nowPlaying => stopped ? _lastShown : queue.current;

  StreamSubscription<void>? _changesSub;
  StreamSubscription<PlaybackFailure>? _failSub;
  StreamSubscription<Listen>? _listenSub;
  Timer? _ticker;
  bool _foreground = true;

  // ── startup ──────────────────────────────────────────────────────────────

  Future<void> init() async {
    backend.setPaths(
      ledgerPath: store.ledgerPath,
      resumePath: store.nativeResumePath,
    );
    await prefs.load();
    queue.shuffle = prefs.getBool('is_shuffle');
    final ap = autoplay;
    if (ap != null) {
      ap.enabled = prefs.getBool('play_similar_mode');
      unawaited(ap.loadHistory());
    }
    _repeat = Repeat.parse(prefs['repeat_mode']);
    _op(() => backend.setRepeat(_repeat));
    backend.remoteControls = (next: next, previous: previous);
    _changesSub = backend.changes.listen((_) => _onBackendChange());
    _failSub = backend.failures.listen(_onFailure);
    _listenSub = backend.listens.listen(
      (l) => autoplay?.onListen(l.path, l.listenedS, l.durationS),
    );
    await restore();
    _positionSaveLoop();
  }

  /// `_restore_queue_state_async` + `restore_queue`: the saved queue at the
  /// saved row and offset, loaded but not playing.
  Future<void> restore() async {
    final loaded = await store.loadResume();
    if (loaded == null) return;
    final (saved, at) = loaded;
    _autoplayHeld = saved.autoplayHeld;
    autoplay?.restoreHeldJson(saved.autoplayHeld);
    final change = queue.set(saved.queue, at.index);
    if (change is! Reload) return;
    store.held = true;
    _show(queue.current);
    // A restore is not a track change (Flet's `restoring` flag).
    _changedPath = queue.current?.path;
    _applyDsp();
    position.value = _secs(at.position);
    duration = _secs(at.duration);
    playing = false;
    stopped = false;
    // A missing file isn't preloaded (its load would only fail); the first
    // play pushes instead. Flet skipped the push and then couldn't play.
    if (await File(queue.current!.path).exists()) {
      _push(queue.currentIndex, autoplay: false, seekTo: position.value);
    } else {
      _needsPush = true;
    }
    autoplay?.resume(currentPath);
    notifyListeners();
  }

  // ── what the screens call ────────────────────────────────────────────────

  /// `_play_track_core` for a tap in a track list. [context] is the list as
  /// shown (sorted and filtered); [generalView] is the unfiltered library
  /// list, where a track already queued is just jumped to.
  Future<void> playTrack(
    Track target, {
    required List<Track> context,
    bool generalView = false,
  }) async {
    if (generalView && queue.isNotEmpty) {
      final existing = queue.entries.indexWhere((t) => t.path == target.path);
      if (existing >= 0) {
        playTrackAt(existing);
        _userChose(target.path);
        return;
      }
    }
    if (queue.shuffle) {
      // Shuffle plays from the ENTIRE library.
      final lib = [
        for (final t in await libraryTracks()) QueueEntry.fromTrack(t),
      ];
      var idx = lib.indexWhere((t) => t.path == target.path);
      if (idx < 0) {
        lib.insert(0, QueueEntry.fromTrack(target));
        idx = 0;
      }
      _setQueue(lib, idx);
      return;
    }
    final items = [for (final t in context) QueueEntry.fromTrack(t)];
    final idx = items.indexWhere((t) => t.path == target.path);
    if (items.isEmpty) {
      _messages.add('No playable media found.');
      return;
    }
    if (idx < 0) {
      _messages.add('Track not found in current view.');
      return;
    }
    _setQueue(items, idx);
    _userChose(target.path);
  }

  /// `autoplay.on_user_chose`, after the track-change hook has run.
  void _userChose(String path) =>
      scheduleMicrotask(() => autoplay?.onUserChose(path));

  void playTrackAt(int index) {
    if (index < 0 || index >= queue.length) return;
    _completedTerminal = false;
    queue.currentIndex = index;
    _syncForCurrent();
    _skip(index);
  }

  void toggle() {
    if (queue.isEmpty) return;
    if (_completedTerminal || _needsPush || stopped) {
      play();
      return;
    }
    if (playing) {
      pause();
    } else {
      play();
    }
  }

  /// `audio_engine.play()`: resume playback, or push the queue if stopped,
  /// completed or never loaded.
  void play() {
    if (queue.isEmpty) return;
    if (_completedTerminal || _needsPush || stopped) {
      // A finished queue replays from the top; a stopped or never-loaded one
      // loads where it is.
      final start = _completedTerminal ? 0 : queue.currentIndex;
      _completedTerminal = false;
      final seekTo = _needsPush ? position.value : Duration.zero;
      queue.currentIndex = start;
      _syncForCurrent(keepPosition: _needsPush);
      _push(start, autoplay: true, seekTo: seekTo);
      return;
    }
    _op(backend.play);
  }

  /// `audio_engine.pause()`.
  void pause() => _op(backend.pause);

  void next() {
    if (queue.isEmpty) return;
    _completedTerminal = false;
    if (_repeat == Repeat.one) {
      _op(() async {
        await backend.seek(Duration.zero);
        await backend.play();
      });
      return;
    }
    var target = queue.neighbour(1, shuffleIndices);
    if (target == null && _repeat == Repeat.all) {
      target = (queue.shuffle && shuffleIndices.isNotEmpty)
          ? shuffleIndices.first
          : 0;
      target = target.clamp(0, queue.length - 1);
    }
    if (target != null) {
      playTrackAt(target);
      return;
    }
    if (continuesAtEnd) {
      autoplay?.onQueueDry();
    } else {
      stop();
    }
  }

  void previous() {
    // The player's own position when nothing is pending: [position] isn't
    // ticked in the background, where the notification's Previous lands.
    final at = (_pending > 0 || _needsPush || stopped)
        ? position.value
        : backend.position;
    if (at > const Duration(seconds: 3)) {
      seek(Duration.zero);
      return;
    }
    if (queue.isEmpty) return;
    _completedTerminal = false;
    final target = queue.neighbour(-1, shuffleIndices);
    if (target != null) playTrackAt(target);
  }

  /// Clamped half a second short of the end, so a scrub can't complete the
  /// track (which auto-advances: "scrubbing skips the song").
  void seek(Duration target) {
    var t = target < Duration.zero ? Duration.zero : target;
    if (duration > Duration.zero) {
      final max = duration - const Duration(milliseconds: 500);
      if (t > max) t = max < Duration.zero ? Duration.zero : max;
    }
    position.value = t;
    if (_needsPush || stopped) return; // applied by the push on first play
    _op(() => backend.seek(t));
  }

  void seekBy(Duration delta) => seek(position.value + delta);

  void stop() {
    _op(backend.stop);
    _needsPush = true;
    stopped = true;
    queue.currentIndex = 0;
    playing = false;
    position.value = Duration.zero;
    duration = Duration.zero;
    notifyListeners();
  }

  void queueNext(Track t) => _apply(queue.queueNext(QueueEntry.fromTrack(t)));
  void queueLast(Track t) => _apply(queue.queueLast(QueueEntry.fromTrack(t)));
  void queueLastMany(List<Track> ts) => _apply(
    queue.queueLastMany([for (final t in ts) QueueEntry.fromTrack(t)]),
  );

  // Entry-level queue edits (`audio_engine.set_queue`, `queue_last`,
  // `queue_next`, `queue_last_many`, `remove_from_queue`), for callers
  // (e.g. AssistantPlayer) that already hold engine-shaped rows.
  void setQueueEntries(List<QueueEntry> rows, {int startIndex = 0}) =>
      _setQueue(rows, startIndex);
  void queueNextEntry(QueueEntry row) => _apply(queue.queueNext(row));
  void queueLastEntry(QueueEntry row) => _apply(queue.queueLast(row));
  void queueLastEntries(List<QueueEntry> rows) =>
      _apply(queue.queueLastMany(rows));

  void removeAt(int index) {
    if (index < 0 || index >= queue.length) return;
    final row = queue.entries[index];
    _apply(queue.removeAt(index));
    if (row.autoplay) autoplay?.reject(row.path);
  }

  /// library.py `_enqueue_network_tracks` (the selection bar's Play and Add
  /// to Queue): an ordered block. [replace] plays it now, non-destructively:
  /// shuffle order off, the block right after the playing row and a jump to
  /// its head, so the rest of the queue and auto-play survive. Otherwise it
  /// is Add to Queue (last of the listener's picks), without a jump. An empty
  /// queue just becomes the block. Returns how many tracks went in.
  int playBlock(List<Track> tracks, {required bool replace}) {
    final rows = [
      for (final t in tracks)
        if (t.path.isNotEmpty) QueueEntry.fromTrack(t),
    ];
    if (rows.isEmpty) return 0;
    if (replace && queue.shuffle) disableShuffleOrder();
    if (queue.isEmpty) {
      _setQueue(rows, 0);
    } else if (replace) {
      final start = queue.currentIndex + 1;
      insertAfter(rows);
      if (_needsPush || stopped) {
        // Nothing loaded to skip in: load the queue at the block.
        _completedTerminal = false;
        queue.currentIndex = start;
        _syncForCurrent();
        _push(start, autoplay: true);
        notifyListeners();
      } else {
        playTrackAt(start);
      }
    } else {
      _apply(queue.queueLastMany(rows));
    }
    return rows.length;
  }

  /// main.py `_delete_track`'s queue half: stop if [path] is playing, then
  /// drop every row of it.
  void removePath(String path) {
    if (path.isEmpty) return;
    if (!stopped && currentPath == path) stop();
    final rows = [
      for (final (i, r) in queue.entries.indexed)
        if (r.path == path) i,
    ];
    if (rows.isNotEmpty) removeIndices(rows);
  }

  /// Remove one row (identity: the same song can be queued twice).
  /// Removing a recommendation is a "not this" for the current station,
  /// reported after the removal so the top-up sees the gap.
  void removeRow(QueueEntry row) {
    final i = queue.indexOfRow(row);
    if (i == null) return;
    _apply(queue.removeAt(i));
    if (row.autoplay) autoplay?.reject(row.path);
  }

  // Engine edits auto-play makes (audio_engine.remove_indices,
  // queue_after_current, replace_rows, queue_extend).
  void removeIndices(List<int> indices) => _apply(queue.removeIndices(indices));
  void insertAfter(List<QueueEntry> rows, {int? afterIndex}) =>
      _apply(queue.insertAfterCurrent(rows, afterIndex: afterIndex));
  void replaceRows(List<int> indices, List<QueueEntry> rows) =>
      _apply(queue.replaceRows(indices, rows));
  void extend(List<QueueEntry> rows) => _apply(queue.extend(rows));

  /// A one-line message for the snackbar.
  void say(String message) => _messages.add(message);

  /// `audio_engine.is_shuffle = value`: switch play order in place without
  /// swapping the library queue for the set-aside one (unlike [toggleShuffle]).
  void setShuffleOrder(bool value) {
    if (queue.shuffle == value) return;
    queue.shuffle = value;
    unawaited(prefs.set('is_shuffle', value));
    _op(() => backend.setShuffle(value));
    notifyListeners();
  }

  /// `audio_engine.is_shuffle = False`: play order back to queue order,
  /// keeping the queue (auto-play turning on).
  void disableShuffleOrder() => setShuffleOrder(false);

  void moveRow(int from, int to) => _apply(queue.move(from, to));

  void clearQueue() => _apply(queue.clear());

  /// `_toggle_shuffle_async`. On: the current queue is set aside
  /// (.cache/queue_regular.json) and the whole library plays shuffled,
  /// starting from the current track. Off: the set-aside queue comes back,
  /// positioned on the current track.
  Future<void> toggleShuffle() async {
    final on = !queue.shuffle;
    unawaited(prefs.set('is_shuffle', on));
    if (on && (autoplay?.enabled ?? false)) autoplay!.setMode(false);
    final cur = queue.current;
    if (on) {
      unawaited(_savePartition('queue_regular.json'));
      final lib = [
        for (final t in await libraryTracks()) QueueEntry.fromTrack(t),
      ];
      queue.shuffle = true;
      if (lib.isEmpty) {
        _op(() => backend.setShuffle(true));
        notifyListeners();
        return;
      }
      var found = 0;
      if (cur != null) {
        found = lib.indexWhere((t) => t.path == cur.path);
        if (found < 0) {
          lib.insert(0, cur);
          found = 0;
        }
      }
      _swapQueue(lib, found);
      unawaited(_savePartition('queue_shuffle.json'));
    } else {
      unawaited(_savePartition('queue_shuffle.json'));
      final regular = await store.loadPartition('queue_regular.json');
      queue.shuffle = false;
      if (regular == null || regular.queue.isEmpty) {
        _op(() => backend.setShuffle(false));
        notifyListeners();
        return;
      }
      final rows = List.of(regular.queue);
      var found = cur == null ? -1 : rows.indexWhere((t) => t.path == cur.path);
      if (found < 0) {
        found = regular.index.clamp(0, rows.length);
        if (cur != null) rows.insert(found, cur);
      }
      _swapQueue(rows, found);
    }
  }

  void cycleRepeat() {
    _repeat = Repeat.values[(_repeat.index + 1) % Repeat.values.length];
    _op(() => backend.setRepeat(_repeat));
    unawaited(prefs.set('repeat_mode', _repeat.name));
    notifyListeners();
  }

  /// Called from the app's lifecycle listener.
  void setForeground(bool foreground) {
    if (_foreground == foreground) return;
    _foreground = foreground;
    if (foreground) {
      // Nothing was ticked while hidden.
      _onBackendChange();
    } else {
      // Nothing may refill auto-play while hidden for long: queue enough to
      // ride it out.
      autoplay?.prefillForBackground();
      unawaited(_savePosition());
    }
    _syncTicker();
  }

  // ── queue → backend ──────────────────────────────────────────────────────

  bool _completedTerminal = false;
  bool _needsPush = false;
  Object? _autoplayHeld;

  void _setQueue(List<QueueEntry> rows, int start) {
    _completedTerminal = false;
    final change = queue.set(rows, start);
    _syncForCurrent();
    _queueMutated();
    if (change is Reload) _push(change.start, autoplay: true);
  }

  /// Replaces the queue around the track that is playing without
  /// interrupting it (shuffle on/off). Flet's set_queue here restarted the
  /// track from 0:00 and started playback even when paused; the port keeps
  /// the position and the play/pause state (PORT_MAP §7).
  void _swapQueue(List<QueueEntry> rows, int start) {
    final keep = position.value;
    final wasPlaying = playing;
    final change = queue.set(rows, start);
    _queueMutated();
    notifyListeners();
    if (change is Reload) {
      _push(change.start, autoplay: wasPlaying, seekTo: keep, keepListen: true);
    }
  }

  void _apply(QueueChange change) {
    switch (change) {
      case NoChange():
        return;
      case Emptied():
        stop();
      case Reload(:final start):
        _completedTerminal = false;
        _syncForCurrent();
        _push(start, autoplay: true);
      case Splice(:final edits):
        // Removing the playing row moves the shown track along with the
        // player (provisional; the player's index is adopted after the op).
        _syncForCurrent(keepPosition: true);
        // Nothing is loaded (stopped, or a restored track missing on disk):
        // the first play pushes the whole queue, edits included.
        if (!_needsPush) _op(() => backend.splice(edits));
      case Move(:final from, :final to):
        if (!_needsPush) _op(() => backend.move(from, to));
    }
    _queueMutated();
    notifyListeners();
  }

  int _gen = 0;
  int _pushFloor = 0;
  int _pending = 0;
  bool _opFailed = false;
  Future<void> _chain = Future.value();

  /// Queue one backend op. Ops run in submission order; an op submitted
  /// before the newest full push is skipped (the push already contains it).
  void _op(Future<void> Function() body, {bool push = false}) {
    final gen = ++_gen;
    if (push) _pushFloor = gen;
    _pending++;
    _chain = _chain.then((_) async {
      try {
        if (gen >= _pushFloor) await body();
      } catch (e) {
        debugPrint('PlayerController: native op failed: $e');
        _opFailed = true;
      } finally {
        _pending--;
        if (_pending == 0) _reconcile();
      }
    });
  }

  void _push(
    int start, {
    required bool autoplay,
    Duration seekTo = Duration.zero,
    bool keepListen = false,
  }) {
    _needsPush = false;
    stopped = false;
    final rows = List.of(queue.entries);
    final shuffle = queue.shuffle;
    _op(push: true, () async {
      await backend.setPlaylist(
        rows,
        start,
        autoplay: autoplay,
        shuffle: shuffle,
        keepListen: keepListen,
      );
      if (seekTo > Duration.zero) await backend.seek(seekTo);
    });
  }

  void _skip(int index) => _op(() => backend.skipTo(index));

  /// After the op chain drains: the two playlists must agree. A mismatch
  /// means an op failed or was lost; re-push.
  void _reconcile() {
    final failed = _opFailed;
    _opFailed = false;
    if (queue.isEmpty || _needsPush) return;
    if (failed) {
      _resync('queue op failed');
    } else if (backend.length != queue.length) {
      _resync(
        'native playlist has ${backend.length} rows, queue has ${queue.length}',
      );
    } else {
      _onBackendChange();
    }
  }

  static const _resyncCooldown = Duration(seconds: 30);
  static const _verifyDelay = Duration(milliseconds: 1500);
  DateTime? _lastResync;
  bool _resyncDeferred = false;
  bool _verifyPending = false;

  /// `_resync_native`: re-push the whole queue, keeping the audible track
  /// (found by [playingSrc]) and its position. Rate-limited so a fault that
  /// survives the repair can't become a reload loop; a repair inside the
  /// cooldown is deferred, never dropped.
  void _resync(String reason, {String? playingSrc}) {
    if (queue.isEmpty) return;
    final now = DateTime.now();
    final last = _lastResync;
    if (last != null && now.difference(last) < _resyncCooldown) {
      if (_resyncDeferred) return;
      _resyncDeferred = true;
      final floor = _pushFloor;
      debugPrint('PlayerController: divergence ($reason); repair deferred');
      Timer(_resyncCooldown - now.difference(last), () {
        _resyncDeferred = false;
        if (_pushFloor != floor) return; // a push since then re-synced
        _resync(
          '$reason, deferred',
          playingSrc: backend.idAt(backend.currentIndex),
        );
      });
      return;
    }
    _lastResync = now;
    var start = queue.currentIndex;
    var pos = position.value;
    if (playingSrc != null) {
      final found = queue.findSrc(playingSrc, start);
      if (found == null) {
        pos = Duration.zero; // the audible track isn't ours
      } else {
        start = found;
      }
    }
    debugPrint(
      'PlayerController: divergence ($reason); re-pushing ${queue.length} rows at $start',
    );
    if (start != queue.currentIndex) {
      queue.currentIndex = start;
      _syncForCurrent();
    }
    _push(start, autoplay: playing, seekTo: pos, keepListen: true);
  }

  // ── backend → state ──────────────────────────────────────────────────────

  void _onBackendChange() {
    final wasCompleted = processing == Processing.completed;
    playing = backend.playing;
    processing = backend.processing;
    shuffleIndices = backend.shuffleIndices;
    final d = backend.duration;
    if (d != null && d > Duration.zero) duration = d;

    if (processing == Processing.completed && !wasCompleted) {
      // A finite queue ran out (repeat-all never completes: the player loops).
      playing = false;
      if (continuesAtEnd && autoplay != null) {
        autoplay!.onQueueDry();
      } else {
        _completedTerminal = true;
      }
    }

    if (_pending == 0 && !_needsPush) _mirrorIndex();

    final cur = queue.current;
    if (cur != null) art = backend.artFor(cur.src);
    _syncTicker();
    notifyListeners();
  }

  /// `_mirror_native`: adopt the player's row when it is the one it says is
  /// playing; a mismatch is re-checked after a beat before it counts.
  void _mirrorIndex() {
    final idx = backend.currentIndex;
    if (idx == null || queue.isEmpty) return;
    final src = backend.idAt(idx);
    if (src == null) return;
    if (idx >= queue.length || queue.entries[idx].src != src) {
      if (_verifyPending) return;
      _verifyPending = true;
      Timer(_verifyDelay, () {
        _verifyPending = false;
        if (_pending > 0 || _needsPush || queue.isEmpty) return;
        final i = backend.currentIndex;
        final s = backend.idAt(i);
        if (i == null || s == null) return;
        if (i < queue.length && queue.entries[i].src == s) {
          _mirrorIndex();
        } else {
          _resync('playing track is not the mirrored queue row', playingSrc: s);
        }
      });
      return;
    }
    if (idx != queue.currentIndex) {
      queue.currentIndex = idx;
      _syncForCurrent();
      unawaited(_savePosition());
    }
  }

  void _onFailure(PlaybackFailure f) {
    final name = f.title.isEmpty ? 'a track' : '“${f.title}”';
    _messages.add(
      f.action == 'skipped'
          ? "Couldn't play $name. Skipped to the next track."
          : "Playback stopped: couldn't play $name or the tracks after it.",
    );
  }

  /// `_sync_metadata_for_current`: a new current row shows from 0:00 with
  /// its own duration until the decoder reports.
  void _syncForCurrent({bool keepPosition = false}) {
    final cur = queue.current;
    _show(cur);
    _trackChanged(cur?.path);
    if (!keepPosition) position.value = Duration.zero;
    if (cur != null) {
      duration = _secs(cur.duration);
      art = backend.artFor(cur.src);
    }
    _applyDsp();
    notifyListeners();
  }

  // ── per-track sound (EQ preset, Dynamism) ────────────────────────────────

  String? _dspPath;
  int _dspGen = 0;

  /// Flet `audio_engine.reapply_dsp()` from Settings (the Dynamism
  /// switch): the playing track's settings again, from the config as it
  /// now is.
  void reapplyDsp() {
    _dspPath = null;
    _applyDsp();
  }

  /// `reapply_dsp`, run when the playing row changes (Flet: from
  /// `_sync_metadata_for_current`). Here it also follows background
  /// auto-advance, which the Flet session missed while torn down.
  void _applyDsp() {
    final loadCfg = dspConfig;
    final cur = queue.current;
    if (loadCfg == null || cur == null || cur.path == _dspPath) return;
    _dspPath = cur.path;
    final gen = ++_dspGen;
    () async {
      DspSettings out;
      try {
        final cfg = await loadCfg();
        if (!cfg.dynamismEnabled) {
          out = computeDsp(cfg, null);
        } else {
          // The row's own features first (as Flet reads the track dict),
          // then the database.
          final own = _rowFeatures(cur);
          final f = hasDspFeatures(own)
              ? own
              : await dspFeatures?.call(cur.path);
          out = computeDsp(cfg, f);
        }
      } catch (e) {
        debugPrint('PlayerController: DSP failed: $e');
        out = kNeutralDsp;
      }
      if (gen != _dspGen || _disposed) return;
      loudnessBoost.value = out.boostDb;
      try {
        await backend.setLoudnessBoost(out.boostDb);
        for (var i = 0; i < out.bands.length; i++) {
          await backend.setEqBandGain(i, out.bands[i]);
        }
      } catch (e) {
        debugPrint('PlayerController: applying DSP failed: $e');
      }
    }();
  }

  String? _changedPath;

  /// `autoplay.on_track_changed` when the current path moves, run once the
  /// edit that moved it has finished (Flet's engine observer).
  void _trackChanged(String? path) {
    if (path == null || path == _changedPath) return;
    _changedPath = path;
    if (autoplay != null) {
      scheduleMicrotask(() => autoplay?.onTrackChanged(path));
    }
  }

  static DspFeatures? _rowFeatures(QueueEntry row) {
    double? n(Object? v) => v is num ? v.toDouble() : null;
    final d = row.data;
    return (
      energy: n(d['energy']),
      beatStrength: n(d['beat_strength']),
      spectralContrast: n(d['spectral_contrast']),
    );
  }

  void _show(QueueEntry? row) {
    if (row != null) {
      _lastShown = row;
      stopped = false;
    }
  }

  void _syncTicker() {
    final want = playing && _foreground;
    if (want && _ticker == null) {
      position.value = backend.position;
      _ticker = Timer.periodic(const Duration(seconds: 1), (_) {
        position.value = backend.position;
      });
    } else if (!want && _ticker != null) {
      _ticker!.cancel();
      _ticker = null;
      if (!_needsPush) position.value = backend.position;
    }
  }

  // ── persistence ──────────────────────────────────────────────────────────

  Timer? _queueSave;
  Timer? _posLoop;

  /// on_queue_mutated → `_schedule_queue_save`: coalesced over 400 ms.
  void _queueMutated() {
    _queueSave?.cancel();
    _queueSave = Timer(const Duration(milliseconds: 400), () {
      unawaited(saveQueue());
    });
  }

  Future<void> saveQueue() => store.saveQueueState(
    queue: List.of(queue.entries),
    index: queue.currentIndex,
    position: _s(position.value),
    duration: _s(duration),
    autoplayHeld: autoplay != null ? autoplay!.heldJson : _autoplayHeld,
  );

  Future<void> _savePosition() => store.savePosition(
    queue: List.of(queue.entries),
    index: queue.currentIndex,
    position: _s(position.value),
    duration: _s(duration),
  );

  Future<void> _savePartition(String name) => store.savePartition(
    name,
    queue: List.of(queue.entries),
    index: queue.currentIndex,
    position: _s(position.value),
    duration: _s(duration),
  );

  /// `_position_save_loop`: every 10 s of play, when the track or the
  /// position (by 5 s+) moved. A ~150-byte write; the queue isn't rewritten.
  void _positionSaveLoop() {
    String? lastPath;
    var lastPos = -10.0;
    _posLoop = Timer.periodic(const Duration(seconds: 10), (_) {
      if (queue.isEmpty || !playing) return;
      final pos = _s(backend.position);
      final path = queue.current?.path;
      if (path == lastPath && (pos - lastPos).abs() < 5.0) return;
      lastPath = path;
      lastPos = pos;
      position.value = backend.position;
      unawaited(_savePosition());
    });
  }

  static Duration _secs(double s) =>
      Duration(microseconds: (s * Duration.microsecondsPerSecond).round());
  static double _s(Duration d) =>
      d.inMicroseconds / Duration.microsecondsPerSecond;

  /// Completes when every queued backend op has run.
  Future<void> get settled async {
    while (true) {
      final c = _chain;
      await c;
      if (identical(c, _chain)) return;
    }
  }

  /// Settles the ops, then writes the queue and waits for every file write.
  Future<void> flush() async {
    await settled;
    _queueSave?.cancel();
    await saveQueue();
    await prefs.flush();
    await store.flush();
  }

  bool _disposed = false;

  @override
  void notifyListeners() {
    if (!_disposed) super.notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    backend.remoteControls = null;
    _changesSub?.cancel();
    _failSub?.cancel();
    _listenSub?.cancel();
    _ticker?.cancel();
    _queueSave?.cancel();
    _posLoop?.cancel();
    position.dispose();
    loudnessBoost.dispose();
    _messages.close();
    super.dispose();
  }
}
