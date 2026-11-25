import 'dart:async';
import 'dart:collection';

import 'package:flutter/foundation.dart';

import '../data/library_repository.dart';
import '../stations/station.dart';
import 'player_controller.dart';
import 'queue_entry.dart';

/// Auto-play: after the songs you queued yourself, similar songs from your
/// library keep playing. A port of `utils/autoplay.py`; its module docstring
/// describes the model, which is unchanged:
///
///     [playing] [your picks: _queued] [auto-play: _autoplay] — nothing else
///
/// The queue auto-play took over from is set aside in [held] and put back
/// after the playing song when auto-play is turned off. The station opens
/// from one track (the anchor), stays inside its genre umbrella, and starts
/// the genre over when it is played out.
///
/// Differences from Flet (PORT_MAP §7): listens arrive straight from the
/// audio handler instead of through the ledger drain, and there is one
/// isolate, so a fill can't be stranded on a torn-down UI session.
class AutoPlay extends ChangeNotifier {
  AutoPlay({required this.player, required this.library});

  final PlayerController player;
  final AutoPlayLibrary library;

  static const bufferTarget = 4;
  static const bufferLow = 3;
  static const backgroundTarget = 24;
  static const historyLen = 150;
  static const fillStale = Duration(seconds: 60);
  static const restartGap = 5;
  static const artistCap = 5;
  static const albumCap = 1;
  static const capWindow = 24;
  static const negativesToReplan = 2;

  bool _enabled = false;

  /// The track the station started from.
  String anchor = '';
  StationHandle? station;

  /// The queue auto-play set aside.
  List<QueueEntry> held = [];
  final Queue<String> history = Queue();

  /// Recommendations the listener removed this session, oldest first. Never
  /// offered again, even by a new station (Flet's station forgot them when
  /// it reopened, so a removed song came back).
  final LinkedHashSet<String> removed = LinkedHashSet();
  List<String> _negatives = [];
  int _taken = 0;
  bool _startedOver = false;
  bool _quiet = false;
  String _dead = '';
  int _gen = 0;
  bool _filling = false;
  final Stopwatch _fillClock = Stopwatch();

  // ── mode ────────────────────────────────────────────────────────────────

  bool get enabled => _enabled;

  /// The engine asks auto-play for more at the end of the queue only while on.
  set enabled(bool on) {
    _enabled = on;
    player.continuesAtEnd = on;
    notifyListeners();
  }

  /// main.py `set_play_similar_mode`: the chain button, the sheet's switch,
  /// Start radio. Persisted as `play_similar_mode`; turns shuffle off, since
  /// shuffle order would scatter the station.
  void setMode(bool on) {
    if (on == _enabled) return;
    unawaited(player.prefs.set('play_similar_mode', on));
    if (on && player.shuffle) player.disableShuffleOrder();
    setEnabled(on, player.currentPath);
  }

  void setEnabled(bool on, [String seed = '']) {
    if (on == enabled) return;
    enabled = on;
    if (on) {
      start(seed.isNotEmpty ? seed : player.currentPath);
    } else {
      _reset('');
      _quiet = false;
      _dead = '';
      dropBuffer();
      _restoreHeld();
    }
  }

  /// A new station from [path] (playing now): set the rest of the queue
  /// aside and replace the pending buffer.
  void start(String path) {
    if (!enabled || path.isEmpty) return;
    debugPrint('AutoPlay: new station from $path');
    _reset(path);
    _setAside();
    _scheduleFill(bufferTarget, replace: true);
  }

  /// A session started on [path] (restore): give an enabled station its
  /// anchor back and top the buffer up.
  void resume(String path) {
    if (!enabled || path.isEmpty) return;
    if (_foreignRows().isNotEmpty) {
      start(path);
      return;
    }
    if (anchor.isEmpty) anchor = path;
    ensureBuffer();
  }

  void _reset(String newAnchor) {
    _gen++;
    _filling = false;
    anchor = newAnchor;
    station = null;
    _negatives = [];
    _taken = 0;
    _startedOver = false;
    notifyListeners();
  }

  /// The genre the station is playing now, for display.
  String? get currentGenre => station?.flavour;

  /// Main.py `start_radio` (long-press → Start radio): play [t] now and
  /// restart auto-play from it, turning auto-play on if needed.
  void startRadio(Track t) {
    final path = t.path;
    if (path.isEmpty) return;
    if (player.currentPath != path) {
      var idx = player.queue.entries.indexWhere((r) => r.path == path);
      if (idx == -1) {
        player.insertAfter([QueueEntry.fromTrack(t)]);
        idx = player.queue.length > 1 ? player.queue.currentIndex + 1 : 0;
      }
      player.playTrackAt(idx);
    }
    if (enabled) {
      start(path);
    } else {
      setMode(true);
    }
  }

  // ── events from the player ──────────────────────────────────────────────

  /// The playing track changed (skip, auto-advance, a tap, a new queue).
  void onTrackChanged(String path) {
    if (path.isNotEmpty) {
      history.addLast(path);
      _trimHistory();
      station?.exclude([path]);
    }
    if (!enabled || path.isEmpty) return;
    final row = player.queue.current;
    if (row?.path != path || path == _dead) {
      ensureBuffer();
      return;
    }
    final carriesOn = path == anchor || row!.autoplay || row.queued;
    if (!carriesOn || _foreignRows().isNotEmpty) {
      start(path);
      return;
    }
    ensureBuffer();
  }

  /// The user explicitly chose [path]; the player is already on it. A
  /// pending recommendation just plays; anything else starts a new station.
  void onUserChose(String path) {
    if (!enabled || path.isEmpty) return;
    final row = player.queue.current;
    if (station != null && row != null && row.autoplay && row.path == path) {
      ensureBuffer();
      return;
    }
    start(path);
  }

  /// One finished listen. A track listened through pulls the station toward
  /// its artist; an early skip of a recommendation is a negative.
  void onListen(String path, double listenedS, double durationS) {
    if (path.isEmpty) return;
    _remember(path);
    if (isListen(listenedS, durationS)) {
      station?.accept(path);
      return;
    }
    if (enabled && _isAutoplay(path)) _negative(path);
  }

  /// The listener removed a recommendation (call AFTER the row is gone):
  /// steer away from it and top up behind the rows still showing. Unlike an
  /// early skip it never replans: replacing the list the listener is editing
  /// swapped the rows under their finger (Flet counted it as a negative).
  void reject(String path) {
    if (path.isEmpty) return;
    removed
      ..remove(path)
      ..add(path);
    while (removed.length > historyLen) {
      removed.remove(removed.first);
    }
    station?.reject([path]);
    if (!enabled) return;
    final have = _pendingRows().length;
    if (!_fillBusy() && have < bufferTarget) {
      _scheduleFill(bufferTarget - have);
    }
  }

  /// The queue played out while enabled: append a block and play it.
  void onQueueDry() {
    if (!enabled || _fillBusy()) return;
    _scheduleFill(bufferTarget, appendAndPlay: true);
  }

  /// Top the buffer up before the app goes to the background.
  void prefillForBackground() => ensureBuffer(backgroundTarget);

  // ── history ─────────────────────────────────────────────────────────────

  /// Heard (possibly while the UI was away): still history.
  void _remember(String path) {
    if (history.contains(path)) return;
    history.addLast(path);
    _trimHistory();
  }

  void _trimHistory() {
    while (history.length > historyLen) {
      history.removeFirst();
    }
  }

  /// On a cold start recent plays stand in for history, oldest first.
  Future<void> loadHistory() async {
    if (history.isNotEmpty) return;
    try {
      final recent = await library.recentPaths(historyLen);
      for (final p in recent.reversed) {
        _remember(p);
      }
    } catch (e) {
      debugPrint('AutoPlay: could not load recent plays: $e');
    }
  }

  // ── queue ───────────────────────────────────────────────────────────────

  List<int> _foreignRows() {
    final q = player.queue.entries;
    final start = player.queue.upNextEnd();
    return [
      for (var i = start; i < q.length; i++)
        if (!q[i].autoplay) i,
    ];
  }

  void _setAside() {
    final rows = _foreignRows();
    if (rows.isEmpty) return;
    final q = player.queue.entries;
    held = [for (final i in rows) q[i]];
    player.removeIndices(rows);
  }

  void _restoreHeld() {
    final rows = held;
    held = [];
    if (rows.isEmpty || player.queue.isEmpty) return;
    final at = player.queue.upNextEnd();
    player.insertAfter(rows, afterIndex: at - 1);
  }

  List<int> _pendingRows() {
    final q = player.queue.entries;
    return [
      for (var i = player.queue.currentIndex + 1; i < q.length; i++)
        if (q[i].autoplay) i,
    ];
  }

  /// The pending auto-play tracks ahead of the playing row, in order.
  List<String> bufferPaths() {
    final q = player.queue.entries;
    return [
      for (final i in _pendingRows())
        if (q[i].path.isNotEmpty) q[i].path,
    ];
  }

  /// Remove every pending auto-play track (never the playing one).
  int dropBuffer() {
    final victims = _pendingRows();
    if (victims.isNotEmpty) player.removeIndices(victims);
    return victims.length;
  }

  void ensureBuffer([int target = bufferTarget]) {
    if (!enabled || _fillBusy()) return;
    final have = _pendingRows().length;
    final low = target == bufferTarget ? bufferLow : target;
    if (have < low) _scheduleFill(target - have);
  }

  // ── internals ───────────────────────────────────────────────────────────

  bool _negative(String path) {
    _negatives.add(path);
    station?.reject([path]);
    if (_negatives.length >= negativesToReplan) {
      _negatives = [];
      if (station != null) {
        debugPrint('AutoPlay: $negativesToReplan early skips; new buffer');
        _gen++;
        _filling = false;
        _scheduleFill(bufferTarget, replace: true);
      }
      return true;
    }
    return false;
  }

  bool _isAutoplay(String path) =>
      player.queue.entries.any((t) => t.autoplay && t.path == path);

  Set<String> _avoid([int? recent]) {
    final avoid = {...bufferPaths()};
    final hist = history.toList();
    avoid.addAll(
      recent == null || recent >= hist.length
          ? hist
          : hist.sublist(hist.length - recent),
    );
    final cur = player.currentPath;
    if (cur.isNotEmpty) avoid.add(cur);
    return avoid..addAll(removed);
  }

  int _insertAfter() {
    final rows = _pendingRows();
    if (rows.isNotEmpty) return rows.last;
    return player.queue.upNextEnd() - 1;
  }

  bool _fillBusy() {
    if (_filling && _fillClock.elapsed > fillStale) {
      debugPrint('AutoPlay: a fill never finished; clearing it');
      _filling = false;
    }
    return _filling;
  }

  void _scheduleFill(
    int n, {
    bool replace = false,
    bool appendAndPlay = false,
  }) {
    if (n <= 0 || (_fillBusy() && !replace)) return;
    _filling = true;
    _fillClock
      ..reset()
      ..start();
    unawaited(_fill(n, _gen, replace: replace, appendAndPlay: appendAndPlay));
  }

  Future<StationHandle?> _open([int? recent]) async {
    final seed = anchor.isNotEmpty ? anchor : player.currentPath;
    if (seed.isEmpty) return null;
    anchor = seed;
    return library.openStation(
      seed,
      avoid: _avoid(recent),
      focused: true,
      maxPerArtist: artistCap,
      maxPerAlbum: albumCap,
      window: capWindow,
    );
  }

  /// Up to [n] paths from the station, opening it on first use and starting
  /// the genre over when it is played out. (paths, justOpened).
  Future<(List<String>, bool)> _take(int n, int gen) async {
    final opened = station == null;
    if (opened) {
      final s = await _open();
      if (s == null || gen != _gen) return (const <String>[], opened);
      station = s;
    }
    final paths = station!.take(n);
    _taken += paths.length;
    if (paths.length < n && _taken > 0) {
      final genre = station!.flavour ?? 'similar songs';
      final s = await _open(restartGap);
      if (s == null || gen != _gen) return (paths, opened);
      s.exclude(paths);
      final more = [
        for (final p in s.take(n - paths.length))
          if (!paths.contains(p)) p,
      ];
      station = s;
      _taken = more.length;
      if (more.isNotEmpty && !_startedOver) {
        _startedOver = true;
        player.say('Played all the $genre in your library. Starting over.');
      }
      paths.addAll(more);
    }
    return (paths, opened);
  }

  Future<void> _fill(
    int n,
    int gen, {
    bool replace = false,
    bool appendAndPlay = false,
  }) async {
    try {
      final (taken, opened) = await _take(n, gen);
      if (gen != _gen || !enabled) return;
      final paths = [
        for (final p in taken)
          if (!removed.contains(p)) p,
      ];
      final batch = await _tracks(paths);
      if (gen != _gen || !enabled) return;
      if (batch.isEmpty && opened) {
        await _cannotStart();
        return;
      }
      if (batch.isNotEmpty) {
        _quiet = false;
        _dead = '';
      }
      if (appendAndPlay) {
        if (batch.isEmpty) {
          player.stop();
          return;
        }
        final firstNew = player.queue.length;
        player.extend(batch);
        player.playTrackAt(firstNew);
      } else if (replace) {
        final victims = _pendingRows();
        if (victims.isNotEmpty) {
          player.replaceRows(victims, batch);
        } else if (batch.isNotEmpty) {
          player.insertAfter(batch, afterIndex: _insertAfter());
        }
      } else if (batch.isNotEmpty) {
        player.insertAfter(batch, afterIndex: _insertAfter());
      }
    } catch (e, st) {
      debugPrint('AutoPlay: fill failed: $e\n$st');
    } finally {
      if (gen == _gen) _filling = false;
      notifyListeners();
    }
  }

  /// No station from the anchor: drop the old buffer and give back the
  /// set-aside queue. The next song that plays from it is a fresh start.
  Future<void> _cannotStart() async {
    _dead = anchor;
    dropBuffer();
    _restoreHeld();
    if (_quiet) return;
    _quiet = true;
    bool analysed;
    try {
      analysed = await library.hasCurrentFeatures(anchor);
    } catch (_) {
      analysed = true;
    }
    player.say(
      analysed
          ? 'No similar songs found for this one. Your queue continues.'
          : "This song isn't analysed yet, so auto-play can't start from it. "
                'Your queue continues.',
    );
  }

  /// Queue rows for [paths], tagged as recommendations.
  Future<List<QueueEntry>> _tracks(List<String> paths) async {
    if (paths.isEmpty) return const [];
    final rows = await library.tracksBrief(paths);
    return [
      for (final p in paths)
        if (rows[p] case final t?)
          QueueEntry({
            'path': p,
            'track_title': t.title,
            'artist_name': t.artist,
            'album_title': t.album,
            'duration': t.duration,
            kAutoplay: true,
          }),
    ];
  }

  /// queue_state.json `autoplay_held`.
  List<Map<String, Object?>>? get heldJson =>
      held.isEmpty ? null : [for (final r in held) r.toJson()];

  void restoreHeldJson(Object? raw) {
    held = [
      if (raw is List)
        for (final r in raw)
          if (r is Map) QueueEntry(r.cast<String, Object?>()),
    ];
  }
}

/// `play_ledger.is_listen`: a listen counts after 30 s, or half of a short
/// track. Anything less is an early skip.
bool isListen(double listenedS, double durationS, {double minS = 30.0}) =>
    listenedS >= minS || (durationS > 0.0 && listenedS >= 0.5 * durationS);

/// What auto-play reads from the library.
abstract interface class AutoPlayLibrary implements Listenable {
  /// Some track has coordinates, so a station can start somewhere.
  bool get stationsAvailable;

  /// `track_graph.open_station`; null when the library has no station
  /// graphs at all. A seed without coordinates gives an empty station.
  Future<StationHandle?> openStation(
    String seed, {
    required Set<String> avoid,
    required bool focused,
    required int maxPerArtist,
    required int maxPerAlbum,
    required int window,
  });
  Future<Map<String, Track>> tracksBrief(List<String> paths);
  Future<bool> hasCurrentFeatures(String path);
  Future<List<String>> recentPaths(int limit);
}
