import 'package:flutter/foundation.dart';

import '../data/library_repository.dart';

/// library.py's multi-select state (`_selection_mode`,
/// `_selected_tracks_meta`): opened from a track's long-press menu with
/// that track ticked, shared by every view. Tracks keep the order they were
/// picked in, which is the order Play and Add to Queue use.
class SelectionController extends ChangeNotifier {
  bool active = false;
  final Map<String, Track> _picked = {};

  int get count => _picked.length;
  bool contains(String path) => _picked.containsKey(path);

  /// The selection, in the order it was picked.
  List<Track> get tracks => [
    for (final t in _picked.values)
      if (t.path.isNotEmpty) t,
  ];

  /// `_enable_selection_mode`.
  void enable([Track? first]) {
    active = true;
    _picked.clear();
    if (first != null && first.path.isNotEmpty) _picked[first.path] = first;
    notifyListeners();
  }

  /// `_exit_selection_mode`.
  void exit() {
    if (!active && _picked.isEmpty) return;
    active = false;
    _picked.clear();
    notifyListeners();
  }

  /// `_toggle_track_selection`.
  void toggle(Track t) {
    if (t.path.isEmpty) return;
    if (_picked.remove(t.path) == null) _picked[t.path] = t;
    notifyListeners();
  }
}
