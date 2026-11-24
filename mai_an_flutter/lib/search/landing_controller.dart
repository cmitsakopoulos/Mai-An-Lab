import 'package:flutter/foundation.dart';

import '../core/app_config.dart';
import '../data/library_repository.dart';
import '../library/library_controller.dart';
import 'new_releases.dart';

/// search.py's landing page data (`_refresh_landing_page`): New From Your
/// Artists, Most Listened cards, and Library at a Glance, each from its own
/// query; a query that fails leaves its card out.
///
/// Refreshed as `refresh_setup_state` does on every show of the tab: when
/// nothing is built yet, marked stale, or the cards are over [ttl] old (play
/// counts and totals change as the user listens and scans).
class LandingController extends ChangeNotifier {
  LandingController(
    this.library, {
    this.sections = const LandingSections(),
    this.releasesScheduler,
    DateTime Function()? clock,
  }) : _clock = clock ?? DateTime.now {
    library.addListener(_onLibrary);
    releasesScheduler?.addListener(_onScheduler);
    if (releasesScheduler case final s?) {
      s.onRefreshed = _onNewReleasesRefreshed;
    }
  }

  final LibraryController library;
  LandingSections sections;
  final NewReleasesScheduler? releasesScheduler;

  /// Settings → Appearance's landing switches: the cards again
  /// (`refresh_setup_state`).
  void setSections(LandingSections s) {
    if (s == sections) return;
    final turnedOnReleases = !sections.newReleases && s.newReleases;
    sections = s;
    if (turnedOnReleases) {
      releasesScheduler?.ensureRefresh();
    }
    markStale();
    refresh();
  }

  final DateTime Function() _clock;

  /// search.py `_LANDING_TTL_S`.
  static const ttl = Duration(seconds: 60);

  /// Rows per card (`limit=5`).
  static const limit = 5;

  /// Releases fetched for the New From Your Artists shelf (`limit=30`).
  static const releasesLimit = 30;

  List<ReleaseItem> newReleases = const [];
  ReleaseSyncEntry? latestReleaseSync;
  List<MostPlayedTrack> mostPlayed = const [];
  List<MostPlayedGroup> topArtists = const [];
  List<MostPlayedGroup> topGenres = const [];
  LibraryCounts? counts;

  DateTime? _builtAt;
  bool _stale = true;
  bool _refreshing = false;
  bool _rerunRequested = false;
  bool _showPending = false;

  bool get isRefreshingReleases => releasesScheduler?.isRefreshing ?? false;

  /// Some card has something to show (Flet: `_landing_container.controls`).
  bool get hasCards =>
      (sections.newReleases &&
          (newReleases.isNotEmpty || isRefreshingReleases)) ||
      mostPlayed.isNotEmpty ||
      topArtists.isNotEmpty ||
      topGenres.isNotEmpty ||
      counts != null;

  void markStale() {
    _stale = true;
  }

  /// The Search tab was shown.
  void onShow() {
    if (sections.newReleases) {
      releasesScheduler?.maybeRecheck();
    }
    final builtAt = _builtAt;
    if (!hasCards ||
        _stale ||
        builtAt == null ||
        _clock().difference(builtAt) > ttl) {
      refresh();
    }
  }

  /// `search.py` `_on_manual_releases_refresh`.
  void triggerManualReleasesRefresh() {
    if (isRefreshingReleases) return;
    releasesScheduler?.triggerManualRefresh();
    markStale();
    refresh();
  }

  Future<void> refresh() async {
    final repo = library.repository;
    if (repo == null) {
      // The library is still opening: build once it is.
      _showPending = true;
      return;
    }
    if (_refreshing) {
      _rerunRequested = true;
      return;
    }
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

      final (releases, latestSync, most, artists, genres, totals) = await (
        guard(
          sections.newReleases,
          () => getNewReleases(repo, limit: releasesLimit, today: _clock()),
        ),
        guard(sections.newReleases, repo.latestReleaseSync),
        guard(sections.mostListened, () => repo.mostPlayed(limit)),
        guard(sections.topArtists, () => repo.mostPlayedArtists(limit)),
        guard(sections.topGenres, () => repo.mostPlayedGenres(limit)),
        guard(sections.stats, repo.libraryCounts),
      ).wait;
      newReleases = releases ?? const [];
      latestReleaseSync = latestSync;
      mostPlayed = most ?? const [];
      topArtists = artists ?? const [];
      topGenres = genres ?? const [];
      counts = totals;
      _builtAt = _clock();
      _stale = false;
      notifyListeners();
    } finally {
      _refreshing = false;
      if (_rerunRequested) {
        _rerunRequested = false;
        refresh();
      }
    }
  }

  void _onScheduler() {
    notifyListeners();
  }

  void _onNewReleasesRefreshed(int written) {
    markStale();
    refresh();
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
    releasesScheduler?.removeListener(_onScheduler);
    if (releasesScheduler?.onRefreshed == _onNewReleasesRefreshed) {
      releasesScheduler?.onRefreshed = null;
    }
    super.dispose();
  }
}
