import 'dart:async';

import 'package:flutter/material.dart';

import '../data/library_repository.dart';
import '../library/browse_controller.dart';
import '../player/ui/artwork.dart';
import '../settings/settings_search.dart';
import '../ui/tokens.dart';
import 'landing_controller.dart';
import 'local_search.dart';
import 'new_releases.dart';
import 'search_history.dart';

/// ui/views/search.py: header with history & settings shortcuts, search bar
/// with local-library hits, recent searches sheet, and the landing page
/// (New From Your Artists, Most Listened cards, and Library at a Glance).
/// Remote source search (Qobuz & Deezer), previews, and downloads plug in
/// during Phase 6b/6c.
class SearchScreen extends StatefulWidget {
  const SearchScreen({
    super.key,
    required this.landing,
    required this.onPlayTrack,
    required this.onOpenInLibrary,
    this.searchHistory,
    this.onOpenSettings,
  });

  final LandingController landing;

  /// main.py `play_track(path)`: a Most Listened Tracks or local hit row.
  final ValueChanged<String> onPlayTrack;

  /// search.py `_open_in_library(mode, name)`: the Library on that view,
  /// filtered to [name] ("" for none).
  final void Function(LibraryView view, String name) onOpenInLibrary;

  /// `utils/search_history.py`: persisted recent searches (`recent_searches.json`).
  final SearchHistoryStore? searchHistory;

  /// The header's settings shortcut (search.py: `_switch_tab(3)`).
  final void Function([SettingsPage?])? onOpenSettings;

  @override
  State<SearchScreen> createState() => _SearchScreenState();
}

class _SearchScreenState extends State<SearchScreen> {
  final _queryController = TextEditingController();
  int _searchId = 0;
  String _activeQuery = '';
  SearchViewMode _viewMode = SearchViewMode.all;
  LocalMatches _localMatches = LocalMatches.empty;
  bool _localExpanded = false;
  bool _searchingLocal = false;

  bool get _hasActiveQuery => _activeQuery.isNotEmpty;

  @override
  void dispose() {
    _queryController.dispose();
    super.dispose();
  }

  Future<void> _submitSearch(String raw, {SearchViewMode? mode}) async {
    final query = raw.trim();
    if (query.isEmpty) {
      _clearSearch();
      return;
    }
    final id = ++_searchId;
    setState(() {
      _activeQuery = query;
      if (mode != null) _viewMode = mode;
      _localMatches = LocalMatches.empty;
      _localExpanded = true;
      _searchingLocal = true;
    });
    if (_queryController.text != query) {
      _queryController.text = query;
      _queryController.selection = TextSelection.collapsed(
        offset: query.length,
      );
    }
    unawaited(widget.searchHistory?.add(query));
    final repo = widget.landing.library.repository;
    if (repo == null) {
      if (!mounted || _searchId != id) return;
      setState(() => _searchingLocal = false);
      return;
    }
    try {
      final matches = await loadLocalMatches(repo, query);
      if (!mounted || _searchId != id) return;
      setState(() {
        _localMatches = matches;
        _searchingLocal = false;
      });
    } catch (e) {
      debugPrint('Library half of the search failed: $e');
      if (!mounted || _searchId != id) return;
      setState(() => _searchingLocal = false);
    }
  }

  void _clearSearch() {
    _searchId++;
    _queryController.clear();
    setState(() {
      _activeQuery = '';
      _localMatches = LocalMatches.empty;
      _localExpanded = false;
      _searchingLocal = false;
    });
    widget.landing.onShow();
  }

  void _searchRelease(ReleaseItem rel) {
    final query = '${rel.artist} ${rel.title}'.trim();
    final mode = rel.type == 'single'
        ? SearchViewMode.tracks
        : SearchViewMode.albums;
    _submitSearch(query, mode: mode);
  }

  Future<void> _openRecentSearches() async {
    final store = widget.searchHistory;
    if (store == null) return;
    final searches = await store.load();
    if (!mounted || searches.isEmpty) return;
    final picked = await showRecentSearchesSheet(context, searches);
    if (!mounted || picked == null) return;
    await _submitSearch(picked);
  }

  @override
  Widget build(BuildContext context) {
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
                  children: [
                    const Expanded(
                      child: Text(
                        'Streamrip',
                        style: TextStyle(
                          color: Tokens.text,
                          fontSize: 26,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                    ),
                    if (widget.searchHistory != null)
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
                    onChanged: (_) => setState(() {}),
                    onSubmitted: _submitSearch,
                    onClear: _clearSearch,
                  ),
                ),
                if (_hasActiveQuery) ...[
                  const SizedBox(height: 10),
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: _SearchModeBar(
                      selected: _viewMode,
                      onSelect: (m) => setState(() => _viewMode = m),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
        Expanded(
          child: _hasActiveQuery
              ? _buildSearchResults(context)
              : ListenableBuilder(
                  listenable: widget.landing,
                  builder: (context, _) => ListView(
                    padding: const EdgeInsets.fromLTRB(16, 10, 16, 100),
                    children: _cards(context),
                  ),
                ),
        ),
      ],
    );
  }

  Widget _buildSearchResults(BuildContext context) {
    final hasLocalForMode = switch (_viewMode) {
      SearchViewMode.all => _localMatches.isNotEmpty,
      SearchViewMode.tracks => _localMatches.tracks.isNotEmpty,
      SearchViewMode.albums => _localMatches.albums.isNotEmpty,
      SearchViewMode.artists => _localMatches.artists.isNotEmpty,
    };
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 100),
      children: [
        if (_searchingLocal)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 20),
            child: Center(
              child: SizedBox(
                width: 20,
                height: 20,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
            ),
          )
        else if (hasLocalForMode)
          LocalLibrarySection(
            query: _activeQuery,
            matches: _localMatches,
            mode: _viewMode,
            expanded: _localExpanded,
            onToggle: () => setState(() => _localExpanded = !_localExpanded),
            onPlayTrack: widget.onPlayTrack,
            onOpenInLibrary: widget.onOpenInLibrary,
          )
        else
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 24),
            child: Column(
              children: [
                const Icon(
                  Icons.search_off_rounded,
                  color: Tokens.dim,
                  size: 36,
                ),
                const SizedBox(height: 8),
                Text(
                  'No matches in your library for "$_activeQuery".',
                  textAlign: TextAlign.center,
                  style: const TextStyle(color: Tokens.dim, fontSize: 13),
                ),
              ],
            ),
          ),
      ],
    );
  }

  /// `_refresh_landing_page`'s order: New From Your Artists, what you play,
  /// then the stats.
  List<Widget> _cards(BuildContext context) {
    final landing = widget.landing;
    final newReleasesCard = _buildNewReleasesCard(context);
    return [
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
    final color = view == LibraryView.artists
        ? Tokens.libArtist
        : Tokens.libGenre;
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
              null, // the accent
              LibraryView.tracks,
            ),
            const SizedBox(width: 10),
            card(
              'Albums',
              c.albums,
              'Full-length releases',
              Icons.album_rounded,
              Tokens.libAlbum,
              LibraryView.albums,
            ),
          ],
        ),
        const SizedBox(height: 10),
        Row(
          children: [
            card(
              'Artists',
              c.artists,
              'Catalogued creators',
              Icons.person_rounded,
              Tokens.libArtist,
              LibraryView.artists,
            ),
            const SizedBox(width: 10),
            card(
              'Playlists',
              c.playlists,
              'Curated mixes & sets',
              Icons.queue_music_rounded,
              Tokens.libPlaylist,
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
  return showModalBottomSheet<String>(
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
    required this.onChanged,
    required this.onSubmitted,
    required this.onClear,
  });

  final TextEditingController controller;
  final ValueChanged<String> onChanged;
  final ValueChanged<String> onSubmitted;
  final VoidCallback onClear;

  @override
  Widget build(BuildContext context) {
    final hasText = controller.text.isNotEmpty;
    return TextField(
      controller: controller,
      textInputAction: TextInputAction.search,
      style: const TextStyle(color: Tokens.text, fontSize: 14),
      decoration: InputDecoration(
        hintText: 'Search tracks, albums, artists…',
        prefixIcon: const Icon(Icons.search_rounded, color: Tokens.dim, size: 20),
        suffixIcon: hasText
            ? IconButton(
                icon: const Icon(Icons.close_rounded, color: Tokens.dim, size: 18),
                tooltip: 'Clear search',
                onPressed: onClear,
              )
            : null,
        isDense: true,
        contentPadding: const EdgeInsets.symmetric(vertical: 10, horizontal: 14),
      ),
      onChanged: onChanged,
      onSubmitted: onSubmitted,
    );
  }
}

class _SearchModeBar extends StatelessWidget {
  const _SearchModeBar({required this.selected, required this.onSelect});

  final SearchViewMode selected;
  final ValueChanged<SearchViewMode> onSelect;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Row(
      children: [
        for (final mode in SearchViewMode.values) ...[
          if (mode != SearchViewMode.all) const SizedBox(width: 6),
          Expanded(
            child: InkWell(
              borderRadius: BorderRadius.circular(Tokens.radiusPill),
              onTap: () => onSelect(mode),
              child: Container(
                height: 32,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: mode == selected
                      ? accent.withValues(alpha: 0.16)
                      : Tokens.surface2,
                  borderRadius: BorderRadius.circular(Tokens.radiusPill),
                  border: Border.all(
                    color: mode == selected
                        ? accent.withValues(alpha: 0.45)
                        : Tokens.borderSubtle,
                  ),
                ),
                child: Text(
                  mode.label,
                  style: TextStyle(
                    color: mode == selected ? accent : Tokens.dim,
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ),
          ),
        ],
      ],
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
    final dt = DateTime.fromMillisecondsSinceEpoch(
      (finishedAt * 1000).round(),
    );
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
    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Tokens.surface,
        border: Border.all(color: Tokens.borderSubtle),
        borderRadius: BorderRadius.circular(Tokens.radiusCard),
      ),
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
      child: Material(
        color: Tokens.surface2,
        shape: RoundedRectangleBorder(
          side: const BorderSide(color: Tokens.borderSubtle),
          borderRadius: BorderRadius.circular(Tokens.radiusCard),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: onTap,
          child: Padding(
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
