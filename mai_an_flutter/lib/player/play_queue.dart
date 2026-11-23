import 'dart:collection';

import 'queue_entry.dart';

/// One step of a native splice: remove [deleteCount] rows at [start], then
/// insert [items] there. Edits apply in order, each against the playlist as
/// the previous one left it (the Flet `splice` op).
class SpliceEdit {
  const SpliceEdit(this.start, this.deleteCount, this.items);
  final int start;
  final int deleteCount;
  final List<QueueEntry> items;

  @override
  String toString() => 'SpliceEdit($start, $deleteCount, ${items.length})';
}

/// What the native playlist must do to match a queue edit.
sealed class QueueChange {
  const QueueChange();
}

/// Nothing to send.
final class NoChange extends QueueChange {
  const NoChange();
}

/// Replace the whole native playlist and start at [start] (set_playlist).
final class Reload extends QueueChange {
  const Reload(this.start);
  final int start;
}

final class Splice extends QueueChange {
  const Splice(this.edits);
  final List<SpliceEdit> edits;
}

final class Move extends QueueChange {
  const Move(this.from, this.to);
  final int from;
  final int to;
}

/// The queue became empty: stop the player (engine.remove_from_queue).
final class Emptied extends QueueChange {
  const Emptied();
}

/// The play queue: the Python side of utils/audio_engine.py (queue list,
/// current_index, every mutation) and utils/queue_layout.py, without the
/// Flet bridge. Pure: each mutation edits the list synchronously and returns
/// the [QueueChange] the native playlist needs, which the player controller
/// applies in order. Tests check both against the Flet behaviour.
///
/// Layout after the playing row (queue_layout.py):
///
///     [playing] [up next: _queued rows] [auto-play: _autoplay rows] [rest]
class PlayQueue {
  final List<QueueEntry> _rows = [];
  int currentIndex = 0;

  /// Only changes where Add to Queue lands (random order anyway: append).
  bool shuffle = false;

  UnmodifiableListView<QueueEntry> get entries => UnmodifiableListView(_rows);
  int get length => _rows.length;
  bool get isEmpty => _rows.isEmpty;
  bool get isNotEmpty => _rows.isNotEmpty;

  QueueEntry? get current =>
      (currentIndex >= 0 && currentIndex < _rows.length)
      ? _rows[currentIndex]
      : null;

  /// Row index of [row] by identity, or null once it has left the queue.
  int? indexOfRow(QueueEntry row) {
    final i = _rows.indexWhere((t) => identical(t, row));
    return i < 0 ? null : i;
  }

  /// `AudioEngine._playable`: drop rows with no path, remapping [index] onto
  /// the kept list.
  static (List<QueueEntry>, int) playable(List<QueueEntry> tracks, int index) {
    if (tracks.every((t) => t.path.isNotEmpty)) return (tracks, index);
    final kept = [for (final t in tracks) if (t.path.isNotEmpty) t];
    final upTo = index.clamp(0, tracks.length);
    final newIndex = tracks.take(upTo).where((t) => t.path.isNotEmpty).length;
    return (kept, kept.isEmpty ? 0 : newIndex.clamp(0, kept.length - 1));
  }

  /// `queue_layout.up_next_end`.
  int upNextEnd() {
    var i = currentIndex + 1 < 0 ? 0 : currentIndex + 1;
    while (i < _rows.length && _rows[i].queued) {
      i++;
    }
    return i < _rows.length ? i : _rows.length;
  }

  /// `set_queue` / `restore_queue`.
  QueueChange set(List<QueueEntry> tracks, [int start = 0]) {
    final (kept, idx) = playable(tracks, start);
    _rows
      ..clear()
      ..addAll(kept);
    currentIndex = _rows.isEmpty ? 0 : idx.clamp(0, _rows.length - 1);
    return _rows.isEmpty ? const Emptied() : Reload(currentIndex);
  }

  /// Play Next: first in the up-next block.
  QueueChange queueNext(QueueEntry track) {
    if (track.path.isEmpty) return const NoChange();
    final row = track.asQueued();
    if (_rows.isEmpty) return set([row]);
    final at = (currentIndex + 1).clamp(0, _rows.length);
    _rows.insert(at, row);
    return Splice([
      SpliceEdit(at, 0, [row]),
    ]);
  }

  /// Add to Queue: last in the up-next block (ahead of auto-play and the rest
  /// of the queue). Under shuffle it appends.
  QueueChange queueLast(QueueEntry track) => queueLastMany([track]);

  QueueChange queueLastMany(List<QueueEntry> tracks) {
    final rows = [
      for (final t in playable(tracks, 0).$1) t.asQueued(),
    ];
    if (rows.isEmpty) return const NoChange();
    if (_rows.isEmpty) return set(rows);
    final at = shuffle ? _rows.length : upNextEnd();
    _rows.insertAll(at, rows);
    return Splice([SpliceEdit(at, 0, rows)]);
  }

  /// Append as one mutation.
  QueueChange extend(List<QueueEntry> tracks) {
    final rows = playable(tracks, 0).$1;
    if (rows.isEmpty) return const NoChange();
    if (_rows.isEmpty) return set(rows);
    final start = _rows.length;
    _rows.addAll(rows);
    return Splice([SpliceEdit(start, 0, List.of(rows))]);
  }

  /// A block right after the playing row (or after [afterIndex]), without
  /// touching what plays. Order kept; current index unchanged.
  QueueChange insertAfterCurrent(List<QueueEntry> tracks, {int? afterIndex}) {
    final rows = playable(tracks, 0).$1;
    if (rows.isEmpty) return const NoChange();
    if (_rows.isEmpty) return set(rows);
    final base = afterIndex ?? currentIndex;
    final start = (base + 1).clamp(0, _rows.length);
    _rows.insertAll(start, rows);
    return Splice([SpliceEdit(start, 0, List.of(rows))]);
  }

  /// `remove_from_queue`. Removing the playing row leaves the index on the
  /// row that slid into its place; the native player advances the same way.
  QueueChange removeAt(int index) {
    if (index < 0 || index >= _rows.length) return const NoChange();
    final removedActive = index == currentIndex;
    _rows.removeAt(index);
    if (_rows.isEmpty) {
      currentIndex = 0;
      return const Emptied();
    }
    if (index < currentIndex) {
      currentIndex -= 1;
    } else if (removedActive) {
      currentIndex = currentIndex.clamp(0, _rows.length - 1);
    }
    return Splice([SpliceEdit(index, 1, const [])]);
  }

  /// `_drop_rows`: pop [indices] (never the playing row), keeping the index on
  /// the playing track. Edits are contiguous runs, highest first.
  (List<SpliceEdit>, int?) _dropRows(Iterable<int> indices) {
    final targets =
        {
          for (final i in indices)
            if (i >= 0 && i < _rows.length && i != currentIndex) i,
        }.toList()
          ..sort((a, b) => b.compareTo(a));
    final edits = <SpliceEdit>[];
    for (final i in targets) {
      if (edits.isNotEmpty && edits.last.start == i + 1) {
        edits.last = SpliceEdit(i, edits.last.deleteCount + 1, const []);
      } else {
        edits.add(SpliceEdit(i, 1, const []));
      }
    }
    for (final i in targets) {
      _rows.removeAt(i);
    }
    final shift = targets.where((i) => i < currentIndex).length;
    if (shift > 0) currentIndex = (currentIndex - shift).clamp(0, 1 << 30);
    return (edits, targets.isEmpty ? null : targets.last);
  }

  /// Several rows as one mutation; the playing row is never removed.
  QueueChange removeIndices(Iterable<int> indices) {
    final (edits, _) = _dropRows(indices);
    if (edits.isEmpty) return const NoChange();
    if (_rows.isEmpty) return const Emptied();
    return Splice(edits);
  }

  /// Swap the rows at [indices] (never the playing one) for [tracks] as one
  /// mutation. The block lands where the first removed row was, or right
  /// after the playing row when nothing is removed.
  QueueChange replaceRows(Iterable<int> indices, List<QueueEntry> tracks) {
    final rows = playable(tracks, 0).$1;
    if (_rows.isEmpty) return rows.isEmpty ? const NoChange() : set(rows);
    final (edits, first) = _dropRows(indices);
    if (rows.isNotEmpty) {
      final at = (first ?? currentIndex + 1).clamp(
        currentIndex + 1,
        _rows.length,
      );
      _rows.insertAll(at, rows);
      edits.add(SpliceEdit(at, 0, List.of(rows)));
    }
    return edits.isEmpty ? const NoChange() : Splice(edits);
  }

  /// The current index follows the playing row by position arithmetic, never
  /// by value (a duplicate of the playing track must not steal it).
  QueueChange move(int from, int to) {
    if (from < 0 || from >= _rows.length || to < 0 || to >= _rows.length) {
      return const NoChange();
    }
    if (from == to) return const NoChange();
    _rows.insert(to, _rows.removeAt(from));
    final cur = currentIndex;
    if (from == cur) {
      currentIndex = to;
    } else if (from < cur && cur <= to) {
      currentIndex = cur - 1;
    } else if (to <= cur && cur < from) {
      currentIndex = cur + 1;
    }
    return Move(from, to);
  }

  QueueChange clear() {
    _rows.clear();
    currentIndex = 0;
    return const Emptied();
  }

  /// `_logical_neighbour`: the row [delta] steps from the current one in PLAY
  /// order: [shuffleOrder] (play position → row) while shuffling, sequential
  /// otherwise. Null at either end.
  int? neighbour(int delta, List<int> shuffleOrder) {
    if (_rows.isEmpty) return null;
    if (shuffle && shuffleOrder.isNotEmpty && shuffleOrder.length == _rows.length) {
      final pos = shuffleOrder.indexOf(currentIndex);
      if (pos < 0) return null;
      final n = pos + delta;
      return (n >= 0 && n < shuffleOrder.length) ? shuffleOrder[n] : null;
    }
    final n = currentIndex + delta;
    return (n >= 0 && n < _rows.length) ? n : null;
  }

  /// `_find_src`: row of [src], searching outward from [near] so the
  /// duplicate closest to where we think we are wins.
  int? findSrc(String src, int near) {
    final n = _rows.length;
    for (var d = 0; d < n; d++) {
      for (final i in d == 0 ? [near] : [near + d, near - d]) {
        if (i >= 0 && i < n && _rows[i].src == src) return i;
      }
    }
    return null;
  }

  /// Rows from the playing one on, in play order (the queue sheet's list).
  List<(int, QueueEntry)> upcoming(List<int> shuffleOrder) {
    if (shuffle && shuffleOrder.isNotEmpty && shuffleOrder.length == _rows.length) {
      final pos = shuffleOrder.indexOf(currentIndex);
      if (pos >= 0) {
        return [for (final i in shuffleOrder.skip(pos)) (i, _rows[i])];
      }
    }
    return [
      for (var i = currentIndex; i < _rows.length; i++)
        if (i >= 0) (i, _rows[i]),
    ];
  }
}
