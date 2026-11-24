import 'package:flutter/foundation.dart';

import '../core/app_config.dart';
import '../data/library_repository.dart';
import '../library/library_controller.dart';

/// search.py's landing page data (`_refresh_landing_page`): the Most
/// Listened cards and Library at a Glance, each from its own query; a query
/// that fails leaves its card out. New From Your Artists comes with phase 6.
///
/// Refreshed as `refresh_setup_state` does on every show of the tab: when
/// nothing is built yet, or the cards are over [ttl] old (play counts and
/// totals change as the user listens and scans).
class LandingController extends ChangeNotifier {
  LandingController(
    this.library, {
    this.sections = const LandingSections(),
    DateTime Function()? clock,
  }) : _clock = clock ?? DateTime.now {
    library.addListener(_onLibrary);
  }

  final LibraryController library;
  LandingSections sections;

  /// Settings → Appearance's landing switches: the cards again
  /// (`refresh_setup_state`).
  void setSections(LandingSections s) {
    if (s == sections) return;
    sections = s;
    refresh();
  }

  final DateTime Function() _clock;

  /// search.py `_LANDING_TTL_S`.
  static const ttl = Duration(seconds: 60);

  /// Rows per card (`limit=5`).
  static const limit = 5;

  List<MostPlayedTrack> mostPlayed = const [];
  List<MostPlayedGroup> topArtists = const [];
  List<MostPlayedGroup> topGenres = const [];
  LibraryCounts? counts;

  DateTime? _builtAt;
  bool _refreshing = false;
  bool _showPending = false;

  /// Some card has something to show (Flet: `_landing_container.controls`).
  bool get hasCards =>
      mostPlayed.isNotEmpty ||
      topArtists.isNotEmpty ||
      topGenres.isNotEmpty ||
      counts != null;

  /// The Search tab was shown.
  void onShow() {
    final builtAt = _builtAt;
    if (!hasCards || builtAt == null || _clock().difference(builtAt) > ttl) {
      refresh();
    }
  }

  Future<void> refresh() async {
    final repo = library.repository;
    if (repo == null) {
      // The library is still opening: build once it is.
      _showPending = true;
      return;
    }
    if (_refreshing) return;
    _refreshing = true;
    try {
      Future<T?> guard<T>(bool on, Future<T> Function() query) async {
        if (!on) return null;
        try {
          return await query();
        } catch (e) {
          debugPrint('Landing card query failed: $e');
          return null;
        }
      }

      final (most, artists, genres, totals) = await (
        guard(sections.mostListened, () => repo.mostPlayed(limit)),
        guard(sections.topArtists, () => repo.mostPlayedArtists(limit)),
        guard(sections.topGenres, () => repo.mostPlayedGenres(limit)),
        guard(sections.stats, repo.libraryCounts),
      ).wait;
      mostPlayed = most ?? const [];
      topArtists = artists ?? const [];
      topGenres = genres ?? const [];
      counts = totals;
      _builtAt = _clock();
      notifyListeners();
    } finally {
      _refreshing = false;
    }
  }

  void _onLibrary() {
    if (_showPending && library.repository != null) {
      _showPending = false;
      refresh();
    }
  }

  @override
  void dispose() {
    library.removeListener(_onLibrary);
    super.dispose();
  }
}
