import 'dart:async';

import 'package:flutter/material.dart';

import '../data/library_repository.dart';
import '../downloads/download_queue.dart';
import '../downloads/quality_selector.dart';
import '../library/browse_controller.dart';
import '../player/ui/artwork.dart';
import '../settings/settings_search.dart';
import '../ui/frost.dart';
import '../ui/sliding_segmented.dart';
import '../ui/toast.dart';
import '../ui/tokens.dart';
import 'landing_controller.dart';
import 'local_search.dart';
import 'new_releases.dart';
import 'search_cards.dart';
import 'search_controller.dart';
import 'search_history.dart';

/// ui/views/search.py: Qobuz / Deezer source switch, connection signal, search
/// bar with local-library hits & ownership badges, unified All view (hero,
/// top songs, album/artist shelves), paginated typed views with expandable
/// artist/album trees, multi-select batch download, stream previews, recent
/// searches sheet, and the landing page.
class SearchScreen extends StatefulWidget {
  const SearchScreen({
    super.key,
    required this.landing,
    required this.onPlayTrack,
    required this.onOpenInLibrary,
    this.controller,
    this.downloads,
    this.searchHistory,
    this.onOpenSettings,
    this.unifiedPills = false,
    this.onSnack,
  });

  final LandingController landing;

  /// main.py `play_track(path)`: a Most Listened Tracks or local hit row.
  final ValueChanged<String> onPlayTrack;

  /// search.py `_open_in_library(mode, name)`: the Library on that view,
  /// filtered to [name] ("" for none).
  final void Function(LibraryView view, String name) onOpenInLibrary;

  /// Full remote + local search state controller (optional; when omitted, a
  /// local-only controller is created so lightweight tests keep working).
  final SearchTabController? controller;

  /// App-wide download queue for single & batch quality-sheet enqueues.
  final DownloadQueue? downloads;

  /// `utils/search_history.py`: persisted recent searches (`recent_searches.json`).
  final SearchHistoryStore? searchHistory;

  /// The header's settings shortcut (search.py: `_switch_tab(3)`).
  final void Function([SettingsPage?])? onOpenSettings;

  /// `appearance.pill_style = "unified"`.
  final bool unifiedPills;

  /// Optional app-level snackbar helper.
  final void Function(String text, {IconData? icon})? onSnack;

  @override
  State<SearchScreen> createState() => _SearchScreenState();
}

class _SearchScreenState extends State<SearchScreen> {
  final _queryController = TextEditingController();
  final _resultsScroll = ScrollController();
  SearchTabController? _ownedController;

  SearchTabController get _ctrl => widget.controller ?? _ownedController!;

  @override
  void initState() {
    super.initState();
    if (widget.controller == null) {
      _ownedController = SearchTabController(
        repository: () => widget.landing.library.repository,
        searchHistory: widget.searchHistory,
      );
    }
    _ctrl.addListener(_syncQueryText);
    _syncQueryText();
  }

  @override
  void didUpdateWidget(SearchScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.controller != widget.controller) {
      (oldWidget.controller ?? _ownedController)?.removeListener(
        _syncQueryText,
      );
      if (widget.controller == null && _ownedController == null) {
        _ownedController = SearchTabController(
          repository: () => widget.landing.library.repository,
          searchHistory: widget.searchHistory,
        );
      }
      _ctrl.addListener(_syncQueryText);
      _syncQueryText();
    }
  }

  void _syncQueryText() {
    final active = _ctrl.activeQuery;
    if (active.isNotEmpty && _queryController.text != active) {
      _queryController.text = active;
      _queryController.selection = TextSelection.collapsed(
        offset: active.length,
      );
    }
  }

  @override
  void dispose() {
    _ctrl.removeListener(_syncQueryText);
    _ownedController?.dispose();
    _queryController.dispose();
    _resultsScroll.dispose();
    super.dispose();
  }

  void _snack(String message, {IconData? icon}) {
    if (widget.onSnack != null) {
      widget.onSnack!(message, icon: icon);
      return;
    }
    if (!mounted) return;
    showAppToast(context, message, icon: icon);
  }

  Future<void> _submitSearch(String raw, {SearchViewMode? mode}) async {
    final query = raw.trim();
    if (query.isEmpty) {
      _clearSearch();
      return;
    }
    if (_queryController.text != query) {
      _queryController.text = query;
      _queryController.selection = TextSelection.collapsed(
        offset: query.length,
      );
    }
    await _ctrl.search(query, mode: mode);
  }

  void _clearSearch() {
    _queryController.clear();
    _ctrl.clearSearch();
    widget.landing.onShow();
  }

  void _searchRelease(ReleaseItem rel) {
    final query = '${rel.artist} ${rel.title}'.trim();
    final mode = rel.type == 'single'
        ? SearchViewMode.tracks
        : SearchViewMode.albums;
    unawaited(_submitSearch(query, mode: mode));
  }

  Future<void> _openRecentSearches() async {
    final store = widget.searchHistory ?? _ctrl.searchHistory;
    if (store == null) return;
    final searches = await store.load();
    if (!mounted || searches.isEmpty) return;
    final picked = await showRecentSearchesSheet(context, searches);
    if (!mounted || picked == null) return;
    await _submitSearch(picked);
  }

  void _downloadOne(Map<String, Object?> record) {
    final queue = widget.downloads;
    if (queue == null) {
      _snack('Downloads are unavailable right now.');
      return;
    }
    unawaited(
      showQualitySelector(
        context,
        record: record,
        queue: queue,
        defaultQuality: _ctrl.sourceConfig.defaultQuality,
        onSnack: _snack,
      ),
    );
  }

  void _downloadSelection() {
    final queue = widget.downloads;
    final records = _ctrl.selectedRecords;
    if (records.isEmpty) return;
    if (queue == null) {
      _snack('Downloads are unavailable right now.');
      return;
    }
    unawaited(
      showQualitySelectorBatch(
        context,
        records: records,
        queue: queue,
        defaultQuality: _ctrl.sourceConfig.defaultQuality,
        onSnack: _snack,
        onDone: _ctrl.exitSelection,
      ),
    );
  }

  void _changePage(int newPage) {
    _ctrl.setPage(newPage);
    if (_resultsScroll.hasClients) {
      _resultsScroll.jumpTo(0);
    }
  }

  @override
  Widget build(BuildContext context) {
    final hasHistory = (widget.searchHistory ?? _ctrl.searchHistory) != null;
    return ListenableBuilder(
      listenable: _ctrl,
      builder: (context, _) {
        final ctrl = _ctrl;
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            SafeArea(
              bottom: false,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 20, 8, 8),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Row(
                      crossAxisAlignment: CrossAxisAlignment.center,
                      children: [
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              const Text(
                                'Streamrip',
                                style: TextStyle(
                                  color: Tokens.text,
                                  fontSize: 26,
                                  fontWeight: FontWeight.w800,
                                ),
                              ),
                              if (ctrl.remoteEnabled) ...[
                                const SizedBox(height: 6),
                                Row(
                                  children: [
                                    SourceSegmentedBar(
                                      selected: ctrl.selectedSource,
                                      onChanged: (s) =>
                                          unawaited(ctrl.setSource(s)),
                                    ),
                                    const SizedBox(width: 8),
                                    ConnectionSignal(state: ctrl.signal),
                                  ],
                                ),
                              ],
                            ],
                          ),
                        ),
                        if (hasHistory)
                          IconButton(
                            icon: const Icon(Icons.history_rounded, size: 22),
                            color: Tokens.dim,
                            tooltip: 'Recent Searches',
                            onPressed: _openRecentSearches,
                          ),
                        if (widget.onOpenSettings case final open?)
                          IconButton(
                            icon: const Icon(Icons.settings_outlined, size: 22),
                            color: Tokens.dim,
                            tooltip: 'Settings',
                            onPressed: () => open(),
                          ),
                      ],
                    ),
                    const SizedBox(height: 10),
                    Padding(
                      padding: const EdgeInsets.only(right: 8),
                      child: _SearchBarField(
                        controller: _queryController,
                        searching: ctrl.remotePending,
                        onChanged: (_) => setState(() {}),
                        onSubmitted: _submitSearch,
                        onClear: _clearSearch,
                      ),
                    ),
                    if (ctrl.hasActiveQuery || ctrl.selectionMode) ...[
                      const SizedBox(height: 10),
                      Padding(
                        padding: const EdgeInsets.only(right: 8),
                        child: AnimatedSwitcher(
                          duration: const Duration(milliseconds: 160),
                          child: ctrl.selectionMode
                              ? SearchSelectionBar(
                                  key: const ValueKey('sel_bar'),
                                  count: ctrl.selectedCount,
                                  onExit: ctrl.exitSelection,
                                  onSelectAll: ctrl.selectAllOnPage,
                                  onDownload: _downloadSelection,
                                )
                              : _SearchModeBar(
                                  key: const ValueKey('mode_bar'),
                                  selected: ctrl.viewMode,
                                  unifiedPills: widget.unifiedPills,
                                  onSelect: ctrl.setViewMode,
                                ),
                        ),
                      ),
                    ],
                  ],
                ),
              ),
            ),
            Expanded(
              child: Stack(
                children: [
                  Positioned.fill(
                    child: ctrl.hasActiveQuery
                        ? _buildSearchResults(context, ctrl)
                        : ListenableBuilder(
                            listenable: widget.landing,
                            builder: (context, _) => ListView(
                              padding: EdgeInsets.fromLTRB(
                                Tokens.pagePad(context),
                                10,
                                Tokens.pagePad(context),
                                24 + ShellInsets.bottomOf(context),
                              ),
                              children: _cards(context, ctrl),
                            ),
                          ),
                  ),
                  if (ctrl.searchProgressVisible || ctrl.previewCardVisible)
                    Positioned(
                      top: 4,
                      left: 0,
                      right: 0,
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          if (ctrl.searchProgressVisible)
                            SearchOverlayCard(
                              status: ctrl.searchProgressStatus,
                              detail: ctrl.searchProgressDetail,
                            ),
                          if (ctrl.previewCardVisible)
                            SearchOverlayCard(
                              status: ctrl.previewStatus,
                              detail: ctrl.previewDetail,
                              onCancel: ctrl.cancelPreview,
                            ),
                        ],
                      ),
                    ),
                ],
              ),
            ),
            if (ctrl.hasActiveQuery &&
                ctrl.viewMode != SearchViewMode.all &&
                ctrl.totalPages > 1)
              Padding(
                padding: EdgeInsets.only(bottom: ShellInsets.bottomOf(context)),
                child: SearchPaginationBar(
                  currentPage: ctrl.currentPage,
                  totalPages: ctrl.totalPages,
                  onChangePage: _changePage,
                ),
              ),
          ],
        );
      },
    );
  }

  Widget _buildSearchResults(BuildContext context, SearchTabController ctrl) {
    final openAuth = widget.onOpenSettings == null
        ? null
        : () => widget.onOpenSettings!(SettingsPage.account);
    final hasLocalForMode = switch (ctrl.viewMode) {
      SearchViewMode.all => ctrl.localMatches.isNotEmpty,
      SearchViewMode.tracks => ctrl.localMatches.tracks.isNotEmpty,
      SearchViewMode.albums => ctrl.localMatches.albums.isNotEmpty,
      SearchViewMode.artists => ctrl.localMatches.artists.isNotEmpty,
    };

    // Pending remote search: spacer for overlay card, local hits if ready,
    // then skeleton rows.
    if (ctrl.remotePending) {
      return ListView(
        controller: _resultsScroll,
        padding: EdgeInsets.fromLTRB(
          Tokens.pagePad(context),
          76,
          Tokens.pagePad(context),
          24 + ShellInsets.bottomOf(context),
        ),
        children: [
          if (hasLocalForMode)
            LocalLibrarySection(
              query: ctrl.activeQuery,
              matches: ctrl.localMatches,
              mode: ctrl.viewMode,
              expanded: ctrl.localExpanded,
              onToggle: ctrl.toggleLocalExpanded,
              onPlayTrack: widget.onPlayTrack,
              onOpenInLibrary: widget.onOpenInLibrary,
            ),
          for (var i = 0; i < 8; i++) const SearchSkeletonRow(),
        ],
      );
    }

    // Remote failed and there are no local matches at all: full error view.
    if (ctrl.remoteError != null && !ctrl.localMatches.isNotEmpty) {
      return SearchErrorState(
        message: ctrl.remoteError!,
        onSettings: openAuth,
        onRetry: () => unawaited(ctrl.search(ctrl.activeQuery)),
      );
    }

    final children = <Widget>[];
    if (ctrl.previewCardVisible) {
      children.add(const SizedBox(height: 68));
    }

    if (ctrl.remoteError != null) {
      children.add(
        SearchErrorBanner(
          sourceLabel: ctrl.sourceLabel,
          message: ctrl.remoteError!,
          onSettings: openAuth,
          onRetry: () => unawaited(ctrl.search(ctrl.activeQuery)),
        ),
      );
    }

    if (ctrl.searchingLocal && !ctrl.remoteEnabled) {
      children.add(
        const Padding(
          padding: EdgeInsets.symmetric(vertical: 20),
          child: Center(
            child: SizedBox(
              width: 20,
              height: 20,
              child: CircularProgressIndicator(strokeWidth: 2),
            ),
          ),
        ),
      );
    } else if (hasLocalForMode) {
      children.add(
        LocalLibrarySection(
          query: ctrl.activeQuery,
          matches: ctrl.localMatches,
          mode: ctrl.viewMode,
          expanded: ctrl.localExpanded,
          onToggle: ctrl.toggleLocalExpanded,
          onPlayTrack: widget.onPlayTrack,
          onOpenInLibrary: widget.onOpenInLibrary,
        ),
      );
    }

    if (ctrl.viewMode == SearchViewMode.all) {
      _appendUnifiedResults(context, ctrl, children, hasLocal: hasLocalForMode);
    } else {
      _appendTypedResults(context, ctrl, children, hasLocal: hasLocalForMode);
    }

    return ListView(
      controller: _resultsScroll,
      padding: EdgeInsets.fromLTRB(
        Tokens.pagePad(context),
        8,
        Tokens.pagePad(context),
        24 + ShellInsets.bottomOf(context),
      ),
      children: children,
    );
  }

  void _appendUnifiedResults(
    BuildContext context,
    SearchTabController ctrl,
    List<Widget> out, {
    required bool hasLocal,
  }) {
    if (!ctrl.hasRemoteResults) {
      if (ctrl.remoteError == null && !hasLocal && !ctrl.searchingLocal) {
        out.add(
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 28, horizontal: 16),
            child: Column(
              children: [
                const Icon(
                  Icons.search_off_rounded,
                  color: Tokens.dim,
                  size: 40,
                ),
                const SizedBox(height: 8),
                Text(
                  ctrl.remoteEnabled
                      ? 'No results found for "${ctrl.activeQuery}"'
                      : 'No matches in your library for "${ctrl.activeQuery}".',
                  textAlign: TextAlign.center,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 14,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                if (ctrl.remoteEnabled) ...[
                  const SizedBox(height: 4),
                  const Text(
                    'Try checking the spelling or searching for another artist, album, or track.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: Tokens.dim, fontSize: 12),
                  ),
                ],
              ],
            ),
          ),
        );
      }
      return;
    }

    final hero = ctrl.heroResult;
    if (hero != null) {
      final mType = '${hero['media_type'] ?? 'track'}';
      out.add(
        HeroResultCard(
          hero: hero,
          onTapCard: () => ctrl.setViewMode(switch (mType) {
            'artist' => SearchViewMode.artists,
            'album' => SearchViewMode.albums,
            _ => SearchViewMode.tracks,
          }),
          onDownload: () => _downloadOne(hero),
        ),
      );
    }

    if (ctrl.tracks.isNotEmpty) {
      out.add(
        SearchSectionHeader(
          title: 'Top Songs',
          count: ctrl.tracks.length,
          icon: Icons.music_note_rounded,
          onSeeAll: () => ctrl.setViewMode(SearchViewMode.tracks),
        ),
      );
      for (final (i, trk) in ctrl.tracks.take(4).indexed) {
        out.add(_buildResultRow(ctrl, i, trk, depth: 0));
      }
    }

    if (ctrl.albums.isNotEmpty) {
      out.add(
        UnifiedAlbumShelf(
          albums: ctrl.albums,
          totalCount: ctrl.albums.length,
          onSeeAll: () => ctrl.setViewMode(SearchViewMode.albums),
          onDownload: _downloadOne,
        ),
      );
    }

    final otherArtists = (hero != null && hero['media_type'] == 'artist')
        ? [
            for (final a in ctrl.artists)
              if (!identical(a, hero)) a,
          ]
        : ctrl.artists;
    if (otherArtists.isNotEmpty) {
      out.add(
        UnifiedArtistShelf(
          artists: otherArtists,
          totalCount: ctrl.artists.length,
          onSeeAll: () => ctrl.setViewMode(SearchViewMode.artists),
        ),
      );
    }
  }

  void _appendTypedResults(
    BuildContext context,
    SearchTabController ctrl,
    List<Widget> out, {
    required bool hasLocal,
  }) {
    final rows = ctrl.currentPageRows();
    if (rows.isEmpty) {
      if (ctrl.remoteError == null && !ctrl.searchingLocal) {
        out.add(
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 16, horizontal: 8),
            child: Text(
              hasLocal
                  ? 'No ${ctrl.sourceLabel} results for this search.'
                  : ctrl.remoteEnabled
                  ? 'No results found for "${ctrl.activeQuery}".'
                  : 'No matches in your library for "${ctrl.activeQuery}".',
              textAlign: hasLocal ? TextAlign.start : TextAlign.center,
              style: const TextStyle(color: Tokens.dim, fontSize: 12),
            ),
          ),
        );
      }
      return;
    }

    final startIndex = ctrl.currentPage * kSearchItemsPerPage;
    for (final (i, row) in rows.indexed) {
      out.add(
        _buildResultRow(ctrl, startIndex + i, row.item, depth: row.depth),
      );
    }
  }

  Widget _buildResultRow(
    SearchTabController ctrl,
    int index,
    Map<String, Object?> item, {
    required int depth,
  }) {
    final mType = '${item['media_type'] ?? 'track'}';
    return SearchResultRow(
      item: item,
      depth: depth,
      selectionMode: ctrl.selectionMode,
      selected: ctrl.isSelected(item),
      expanded: ctrl.isNodeExpanded(item),
      loadingChildren: ctrl.isNodeLoading(item),
      loadingMore: ctrl.isLoadingMore(item),
      playing: mType == 'track' && ctrl.isRowPlaying(item),
      previewState: ctrl.previewStateFor(item),
      onTap: () {
        if (ctrl.selectionMode) {
          ctrl.toggleSelection(item);
          return;
        }
        if (mType == 'track') {
          unawaited(ctrl.togglePreview(index, item));
        } else if (mType == 'artist' || mType == 'album') {
          unawaited(ctrl.toggleNode(item));
        }
      },
      onLongPress: () => ctrl.enterSelection(item),
      onDownload: () => _downloadOne(item),
      onPreview: () => unawaited(ctrl.togglePreview(index, item)),
      onLoadMore: () => unawaited(ctrl.loadMoreArtistAlbums(item)),
    );
  }

  /// `_refresh_landing_page`'s order: missing-credentials banner (when remote
  /// search is enabled but no account is linked yet), New From Your Artists,
  /// what you play, then the stats.
  List<Widget> _cards(BuildContext context, SearchTabController ctrl) {
    final landing = widget.landing;
    final newReleasesCard = _buildNewReleasesCard(context);
    return [
      if (ctrl.remoteEnabled &&
          !ctrl.hasAnyCredentials &&
          widget.onOpenSettings != null)
        MissingCredentialsBanner(
          onConnect: () => widget.onOpenSettings!(SettingsPage.account),
        ),
      ?newReleasesCard,
      if (landing.mostPlayed.isNotEmpty)
        LandingCard(
          title: 'Most Listened Tracks',
          icon: Icons.replay_rounded,
          child: Column(
            children: [
              for (final (i, t) in landing.mostPlayed.indexed)
                _RankedRow(
                  rank: i + 1,
                  leading: Icon(
                    Icons.play_circle_filled_rounded,
                    color: Theme.of(context).colorScheme.primary,
                    size: 18,
                  ),
                  title: _or(t.title, 'Untitled'),
                  subtitle: _or(t.artist, 'Unknown Artist'),
                  trailing: _PlaysPill(count: t.count),
                  tooltip: 'Play ${t.title ?? ''}',
                  onTap: () => widget.onPlayTrack(t.path),
                ),
            ],
          ),
        ),
      if (landing.topArtists.isNotEmpty)
        LandingCard(
          title: 'Most Listened Artists',
          icon: Icons.person_rounded,
          child: _topList(
            landing.topArtists,
            Icons.person_outline_rounded,
            LibraryView.artists,
            (r) => '${r.plays} plays • ${r.secondary} tracks',
          ),
        ),
      if (landing.topGenres.isNotEmpty)
        LandingCard(
          title: 'Most Listened Genres',
          icon: Icons.category_rounded,
          child: _topList(
            landing.topGenres,
            Icons.label_outline_rounded,
            LibraryView.genres,
            (r) => '${r.plays} plays • ${r.secondary} albums',
          ),
        ),
      if (landing.counts case final c?)
        LandingCard(
          title: 'Library at a Glance',
          icon: Icons.insert_chart_outlined_rounded,
          child: _stats(c),
        ),
    ];
  }

  /// `search.py` `_build_new_releases_card`.
  Widget? _buildNewReleasesCard(BuildContext context) {
    final landing = widget.landing;
    if (!landing.sections.newReleases) return null;
    final releases = landing.newReleases;
    final refreshing = landing.isRefreshingReleases;

    if (releases.isEmpty) {
      if (!refreshing) return null;
      final accent = Theme.of(context).colorScheme.primary;
      return LandingCard(
        title: 'New From Your Artists',
        icon: Icons.new_releases_rounded,
        child: Row(
          children: [
            SizedBox(
              width: 14,
              height: 14,
              child: CircularProgressIndicator(strokeWidth: 2, color: accent),
            ),
            const SizedBox(width: 10),
            const Expanded(
              child: Text(
                'Checking your artists for new releases…',
                style: TextStyle(color: Tokens.dim, fontSize: 12),
              ),
            ),
          ],
        ),
      );
    }

    final albums = releases.where((r) => r.type != 'single').take(12).toList();
    final singles = releases.where((r) => r.type == 'single').take(12).toList();

    return LandingCard(
      title: 'New From Your Artists',
      icon: Icons.new_releases_rounded,
      action: _ReleasesRefreshAction(
        refreshing: refreshing,
        latestSync: landing.latestReleaseSync,
        onRefresh: landing.triggerManualReleasesRefresh,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (albums.isNotEmpty) ...[
            _ReleaseShelf(
              label: 'Albums & EPs',
              items: albums,
              onSelect: _searchRelease,
            ),
            const SizedBox(height: 8),
          ],
          if (singles.isNotEmpty) ...[
            _ReleaseShelf(
              label: 'Singles',
              items: singles,
              onSelect: _searchRelease,
            ),
            const SizedBox(height: 8),
          ],
          const Text(
            'From Deezer and MusicBrainz, checked daily. Tap one to search for it.',
            style: TextStyle(color: Tokens.dim, fontSize: 10),
          ),
        ],
      ),
    );
  }

  /// `_build_top_list_content`: tapping a row opens it in the Library.
  Widget _topList(
    List<MostPlayedGroup> rows,
    IconData icon,
    LibraryView view,
    String Function(MostPlayedGroup) subtitle,
  ) {
    final palette = CategoryPalette.of(context);
    final color = view == LibraryView.artists ? palette.artist : palette.genre;
    return Column(
      children: [
        for (final (i, r) in rows.indexed)
          _RankedRow(
            rank: i + 1,
            leading: Icon(icon, color: color, size: 18),
            title: r.name ?? '',
            subtitle: subtitle(r),
            trailing: Icon(
              Icons.arrow_forward_ios_rounded,
              color: Tokens.dim.withValues(alpha: 0.3),
              size: 11,
            ),
            tooltip: 'View ${r.name ?? ''} in Library',
            onTap: () => widget.onOpenInLibrary(view, r.name ?? ''),
          ),
      ],
    );
  }

  /// `_get_library_stats_content`: two rows of two.
  Widget _stats(LibraryCounts c) {
    final palette = CategoryPalette.of(context);
    Widget card(
      String label,
      int count,
      String subtitle,
      IconData icon,
      Color? color,
      LibraryView view,
    ) => Expanded(
      child: _StatCard(
        label: label,
        count: count,
        subtitle: subtitle,
        icon: icon,
        color: color,
        onTap: () => widget.onOpenInLibrary(view, ''),
      ),
    );
    return Column(
      children: [
        Row(
          children: [
            card(
              'Tracks',
              c.tracks,
              'Audio tracks in library',
              Icons.music_note_rounded,
              palette.track,
              LibraryView.tracks,
            ),
            const SizedBox(width: 12),
            card(
              'Albums',
              c.albums,
              'Full-length releases',
              Icons.album_rounded,
              palette.album,
              LibraryView.albums,
            ),
          ],
        ),
        const SizedBox(height: 12),
        Row(
          children: [
            card(
              'Artists',
              c.artists,
              'Catalogued creators',
              Icons.person_rounded,
              palette.artist,
              LibraryView.artists,
            ),
            const SizedBox(width: 12),
            card(
              'Playlists',
              c.playlists,
              'Curated mixes & sets',
              Icons.queue_music_rounded,
              palette.playlist,
              LibraryView.playlists,
            ),
          ],
        ),
      ],
    );
  }
}

/// `search.py` `_show_recent_searches`: bounded, scrollable modal bottom sheet
/// listing recent queries. Returns the tapped query or null if dismissed.
Future<String?> showRecentSearchesSheet(
  BuildContext context,
  List<String> searches,
) {
  if (searches.isEmpty) return Future<String?>.value(null);
  return showFrostedSheet<String>(
    context: context,
    backgroundColor: Tokens.surface,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(18)),
    ),
    builder: (ctx) => SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              'Recent Searches',
              style: TextStyle(
                color: Tokens.text,
                fontSize: 15,
                fontWeight: FontWeight.w700,
              ),
            ),
            const Divider(color: Tokens.border),
            SizedBox(
              height: 300,
              child: Scrollbar(
                child: ListView.separated(
                  padding: const EdgeInsets.symmetric(horizontal: 4),
                  itemCount: searches.length,
                  separatorBuilder: (_, _) => const SizedBox(height: 2),
                  itemBuilder: (_, i) {
                    final q = searches[i];
                    return ListTile(
                      dense: true,
                      leading: const Icon(
                        Icons.history_rounded,
                        color: Tokens.dim,
                        size: 18,
                      ),
                      title: Text(
                        q,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(color: Tokens.text),
                      ),
                      onTap: () => Navigator.of(ctx).pop(q),
                    );
                  },
                ),
              ),
            ),
          ],
        ),
      ),
    ),
  );
}

class _SearchBarField extends StatelessWidget {
  const _SearchBarField({
    required this.controller,
    required this.searching,
    required this.onChanged,
    required this.onSubmitted,
    required this.onClear,
  });

  final TextEditingController controller;
  final bool searching;
  final ValueChanged<String> onChanged;
  final ValueChanged<String> onSubmitted;
  final VoidCallback onClear;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final hasText = controller.text.isNotEmpty;
    return TextField(
      controller: controller,
      textInputAction: TextInputAction.search,
      style: const TextStyle(color: Tokens.text, fontSize: 14),
      decoration: InputDecoration(
        hintText: 'Artist, album or track…',
        prefixIcon: const Icon(
          Icons.search_rounded,
          color: Tokens.dim,
          size: 20,
        ),
        suffixIcon: (searching || hasText)
            ? Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (searching)
                    Padding(
                      padding: const EdgeInsets.only(right: 4),
                      child: SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(
                          strokeWidth: 2,
                          color: accent,
                        ),
                      ),
                    ),
                  if (hasText)
                    IconButton(
                      icon: const Icon(
                        Icons.close_rounded,
                        color: Tokens.dim,
                        size: 18,
                      ),
                      tooltip: 'Clear search',
                      onPressed: onClear,
                    ),
                ],
              )
            : null,
        isDense: true,
        contentPadding: const EdgeInsets.symmetric(
          vertical: 10,
          horizontal: 14,
        ),
      ),
      onChanged: onChanged,
      onSubmitted: onSubmitted,
    );
  }
}

class _SearchModeBar extends StatelessWidget {
  const _SearchModeBar({
    super.key,
    required this.selected,
    required this.unifiedPills,
    required this.onSelect,
  });

  final SearchViewMode selected;
  final bool unifiedPills;
  final ValueChanged<SearchViewMode> onSelect;

  Color _accentFor(
    SearchViewMode mode,
    Color primary,
    CategoryPalette palette,
  ) {
    if (unifiedPills || palette.unified || mode == SearchViewMode.all) {
      return primary;
    }
    return switch (mode) {
      SearchViewMode.artists => palette.artist,
      SearchViewMode.albums => palette.album,
      SearchViewMode.tracks => palette.track,
      SearchViewMode.all => primary,
    };
  }

  @override
  Widget build(BuildContext context) {
    final primary = Theme.of(context).colorScheme.primary;
    final palette = CategoryPalette.of(context);
    return SlidingSegmented<SearchViewMode>(
      segments: [
        for (final mode in const [
          SearchViewMode.all,
          SearchViewMode.artists,
          SearchViewMode.albums,
          SearchViewMode.tracks,
        ])
          Segment(
            value: mode,
            label: mode.label,
            color: _accentFor(mode, primary, palette),
          ),
      ],
      selected: selected,
      onSelect: onSelect,
      accent: primary,
      height: 38,
      fontSize: 12,
    );
  }
}

/// `search.py` `_build_releases_refresh_button`.
class _ReleasesRefreshAction extends StatelessWidget {
  const _ReleasesRefreshAction({
    required this.refreshing,
    required this.latestSync,
    required this.onRefresh,
  });

  final bool refreshing;
  final ReleaseSyncEntry? latestSync;
  final VoidCallback onRefresh;

  static String tooltipFor(ReleaseSyncEntry? sync, {DateTime? now}) {
    final finishedAt = sync?.finishedAt;
    if (finishedAt == null) return 'Check for new releases now';
    final dt = DateTime.fromMillisecondsSinceEpoch((finishedAt * 1000).round());
    final ref = now ?? DateTime.now();
    final today = DateTime(ref.year, ref.month, ref.day);
    final dtDay = DateTime(dt.year, dt.month, dt.day);
    final hh = dt.hour.toString().padLeft(2, '0');
    final mm = dt.minute.toString().padLeft(2, '0');
    final String timeStr;
    if (dtDay == today) {
      timeStr = 'Today $hh:$mm';
    } else if (dtDay == today.subtract(const Duration(days: 1))) {
      timeStr = 'Yesterday $hh:$mm';
    } else {
      const months = [
        'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', //
        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
      ];
      final dd = dt.day.toString().padLeft(2, '0');
      timeStr = '${months[dt.month - 1]} $dd, $hh:$mm';
    }
    return 'Check for new releases (Last checked: $timeStr)';
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    if (refreshing) {
      return SizedBox(
        width: 28,
        height: 28,
        child: Center(
          child: SizedBox(
            width: 14,
            height: 14,
            child: CircularProgressIndicator(strokeWidth: 2, color: accent),
          ),
        ),
      );
    }
    return IconButton(
      icon: const Icon(Icons.refresh_rounded, size: 18),
      color: Tokens.dim,
      padding: EdgeInsets.zero,
      constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
      tooltip: tooltipFor(latestSync),
      onPressed: onRefresh,
    );
  }
}

/// Horizontal New From Your Artists shelf with prev/next chevron controls
/// (`_build_shelf_header`, `_shelf_nav`, `_release_tile`).
class _ReleaseShelf extends StatefulWidget {
  const _ReleaseShelf({
    required this.label,
    required this.items,
    required this.onSelect,
  });

  final String label;
  final List<ReleaseItem> items;
  final ValueChanged<ReleaseItem> onSelect;

  @override
  State<_ReleaseShelf> createState() => _ReleaseShelfState();
}

class _ReleaseShelfState extends State<_ReleaseShelf> {
  final _scroll = ScrollController();

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  void _nudge(double delta) {
    if (!_scroll.hasClients) return;
    final pos = _scroll.position;
    final target = (pos.pixels + delta).clamp(
      pos.minScrollExtent,
      pos.maxScrollExtent,
    );
    _scroll.animateTo(
      target,
      duration: const Duration(milliseconds: 180),
      curve: Curves.easeOut,
    );
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(
              widget.label,
              style: const TextStyle(
                color: Tokens.dim,
                fontSize: 11,
                fontWeight: FontWeight.w600,
              ),
            ),
            if (widget.items.length > 3)
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  IconButton(
                    icon: const Icon(Icons.chevron_left_rounded, size: 18),
                    color: Tokens.dim,
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(
                      minWidth: 28,
                      minHeight: 28,
                    ),
                    tooltip: 'Scroll left',
                    onPressed: () => _nudge(-260),
                  ),
                  const SizedBox(width: 2),
                  IconButton(
                    icon: const Icon(Icons.chevron_right_rounded, size: 18),
                    color: Tokens.dim,
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(
                      minWidth: 28,
                      minHeight: 28,
                    ),
                    tooltip: 'Scroll right',
                    onPressed: () => _nudge(260),
                  ),
                ],
              ),
          ],
        ),
        const SizedBox(height: 6),
        SizedBox(
          height: 196,
          child: ListView.separated(
            controller: _scroll,
            scrollDirection: Axis.horizontal,
            itemCount: widget.items.length,
            separatorBuilder: (_, _) => const SizedBox(width: 12),
            itemBuilder: (_, i) => _ReleaseTile(
              release: widget.items[i],
              onTap: () => widget.onSelect(widget.items[i]),
            ),
          ),
        ),
      ],
    );
  }
}

const _releaseTypeLabels = {'album': 'Album', 'ep': 'EP', 'single': 'Single'};

class _ReleaseTile extends StatelessWidget {
  const _ReleaseTile({required this.release, required this.onTap});

  final ReleaseItem release;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    const size = 116.0;
    final kind = _releaseTypeLabels[release.type] ?? 'Release';
    return Tooltip(
      message: '${release.title} · ${release.artist}',
      child: InkWell(
        borderRadius: BorderRadius.circular(10),
        onTap: onTap,
        child: SizedBox(
          width: size,
          child: Padding(
            padding: const EdgeInsets.only(bottom: 4),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Artwork(
                  path: release.cover.isEmpty ? null : release.cover,
                  size: size,
                  radius: 10,
                  placeholder: Icons.album_rounded,
                  placeholderSize: 36,
                  cacheWidth: (size * 2).toInt(),
                ),
                const SizedBox(height: 4),
                Text(
                  release.title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  release.artist,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(color: Tokens.dim, fontSize: 11),
                ),
                const SizedBox(height: 2),
                Text(
                  '$kind · ${releaseDateLabel(release.date)}',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Tokens.textTertiary,
                    fontSize: 10,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

String _or(String? v, String fallback) =>
    (v == null || v.isEmpty) ? fallback : v;

/// search.py `_build_landing_card`.
class LandingCard extends StatelessWidget {
  const LandingCard({
    super.key,
    required this.title,
    required this.icon,
    required this.child,
    this.action,
  });

  final String title;
  final IconData icon;
  final Widget child;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return GlassCard(
      margin: const EdgeInsets.only(bottom: 16),
      flat: Tokens.surface,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              _IconBadge(icon: icon, color: accent, alpha: 0.12),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 14,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              if (action != null) ...[const SizedBox(width: 8), action!],
            ],
          ),
          const Divider(color: Tokens.borderSubtle, height: 18),
          child,
        ],
      ),
    );
  }
}

class _IconBadge extends StatelessWidget {
  const _IconBadge({
    required this.icon,
    required this.color,
    required this.alpha,
  });

  final IconData icon;
  final Color color;
  final double alpha;

  @override
  Widget build(BuildContext context) => Container(
    width: 28,
    height: 28,
    alignment: Alignment.center,
    decoration: BoxDecoration(
      color: color.withValues(alpha: alpha),
      borderRadius: BorderRadius.circular(7),
    ),
    child: Icon(icon, color: color, size: 15),
  );
}

/// ui/tokens.py RANK_COLORS: gold, silver, bronze; DIM below.
const _rankColors = {
  1: Color(0xFFFFD60A),
  2: Color(0xFFAEAEB2),
  3: Color(0xFFFF9F0A),
};

/// One row of a Most Listened card: the rank badge, an icon, two lines and
/// a trailing widget.
class _RankedRow extends StatelessWidget {
  const _RankedRow({
    required this.rank,
    required this.leading,
    required this.title,
    required this.subtitle,
    required this.trailing,
    required this.tooltip,
    required this.onTap,
  });

  final int rank;
  final Widget leading;
  final String title;
  final String subtitle;
  final Widget trailing;
  final String tooltip;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final rankColor = _rankColors[rank] ?? Tokens.dim;
    return Padding(
      padding: const EdgeInsets.only(bottom: 3),
      child: Tooltip(
        message: tooltip,
        child: InkWell(
          borderRadius: BorderRadius.circular(8),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
            child: Row(
              children: [
                Container(
                  width: 22,
                  height: 22,
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: rank <= 3
                        ? rankColor.withValues(alpha: 0.12)
                        : Colors.transparent,
                    borderRadius: BorderRadius.circular(6),
                  ),
                  child: Text(
                    '$rank',
                    style: TextStyle(
                      color: rankColor,
                      fontSize: 11,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                ),
                const SizedBox(width: 10),
                leading,
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        title,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: Tokens.text,
                          fontSize: 13,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      const SizedBox(height: 1),
                      Text(
                        subtitle,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(color: Tokens.dim, fontSize: 11),
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: 10),
                trailing,
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _PlaysPill extends StatelessWidget {
  const _PlaysPill({required this.count});
  final int count;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: accent.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(Tokens.radiusPill),
      ),
      child: Text(
        '$count plays',
        style: TextStyle(
          color: accent,
          fontSize: 10,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

/// search.py `_build_stat_card`: links to that Library view.
class _StatCard extends StatelessWidget {
  const _StatCard({
    required this.label,
    required this.count,
    required this.subtitle,
    required this.icon,
    required this.color,
    required this.onTap,
  });

  final String label;
  final int count;
  final String subtitle;
  final IconData icon;

  /// Null: the accent.
  final Color? color;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final c = color ?? Theme.of(context).colorScheme.primary;
    return Tooltip(
      message: 'View $label in Library',
      child: GlassCard(
        onTap: onTap,
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                _IconBadge(icon: icon, color: c, alpha: 0.14),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    label.toUpperCase(),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: Tokens.dim,
                      fontSize: 11,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
                Icon(
                  Icons.arrow_forward_ios_rounded,
                  color: Tokens.dim.withValues(alpha: 0.35),
                  size: 11,
                ),
              ],
            ),
            const SizedBox(height: 5),
            Text(
              thousands(count),
              style: const TextStyle(
                color: Tokens.text,
                fontSize: 20,
                fontWeight: FontWeight.bold,
              ),
            ),
            const SizedBox(height: 1),
            Text(
              subtitle,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: Tokens.textTertiary, fontSize: 10),
            ),
          ],
        ),
      ),
    );
  }
}

/// Python's `f"{n:,}"`.
String thousands(int n) {
  final s = n.abs().toString();
  final b = StringBuffer(n < 0 ? '-' : '');
  for (var i = 0; i < s.length; i++) {
    if (i > 0 && (s.length - i) % 3 == 0) b.write(',');
    b.write(s[i]);
  }
  return b.toString();
}
