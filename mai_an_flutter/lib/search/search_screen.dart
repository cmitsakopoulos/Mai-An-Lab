import 'package:flutter/material.dart';

import '../data/library_repository.dart';
import '../library/browse_controller.dart';
import '../ui/tokens.dart';
import '../settings/settings_search.dart';
import 'landing_controller.dart';

/// ui/views/search.py, the landing page only: the title and the cards built
/// from local data. The source search, its results, recent searches and New
/// From Your Artists come with phase 6 (decision A).
class SearchScreen extends StatelessWidget {
  const SearchScreen({
    super.key,
    required this.landing,
    required this.onPlayTrack,
    required this.onOpenInLibrary,
    this.onOpenSettings,
  });

  final LandingController landing;

  /// main.py `play_track(path)`: a Most Listened Tracks row.
  final ValueChanged<String> onPlayTrack;

  /// search.py `_open_in_library(mode, name)`: the Library on that view,
  /// filtered to [name] ("" for none).
  final void Function(LibraryView view, String name) onOpenInLibrary;

  /// The header's settings shortcut (search.py: `_switch_tab(3)`).
  final void Function([SettingsPage?])? onOpenSettings;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SafeArea(
          bottom: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 20, 8, 8),
            child: Row(
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
                if (onOpenSettings case final open?)
                  IconButton(
                    icon: const Icon(Icons.settings_outlined, size: 22),
                    color: Tokens.dim,
                    tooltip: 'Settings',
                    onPressed: () => open(),
                  ),
              ],
            ),
          ),
        ),
        Expanded(
          child: ListenableBuilder(
            listenable: landing,
            builder: (context, _) => ListView(
              padding: const EdgeInsets.fromLTRB(16, 10, 16, 100),
              children: _cards(context),
            ),
          ),
        ),
      ],
    );
  }

  /// `_refresh_landing_page`'s order: what you play, then the stats.
  List<Widget> _cards(BuildContext context) => [
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
                onTap: () => onPlayTrack(t.path),
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
            onTap: () => onOpenInLibrary(view, r.name ?? ''),
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
        onTap: () => onOpenInLibrary(view, ''),
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

String _or(String? v, String fallback) =>
    (v == null || v.isEmpty) ? fallback : v;

/// search.py `_build_landing_card`.
class LandingCard extends StatelessWidget {
  const LandingCard({
    super.key,
    required this.title,
    required this.icon,
    required this.child,
  });

  final String title;
  final IconData icon;
  final Widget child;

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
              Flexible(
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
